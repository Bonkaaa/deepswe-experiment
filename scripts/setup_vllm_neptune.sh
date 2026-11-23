#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Install vLLM and download Qwen/Qwen3.8-27B-FP8 into .local/vllm-neptune/ (zero system impact).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VLLM_ROOT="${REPO_ROOT}/.local/vllm-neptune"
VENV_DIR="${VLLM_ROOT}/venv"
HF_CACHE="${VLLM_ROOT}/hf_cache"
MODEL_ID="Qwen/Qwen3.8-27B-FP8"

mkdir -p "${VLLM_ROOT}" "${HF_CACHE}"

if [[ ! -x "${VENV_DIR}/bin/vllm" ]]; then
  echo "[setup_vllm] Creating repo-local Python 3.12 virtualenv at ${VENV_DIR}..."
  uv venv --python 3.12 "${VENV_DIR}"
  echo "[setup_vllm] Installing vllm into ${VENV_DIR}..."
  uv pip install --python "${VENV_DIR}/bin/python" vllm
fi

export HF_HOME="${HF_CACHE}"
echo "[setup_vllm] vLLM installed at ${VENV_DIR}/bin/vllm"
"${VENV_DIR}/bin/vllm" --version

echo "[setup_vllm] Downloading ${MODEL_ID} into ${HF_CACHE} (skips files already cached)..."
"${VENV_DIR}/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_ID}')"
echo "[setup_vllm] Model ${MODEL_ID} is ready in ${HF_CACHE}."
