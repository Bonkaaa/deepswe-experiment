#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE=""
RESTART=false
ORIGINAL_CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --restart)
      RESTART=true
      shift
      ;;
    *)
      shift
      ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  echo "ERROR: --profile <saturn|neptune> is required." >&2
  exit 1
fi

# Load Ollama configuration using single source of truth from settings in 1 Python invocation
eval "$(uv run --frozen python -c '
import sys, shlex
from deepswe_exp.components.configuration.settings import load_settings
profile = sys.argv[1]
s = load_settings(profile=profile)
base_url = str(s.ollama_base_url)
models_dir = str(s.ollama_models_dir) if s.ollama_models_dir else ""
state_dir = str(s.ollama_state_dir) if s.ollama_state_dir else f"scratch/ollama/{profile}"
binary = str(s.ollama_binary) if s.ollama_binary else "ollama"
model = str(s.model)
num_parallel = str(s.ollama_options.get("num_parallel", 1))
max_loaded_models = str(s.ollama_options.get("max_loaded_models", 1))
cuda_visible_devices = str(s.ollama_options.get("cuda_visible_devices", ""))
num_gpus = str(s.ollama_options.get("num_gpus", 2 if profile == "saturn" else 1))
print(f"BASE_URL={shlex.quote(base_url)}")
print(f"MODELS_DIR={shlex.quote(models_dir)}")
print(f"STATE_DIR={shlex.quote(state_dir)}")
print(f"BINARY={shlex.quote(binary)}")
print(f"MODEL={shlex.quote(model)}")
print(f"NUM_PARALLEL={shlex.quote(num_parallel)}")
print(f"MAX_LOADED_MODELS={shlex.quote(max_loaded_models)}")
print(f"CUDA_VISIBLE_DEVICES_CONFIG={shlex.quote(cuda_visible_devices)}")
print(f"NUM_GPUS={shlex.quote(num_gpus)}")
' "$PROFILE")"

# Resolve STATE_DIR to absolute path if relative
if [[ "$STATE_DIR" != /* ]]; then
  STATE_DIR="${REPO_ROOT}/${STATE_DIR}"
fi

mkdir -p "$STATE_DIR"

PID_FILE="${STATE_DIR}/ollama.pid"
LOG_FILE="${STATE_DIR}/ollama.log"

if [[ "$RESTART" = true ]] && curl -s -f "${BASE_URL}/api/tags" >/dev/null 2>&1; then
  echo "[start_ollama] Restart requested. Stopping existing Ollama daemon to apply new GPU/context configuration..."
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 2
  fi
  pkill -u "$(id -u)" -f "${BINARY} serve" 2>/dev/null || true
  sleep 1
fi

verify_model() {
  local tags_json
  tags_json="$(curl -s "${BASE_URL}/api/tags" || true)"
  if ! grep -q "\"${MODEL}\"" <<<"$tags_json"; then
    echo "[start_ollama] WARNING: Model '${MODEL}' not found in '${BASE_URL}/api/tags'. Please ensure the model is pulled." >&2
  else
    echo "[start_ollama] Model '${MODEL}' verified in Ollama repository."
  fi
}

echo "[start_ollama] Checking if Ollama is already active at ${BASE_URL}..."
if curl -s -f "${BASE_URL}/api/tags" >/dev/null 2>&1; then
  echo "[start_ollama] Ollama is already running and reachable at ${BASE_URL}."
  verify_model
  exit 0
fi

echo "[start_ollama] Starting Ollama server background daemon..."
export OLLAMA_HOST="${BASE_URL}"
export OLLAMA_NUM_PARALLEL="${NUM_PARALLEL}"
export OLLAMA_MAX_LOADED_MODELS="${MAX_LOADED_MODELS}"
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE="q8_0"
if [[ "${NUM_GPUS:-1}" -gt 1 ]]; then
  export OLLAMA_SCHED_SPREAD=1
fi
resolve_gpus() {
  local cfg="${CUDA_VISIBLE_DEVICES_CONFIG:-}"
  local target="${ORIGINAL_CUDA_VISIBLE_DEVICES:-$cfg}"
  local gpu_count="${NUM_GPUS:-1}"

  if [[ -z "$target" ]]; then
    return 0
  fi

  if ! command -v nvidia-smi >/dev/null 2>&1; then
    if [[ "$target" != "auto" ]]; then
      export CUDA_VISIBLE_DEVICES="$target"
      echo "[start_ollama] GPU distribution configured: CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"
    fi
    return 0
  fi

  # Auto mode: dynamically select top N GPUs with most free VRAM (1 for Neptune, 2 for Saturn)
  if [[ "$target" == "auto" ]]; then
    local auto_selected
    auto_selected="$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits 2>/dev/null | \
      sort -t',' -k2 -nr | \
      head -n "${gpu_count}" | \
      cut -d',' -f1 | \
      tr '\n' ',' | sed 's/,$//')"
    if [[ -n "$auto_selected" ]]; then
      echo "[start_ollama] Auto-detected ${gpu_count} idle GPU(s) with most free VRAM: CUDA_VISIBLE_DEVICES=${auto_selected}"
      export CUDA_VISIBLE_DEVICES="${auto_selected}"
    fi
    return 0
  fi

  # Explicit list (e.g. "0,1"): check free VRAM per card
  local busy=false
  local IFS=','
  read -ra GPUS <<< "$target"
  for g in "${GPUS[@]}"; do
    local free_mb
    free_mb="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i "$g" 2>/dev/null | head -n1 | tr -d ' ')"
    if [[ -n "$free_mb" && "$free_mb" -lt 18000 ]]; then
      echo "[start_ollama] WARNING: Configured GPU $g has only ${free_mb} MiB free VRAM (< 18000 MiB recommended for 128k context)." >&2
      busy=true
    fi
  done

  if [[ "$busy" == true ]]; then
    echo "[start_ollama] Target GPUs ($target) appear busy. Searching for idle GPUs with sufficient VRAM..." >&2
    local alt
    alt="$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits 2>/dev/null | \
      sort -t',' -k2 -nr | \
      head -n "${#GPUS[@]}" | \
      cut -d',' -f1 | \
      tr '\n' ',' | sed 's/,$//')"
    if [[ -n "$alt" && "$alt" != "$target" ]]; then
      echo "[start_ollama] Automatically switching to idle GPUs: CUDA_VISIBLE_DEVICES=${alt}"
      export CUDA_VISIBLE_DEVICES="${alt}"
      return 0
    fi
  fi

  export CUDA_VISIBLE_DEVICES="$target"
  echo "[start_ollama] Multi-GPU distribution configured: CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"
}

resolve_gpus
if [[ -n "$MODELS_DIR" ]]; then
  if [[ "$MODELS_DIR" != /* ]]; then
    MODELS_DIR="${REPO_ROOT}/${MODELS_DIR}"
  fi
  mkdir -p "$MODELS_DIR"
  export OLLAMA_MODELS="${MODELS_DIR}"
fi

# Auto-enable repo-local NVIDIA CUDA Forward Compatibility (e.g. cuda-compat-12-8 on R535 H100 hosts)
for compat_dir in "${STATE_DIR}"/cuda-compat/usr/local/cuda*/compat "${STATE_DIR}/cuda-compat"; do
  if [[ -f "${compat_dir}/libcuda.so.1" ]]; then
    export LD_LIBRARY_PATH="${compat_dir}:${LD_LIBRARY_PATH:-}"
    echo "[start_ollama] Enabled local CUDA forward compatibility: ${compat_dir}"
    break
  fi
done

nohup "${BINARY}" serve > "${LOG_FILE}" 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" > "${PID_FILE}"
echo "[start_ollama] Process spawned (PID: ${SERVER_PID}). Waiting for healthcheck (${BASE_URL}/api/tags)..."

MAX_WAIT=30
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
  if curl -s -f "${BASE_URL}/api/tags" >/dev/null 2>&1; then
    echo "[start_ollama] Ollama is healthy and ready (PID: ${SERVER_PID}, Profile: ${PROFILE})."
    verify_model
    exit 0
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

echo "ERROR: Ollama server failed to start within ${MAX_WAIT} seconds." >&2
echo "--- Last 20 lines of log (${LOG_FILE}) ---" >&2
tail -n 20 "${LOG_FILE}" >&2 || true
kill "${SERVER_PID}" 2>/dev/null || true
rm -f "${PID_FILE}"
exit 1
