#!/usr/bin/env python3
"""Extract frames from a video, run pose detection via Triton, and save results."""

import json
import os
import subprocess
import sys
import numpy as np
from PIL import Image
from PIL import ImageDraw
import tritonclient.http as tritonhttp

TRITON_HTTP_PORT = os.environ.get("TRITON_HTTP_PORT", "8010")
BASE_URL = f"http://localhost:{TRITON_HTTP_PORT}"
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", "videos")
RESULTS_DIR = os.environ.get("RESULTS_DIR", "tests/results")
NUM_FRAMES = int(os.environ.get("NUM_FRAMES", "5"))
DET_THRESHOLD = float(os.environ.get("DET_THRESHOLD", "0.25"))
KPT_THRESHOLD = float(os.environ.get("KPT_THRESHOLD", "0.5"))

# COCO 17 keypoints, ordered by index. Each keypoint has (x, y, confidence).
KEYPOINT_NAMES = (
    "nose",           # 0
    "left_eye",       # 1
    "right_eye",      # 2
    "left_ear",       # 3
    "right_ear",      # 4
    "left_shoulder",  # 5
    "right_shoulder", # 6
    "left_elbow",     # 7
    "right_elbow",    # 8
    "left_wrist",     # 9
    "right_wrist",    # 10
    "left_hip",       # 11
    "right_hip",      # 12
    "left_knee",      # 13
    "right_knee",     # 14
    "left_ankle",     # 15
    "right_ankle",    # 16
)

# Skeleton connections as keypoint index pairs, grouped by body region.
SKELETON = [
    # Head: nose to eyes, eyes to ears.
    (0, 1), (0, 2), (1, 3), (2, 4),
    # Arms: shoulders to elbows, elbows to wrists.
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
    # Torso: shoulder to hip on each side, hips to each other.
    (5, 11), (6, 12), (11, 12),
    # Legs: hips to knees, knees to ankles.
    (11, 13), (13, 15), (12, 14), (14, 16),
]

# Skeleton connections resolved to body part names.
SKELETON_NAMES = [(KEYPOINT_NAMES[a], KEYPOINT_NAMES[b]) for a, b in SKELETON]

def extract_frames(video_path, num_frames, output_dir):
    """Extract evenly spaced frames from a video using ffmpeg."""
    os.makedirs(output_dir, exist_ok=True)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", video_path,
        ],
        capture_output=True, text=True, check=True,
    )
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    interval = duration / (num_frames + 1)

    frame_paths = []
    for i in range(1, num_frames + 1):
        timestamp = i * interval
        frame_path = os.path.join(output_dir, f"frame_{i:03d}.png")
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", video_path,
                "-frames:v", "1", frame_path,
            ],
            capture_output=True, check=True,
        )
        frame_paths.append(frame_path)
    return frame_paths


def preprocess(frame_path):
    """Load a frame and convert to CHW FP32 [1, 3, 640, 640] normalized to [0, 1]."""
    img = Image.open(frame_path).convert("RGB")
    img = img.resize((640, 640))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return np.expand_dims(arr.transpose(2, 0, 1), axis=0)


def infer(model_name, input_data):
    """Send input to Triton and return the raw output tensor."""
    client = tritonhttp.InferenceServerClient(
        url=f"localhost:{TRITON_HTTP_PORT}", verbose=False
    )
    inp = tritonhttp.InferInput("images", list(input_data.shape), "FP32")
    inp.set_data_from_numpy(input_data)
    out = tritonhttp.InferRequestedOutput("output0")
    response = client.infer(model_name, inputs=[inp], outputs=[out])
    return response.as_numpy("output0")


def annotate(frame_path, detections, output_path):
    """Draw pose points, skeleton, and bounding boxes on a frame."""
    img = Image.open(frame_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    orig_w, orig_h = img.size
    scale_x = orig_w / 640.0
    scale_y = orig_h / 640.0

    for det in detections:
        score = det[4]
        if score < DET_THRESHOLD:
            continue
        x1, y1, x2, y2 = (v * s for v, s in zip(det[:4], (scale_x, scale_y, scale_x, scale_y)))
        draw.rectangle([x1, y1, x2, y2], outline="lime", width=2)

        points = []
        for k in range(17):
            kx = det[6 + k * 3] * scale_x
            ky = det[6 + k * 3 + 1] * scale_y
            kconf = det[6 + k * 3 + 2]
            points.append((kx, ky, kconf))
            if kconf >= KPT_THRESHOLD:
                draw.ellipse(
                    [kx - 3, ky - 3, kx + 3, ky + 3],
                    fill="red", outline="white", width=1,
                )

        for a, b in SKELETON:
            if points[a][2] >= KPT_THRESHOLD and points[b][2] >= KPT_THRESHOLD:
                draw.line([points[a][:2], points[b][:2]], fill="cyan", width=2)

    img.save(output_path)


def main():
    model_name = sys.argv[1] if len(sys.argv) > 1 else "yolo26_m_pose"
    video_path = os.path.join(VIDEOS_DIR, "input.mp4")
    if not os.path.isfile(video_path):
        print(f"Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    frames_dir = os.path.join(RESULTS_DIR, "frames")
    annotated_dir = os.path.join(RESULTS_DIR, "annotated")
    frame_paths = extract_frames(video_path, NUM_FRAMES, frames_dir)
    print(f"Extracted {len(frame_paths)} frames from {video_path}")

    for i, frame_path in enumerate(frame_paths, 1):
        input_data = preprocess(frame_path)
        output = infer(model_name, input_data)
        result = {
            "frame": i,
            "source": frame_path,
            "model": model_name,
            "output_shape": list(output.shape),
            "detections": output.tolist(),
        }
        output_path = os.path.join(RESULTS_DIR, f"pose_{i:03d}.json")
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        annotated_path = os.path.join(annotated_dir, f"annotated_{i:03d}.png")
        os.makedirs(annotated_dir, exist_ok=True)
        annotate(frame_path, output[0].tolist(), annotated_path)
        print(f"Frame {i}: {output_path} + {annotated_path}")

    print(f"Annotated frames in {annotated_dir}")


if __name__ == "__main__":
    main()
