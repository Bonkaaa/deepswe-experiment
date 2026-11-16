# Nhật ký chạy và xử lý lỗi - Thực nghiệm A

Tài liệu này ghi lại quá trình thực hiện thực nghiệm A theo pipeline tài liệu `docs/pipeline-thuc-nghiem-A.md`, bao gồm các bước thực hiện, các lỗi phát sinh (nếu có), nguyên nhân, hướng sửa chữa và kết quả xử lý.

---

## 1. Môi trường và Kiểm tra ban đầu
- **Thời gian bắt đầu:** 2026-11-16 12:00:00 (Local Time)
- **Hệ điều hành:** Windows, Docker Desktop WSL2 (Linux containers)
- **Lệnh `doctor`:** Đã chạy thành công (`exit code 0`), các thành phần Python 3.12, MODEL, Gemini API key, Quota, System prompt, Task adaptix-name-mapping-aliases, Docker Linux engine đều ở trạng thái `OK`.
- **Lệnh `check-model`:** Kết nối LangChain tới Gemini model `gemini-3.5-flash-lite` thành công (1.0s).

---

## 2. Nhật ký chi tiết các bước và lỗi phát sinh

### Lỗi 1: `RuntimeError: Filtered collection does not match manifest` tại bước `prepare`
- **Thời điểm phát sinh:** 2026-11-16 12:01:17
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp prepare`
- **Hiện tượng:** Quá trình chuẩn bị task `adaptix-name-mapping-aliases` gặp lỗi `RuntimeError: Filtered collection does not match manifest` tại `src/deepswe_exp/components/execution/preparation.py:103` khi kiểm tra đối chiếu danh sách test thực tế trong snapshot với manifest dự kiến.
- **Phân tích nguyên nhân gốc rễ:**
  1. **Địa chỉ bộ nhớ động trong node ID pytest:** Một số test case (74 test cases trong Adaptix, ví dụ `test_bytes_like_provider`, `test_invalid_input_timestamp`, `test_input_extra_dict`) nhận tham số là hàm Python (ví dụ `<function my_saturator at 0x...>`) hoặc object chưa định nghĩa `__repr__` riêng (ví dụ `<DatetimeTimestampProvider object at 0x...>`). Do ASLR (Address Space Layout Randomization), mỗi container/tiến trình Python sinh ra một địa chỉ ô nhớ RAM `0x...` khác nhau. Khi container thứ 2 thu thập lại danh sách test để đối chiếu với container ban đầu, chuỗi node ID bị lệch địa chỉ RAM, khiến tập hợp test ID không khớp.
  2. **Rò rỉ hook `pytest_make_parametrize_id` từ `model/conftest.py`:** File `tests/unit/morphing/model/conftest.py` định nghĩa `def pytest_make_parametrize_id(config, val, argname): return str(val)`. Do cơ chế plugin của pytest/pluggy, một khi thư mục `model/` được load, hook này sẽ tác động lên toàn bộ các test của thư mục khác (như `test_literal_provider.py` biến `[]` thành chuỗi `"[]"` thay vì `recipe0`, và `test_sqlalchemy.py` biến `List` thành `typing.List`).
- **Hướng giải quyết:**
  1. Nâng cấp plugin tin cậy `src/deepswe_exp/components/tasks/collection_plugin.py` với hookwrapper `pytest_make_parametrize_id` có cờ `tryfirst=True`:
     - Giới hạn phạm vi tác động của `model/conftest.py` chỉ cho các test thuộc thư mục `tests/unit/morphing/model/`.
     - Giữ nguyên định danh chuẩn cho các kiểu typing (`typing.List` -> `List`).
     - Tự động chuẩn hóa và loại bỏ hoàn toàn các chuỗi địa chỉ bộ nhớ RAM ` at 0x[0-9a-fA-F]+`.
  2. Cập nhật `src/deepswe_exp/components/evaluation/verifier.py` trong hàm `evaluate_supplied_tests` để nạp plugin tin cậy `collection_plugin.py`, đảm bảo cả hai pha thu thập và chấm test sử dụng chung quy tắc định danh test đồng nhất tuyệt đối.
- **Kết quả xử lý:**
  - Chạy thử nghiệm độc lập trên toàn bộ 1.356 test cases của phạm vi `morphing` cho kết quả khớp 100% (`Returncode: 0`, 0 test lỗi).
  - Kiểm tra đối chiếu trên cả 3 nhánh (`100`, `50-random`, `0`) đều cho `actual == expected -> True`.
  - Bộ unit test `tests/test_component_smoke.py` tiếp tục pass toàn bộ (13/13 tests).

---

### Lỗi 2: `DockerFailure: Command failed (exit 255): exec /usr/bin/bash: argument list too long` tại bước `prepare` (audit)
- **Thời điểm phát sinh:** 2026-11-16 12:25:47
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp prepare`
- **Hiện tượng:** Khi chạy kiểm tra bảo toàn (audit) snapshot trên nhánh `0` (nhánh bỏ toàn bộ 182 nhóm test), container trả về lỗi exit 255: `argument list too long`.
- **Phân tích nguyên nhân gốc rễ:**
  - Hàm `audit(box, manifest)` trong `src/deepswe_exp/components/execution/preparation.py` serialize toàn bộ object `TestGroup` bao gồm toàn bộ danh sách `node_ids`, `fixtures` và docstring dài `description`. Chuỗi JSON sinh ra dài tới 229.190 ký tự (>229 KB).
  - Linux kernel có giới hạn kích thước cho một argument đơn lẻ `MAX_ARG_STRLEN = 131.072 bytes` (128 KB). Việc truyền chuỗi JSON 229 KB qua `python -c '<script>'` khiến lệnh `execve` của bash bị tràn giới hạn tham số dòng lệnh và văng mã lỗi exit 255.
- **Hướng giải quyết:**
  - Trong `audit`, chỉ serialize 4 trường dữ liệu thực sự cần thiết để kiểm tra AST: `[file, class, function, id]`.
  - Giảm kích thước payload từ 229 KB xuống chỉ còn ~29 KB (giảm gần 8 lần), nằm hoàn toàn trong giới hạn an toàn của Linux kernel.
- **Kết quả xử lý:**
  - Lệnh kiểm tra audit chạy thành công trơn tru, không còn lỗi `argument list too long`.
  - Lệnh `prepare` hoàn tất thành công (`Prepared adaptix-name-mapping-aliases: 3 snapshots`).

---

### Lỗi 3: Ký tự ngắt dòng Windows CRLF (`\r\n`) trong `test.sh` khi build `verifier_image`
- **Thời điểm phát sinh:** 2026-11-16 12:27:32
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp validate`
- **Hiện tượng:** Quá trình validate base/reference patch văng lỗi exit code 2, `Missing reward.json`. Trong file `test-stdout.txt` ghi nhận lỗi cú pháp do `\r` (`pipefail\r`, `EXIT\r`, `do\r`).
- **Phân tích nguyên nhân gốc rễ:**
  - File nguồn `data/<task>/tests/test.sh` trên Windows có ký tự ngắt dòng CRLF (`\r\n`). Khi đóng gói vào image Linux, bash không thực thi được script chứa `\r`.
- **Hướng giải quyết:**
  - Cập nhật hàm `verifier_image()` trong `src/deepswe_exp/components/execution/docker_env.py` để chuẩn hóa tất cả `\r\n` thành `\n` cho các file script/văn bản và gán quyền thực thi `0o755` cho file `.sh`.
  - Rebuild lại verifier image với chuẩn POSIX LF.
- **Kết quả xử lý:**
  - Lệnh `prepare` đã tạo lại image verifier mới chuẩn LF thành công.

---

### Lỗi 4: `error: corrupt patch at line 16` khi áp dụng `solution.patch` tại bước `validate`
- **Thời điểm phát sinh:** 2026-11-16 12:30:40
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp validate`
- **Hiện tượng:** Base patch pass hoàn hảo (2.738 p2p pass, 44 f2p fail), nhưng cả 3 lần chạy reference patch đều báo `apply_failure` (`apply_failed: 1`) do `error: corrupt patch at line 16`.
- **Phân tích nguyên nhân gốc rễ:**
  - File `data/adaptix-name-mapping-aliases/solution/solution.patch` trên Windows có line endings CRLF (`\r\n`). Tại dòng 16 là dòng trống trong diff hunk, chứa `\r\n`. Khi nạp nguyên xi vào Linux container, `git apply` nhận diện ký tự `\r` là patch bị corrupt.
- **Hướng giải quyết:**
  - Cập nhật `validation.py` và `verifier.py` (cả hàm `evaluate_patch` lẫn `evaluate_supplied_tests`) để luôn chuẩn hóa `patch_data = Path(patch).read_bytes().replace(b'\r\n', b'\n')` trước khi nạp vào container.
- **Kết quả xử lý:**
  - Lệnh `validate` chạy lại thành công mỹ mãn: `Validation adaptix-name-mapping-aliases: valid`.

---

### Lỗi 5: Đường dẫn tương đối gây lỗi `fatal: not a git repository` trong `collect_diff` sau khi agent hoàn tất
- **Thời điểm phát sinh:** 2026-11-16 12:36:14
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp run-next` (nhánh `50-random`, attempt-001)
- **Hiện tượng:** Agent hoàn thành việc sinh patch và xuất `final/`, nhưng đến bước thu thập diff bằng Git ở host (`collect_diff`), lệnh git trả về exit 128: `fatal: not a git repository`.
- **Phân tích nguyên nhân gốc rễ:**
  - Trong `src/deepswe_exp/components/evaluation/patches.py`, biến `output_dir` được truyền vào là đường dẫn tương đối (`runs/...`).
  - Lệnh `git` được thực thi với `cwd=repo` (`worktree/`). Do `gitdir` là đường dẫn tương đối, Git đã tìm kiếm đường dẫn `gitdir` bên trong `worktree/` thay vì ở thư mục gốc, dẫn đến không tìm thấy kho Git tạm và văng lỗi exit code 128.
- **Hướng giải quyết:**
  - Trong `collect_diff`, gọi `.resolve()` cho `output_dir`, `tmp`, `baseline` và `final` để chuyển toàn bộ sang đường dẫn tuyệt đối trước khi truyền cho các cờ `--git-dir` và `--work-tree`.
- **Kết quả xử lý:**
  - Thử nghiệm độc lập hàm `collect_diff` với đường dẫn tuyệt đối đã giải quyết được lỗi tìm repository.

---

### Lỗi 6: `Filename too long` trên Windows Git khi chạy `git add` trong `collect_diff`
- **Thời điểm phát sinh:** 2026-11-16 12:40:22
- **Lệnh thực thi:** `uv --cache-dir .uv-cache run deepswe-exp run-next` (nhánh `50-random`, attempt-0cb0315d0b)
- **Hiện tượng:** Sau khi khắc phục lỗi 5, khi chạy `git add -f .`, Git trên Windows báo lỗi `fatal: unable to stat '...': Filename too long` và exit code 128 do đường dẫn cây file lồng nhau trong thư mục `runs/pilot-a/...` vượt quá giới hạn 260 ký tự của Windows API (MAX_PATH).
- **Phân tích nguyên nhân gốc rễ:**
  - Cấu hình mặc định của Git for Windows kích hoạt giới hạn 260 ký tự trừ khi `core.longpaths = true`. Khi đường dẫn tạm dài lồng nhiều cấp thư mục (`runs/pilot-a/adaptix-name-mapping-aliases/...`), một số file mã nguồn vượt quá 260 ký tự.
- **Hướng giải quyết:**
  - Trong `src/deepswe_exp/components/evaluation/patches.py`, thêm cờ cấu hình `-c core.longpaths=true` vào tất cả các lệnh Git (`git -c core.longpaths=true init`, `git -c core.longpaths=true add`, `git -c core.longpaths=true diff`,...).
- **Kết quả xử lý:**
  - Thử nghiệm chạy diff trên toàn bộ cây thư mục hoàn tất trơn tru, không còn lỗi độ dài file.

---

## 3. Quá trình thực thi hoàn thiện các lượt chạy (Attempt `attempt-c840b95754`)

Sau khi khắc phục các lỗi kỹ thuật trên môi trường Windows và Docker, toàn bộ 3 lượt chạy của nhóm thực nghiệm đã được lập lịch và thực thi tự động:

1. **Lượt 1: Nhánh `50-random` (Seed 42)**
   - `run_id`: `5e4963a685034d778f82c284f26fbe03`
   - Tiến trình: Agent thực thi 25 model requests (đạt ngân sách `request_budget`), xuất worktree.
   - Thu thập diff: Hoàn tất trơn tru (`agent_patch.diff` và `full_worktree.diff`).
   - Đánh giá test được cấp (Visible tests): 678/678 tests pass.
   - Đánh giá benchmark verifier độc lập: Hoàn tất (`status: fail_test`, `reward: 0`, P2P: 2738/2738 pass, F2P: 0/44 pass).
   - Trạng thái cuối: `scored`.

2. **Lượt 2: Nhánh `100` (Seed 42)**
   - `run_id`: `98f7af956d75494c88cf5e7d63b8d6c7`
   - Tiến trình: Agent thực thi 25 model requests (`request_budget`).
   - Thu thập diff: Hoàn tất trơn tru.
   - Đánh giá test được cấp (Visible tests): 1356/1356 tests pass.
   - Đánh giá benchmark verifier độc lập: Hoàn tất (`status: fail_test`, `reward: 0`, P2P: 2738/2738 pass, F2P: 0/44 pass).
   - Trạng thái cuối: `scored`.

3. **Lượt 3: Nhánh `0` (Seed 42)**
   - `run_id`: `9fd5ad69196a431fb8db66581e86cf3b`
   - Tiến trình: Agent thực thi 25 model requests (`request_budget`).
   - Thu thập diff: Hoàn tất trơn tru.
   - Đánh giá test được cấp: Không có test visible (`not_applicable 0/0`).
   - Đánh giá benchmark verifier độc lập: Hoàn tất (`status: fail_test`, `reward: 0`, P2P: 2738/2738 pass, F2P: 0/44 pass).
   - Trạng thái cuối: `scored`.

---

## 4. Tổng kết thực nghiệm A

- Lệnh `summarize` đã được thực thi thành công:
  - Báo cáo tổng hợp: `runs/pilot-a/summary/summary.md`
  - Bảng số liệu chi tiết: `runs/pilot-a/summary/runs.csv`
- Toàn bộ pipeline từ `doctor` -> `check-model` -> `prepare` -> `validate` -> `plan-runs` -> `run-next` -> `summarize` đã được kiểm chứng hoạt động hoàn hảo và khép kín 100% trên nền tảng Windows + Docker.
