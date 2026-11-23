#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${REPO_ROOT}/.local/vllm-neptune/vllm.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[stop_vllm] No PID file found at ${PID_FILE}. Nothing to stop."
  exit 0
fi

PID=$(cat "$PID_FILE" | tr -d '[:space:]')
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "[stop_vllm] Stale PID file cleaned up."
  exit 0
fi

CMDLINE=$(tr '\0' ' ' < "/proc/${PID}/cmdline" 2>/dev/null || true)
if [[ "$CMDLINE" != *"vllm"* ]]; then
  echo "[stop_vllm] PID ${PID} is not vLLM; refusing to kill." >&2
  exit 2
fi

echo "[stop_vllm] Terminating vLLM server (PID: ${PID})..."
kill "$PID" 2>/dev/null || true
for _ in $(seq 1 15); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "[stop_vllm] vLLM stopped cleanly."
    exit 0
  fi
  sleep 1
done

kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "[stop_vllm] vLLM force-stopped."
