#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Install vLLM and download the configured model into repo-local .local/vllm-<profile>/ (zero system impact).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="neptune"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

eval "$(uv run --frozen python -c '
import sys, shlex
from deepswe_exp.components.configuration.settings import load_settings
profile = sys.argv[1]
s = load_settings(profile=profile)
opts = s.ollama_options or {}
venv_dir = opts.get("venv_dir", f".local/vllm-{profile}/venv")
hf_home = opts.get("hf_home", f".local/vllm-{profile}/hf_cache")
state_dir = str(s.ollama_state_dir) if s.ollama_state_dir else f".local/vllm-{profile}"
print(f"MODEL_ID={shlex.quote(str(s.model))}")
print(f"VLLM_ROOT={shlex.quote(state_dir)}")
print(f"VENV_DIR={shlex.quote(str(venv_dir))}")
print(f"HF_CACHE={shlex.quote(str(hf_home))}")
' "$PROFILE")"

[[ "$VLLM_ROOT" != /* ]] && VLLM_ROOT="${REPO_ROOT}/${VLLM_ROOT}"
[[ "$VENV_DIR" != /* ]] && VENV_DIR="${REPO_ROOT}/${VENV_DIR}"
[[ "$HF_CACHE" != /* ]] && HF_CACHE="${REPO_ROOT}/${HF_CACHE}"

mkdir -p "${VLLM_ROOT}" "${HF_CACHE}"

if [[ ! -x "${VENV_DIR}/bin/vllm" ]]; then
  echo "[setup_vllm:${PROFILE}] Creating repo-local Python 3.12 virtualenv at ${VENV_DIR}..."
  uv venv --python 3.12 "${VENV_DIR}"
  echo "[setup_vllm:${PROFILE}] Installing vllm into ${VENV_DIR}..."
  uv pip install --python "${VENV_DIR}/bin/python" vllm
fi

export HF_HOME="${HF_CACHE}"
echo "[setup_vllm:${PROFILE}] vLLM installed at ${VENV_DIR}/bin/vllm"
"${VENV_DIR}/bin/vllm" --version

echo "[setup_vllm:${PROFILE}] Downloading ${MODEL_ID} into ${HF_CACHE} (skips files already cached)..."
"${VENV_DIR}/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_ID}')"
echo "[setup_vllm:${PROFILE}] Model ${MODEL_ID} is ready in ${HF_CACHE}."
