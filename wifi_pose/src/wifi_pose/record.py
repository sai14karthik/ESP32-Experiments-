"""Record a RealSense GT session: timestamped joints (+ optional color).

Session layout::

    sessions/<id>/
      meta.json
      frames.jsonl          # one JSON object per frame
      color/000000.jpg      # if --save-color
      preview.jpg           # last view on quit

Each frames.jsonl line has wall-clock and monotonic timestamps for CSI sync.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

from wifi_pose.pose3d import NAMES, is_real_person, lift_person, make_landmarker
from wifi_pose.realsense import RealSense
from wifi_pose.viz import render

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = ROOT / "models" / "pose_landmarker_lite.task"
DEFAULT_SESSIONS = ROOT / "sessions"


def _chown_path(path: Path) -> None:
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if not uid or not gid:
        return
    try:
        os.chown(path, int(uid), int(gid))
    except OSError:
        pass


def _chown_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        _chown_path(path)


def _fatal_stream_error(message: str) -> bool:
    keys = (
        "before start",
        "No device",
        "failed to set power state",
        "Device or resource busy",
        "Camera disconnected",
        "xioctl",
        "Cannot open",
        "Frame didn't arrive",
    )
    return any(k.lower() in message.lower() for k in keys)


def _new_session_dir(sessions: Path, name: str | None) -> Path:
    sessions.mkdir(parents=True, exist_ok=True)
    _chown_path(sessions)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = (name or "session").strip().replace(" ", "_")
    path = sessions / f"{stamp}_{slug}"
    path.mkdir(parents=False, exist_ok=False)
    _chown_path(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record RealSense 3D joint GT sessions")
    parser.add_argument("--name", default="session", help="session name suffix")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS,
        help="parent folder for sessions",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help="MediaPipe pose_landmarker .task",
    )
    parser.add_argument(
        "--save-color",
        action="store_true",
        help="write color/######.jpg per frame (larger disk use)",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="headless: no OpenCV windows (still records)",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="stop after N frames (0 = until q / Ctrl+C)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="stop after N seconds (0 = until q / Ctrl+C)",
    )
    args = parser.parse_args(argv)

    if not args.model.exists():
        print(f"missing model: {args.model}", file=sys.stderr)
        print(
            "Copy pose_landmarker_lite.task into wifi_pose/models/",
            file=sys.stderr,
        )
        return 1
    if os.geteuid() != 0:
        print("Run via ./scripts/run_record.sh (root needed for librealsense on macOS).", file=sys.stderr)
        return 1

    session = _new_session_dir(args.sessions_dir, args.name)
    color_dir = session / "color"
    if args.save_color:
        color_dir.mkdir()
        _chown_path(color_dir)
    frames_path = session / "frames.jsonl"
    meta_path = session / "meta.json"

    started = datetime.now(timezone.utc)
    meta = {
        "session_id": session.name,
        "project": "wifi_pose",
        "started_at": started.isoformat(),
        "ended_at": None,
        "camera": "Intel RealSense D456",
        "frame": "camera",
        "units": "meters",
        "axes": "x right, y down, z forward",
        "joint_names": NAMES,
        "save_color": bool(args.save_color),
        "model": args.model.name,
        "stream": None,
        "depth_scale": None,
        "frame_count": 0,
        "people_frames": 0,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    _chown_path(meta_path)

    mp, landmarker = make_landmarker(args.model)
    cam = RealSense()
    print(f"Session → {session}")
    print("Opening RealSense D456...")
    cam.start()
    meta["stream"] = (
        {"width": cam.mode[0], "height": cam.mode[1], "fps": cam.mode[2]}
        if cam.mode
        else None
    )
    meta["depth_scale"] = cam.depth_scale
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    _chown_path(meta_path)

    print("Recording. Stand 1–2 m facing the camera. Press q to stop.")
    frames = 0
    people_frames = 0
    misses = 0
    reconnects = 0
    t0 = time.monotonic()
    preview = None

    try:
        with frames_path.open("w", encoding="utf-8") as fout:
            _chown_path(frames_path)
            while True:
                try:
                    color, depth = cam.read()
                except RuntimeError as exc:
                    misses += 1
                    print(exc, file=sys.stderr)
                    if _fatal_stream_error(str(exc)):
                        if reconnects >= 5:
                            print("gave up reopening the camera", file=sys.stderr)
                            break
                        reconnects += 1
                        try:
                            cam.reopen()
                            misses = 0
                            continue
                        except RuntimeError as reopen_exc:
                            print(reopen_exc, file=sys.stderr)
                            break
                    if misses > 60:
                        print("too many dropped frames", file=sys.stderr)
                        break
                    continue
                misses = 0

                t_wall_ns = time.time_ns()
                t_mono_ns = time.monotonic_ns()
                rgb = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
                stamp_ms = max(1, int((t_mono_ns - int(t0 * 1e9)) / 1_000_000))
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = landmarker.detect_for_video(image, stamp_ms)
                assert cam.intrinsics is not None
                people = []
                for pose in result.pose_landmarks or []:
                    joints = lift_person(pose, depth, cam.intrinsics, cam.depth_scale)
                    if is_real_person(joints):
                        people.append(joints)

                row = {
                    "i": frames,
                    "t_wall_ns": t_wall_ns,
                    "t_mono_ns": t_mono_ns,
                    "n_people": len(people),
                    "people": people,
                }
                fout.write(json.dumps(row, separators=(",", ":")) + "\n")
                fout.flush()

                if args.save_color:
                    path = color_dir / f"{frames:06d}.jpg"
                    cv2.imwrite(str(path), color)
                    if frames == 0:
                        _chown_path(path)

                if people:
                    people_frames += 1
                preview = render(color, people, frame_i=frames)
                frames += 1

                if frames == 1 or frames % 30 == 0:
                    ok = sum(j["ok"] for person in people for j in person)
                    print(
                        f"frame {frames}  people {len(people)}  joints {ok}  "
                        f"session={session.name}"
                    )

                if not args.no_preview:
                    cv2.imshow("wifi_pose record", preview)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

                if args.max_frames and frames >= args.max_frames:
                    break
                if args.max_seconds and (time.monotonic() - t0) >= args.max_seconds:
                    break
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C", file=sys.stderr)
    finally:
        cam.stop()
        landmarker.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
        ended = datetime.now(timezone.utc)
        meta["ended_at"] = ended.isoformat()
        meta["frame_count"] = frames
        meta["people_frames"] = people_frames
        meta["depth_scale"] = cam.depth_scale
        if cam.mode:
            meta["stream"] = {
                "width": cam.mode[0],
                "height": cam.mode[1],
                "fps": cam.mode[2],
            }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        if preview is not None:
            preview_path = session / "preview.jpg"
            cv2.imwrite(str(preview_path), preview)
        _chown_tree(session)
        print(
            f"Wrote {frames} frames ({people_frames} with people) → {session}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
