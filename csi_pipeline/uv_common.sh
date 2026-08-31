# Shared uv helpers for csi_pipeline scripts. Source, do not execute.
CSI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CSI_ROOT/.." && pwd)"

ensure_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  (cd "$REPO_ROOT" && uv sync --group csi --quiet)
}

# Run from CSI_ROOT so relative paths (fixtures/, ../sample data /) work.
uv_csi() {
  ensure_uv
  (cd "$CSI_ROOT" && uv run --project "$REPO_ROOT" --group csi python "$@")
}
