# Kế hoạch chạy thực nghiệm PatchEval-Verified (230 CVEs) trên Server Saturn

## 1. Thông số thực nghiệm & Hạ tầng

| Hạng mục | Cấu hình |
|---|---|
| **Benchmark** | PatchEval-Verified — **230 CVEs** (`83 Go`, `77 JavaScript`, `70 Python`) từ `data/patcheval/all_tasks.json` |
| **Thiết kế so sánh** | 3 nhánh tỷ lệ unit test hiển thị: `100`, `50-random`, `0` (`sample_seed: 42`) → **690 runs** |
| **Server** | **Saturn** — 2× RTX 6000 (46 GB VRAM/card = **92 GB VRAM**) |
| **Dung lượng đĩa (đã đo)** | `/mnt/hdd2` (repo + `runs/`): trống **4.6 TB** (68%); `/` (`/var/lib/docker`): trống **2.6 TB** (81%, server dùng chung) |
| **Model** | `Qwen/Qwen3.5-35B-A3B-FP8` qua vLLM (`TP=2`, `num_ctx: 262144`, `max_num_seqs: 16`, `--trust-remote-code`) |
| **Budget mỗi run** | `wall_time_seconds: 1800` (30 phút cứng — chạm hạn dừng sandbox và vẫn eval), `max_model_requests: 150`, `max_output_tokens: 16384`, `tool_timeout_seconds: 120`, per-request timeout `<= 300s` |
| **Validation** | `validation_repeats: 1` (kiểm tra `base` patch rỗng phải `fail_test` với `reward == 0`) |

---

## 2. Cơ sở ước tính từ Pilot 4 CVEs

Kết quả thực tế từ `runs/saturn_results/pilot-patcheval-4cve-saturn-qwen35-35b-a3b_20261128_123945`:
- **Tổng lượt chạy:** 12/12 runs hoàn thành (`scored`), **0 lỗi hạ tầng**, 4/4 nhóm ghép cặp hợp lệ.
- **Kết quả:** 2/12 pass (`CVE-2021-23376` ở nhánh `50-random` và `0`), 10/12 `fail_test`.
- **Thời gian agent (12 runs):** tổng `5,410s` (**90.2 phút**), trung bình **7.5 phút/run** (nhanh nhất `1.1p`, chậm nhất `20.5p`).
- **Tỷ lệ chạm timeout 30p:** **0/12 runs** (5/12 runs dừng tại `request_budget = 150` trước khi chạm 30 phút).

### Dự đoán thời gian cho 230 CVEs (690 runs)
Tổng worker-time ước tính: `690 × 7.5 phút` ≈ **86.4 giờ** (+ ~4–6 giờ overhead pull/prepare/validate/eval):

| Số worker song song | Tổng thời gian dự kiến (230 CVEs) | Thời gian mỗi batch (23 CVEs = 69 runs) |
|---|---|---|
| **`workers = 4`** *(đã kiểm chứng ở pilot)* | **~24 – 30 giờ** (~1 – 1.2 ngày) | **~2.5 – 3 giờ / batch** |
| **`workers = 6`** *(đề xuất tăng tốc)* | **~17 – 22 giờ** (< 1 ngày) | **~1.8 – 2.2 giờ / batch** |

---

## 3. Chiến lược chạy theo Batch (10 batch × 23 CVEs)

Không chạy 1 cục 230 CVEs liên tiếp vì:
1. Nếu 1 CVE lỗi mạng/pull/validate thì pipeline mặc định dừng trước khi vào `run-all`.
2. Giữ nguyên 230 Docker images (~500–800 GB) sẽ đẩy phân vùng `/` (đang 81%) lên ~87–90% trên server dùng chung.
3. Chạy theo batch cho kết quả `runs.csv` + `summary.md` định kỳ mỗi ~2–3 giờ để giám sát sớm.

### Quy trình cho từng batch (`batch_01` → `batch_10`, mỗi batch 23 CVEs)

```
Batch K (23 CVEs)
  ├─ 1. Kiểm tra vLLM health (http://127.0.0.1:11436/v1/models)
  ├─ 2. Prepare từng CVE (song song W workers)
  │     └─ CVE lỗi prepare → ghi skipped_cves.jsonl, KHÔNG dừng batch
  ├─ 3. Validate từng CVE đã prepare (validation_repeats=1, song song W workers)
  │     └─ CVE fail validate → ghi skipped_cves.jsonl, KHÔNG dừng batch
  ├─ 4. plan-runs cho các CVE hợp lệ trong batch
  ├─ 5. run-all --workers W --continue-on-error
  ├─ 6. summarize → xuất runs/<master>/batch_XX/summary/{runs.csv, summary.md}
  └─ 7. Dọn đĩa an toàn sau batch:
        ├─ Xóa thư mục nặng tạm: base/, final/, prepared/*/snapshot/
        │  (GIỮ NGUYÊN: trajectory.json, agent_patch.diff, full_worktree.diff,
        │   agent_result.json, evaluation/, run.json, commands.jsonl, schedule.json, summary/)
        └─ Xóa đúng 23 Docker images của batch:
           docker rmi -f ghcr.io/patcheval-cve/patcheval-cve:<cve>
           (TUYỆT ĐỐI KHÔNG chạy docker system prune -a trên server dùng chung)
```

### Bước tổng hợp cuối (sau khi xong cả 10 batch)
- Gộp 10 file `batch_XX/summary/runs.csv` thành `runs/<master>/summary/all_runs.csv`.
- Sinh báo cáo tổng hợp `runs/<master>/summary/all_summary.md` phân tích theo:
  - Nhánh (`100` vs `50-random` vs `0`) và các cặp chuyển đổi pass/fail.
  - Ngôn ngữ (`Go`, `JavaScript`, `Python`).
  - Nhóm lỗ hổng (`CWE`).
  - Danh sách CVE bị bỏ qua ở `prepare`/`validate` (`skipped_cves.jsonl`).
- Đóng gói 1 file `.tar.gz` nhẹ (chỉ gồm logs, trajectories, diffs, evaluations, summaries — không chứa snapshot repo) để `scp` về máy Windows.

---

## 4. Cơ chế Resume & Vận hành trên Saturn

- **Chạy trong `tmux`:**
  ```bash
  tmux new -s patcheval230
  unset MODEL
  bash scripts/run_patcheval_batches.sh --profile saturn --workers 4
  ```
- **Resume an toàn nếu ngắt giữa chừng:**
  - Mỗi batch xong sẽ ghi cờ `.batch_done`. Khi chạy lại cùng `--master-id`, script tự bỏ qua các batch đã xong và chạy tiếp từ batch dang dở.
  - Hỗ trợ chạy chọn lọc theo khoảng `--start-batch 1 --end-batch 10` nếu muốn chia ca.
- **Theo dõi tiến độ từ ngoài `tmux`:**
  - Xem log tổng: `tail -f runs/<master>/batch_runner.log`
  - Xem dung lượng đĩa: `df -h . /`
  - Xem sức khỏe vLLM: `curl -s http://127.0.0.1:11436/v1/models`

---

## 5. Các điểm cần bạn chốt trước khi tôi viết script batch & đẩy lên Saturn

1. **Số worker song song (`W`):** chọn **`4`** (an toàn, đã test ở pilot, ~24–30h) hay **`6`** (nhanh hơn, ~17–22h)?
2. **Giới hạn bước (`max_model_requests`):** giữ **`150`** như pilot (nhanh, 0/12 chạm timeout 30p) hay tăng lên **`200`**?
3. **Kích thước batch:** **`23 CVEs/batch`** (10 batch) đã hợp lý chưa?
4. **4 CVE đã chạy pilot:** chạy lại chung trong 230 CVEs cho đồng nhất 1 study ID duy nhất? *(Khuyến nghị: **Có**, vì chỉ thêm ~25 phút nhưng toàn bộ 230 CVEs cùng chung 1 `master_id` và protocol.)*
