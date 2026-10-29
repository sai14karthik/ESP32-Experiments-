# Triton Model Repository

This directory is the Triton model repository. Each model has a `config.pbtxt` and a version directory; TensorRT `.plan` files are generated at startup and are ignored by Git.

## Active App Chain

The DeepStream app loads two entry points:

1. Primary GIE: `configs/config_triton_nvinferserver_gdino_grpc.txt` → `ensemble_python_gdino`
2. Secondary GIE: `configs/config_triton_nvinferserver_hpe_ar_grpc.txt` → `bls_hpe_ar`

```text
ensemble_python_gdino
  → gdino_preprocess
  → gdino_trt
  → gdino_postprocess

bls_hpe_ar
  → hpe
  → bls_ar
  → ar
```

## Models

| Model | What it does | Where used |
|---|---|---|
| `ensemble_python_gdino` | Prompt-driven detection ensemble; emits labels, boxes, and scores. | Primary GIE entry point. |
| `gdino_preprocess` | Tokenizes the prompt and prepares attention, position, token, and position-map tensors. | Called by `ensemble_python_gdino`. |
| `gdino_trt` | TensorRT Grounding DINO detector; runs image + text-token inference. | Called by `ensemble_python_gdino`. |
| `gdino_postprocess` | Converts detector logits and boxes into final labels, boxes, and scores. | Called by `ensemble_python_gdino`. |
| `bls_hpe_ar` | Combined BLS wrapper for pose estimation and action recognition. | Secondary GIE entry point. |
| `hpe` | TensorRT human pose estimator; returns 17 COCO/PCT keypoints and scores. | Called by `bls_hpe_ar`. |
| `bls_ar` | BLS action-recognition wrapper; prepares skeleton time series and calls `ar`. | Called by `bls_hpe_ar`. |
| `ar` | TensorRT action-recognition network; predicts an action index. | Called by `bls_ar`. |
| `bls_hpe` | Pose-only BLS wrapper; crops a detected box and calls `hpe`. | Not directly used by the active DeepStream config. |
| `yolo26_m_pose` | YOLO26-M pose estimator; emits person detections and 17 COCO keypoints. | Independent Triton model; not used by DeepStream yet. |

`bls_hpe_ar` converts `hpe` output into the 18-joint RPM pose format used by action recognition, OSD, and event metadata. `ar` output is mapped by the DeepStream custom processor to `Lying Down`, `Sitting`, `Standing`, and `Walking`.

`yolo26_m_pose` is onboarded independently. To deploy it, run
`bash ./scripts/export_yolo26_model.sh` once inside the Triton container. The
startup script then builds the TensorRT plan automatically when
`/models/yolo26_m_pose.onnx` exists.
