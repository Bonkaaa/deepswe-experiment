#!/usr/bin/env bash
# Source this file from a Bash terminal to select Neptune's repo-local Ollama.
# Usage: source scripts/activate_ollama_neptune.sh

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  printf 'Source this file instead of running it: source scripts/activate_ollama_neptune.sh\n' >&2
  exit 2
fi

_neptune_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NEPTUNE_REPO_ROOT="$(cd -- "$_neptune_script_dir/.." && pwd)"
NEPTUNE_OLLAMA_ROOT="$NEPTUNE_REPO_ROOT/.local/ollama-neptune"
NEPTUNE_OLLAMA_BIN="$NEPTUNE_OLLAMA_ROOT/runtime/bin/ollama"

if [[ ! -x "$NEPTUNE_OLLAMA_BIN" ]]; then
  printf 'Neptune Ollama is not installed. Run: bash scripts/setup_ollama_neptune.sh\n' >&2
  return 2
fi

export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MODELS="$NEPTUNE_OLLAMA_ROOT/models"
export PATH="$NEPTUNE_OLLAMA_ROOT/runtime/bin:$PATH"

unset _neptune_script_dir NEPTUNE_OLLAMA_ROOT NEPTUNE_OLLAMA_BIN
printf 'Neptune Ollama selected (repo-local model store, API 127.0.0.1:11435).\n'
