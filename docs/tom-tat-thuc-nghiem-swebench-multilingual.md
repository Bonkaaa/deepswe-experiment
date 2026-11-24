# Tóm tắt quá trình thực nghiệm sắp tới — SWE-bench Multilingual

Tài liệu này tóm tắt kiến trúc, cấu hình và quy trình chạy cho nhánh thực nghiệm **`swe-bench-multilingual`** (chuyển từ DeepSWE sang bài toán **bug fixing / program repair** thực tế trên `SWE-bench Multilingual`, bắt đầu bằng smoke test ngôn ngữ Go).

---

## 1. Mục tiêu & Bài toán thực nghiệm

- **Câu hỏi nghiên cứu:** Khi cắt bớt tỷ lệ unit test có sẵn của repo (`100%`, `50-random`, `0%`) trong phạm vi module liên quan, khả năng sửa bug (F2P) và tránh phá tính năng cũ (P2P) của LLM coding agent thay đổi như thế nào?
- **Benchmark:** `SWE-bench Multilingual` (ghim cố định `dataset_revision: 846e647b9f33c0b51b739d005d13d85493c9af09` và `harness_revision: 02e7a74ffd0b707aab73d203fe87bdc7c76afc8e` trong `pyproject.toml` và `instance.json`).
- **Task smoke test đầu tiên:** `gin-gonic__gin-1805` (`data/gin-gonic__gin-1805/instance.json`)
  - **Repo / Ngôn ngữ:** `gin-gonic/gin` (Go), commit gốc `70a0aba3e423246be37462cfdaedd510c26c566e`.
  - **Mô tả bug:** `gin.Default()` + `router.StaticFS(...)` in ra 2 dòng log cho mỗi request 404 (middleware bị chạy lặp do gán `c.handlers = group.engine.allNoRoute` thay vì `group.engine.noRoute` trong `routergroup.go`).
  - **Bộ chấm điểm chuẩn:** **1 F2P** (`TestMiddlewareCalledOnceByRouterStaticFSNotFound`) + **13 P2P** (các test khớp `^Test.*Router`).
  - **Phạm vi thí nghiệm:**
    - `test_pattern`: `^Test.*Router`
    - `product_paths`: `["routergroup.go"]`
    - `branches`: `["100", "50-random", "0"]`, `sample_seed: 42`
    - `role`: `smoke_test`
- **Cấu hình thực thi:**
  - Neptune (`config/study.neptune.yaml`): `study_id = pilot-multilingual-gin-1805-v10-neptune-qwen38`, ngân sách tối đa `200` requests, `7200s` wall time, `max_output_tokens = 24576`.
  - Các cấu hình DeepSWE cũ được bảo lưu nguyên vẹn tại `config/study*.deepswe.yaml`.

---

## 2. Quy trình chạy thực nghiệm (7 bước tự động)

Khi chạy `scripts/run_experiment_a_neptune.sh` (hoặc `scripts/run_experiment_a.sh`), hệ thống thực hiện tuần tự 7 bước:

1. **`doctor` (Kiểm tra môi trường):** Xác minh Docker Linux engine, profile, biến môi trường và cấu hình study.
2. **`check-model` (Kiểm tra kết nối model):** Gọi probe tới server vLLM (`Qwen/Qwen3.8-27B-FP8` trên GPU 0) để đảm bảo endpoint sẵn sàng.
3. **`prepare` (Chuẩn bị image & cắt unit test Go theo 3 nhánh):**
   - Kéo Docker image gốc `swebench/sweb.eval.x86_64.gin-gonic_1776_gin-1805@sha256:...` và build lớp image cục bộ có sẵn `GOMODCACHE=/opt/swebench-go-mod-cache` (`go mod download`) để chạy hoàn toàn **offline** (`network_mode: none`).
   - Liệt kê danh sách test Go khớp `^Test.*Router` bằng `go test -list`.
   - Chia danh sách test theo `seed = 42` cho 3 nhánh `100`, `50-random`, `0`.
   - Chạy bộ lọc Go AST (`src/deepswe_exp/components/tasks/filter_go_tests.go`) để xóa đúng các hàm `Test*` không được chọn và **tự động loại bỏ import thừa** (`pruneUnusedImports` kiểm tra biên dịch bằng `go test -c`).
   - Dọn sạch lịch sử git (`clean`), audit lại bằng `audit_go`, băm `source_hash` / `snapshot_hash` / `manifest_hash` và lưu `prepared.json`.
4. **`validate` (Xác thực tính ổn định của benchmark — 3 lần lặp):**
   - Chạy official SWE-bench harness (`swebench.harness.run_evaluation.run_instance`) **3 lần** trên `base` (patch rỗng) và **3 lần** trên `reference` (patch chuẩn của benchmark).
   - Yêu cầu bắt buộc: `reference` pass 3/3 (`reward = 1`), `base` fail F2P nhưng pass 100% P2P ở cả 3 lần và kết quả ổn định tuyệt đối giữa các lần lặp.
   - Có cơ chế tự retry tối đa 3 lần nếu gặp lỗi tạm thời (`timeout`, `infrastructure_error`).
5. **`plan-runs` (Lập lịch chạy):** Sinh `schedule.json` cho các tổ hợp `(task, branch, seed)`.
6. **`run-all` (Chạy agent & chấm điểm độc lập):**
   - Với mỗi lượt chạy: xác minh toàn vẹn `prepared.json` và `variant/manifest.json` (`verify_benchmark_record`, `verify_prepared_variant`).
   - Khởi tạo sandbox cô lập tại `/testbed` (user không đặc quyền `10001`, không mạng, `cap_drop: ALL`, giới hạn CPU/RAM).
   - Chạy `mini-swe-agent` với đề bài `problem_statement` và `prompt/system_prompt.md`.
   - Trích xuất 2 diff: `full_worktree.diff` (mọi thay đổi) và `agent_patch.diff` (chỉ giới hạn trong `product_paths`, tức `routergroup.go`).
   - **Chấm điểm 2 tầng:**
     - **Visible tests (`_evaluate_go_tests`):** Áp `agent_patch.diff` lên snapshot sạch của nhánh và chạy `go test -json -run` đúng các test mà nhánh đó được thấy.
     - **Official SWE-bench verifier (`run_swebench_patch`):** Chạy harness chính thức trong container bị giới hạn tương đương sandbox, phân loại rõ `pass`, `fail_test` (bao gồm cả lỗi biên dịch Go, startup panic, hoặc lỗi setup do patch gây ra), `apply_failure`, `timeout`, và `infrastructure_error`.
7. **`summarize` & Đóng gói:**
   - Tổng hợp `summary/runs.csv` và `summary/summary.md` (gồm cả `agent_seen_*`, `seen_tests_*`, `f2p`, `p2p`, `reward`, token usage, thời gian).
   - Nén toàn bộ kết quả thành `runs/<study_id>.tar.gz`.

---

## 3. Cơ chế đảm bảo tính khoa học & tái lập

- **Chống rò rỉ & chống gian lận test:** Mọi sửa đổi của agent vào file `*_test.go` bị loại khỏi `agent_patch.diff` trước khi chấm cả `visible-tests` lẫn official verifier.
- **Chống lệch phiên bản:** Mọi bản chuẩn bị đều ghim `benchmark_instance_hash`, `harness_revision` (đối chiếu trực tiếp với `direct_url.json` của gói `swebench` đã cài), `grader_container_policy` và `image_ref@sha256`.
- **An toàn container chấm điểm:** `_RestrictedDockerClient` ép `network_disabled=True`, `cap_drop=['ALL']`, `no-new-privileges`, `nano_cpus`, `mem_limit` lên container do official SWE-bench harness tạo ra.

---

## 4. Checklist trước khi chạy trên server Neptune

1. **Quyền user trong `preparation.py` (đã sửa):**
   - Trong `src/deepswe_exp/components/execution/preparation.py` (`_prepare_swebench`), `box.user = 'root'` đã được chuyển lên trước `find ... -exec rm -rf` và `filter_go_tests(...)` để tránh lỗi `permission denied` khi lọc file trong Docker Linux.
2. **Đồng bộ môi trường trên Neptune:**
   - Chạy `uv sync --frozen` để cài gói `swebench` đúng commit ghim trong `uv.lock` (cần có `git` và internet trong bước cài đặt).
3. **Đảm bảo Docker truy cập được Docker Hub:**
   - Bước `prepare` cần kéo `swebench/sweb.eval.x86_64.gin-gonic_1776_gin-1805@sha256:...` và chạy `go mod download` lần đầu.
4. **Cân nhắc phạm vi `product_paths` khi mở rộng nhiều task:**
   - Hiện `gin-gonic__gin-1805` khóa chặt `product_paths: ["routergroup.go"]`. Nếu muốn chấp nhận các bản vá hợp lệ sửa sang file Go sản phẩm khác (ví dụ `gin.go`), cần mở rộng `product_paths` tương ứng trước khi chạy chính thức.
