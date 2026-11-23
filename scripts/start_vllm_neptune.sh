#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTART=false
ORIGINAL_CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART=true
      shift
      ;;
    *)
      shift
      ;;
  esac
done

eval "$(uv run --frozen python -c '
import shlex
from deepswe_exp.components.configuration.settings import load_settings
s = load_settings(profile="neptune")
opts = s.ollama_options or {}
venv_dir = opts.get("venv_dir", ".local/vllm-neptune/venv")
hf_home = opts.get("hf_home", ".local/vllm-neptune/hf_cache")
cuda_dev = opts.get("cuda_visible_devices", "0")
gpu_util = opts.get("gpu_memory_utilization", 0.90)
kv_dtype = opts.get("kv_cache_dtype", "fp8")
tool_parser = opts.get("tool_call_parser", "qwen3_coder")
reason_parser = opts.get("reasoning_parser", "qwen3")
print(f"MODEL={shlex.quote(str(s.model))}")
print(f"BASE_URL={shlex.quote(str(s.ollama_base_url))}")
print(f"STATE_DIR={shlex.quote(str(s.ollama_state_dir))}")
print(f"VENV_DIR={shlex.quote(str(venv_dir))}")
print(f"HF_HOME_CFG={shlex.quote(str(hf_home))}")
print(f"CUDA_CFG={shlex.quote(str(cuda_dev))}")
print(f"MAX_MODEL_LEN={shlex.quote(str(s.ollama_num_ctx))}")
print(f"GPU_MEM_UTIL={shlex.quote(str(gpu_util))}")
print(f"KV_CACHE_DTYPE={shlex.quote(str(kv_dtype))}")
print(f"TOOL_PARSER={shlex.quote(str(tool_parser))}")
print(f"REASONING_PARSER={shlex.quote(str(reason_parser))}")
')"

[[ "$STATE_DIR" != /* ]] && STATE_DIR="${REPO_ROOT}/${STATE_DIR}"
[[ "$VENV_DIR" != /* ]] && VENV_DIR="${REPO_ROOT}/${VENV_DIR}"
[[ "$HF_HOME_CFG" != /* ]] && HF_HOME_CFG="${REPO_ROOT}/${HF_HOME_CFG}"

mkdir -p "${STATE_DIR}" "${HF_HOME_CFG}"

PID_FILE="${STATE_DIR}/vllm.pid"
LOG_FILE="${STATE_DIR}/vllm.log"
VLLM_BIN="${VENV_DIR}/bin/vllm"

# Parse host and port from BASE_URL (e.g. http://127.0.0.1:11435/v1)
HOST_PORT="${BASE_URL#*://}"
HOST_PORT="${HOST_PORT%%/*}"
HOST="${HOST_PORT%:*}"
PORT="${HOST_PORT#*:}"
MODELS_URL="http://${HOST}:${PORT}/v1/models"

if [[ "$RESTART" = true ]] && curl -s -f "${MODELS_URL}" >/dev/null 2>&1; then
  echo "[start_vllm] Restart requested. Stopping existing vLLM server..."
  bash "${REPO_ROOT}/scripts/stop_vllm_neptune.sh" || true
fi

echo "[start_vllm] Checking if vLLM is already active at ${MODELS_URL}..."
if curl -s -f "${MODELS_URL}" >/dev/null 2>&1; then
  echo "[start_vllm] vLLM is already running and reachable at ${MODELS_URL}."
  exit 0
fi

if [[ ! -x "${VLLM_BIN}" ]]; then
  echo "[start_vllm] vLLM not installed at ${VLLM_BIN}. Running setup_vllm_neptune.sh..."
  bash "${REPO_ROOT}/scripts/setup_vllm_neptune.sh"
fi

export CUDA_VISIBLE_DEVICES="${ORIGINAL_CUDA_VISIBLE_DEVICES:-$CUDA_CFG}"
export HF_HOME="${HF_HOME_CFG}"

# Auto-enable local cuda-compat if present (for driver 535 H100 hosts; prefer CUDA 13.0 compat for torch cu130)
for compat_dir in \
  "${STATE_DIR}/cuda-compat-13/usr/local/cuda-13.0/compat" \
  "${STATE_DIR}/cuda-compat-13/usr/local/cuda/compat" \
  "${STATE_DIR}/cuda-compat/usr/local/cuda-12.8/compat" \
  "${REPO_ROOT}/.local/ollama-neptune/cuda-compat/usr/local/cuda-12.8/compat" \
  "${STATE_DIR}/cuda-compat"; do
  if [[ -f "${compat_dir}/libcuda.so.1" ]]; then
    export LD_LIBRARY_PATH="${compat_dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    echo "[start_vllm] Enabled local CUDA forward compatibility: ${compat_dir}"
    break
  fi
done

echo "[start_vllm] Starting vLLM on GPU(s) CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES} (${HOST}:${PORT})..."
nohup "${VLLM_BIN}" serve "${MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --tensor-parallel-size 1 \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL}" \
  --kv-cache-dtype "${KV_CACHE_DTYPE}" \
  --enable-chunked-prefill \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser "${TOOL_PARSER}" \
  --reasoning-parser "${REASONING_PARSER}" \
  > "${LOG_FILE}" 2>&1 < /dev/null &

SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"
echo "[start_vllm] Process spawned (PID: ${SERVER_PID}). Waiting for model load & healthcheck (${MODELS_URL})..."

MAX_WAIT=600
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
  if curl -s -f "${MODELS_URL}" >/dev/null 2>&1; then
    echo "[start_vllm] vLLM is healthy and serving '${MODEL}' (PID: ${SERVER_PID})."
    exit 0
  fi
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "ERROR: vLLM exited unexpectedly during startup." >&2
    tail -n 30 "${LOG_FILE}" >&2 || true
    rm -f "${PID_FILE}"
    exit 1
  fi
  sleep 2
  WAITED=$((WAITED + 2))
done

echo "ERROR: vLLM server failed to become ready within ${MAX_WAIT} seconds." >&2
tail -n 30 "${LOG_FILE}" >&2 || true
exit 1
