#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OLLAMA_ROOT="$REPO_ROOT/.local/ollama-neptune"
OLLAMA_BIN="$OLLAMA_ROOT/runtime/bin/ollama"
PID_FILE="$OLLAMA_ROOT/ollama.pid"

if [[ ! -f "$PID_FILE" ]]; then
  printf 'No repo-local Ollama PID file found; nothing to stop.\n'
  exit 0
fi

pid="$(cat "$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f -- "$PID_FILE"
  printf 'Removed stale repo-local PID file.\n'
  exit 0
fi

if [[ ! -x "$OLLAMA_BIN" || ! -r "/proc/$pid/exe" ]] \
  || [[ "$(readlink -f "/proc/$pid/exe")" != "$(readlink -f "$OLLAMA_BIN")" ]]; then
  printf 'PID %s is not running this repo-local Ollama; refusing to stop it.\n' "$pid" >&2
  exit 2
fi

kill "$pid"
for _ in $(seq 1 15); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f -- "$PID_FILE"
    printf 'Stopped Neptune Ollama (PID %s). Model files remain in .local/ollama-neptune/models/.\n' "$pid"
    exit 0
  fi
  sleep 1
done

printf 'Ollama did not exit after SIGTERM; left it running. Inspect .local/ollama-neptune/ollama.log.\n' >&2
exit 2
