#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unset MODEL || true

exec uv run --frozen python "${REPO_ROOT}/scripts/run_patcheval_batches.py" --profile saturn "$@"
