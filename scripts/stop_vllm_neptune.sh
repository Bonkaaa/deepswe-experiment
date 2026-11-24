#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

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

STATE_DIR=$(uv run --frozen python -c '
import sys
from deepswe_exp.components.configuration.settings import load_settings
s = load_settings(profile=sys.argv[1])
print(str(s.ollama_state_dir) if s.ollama_state_dir else f".local/vllm-{sys.argv[1]}")
' "$PROFILE" 2>/dev/null || echo ".local/vllm-${PROFILE}")

[[ "$STATE_DIR" != /* ]] && STATE_DIR="${REPO_ROOT}/${STATE_DIR}"
PID_FILE="${STATE_DIR}/vllm.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[stop_vllm:${PROFILE}] No PID file found at ${PID_FILE}. Nothing to stop."
  exit 0
fi

PID=$(cat "$PID_FILE" | tr -d '[:space:]')
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "[stop_vllm:${PROFILE}] Stale PID file cleaned up."
  exit 0
fi

CMDLINE=$(tr '\0' ' ' < "/proc/${PID}/cmdline" 2>/dev/null || true)
if [[ "$CMDLINE" != *"vllm"* ]]; then
  echo "[stop_vllm:${PROFILE}] PID ${PID} is not vLLM; refusing to kill." >&2
  exit 2
fi

echo "[stop_vllm:${PROFILE}] Terminating vLLM server (PID: ${PID})..."
kill "$PID" 2>/dev/null || true
for _ in $(seq 1 15); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "[stop_vllm:${PROFILE}] vLLM stopped cleanly."
    exit 0
  fi
  sleep 1
done

kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "[stop_vllm:${PROFILE}] vLLM force-stopped."
