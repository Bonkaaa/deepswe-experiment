#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE=""

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

if [[ -z "$PROFILE" ]]; then
  echo "ERROR: --profile <saturn|neptune> is required." >&2
  exit 1
fi

STATE_DIR=$(uv run --frozen python -c '
import sys
from deepswe_exp.components.configuration.settings import load_settings
profile = sys.argv[1]
s = load_settings(profile=profile)
print(str(s.ollama_state_dir) if s.ollama_state_dir else f"scratch/ollama/{profile}")
' "$PROFILE")

if [[ "$STATE_DIR" != /* ]]; then
  STATE_DIR="${REPO_ROOT}/${STATE_DIR}"
fi

PID_FILE="${STATE_DIR}/ollama.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[stop_ollama] No PID file found at ${PID_FILE}. Nothing to stop."
  exit 0
fi

PID=$(cat "$PID_FILE" | tr -d '[:space:]')

if [[ -z "$PID" ]]; then
  echo "[stop_ollama] Empty PID file found. Cleaning up."
  rm -f "$PID_FILE"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "[stop_ollama] Process ${PID} is not running. Removing stale PID file."
  rm -f "$PID_FILE"
  exit 0
fi

COMM=$(ps -p "$PID" -o comm= 2>/dev/null || true)
if [[ -n "$COMM" && "$COMM" != *"ollama"* ]]; then
  echo "[stop_ollama] Process ${PID} is running but does not appear to be ollama (comm=${COMM}). Removing stale PID file without killing."
  rm -f "$PID_FILE"
  exit 0
fi

echo "[stop_ollama] Terminating Ollama server process (PID: ${PID})..."
kill "$PID" 2>/dev/null || true

MAX_WAIT=10
WAITED=0
while kill -0 "$PID" 2>/dev/null && [[ $WAITED -lt $MAX_WAIT ]]; do
  sleep 1
  WAITED=$((WAITED + 1))
done

if kill -0 "$PID" 2>/dev/null; then
  echo "[stop_ollama] Process ${PID} did not stop gracefully; sending SIGKILL..."
  kill -9 "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "[stop_ollama] Ollama server stopped successfully."
