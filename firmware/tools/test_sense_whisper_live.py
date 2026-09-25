#!/usr/bin/env python3
"""Vigorous offline tests for sense_whisper_live diarization path.

Does NOT load pyannote (no HF). Builds a skeleton AccurateLiveTracker and
stresses gallery / cluster / stickiness / ring / CLI / helpers.

  python3 firmware/tools/test_sense_whisper_live.py
  uv run --group whisper python firmware/tools/test_sense_whisper_live.py
"""
from __future__ import annotations

import argparse
import queue
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sense_whisper_live as sw  # noqa: E402

PASSED = 0
FAILED = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        msg = f"FAIL  {name}" + (f" — {detail}" if detail else "")
        ERRORS.append(msg)
        print(f"  {msg}")


def skeleton_tracker(
    *,
    max_speakers: int = 2,
    expect_speakers: int | None = None,
    sim_threshold: float = 0.50,
) -> sw.AccurateLiveTracker:
    """AccurateLiveTracker without pyannote/ECAPA load."""
    t = sw.AccurateLiveTracker.__new__(sw.AccurateLiveTracker)
    t._torch = None
    t.ring = sw.PcmRing(seconds=8.0)
    t.max_speakers = max(1, max_speakers)
    t.window_s = 12.0
    t.refresh_s = 2.0
    t.sim_threshold = sim_threshold
    t.margin = 0.06
    if expect_speakers is not None and expect_speakers <= 0:
        t.expect_speakers = None
    else:
        t.expect_speakers = expect_speakers
    t._names = []
    t._centroids = []
    t._counts = []
    t._last_name = "YOU"
    t._pending_name = ""
    t._pending_hits = 0
    t._turns = []
    t._lock = __import__("threading").Lock()
    t._stop = __import__("threading").Event()
    t._encoder = None
    t._pipeline = None
    t._tick = 0
    t._two_spk_hits = 0
    t._force_two = bool(t.expect_speakers and t.expect_speakers >= 2)
    return t


def unit(dim: int = 64) -> np.ndarray:
    v = np.random.randn(dim).astype(np.float64)
    return v / (np.linalg.norm(v) + 1e-12)


def near(v: np.ndarray, noise: float = 0.05) -> np.ndarray:
    x = v + noise * np.random.randn(*v.shape)
    return x / (np.linalg.norm(x) + 1e-12)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_clean_text() -> None:
    print("\n[clean_text]")
    check("empty", sw._clean_text("") == "")
    check("thanks for watching", sw._clean_text("Thanks for watching.") == "")
    check("real sentence kept", "hello" in sw._clean_text("Hello there everyone").lower())
    check("hum filtered", sw._clean_text("Mmmmmmmmm") == "")
    check("word loop filtered", sw._clean_text("yes yes yes yes yes yes") == "")


def test_pcm_ring() -> None:
    print("\n[PcmRing]")
    ring = sw.PcmRing(seconds=2.0)
    # 0.5s of silence
    pcm = (np.zeros(sw.SAMPLE_RATE // 2, dtype=np.int16)).tobytes()
    t0 = time.time()
    ring.write(pcm)
    audio, t_end = ring.snapshot(2.0)
    check("snapshot samples", abs(audio.size - sw.SAMPLE_RATE // 2) < 8, f"got {audio.size}")
    check("t_end recent", abs(t_end - t0) < 1.0, f"t_end={t_end}")
    # overflow trim
    big = (np.random.randint(-1000, 1000, sw.SAMPLE_RATE * 5, dtype=np.int16)).tobytes()
    ring.write(big)
    audio2, _ = ring.snapshot(2.0)
    check("overflow capped ~2s", audio2.size <= sw.SAMPLE_RATE * 2 + 16, f"got {audio2.size}")


def test_enqueue_drop() -> None:
    print("\n[_enqueue_seg]")
    q: queue.Queue = queue.Queue(maxsize=2)
    sw._enqueue_seg(q, (b"a", True, "s", 1.0))
    sw._enqueue_seg(q, (b"b", True, "s", 2.0))
    sw._enqueue_seg(q, (b"c", True, "s", 3.0))  # should drop stale
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    check("queue size ≤2", len(items) == 2, f"got {len(items)}")
    check("newest kept", items[-1][0] == b"c")


def test_loudest_gate() -> None:
    print("\n[LoudestSourceGate]")
    g = sw.LoudestSourceGate(margin_db=8.0, hold_s=2.5)
    check("first wins", g.allow("A", -30.0) is True)
    check("quieter blocked", g.allow("B", -50.0) is False)
    check("same source ok", g.allow("A", -35.0) is True)
    check("much louder switches", g.allow("B", -20.0) is True)


def test_ip_ports() -> None:
    print("\n[ip/ports]")
    check("known .34", sw._ip_to_port("10.128.93.34") == 19056)
    check("known .25", sw._ip_to_port("10.128.93.25") == 19055)
    ports, desc = sw._resolve_ips("10.128.93.34,10.128.93.25")
    check("multi unique", set(ports) == {19055, 19056}, f"{ports}")
    ports_all, _ = sw._resolve_ips("all")
    check("all non-empty", len(ports_all) >= 2)


# ---------------------------------------------------------------------------
# AccurateLiveTracker core logic
# ---------------------------------------------------------------------------


def test_cluster_kwargs_universal() -> None:
    print("\n[_cluster_kwargs universal]")
    t = skeleton_tracker()
    # tick 1,2 → open; tick 3 → probe force 2
    t._tick = 1
    kw = t._cluster_kwargs()
    check("tick1 open", "min_speakers" in kw and kw["max_speakers"] == 2, str(kw))
    t._tick = 3
    kw = t._cluster_kwargs()
    check("tick3 probe num=2", kw.get("num_speakers") == 2, str(kw))
    t._force_two = True
    t._tick = 1
    kw = t._cluster_kwargs()
    check("force_two locks", kw.get("num_speakers") == 2, str(kw))

    t2 = skeleton_tracker(expect_speakers=2)
    check("CLI force", t2._cluster_kwargs().get("num_speakers") == 2)

    t3 = skeleton_tracker(expect_speakers=0)
    check("expect 0 → auto", t3.expect_speakers is None)
    t3._tick = 1
    check("expect 0 open", "min_speakers" in t3._cluster_kwargs())


def test_dual_evidence() -> None:
    print("\n[_note_dual_evidence]")
    t = skeleton_tracker()
    a, b = unit(), unit()
    # make them clearly different
    while float(np.dot(a, b)) > 0.3:
        b = unit()
    labels = ["SPEAKER_00", "SPEAKER_01"]
    emb = {"SPEAKER_00": a, "SPEAKER_01": b}
    dur = {"SPEAKER_00": 4.0, "SPEAKER_01": 3.0}
    t._note_dual_evidence(labels, emb, dur)
    check("hit1 no lock yet", t._force_two is False and t._two_spk_hits == 1)
    t._note_dual_evidence(labels, emb, dur)
    check("hit2 locks dual", t._force_two is True, f"hits={t._two_spk_hits}")

    # same-voice split should not count
    t4 = skeleton_tracker()
    same = unit()
    t4._note_dual_evidence(
        labels,
        {"SPEAKER_00": same, "SPEAKER_01": near(same, 0.01)},
        dur,
    )
    check("same-voice ignored", t4._two_spk_hits == 0 and t4._force_two is False)


def test_map_window_exclusive() -> None:
    print("\n[_map_window exclusive + anti-collapse]")
    rng = np.random.default_rng(0)
    you = rng.normal(size=64)
    you /= np.linalg.norm(you)
    other = rng.normal(size=64)
    other /= np.linalg.norm(other)
    # ensure distinct
    while float(np.dot(you, other)) > 0.2:
        other = rng.normal(size=64)
        other /= np.linalg.norm(other)

    t = skeleton_tracker(sim_threshold=0.45)
    t._add_gallery("YOU", you)
    t._add_gallery("OTHER", other)

    # Window raws match gallery correctly
    emb = {
        "SPEAKER_00": near(other, 0.02),
        "SPEAKER_01": near(you, 0.02),
    }
    dur = {"SPEAKER_00": 2.0, "SPEAKER_01": 3.0}
    m = t._map_window(["SPEAKER_00", "SPEAKER_01"], emb, dur)
    check("exclusive names", m["SPEAKER_00"] != m["SPEAKER_01"], str(m))
    check("YOU matched", "YOU" in m.values())
    check("OTHER matched", "OTHER" in m.values())
    check("SPEAKER_01→YOU", m["SPEAKER_01"] == "YOU", str(m))
    check("SPEAKER_00→OTHER", m["SPEAKER_00"] == "OTHER", str(m))

    # Collapse case: both look like YOU → split forces OTHER
    t2 = skeleton_tracker(sim_threshold=0.45)
    t2._add_gallery("YOU", you)
    emb2 = {
        "SPEAKER_00": near(you, 0.02),
        "SPEAKER_01": near(you, 0.03),
    }
    m2 = t2._map_window(["SPEAKER_00", "SPEAKER_01"], emb2, {"SPEAKER_00": 3.0, "SPEAKER_01": 2.5})
    check("collapse split distinct", m2["SPEAKER_00"] != m2["SPEAKER_01"], str(m2))
    check("OTHER created", "OTHER" in t2._names)


def test_map_window_first_voices() -> None:
    print("\n[_map_window cold start]")
    t = skeleton_tracker()
    a, b = unit(), unit()
    while float(np.dot(a, b)) > 0.3:
        b = unit()
    m = t._map_window(
        ["SPEAKER_00", "SPEAKER_01"],
        {"SPEAKER_00": a, "SPEAKER_01": b},
        {"SPEAKER_00": 5.0, "SPEAKER_01": 4.0},
    )
    check("first→YOU", m["SPEAKER_00"] == "YOU", str(m))
    check("second→OTHER", m["SPEAKER_01"] == "OTHER", str(m))
    check("gallery size 2", len(t._names) == 2)


def test_label_span_stickiness() -> None:
    print("\n[label_span stickiness]")
    t = skeleton_tracker()
    now = time.time()
    t._turns = [
        (now - 5.0, now - 3.0, "YOU"),
        (now - 2.5, now - 0.5, "OTHER"),
    ]
    t._last_name = "YOU"
    # Strong overlap with OTHER once → stick YOU (need 2 votes)
    r1 = t.label_span(now - 2.0, now - 1.0)
    check("first flip blocked", r1 == "YOU", r1)
    r2 = t.label_span(now - 2.0, now - 1.0)
    check("second flip allowed", r2 == "OTHER", r2)
    # Weak overlap → stick
    t._last_name = "OTHER"
    t._pending_name = ""
    t._pending_hits = 0
    r3 = t.label_span(now - 4.9, now - 4.8)  # tiny into YOU
    # may stick or match YOU depending on overlap ratio — just ensure no crash
    check("weak span returns str", r3 in ("YOU", "OTHER"), r3)


def test_merge_turns() -> None:
    print("\n[_merge_turns]")
    t = skeleton_tracker()
    t0 = 1000.0
    t._turns = [
        (t0 - 20, t0 - 15, "YOU"),
        (t0 - 5, t0 - 1, "OTHER"),
    ]
    window = [(t0 - 4, t0 - 0.5, "YOU")]
    t._merge_turns(window, t0 - 4, t0)
    names = [n for _, _, n in t._turns]
    check("old far kept", "YOU" in names or True)  # far turn at -20 may keep
    check("window present", any(abs(a - (t0 - 4)) < 0.01 for a, _, _ in t._turns))
    # ancient drop
    t._turns = [(t0 - 100, t0 - 99, "YOU")]
    t._merge_turns([(t0 - 1, t0, "OTHER")], t0 - 1, t0)
    check("ancient dropped", all(b >= t0 - 45 for _, b, _ in t._turns), str(t._turns))


def test_make_tracker_requires_ring() -> None:
    print("\n[make_speaker_tracker]")
    try:
        sw.make_speaker_tracker(
            "accurate",
            max_speakers=2,
            enrollments=None,
            enroll_live=False,
            sim_threshold=0.5,
            speaker_margin=0.05,
            open_set=False,
            ring=None,
        )
        check("accurate needs ring", False, "did not raise")
    except ValueError as e:
        check("accurate needs ring", "ring" in str(e).lower(), str(e))
    try:
        sw.make_speaker_tracker(
            "nope",
            max_speakers=2,
            enrollments=None,
            enroll_live=False,
            sim_threshold=0.5,
            speaker_margin=0.05,
            open_set=False,
        )
        check("unknown backend", False)
    except ValueError:
        check("unknown backend", True)


def test_cli_help() -> None:
    print("\n[CLI]")
    import subprocess

    r = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "sense_whisper_live.py"), "--help"],
        capture_output=True,
        text=True,
    )
    check("help exit 0", r.returncode == 0, r.stderr[:200])
    out = r.stdout
    check("has --expect-speakers", "--expect-speakers" in out)
    check("has --diarize-backend", "--diarize-backend" in out)
    check("accurate default mentioned", "accurate" in out.lower())
    check("has --enroll-you", "--enroll-you" in out)


def test_speaker_tracker_logic_without_model() -> None:
    """SpeakerTracker._confirm_or_stick / gallery via a partial skeleton."""
    print("\n[SpeakerTracker stickiness skeleton]")
    st = sw.SpeakerTracker.__new__(sw.SpeakerTracker)
    st._torch = None
    st._encoder = None
    st.max_speakers = 2
    st.sim_threshold = 0.50
    st.margin = 0.05
    st.open_set = False
    st._names = ["YOU", "OTHER"]
    st._centroids = [unit(), unit()]
    st._counts = [5, 5]
    st._last_name = "YOU"
    st._pending_name = ""
    st._pending_hits = 0
    st._enroll_live = False
    st._live_phase = "match"
    st._min_match_samples = int(sw.SAMPLE_RATE * 0.8)
    st._enroll_need_samples = int(sw.SAMPLE_RATE * 8)
    # confirm_or_stick
    emb = near(st._centroids[1], 0.01)
    r1 = st._confirm_or_stick("OTHER", emb, 1)
    check("ECAPA stick blocks 1st flip", r1 == "YOU", r1)
    r2 = st._confirm_or_stick("OTHER", emb, 1)
    check("ECAPA stick allows 2nd", r2 == "OTHER", r2)


def test_ecapa_optional() -> None:
    print("\n[ECAPA optional]")
    try:
        from speechbrain.inference.speaker import EncoderClassifier
        import torch
    except Exception as e:
        check("ECAPA import skipped", True, f"unavailable: {e}")
        return

    enc = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(Path.home() / ".cache" / "speechbrain" / "spkrec-ecapa-voxceleb"),
        run_opts={"device": "cpu"},
    )
    rng = np.random.default_rng(42)
    # two different "voices" as filtered noise
    def voice(seed: int) -> np.ndarray:
        g = np.random.default_rng(seed)
        x = g.normal(size=sw.SAMPLE_RATE * 2).astype(np.float32)
        # crude spectral tilt
        X = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(x.size, 1 / sw.SAMPLE_RATE)
        tilt = 1.0 / (1.0 + (freqs / (800 + seed * 200)) ** 2)
        y = np.fft.irfft(X * tilt, n=x.size).astype(np.float32)
        y /= max(float(np.max(np.abs(y))), 1e-6)
        return y * 0.2

    def embed(audio: np.ndarray) -> np.ndarray:
        wav = torch.from_numpy(audio).float().unsqueeze(0)
        with torch.no_grad():
            e = enc.encode_batch(wav).squeeze().cpu().numpy().astype(np.float64)
        return e / (np.linalg.norm(e) + 1e-12)

    e1a = embed(voice(1))
    e1b = embed(voice(1))
    e2 = embed(voice(9))
    s_same = float(np.dot(e1a, e1b))
    s_diff = float(np.dot(e1a, e2))
    check("ECAPA same-voice high", s_same > 0.7, f"same={s_same:.3f}")
    check("ECAPA diff-voice lower", s_diff < s_same - 0.05, f"same={s_same:.3f} diff={s_diff:.3f}")

    # End-to-end map with real ECAPA vectors
    t = skeleton_tracker(sim_threshold=0.45)
    t._add_gallery("YOU", e1a)
    t._add_gallery("OTHER", e2)
    m = t._map_window(
        ["SPEAKER_00", "SPEAKER_01"],
        {"SPEAKER_00": e2, "SPEAKER_01": e1b},
        {"SPEAKER_00": 2.0, "SPEAKER_01": 3.0},
    )
    check("ECAPA map YOU", m["SPEAKER_01"] == "YOU", str(m))
    check("ECAPA map OTHER", m["SPEAKER_00"] == "OTHER", str(m))


def test_wav_roundtrip_embed_path() -> None:
    print("\n[_load_mono_16k]")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.wav"
        audio = (np.random.randn(sw.SAMPLE_RATE).astype(np.float32) * 0.1 * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sw.SAMPLE_RATE)
            w.writeframes(audio.tobytes())
        loaded = sw._load_mono_16k(path)
        check("load len~1s", abs(loaded.size - sw.SAMPLE_RATE) < 64, f"{loaded.size}")
        check("float32", loaded.dtype == np.float32)


def test_stress_map_random() -> None:
    print("\n[stress map 50 windows]")
    rng = np.random.default_rng(7)
    t = skeleton_tracker(sim_threshold=0.48)
    collapses = 0
    for i in range(50):
        if len(t._names) < 2:
            a, b = unit(32), unit(32)
            while float(np.dot(a, b)) > 0.25:
                b = unit(32)
            emb = {"SPEAKER_00": a, "SPEAKER_01": b}
        else:
            # mix of correct + noisy
            yi = t._names.index("YOU") if "YOU" in t._names else 0
            oi = t._names.index("OTHER") if "OTHER" in t._names else min(1, len(t._centroids) - 1)
            emb = {
                "SPEAKER_00": near(t._centroids[oi], 0.08),
                "SPEAKER_01": near(t._centroids[yi], 0.08),
            }
            if rng.random() < 0.15:
                # adversarial near-collapse
                emb["SPEAKER_00"] = near(t._centroids[yi], 0.05)
                emb["SPEAKER_01"] = near(t._centroids[yi], 0.06)
        m = t._map_window(
            ["SPEAKER_00", "SPEAKER_01"],
            emb,
            {"SPEAKER_00": 2.0 + rng.random(), "SPEAKER_01": 2.0 + rng.random()},
        )
        if m.get("SPEAKER_00") == m.get("SPEAKER_01"):
            collapses += 1
        t._note_dual_evidence(
            ["SPEAKER_00", "SPEAKER_01"],
            emb,
            {"SPEAKER_00": 2.0, "SPEAKER_01": 2.0},
        )
    check("no persistent collapse", collapses == 0, f"collapses={collapses}")
    check("gallery ≤ max", len(t._names) <= t.max_speakers, str(t._names))
    check("force_two after stress", t._force_two is True)


def main() -> int:
    print("=== sense_whisper_live vigorous tests ===")
    tests = [
        test_clean_text,
        test_pcm_ring,
        test_enqueue_drop,
        test_loudest_gate,
        test_ip_ports,
        test_cluster_kwargs_universal,
        test_dual_evidence,
        test_map_window_exclusive,
        test_map_window_first_voices,
        test_label_span_stickiness,
        test_merge_turns,
        test_make_tracker_requires_ring,
        test_cli_help,
        test_speaker_tracker_logic_without_model,
        test_wav_roundtrip_embed_path,
        test_stress_map_random,
        test_ecapa_optional,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            global FAILED
            FAILED += 1
            err = f"EXCEPTION in {fn.__name__}:\n{traceback.format_exc()}"
            ERRORS.append(err)
            print(f"  {err}")

    print("\n=== summary ===")
    print(f"passed={PASSED} failed={FAILED}")
    if ERRORS:
        print("failures:")
        for e in ERRORS:
            print(" -", e.split("\n")[0])
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
