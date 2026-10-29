# Repository Guidelines

## Project Structure & Module Organization

- `deepstream_mycare_app/` contains the C/C++ DeepStream application, Makefile, config parser sources, and the built binary.
- `dependencies/` contains custom NVIDIA Inference Server implementations built during image creation.
- `configs/` holds Triton/NvInferServer model configurations; `project_config.txt` configures the DeepStream pipeline.
- `scripts/` contains entrypoint, startup, TensorRT preparation, model-generation, and Rivermax shim utilities.
- `triton_model/` is the Triton model repository; `videos/` provides RTSP test content. GPU-specific `.plan` files are generated at startup and are not source artifacts.

## Build, Test, and Development Commands

- `cp sample.env .env`: create local configuration, then edit `BASE_IMAGE`, `DEV_IMAGE`, `GPU_ID`, and `MDX_DATA_DIR`.
- `docker compose --env-file .env up --build -d deepstream`: build and launch the DeepStream app against an external Triton service.
- `docker compose --env-file .env --profile triton up --build -d triton`: launch Triton as a standalone Compose service.
- `docker exec -it mycareapp bash`: open a shell in the running DeepStream container.
- `bash ./scripts/start_triton.sh`: prepare model plans and start Triton only.
- `bash ./scripts/create_model.sh`: convert mounted ONNX models into TensorRT plans.
- `make -C deepstream_mycare_app CUDA_VER=12.6 all`: compile the DeepStream application (inside a container with NVIDIA tooling).
- `bash ./scripts/start_project.sh`: wait for Triton readiness and run `deepstream_mycare_app`.

## Coding Style & Naming Conventions

- Use 4-space indentation in C, C++, Bash, YAML, and configuration files.
- Keep functions and variables descriptive and snake_case; use uppercase for Makefile variables and environment variables.
- Prefer focused, self-contained changes: runtime code, model configs, and startup scripts should remain clearly separated.
- Run `make -C deepstream_mycare_app CUDA_VER=12.6 clean all` after modifying application sources.

## Testing Guidelines

- No automated test suite or coverage gate exists.
- Validate manually with live inputs: launch MediaMTX via `docker compose --env-file .env --profile relay up -d mediamtx`, publish `videos/input.mp4` as RTSP, and verify output on port `8665`.
- Check Triton readiness at `http://localhost:8010/v2/health/ready`.
- Preserve or update the source registration JSON example in `README.md` when payload fields change.

## Commit & Pull Request Guidelines

- Use concise, imperative Conventional Commits, such as `fix: handle missing batch dimension` or `docs: clarify source setup`.
- Pull requests should state the change, verification performed, relevant model or config names, and linked issues.
- Include a short RTSP output recording or log excerpt for pipeline changes.

## Security & Configuration Tips

- Never commit `.env`, generated plans, videos, credentials, or absolute paths.
- Keep ports and host GPU IDs configurable through `sample.env`.
- Do not lower Triton, TensorRT, or CUDA versions without documenting compatibility and validation results.
