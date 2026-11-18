#!/usr/bin/env bash
set -Eeuo pipefail

# Install and run Neptune's repo-local Ollama runtime, then download Qwen3.8.
# Saturn's .local/ollama and its Qwen3.6 process are separate and untouched.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OLLAMA_ROOT="$REPO_ROOT/.local/ollama-neptune"
RUNTIME_DIR="$OLLAMA_ROOT/runtime"
DOWNLOAD_DIR="$OLLAMA_ROOT/downloads"
MODEL_DIR="$OLLAMA_ROOT/models"
OLLAMA_BIN="$RUNTIME_DIR/bin/ollama"
PID_FILE="$OLLAMA_ROOT/ollama.pid"
LOG_FILE="$OLLAMA_ROOT/ollama.log"
OLLAMA_HOST="127.0.0.1:11435"
MODEL="qwen3.8:27b"

for tool in curl tar zstd nvidia-smi df readlink; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'Missing prerequisite: %s\n' "$tool" >&2
    exit 2
  }
done

[[ -f "$REPO_ROOT/pyproject.toml" && -d "$REPO_ROOT/config" ]] || {
  printf 'Run this script from a checkout containing pyproject.toml and config/.\n' >&2
  exit 2
}

gpu_list="$(nvidia-smi -L)"
if ! grep -q 'H100' <<<"$gpu_list"; then
  printf 'No H100 found via nvidia-smi; stopping before downloading anything.\n' >&2
  printf '%s\n' "$gpu_list" >&2
  exit 2
fi

available_kib="$(df -Pk "$REPO_ROOT" | awk 'NR == 2 {print $4}')"
if [[ -z "$available_kib" || "$available_kib" -lt 31457280 ]]; then
  printf 'Need at least 30 GiB free in the repo filesystem for Ollama and Qwen3.8.\n' >&2
  exit 2
fi

mkdir -p "$RUNTIME_DIR" "$DOWNLOAD_DIR" "$MODEL_DIR"

if [[ ! -x "$OLLAMA_BIN" ]]; then
  archive="$DOWNLOAD_DIR/ollama-linux-amd64.tar.zst"
  printf 'Downloading the official Linux AMD64 Ollama archive into %s\n' "$DOWNLOAD_DIR"
  curl --fail --location --retry 3 --output "$archive" \
    https://ollama.com/download/ollama-linux-amd64.tar.zst
  tar --zstd -xf "$archive" -C "$RUNTIME_DIR"
  rm -f -- "$archive"
fi

[[ -x "$OLLAMA_BIN" ]] || {
  printf 'Expected repo-local executable not found: %s\n' "$OLLAMA_BIN" >&2
  exit 2
}

export OLLAMA_HOST OLLAMA_MODELS="$MODEL_DIR"

is_our_server() {
  local pid="$1" actual
  [[ -r "/proc/$pid/exe" ]] || return 1
  actual="$(readlink -f "/proc/$pid/exe")"
  [[ "$actual" == "$(readlink -f "$OLLAMA_BIN")" ]]
}

server_pid=""
if [[ -f "$PID_FILE" ]]; then
  saved_pid="$(cat "$PID_FILE")"
  if [[ "$saved_pid" =~ ^[0-9]+$ ]] && kill -0 "$saved_pid" 2>/dev/null; then
    if is_our_server "$saved_pid"; then
      server_pid="$saved_pid"
    else
      printf 'PID file points to a process that is not this repo-local Ollama; refusing to touch it.\n' >&2
      exit 2
    fi
  else
    rm -f -- "$PID_FILE"
  fi
fi

if [[ -z "$server_pid" ]]; then
  if curl --silent --show-error --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
    printf 'Port 11435 is already in use; refusing to use or stop that service.\n' >&2
    exit 2
  fi
  printf 'Starting Neptune Ollama; runtime, model files, and log stay under .local/ollama-neptune/.\n'
  nohup "$OLLAMA_BIN" serve >>"$LOG_FILE" 2>&1 </dev/null &
  server_pid="$!"
  printf '%s\n' "$server_pid" >"$PID_FILE"
  for _ in $(seq 1 60); do
    if curl --silent --show-error --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      printf 'Repo-local Ollama exited during startup; inspect %s\n' "$LOG_FILE" >&2
      exit 2
    fi
    sleep 1
  done
  curl --fail --silent --show-error --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null || {
    printf 'Ollama did not become ready; inspect %s\n' "$LOG_FILE" >&2
    exit 2
  }
fi

printf 'Pulling %s into %s\n' "$MODEL" "$MODEL_DIR"
"$OLLAMA_BIN" pull "$MODEL"
"$OLLAMA_BIN" list
printf '\nReady. API: http://%s | Model: %s | PID: %s\n' "$OLLAMA_HOST" "$MODEL" "$server_pid"
printf 'Stop only Neptune Ollama with: bash scripts/stop_ollama_neptune.sh\n'
