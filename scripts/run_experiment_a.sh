#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE=""
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  echo "ERROR: --profile <saturn|neptune> must be provided." >&2
  echo "Usage: $0 --profile <saturn|neptune>" >&2
  exit 1
fi

echo "================================================================================"
echo " KHỞI ĐỘNG THỰC NGHIỆM A (DEEPSWE-EXPERIMENT)"
echo " Profile: ${PROFILE}"
echo " Root:    ${REPO_ROOT}"
echo "================================================================================"

# Step 0: Ensure local inference daemon (Ollama or vLLM) is running
echo "[Step 0/7] Ensuring local inference server is running..."
MODEL_PROVIDER=$(uv run --frozen python -c '
import sys
from deepswe_exp.components.configuration.settings import load_settings
print(load_settings(profile=sys.argv[1]).model_provider)
' "$PROFILE")
if [[ "$MODEL_PROVIDER" == "vllm" ]]; then
  "${REPO_ROOT}/scripts/start_vllm_neptune.sh"
else
  "${REPO_ROOT}/scripts/start_ollama_local.sh" --profile "$PROFILE"
fi

# Extract base study_id from settings and append timestamp
BASE_STUDY_ID=$(uv run --frozen python -c '
import sys
from deepswe_exp.components.configuration.settings import load_settings
s = load_settings(profile=sys.argv[1])
print(s.config["study_id"])
' "$PROFILE")
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
STUDY_ID="${BASE_STUDY_ID}_${TIMESTAMP}"

echo "Target Study ID: ${STUDY_ID}"

# Step 1: Doctor Check
echo "[Step 1/7] Running system doctor check..."
uv run --frozen python -m deepswe_exp doctor --profile "$PROFILE" --study-id "$STUDY_ID"

# Step 2: Check Model Connection and Tool-calling capability
echo "[Step 2/7] Checking model connectivity and tool calling..."
uv run --frozen python -m deepswe_exp check-model --profile "$PROFILE" --study-id "$STUDY_ID"

# Step 3: Prepare Environment
echo "[Step 3/7] Preparing benchmark environments and instances..."
uv run --frozen python -m deepswe_exp prepare --profile "$PROFILE" --study-id "$STUDY_ID"

# Step 4: Validate Benchmark
echo "[Step 4/7] Validating baseline pass rates..."
uv run --frozen python -m deepswe_exp validate --profile "$PROFILE" --study-id "$STUDY_ID"

# Step 5: Plan Runs
echo "[Step 5/7] Planning experiment matrix and schedule..."
uv run --frozen python -m deepswe_exp plan-runs --profile "$PROFILE" --study-id "$STUDY_ID"

# Step 6: Run All
echo "[Step 6/7] Executing all planned benchmark runs..."
if [[ ${#PASSTHROUGH_ARGS[@]} -gt 0 ]]; then
  uv run --frozen python -m deepswe_exp run-all --profile "$PROFILE" --study-id "$STUDY_ID" "${PASSTHROUGH_ARGS[@]}"
else
  uv run --frozen python -m deepswe_exp run-all --profile "$PROFILE" --study-id "$STUDY_ID"
fi

# Step 7: Summarize
echo "[Step 7/7] Generating study summaries and CSV metrics..."
uv run --frozen python -m deepswe_exp summarize --profile "$PROFILE" --study-id "$STUDY_ID"

# Packaging Step
RUNS_DIR="${REPO_ROOT}/runs"
ARCHIVE_NAME="${STUDY_ID}.tar.gz"
ARCHIVE_PATH="${RUNS_DIR}/${ARCHIVE_NAME}"

echo ""
echo "Packaging study results into ${ARCHIVE_PATH}..."
mkdir -p "${RUNS_DIR}"
tar -czf "${ARCHIVE_PATH}" -C "${RUNS_DIR}" "${STUDY_ID}"

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
SERVER_IP="${SERVER_IP:-<server_ip>}"
CURRENT_USER=$(whoami 2>/dev/null || echo "<user>")

echo ""
echo "================================================================================"
echo " THỰC NGHIỆM A HOÀN TẤT THÀNH CÔNG!"
echo " Profile:   ${PROFILE}"
echo " Study ID:  ${STUDY_ID}"
echo " Báo cáo:   runs/${STUDY_ID}/summary/summary.md"
echo " Tarball:   runs/${ARCHIVE_NAME}"
echo "================================================================================"
echo " Để kéo trọn vẹn artifacts (trajectories, diffs, summary) về máy Windows:"
echo " Mở PowerShell trên Windows và chạy câu lệnh sau:"
echo ""
echo "   scp ${CURRENT_USER}@${SERVER_IP}:${ARCHIVE_PATH} ./runs/"
echo "   tar -xzf ./runs/${ARCHIVE_NAME} -C ./runs/"
echo ""
echo "================================================================================"
