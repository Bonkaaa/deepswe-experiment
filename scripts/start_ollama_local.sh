#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE=""
RESTART=false

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
print(f"BASE_URL={shlex.quote(base_url)}")
print(f"MODELS_DIR={shlex.quote(models_dir)}")
print(f"STATE_DIR={shlex.quote(state_dir)}")
print(f"BINARY={shlex.quote(binary)}")
print(f"MODEL={shlex.quote(model)}")
print(f"NUM_PARALLEL={shlex.quote(num_parallel)}")
print(f"MAX_LOADED_MODELS={shlex.quote(max_loaded_models)}")
print(f"CUDA_VISIBLE_DEVICES={shlex.quote(cuda_visible_devices)}")
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
  pkill -f "${BINARY} serve" 2>/dev/null || true
  sleep 1
fi

verify_model() {
  if ! curl -s "${BASE_URL}/api/tags" | grep -q "\"${MODEL}\""; then
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
if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}"
  echo "[start_ollama] Multi-GPU distribution configured: CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"
fi
if [[ -n "$MODELS_DIR" ]]; then
  if [[ "$MODELS_DIR" != /* ]]; then
    MODELS_DIR="${REPO_ROOT}/${MODELS_DIR}"
  fi
  mkdir -p "$MODELS_DIR"
  export OLLAMA_MODELS="${MODELS_DIR}"
fi

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
