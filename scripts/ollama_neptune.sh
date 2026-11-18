#!/usr/bin/env bash
set -Eeuo pipefail

# Run Neptune's repo-local Ollama CLI with its own model storage and endpoint.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OLLAMA_BIN="$REPO_ROOT/.local/ollama-neptune/runtime/bin/ollama"

if [[ ! -x "$OLLAMA_BIN" ]]; then
  printf 'Neptune Ollama is not installed. Run: bash scripts/setup_ollama_neptune.sh\n' >&2
  exit 2
fi

export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MODELS="$REPO_ROOT/.local/ollama-neptune/models"
exec "$OLLAMA_BIN" "$@"
