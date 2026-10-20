"""Ask Nemotron to read a patient-room frame and, when the source has one, its sound.

The live pose loop stays in charge of tracking and the on-screen activity
labels. This module sends one JPEG, plus a short clip of that same source's
audio track, to the vLLM client in ``_nocommit.py`` when a fall is suspected
or when the operator asks. A source with no sound track sends the picture
alone. A person lying in bed is not reported as a fall.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _nocommit as nemotron_client  # noqa: E402
from source_audio import CLIP_S as SCENE_AUDIO_S  # noqa: E402

_AUDIO_S = int(SCENE_AUDIO_S) if SCENE_AUDIO_S == int(SCENE_AUDIO_S) else SCENE_AUDIO_S

SCENE_SYSTEM = (
    "You assist a hospital room monitor. "
    "Report only what the camera shows now and, when a sound clip is attached, "
    "what was heard in the seconds just before this frame. "
    "Write for a nurse station: plain, specific, and calm. "
    "Do not diagnose, assess injury, or give medical advice."
)

SCENE_PROMPT = (
    "How many people are visible, and what is each person doing? "
    "Each posture must be one of: standing, sitting, walking, lying, "
    "fall_suspected, unknown. "
    "Use fall_suspected only when a person appears to be on the floor after a fall. "
    "A person lying in a bed or on a couch is lying, not fall_suspected. "
    "If the posture is unclear, use unknown. "
    "note is one short sentence about only what you see."
)

SCENE_PROMPT_AV = (
    "The image is the current camera frame. "
    f"The sound clip is the previous {_AUDIO_S} seconds from the same camera, ending at this frame. "
    "It can be shorter only at the very start of the source. "
    "How many people are visible, and what is each person doing? "
    "Each posture must be one of: standing, sitting, walking, lying, "
    "fall_suspected, unknown. "
    "Use fall_suspected only when a person appears to be on the floor after a fall. "
    "A person lying in a bed or on a couch is lying, not fall_suspected. "
    "If the posture is unclear, use unknown. "
    "situation is one or two short sentences a nurse station can read. "
    "Explain the room right now using the picture and that sound together: "
    "who is present, what each person is doing, and what was said or heard, including any words spoken. "
    "If the clip is quiet, say that the room was quiet. "
    "Do not diagnose or give advice."
)

SCENE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "people": {"type": "integer"},
        "postures": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "standing",
                    "sitting",
                    "walking",
                    "lying",
                    "fall_suspected",
                    "unknown",
                ],
            },
        },
        "fall_on_floor": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["people", "postures", "fall_on_floor", "note"],
}

SCENE_SCHEMA_AV = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "people": SCENE_SCHEMA["properties"]["people"],
        "postures": SCENE_SCHEMA["properties"]["postures"],
        "fall_on_floor": SCENE_SCHEMA["properties"]["fall_on_floor"],
        "situation": {"type": "string"},
    },
    "required": ["people", "postures", "fall_on_floor", "situation"],
}


def endpoint_open(endpoint: str, timeout: float = 1.5) -> bool:
    """True when the Nemotron host:port accepts a TCP connection."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def frame_data_url(frame: np.ndarray, max_px: int = 1024) -> str:
    """JPEG data URL. The long side is capped here because data URLs skip Pillow."""
    import base64

    img = frame
    height, width = img.shape[:2]
    long_side = max(height, width)
    if max_px > 0 and long_side > max_px:
        scale = max_px / float(long_side)
        img = cv2.resize(
            img,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("could not encode the frame as JPEG")
    payload = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def format_reply(response: dict) -> str:
    """Turn a chat-completions body into one HUD line."""
    try:
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("nemotron reply had no message") from exc
    content = nemotron_client._THINK.sub("", content).strip()
    if not content:
        raise ValueError("nemotron reply was empty")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content.replace("\n", " ")[:180]
    if not isinstance(data, dict):
        return content.replace("\n", " ")[:180]
    postures = [str(item) for item in data.get("postures") or []]
    posture_text = ", ".join(postures) if postures else "none"
    fall_text = "fall on floor" if data.get("fall_on_floor") else "no floor fall"
    note = str(data.get("note") or "").strip()
    heard = str(data.get("heard") or "").strip()
    situation = str(data.get("situation") or "").strip()
    count = data.get("people", "?")
    if not isinstance(count, int):
        count = "?"
    people_word = "person" if count == 1 else "people"
    line = f"{count} {people_word}: {posture_text}; {fall_text}"
    if situation:
        return f"{line}. Situation: {situation}"
    if note:
        line = f"{line}. {note}"
    if heard:
        line = f"{line}. Heard: {heard}"
    return line


def ask_image_url(
    image_url: str,
    *,
    audio_url: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    think: bool = False,
    timeout: float = 45.0,
) -> str:
    """Send one frame and, when present, that source's sound clip."""
    endpoint = endpoint or nemotron_client.DEFAULT_ENDPOINT
    model = model or nemotron_client.DEFAULT_MODEL
    inputs = [image_url]
    if audio_url:
        inputs.append(audio_url)
    body = nemotron_client.build_request(
        inputs,
        SCENE_PROMPT_AV if audio_url else SCENE_PROMPT,
        model,
        max_tokens=1024 if think else (480 if audio_url else 256),
        temperature=0.2,
        enable_thinking=think,
        max_px=0,
        system_prompt=SCENE_SYSTEM,
        schema=SCENE_SCHEMA_AV if audio_url else SCENE_SCHEMA,
        captions=False,
    )
    try:
        response = nemotron_client.post(endpoint, body, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise ConnectionError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc
    return format_reply(response)


def ask_room(
    frame: np.ndarray,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    think: bool = False,
    max_px: int = 1024,
    timeout: float = 45.0,
) -> str:
    """Encode a camera frame and send it."""
    return ask_image_url(
        frame_data_url(frame, max_px),
        endpoint=endpoint,
        model=model,
        think=think,
        timeout=timeout,
    )


class SceneAsker:
    """One Nemotron request at a time, off the video thread.

    A fall track is asked once. It can be asked again only after that track
    has stayed out of ``fall_suspected`` for ``rearm_after`` seconds, so a
    one-frame pose dropout does not send a second request. A failed fall
    request waits ``retry_s`` before trying the server again. Pressing ``n``
    is not blocked by that wait.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        think: bool = False,
        max_px: int = 1024,
        timeout: float = 45.0,
        rearm_after: float = 1.0,
        retry_s: float = 10.0,
        clock=None,
        source: str | None = None,
        audio_clip=None,
    ):
        import os

        self.endpoint = endpoint or os.environ.get(
            "NEMOTRON_ENDPOINT", nemotron_client.DEFAULT_ENDPOINT
        )
        self.model = model or os.environ.get(
            "NEMOTRON_MODEL", nemotron_client.DEFAULT_MODEL
        )
        self.think = think
        self.max_px = max_px
        self.timeout = timeout
        self.rearm_after = rearm_after
        self.retry_s = retry_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._inflight = False
        self._asked_falls: set[int] = set()
        self._fall_seen: dict[int, float] = {}
        self._retry_after = 0.0
        self._line = ""
        self._error = ""
        self._worker: threading.Thread | None = None
        self._audio_clip = audio_clip
        self._audio = None
        if audio_clip is None and source:
            from source_audio import SourceAudio

            self._audio = SourceAudio(source)

    def note_time(self, seconds: float) -> None:
        """Where the file playback is, so the sound clip matches the frame."""
        if self._audio is not None:
            self._audio.note_time(seconds)

    def close(self) -> None:
        if self._audio is not None:
            self._audio.close()

    def status_line(self) -> str:
        with self._lock:
            if self._inflight:
                return (self._line or "reading the frame") + " …"
            if self._error:
                return self._error
            return self._line

    def on_falls(self, frame: np.ndarray, fall_ids: list[int]) -> None:
        """Call every frame, including when nobody is down, so recovery can re-arm."""
        now = self._clock()
        with self._lock:
            current = set(fall_ids)
            for tid in current:
                self._fall_seen[tid] = now
            for tid in list(self._asked_falls):
                if tid in current:
                    continue
                if now - self._fall_seen.get(tid, now) >= self.rearm_after:
                    self._asked_falls.discard(tid)
            if now < self._retry_after or self._inflight:
                return
            fresh = [tid for tid in fall_ids if tid not in self._asked_falls]
            if not fresh:
                return
            self._inflight = True
            self._asked_falls.update(fresh)
        self._start(frame, "fall")

    def ask_now(self, frame: np.ndarray) -> bool:
        """Operator request (the n key). False when a request is already running."""
        with self._lock:
            if self._inflight:
                return False
            self._inflight = True
        return self._start(frame, "key")

    def wait(self, timeout: float = 5.0) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _start(self, frame: np.ndarray, reason: str) -> bool:
        """Encode on the caller thread so the worker only does HTTP."""
        try:
            image_url = frame_data_url(np.ascontiguousarray(frame), self.max_px)
            audio_url = self._current_audio()
            if audio_url:
                print(
                    f"nemotron ({reason}): sending the current frame and the previous {_AUDIO_S} seconds of source audio",
                    flush=True,
                )
            worker = threading.Thread(
                target=self._run,
                args=(image_url, audio_url, reason),
                daemon=True,
                name="nemotron-scene",
            )
            self._worker = worker
            worker.start()
        except Exception as exc:
            with self._lock:
                self._inflight = False
                if reason == "fall":
                    self._asked_falls.clear()
                    self._retry_after = self._clock() + self.retry_s
                self._error = f"could not send frame: {exc}"
            print(f"nemotron ({reason}) failed: {self._error}", flush=True)
            return False
        return True

    def _current_audio(self) -> str | None:
        if self._audio_clip is not None:
            try:
                return self._audio_clip() or None
            except Exception as exc:
                print(f"nemotron audio: {exc}", flush=True)
                return None
        if self._audio is None:
            return None
        try:
            return self._audio.data_url()
        except Exception as exc:
            print(f"nemotron audio: {exc}", flush=True)
            return None

    def _run(self, image_url: str, audio_url: str | None, reason: str) -> None:
        try:
            text = ask_image_url(
                image_url,
                audio_url=audio_url,
                endpoint=self.endpoint,
                model=self.model,
                think=self.think,
                timeout=self.timeout,
            )
            with self._lock:
                self._line = text
                self._error = ""
            print(f"nemotron ({reason}): {text}", flush=True)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._error = message
                if reason == "fall":
                    self._asked_falls.clear()
                    self._retry_after = self._clock() + self.retry_s
            print(f"nemotron ({reason}) failed: {message}", flush=True)
        finally:
            with self._lock:
                self._inflight = False
