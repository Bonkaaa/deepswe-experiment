# Pipeline chạy Thực nghiệm A

Tài liệu này giải thích cách các file trong repo nối với nhau, dữ liệu đi qua đâu và cách tự chạy pipeline trên Windows PowerShell. Đợt smoke do người phát triển chạy chỉ kiểm tra component bằng dữ liệu/fake backend và một request kết nối Gemini; **không chạy agent sửa Adaptix hoặc verifier benchmark thật**.

## Dữ liệu đi qua pipeline như thế nào?

```text
config/study.yaml + .env + prompt/system_prompt.md + data/<task>/
                         │
                         ▼
       __main__.py → settings.py → task metadata
                         │
          prepare: Docker checkout base + pytest collection
                         ▼
       inventory.json → manifest.json → filtered snapshots
                         │
 plan-runs → schedule.json → run-next → LangChain/Gemini
                                           │
                                           ▼
                                   agent container
                                           │
                         final tree → two patch files
                                           │
              ┌────────────────────────────┴─────────────────────────┐
              ▼                                                      ▼
  test snapshot agent đã thấy                     benchmark verifier riêng
  (snapshot gốc đã lọc, không lấy test             (base commit + test patch
   từ cây agent có thể đã sửa)                      + model patch sản phẩm)
              └────────────────────────────┬─────────────────────────┘
                                           ▼
                                CSV và báo cáo Markdown
```

Runner Python giữ Gemini API key ở máy host. Container agent không có mạng, `.env`, Docker socket, thư mục verifier hoặc patch của lượt khác. Test bị bỏ được xóa khỏi hàm collection bằng AST và collection được kiểm lại. Verifier nhận patch sản phẩm lọc theo allowlist `src/adaptix/`; test agent viết/chỉnh sửa chỉ nằm trong diff tổng.

## File nào gọi file nào?

| File/thư mục | Vai trò |
|---|---|
| `src/deepswe_exp/__main__.py` | CLI, kiểm tra lệnh, chọn task, giữ study lock và gọi component tương ứng. |
| `components/configuration/settings.py` | Đọc YAML/`.env`, kiểm tra schema, giải đường dẫn tương đối theo root repo và che API key. |
| `components/tasks/tasks.py` | Đọc `task.toml` và `instruction.md` của task. |
| `components/tasks/collection_plugin.py` | Plugin pytest tin cậy để ghi node ID, fixture và mô tả test từ base commit. Plugin được chép tạm vào container chuẩn bị, không giao cho agent. |
| `components/tasks/variants.py` | Gom parameterized test, chọn nhóm theo seed/tỷ lệ và xóa test không được cấp khỏi snapshot bằng AST. |
| `components/execution/docker_env.py` | Quản lý Docker container, giới hạn tài nguyên, truyền file bằng archive API, timeout và image. |
| `components/execution/preparation.py` | Tạo inventory, manifest, snapshot từng nhánh và audit collection sau lọc. |
| `components/execution/validation.py` | Chạy base/reference patch ba lần trong verifier để kiểm tra evaluator. |
| `components/execution/runner.py` | Lập lịch, chạy một lượt agent, thu patch, test được cấp, gọi evaluator và tiếp tục khi có lượt lưu sẵn. |
| `components/agent/agent.py` | Dựng `ChatGoogleGenerativeAI`, gắn tool `run_shell`, giữ hội thoại và áp budget. |
| `components/agent/connection.py` | Gửi đúng một request riêng để xác nhận kết nối LangChain/Gemini. |
| `components/agent/quota.py` | Ledger bền vững theo project/model; giới hạn RPM, TPM và RPD với dự phòng. |
| `components/evaluation/patches.py` | So cây file cuối với snapshot gốc, tạo `full_worktree.diff` và `agent_patch.diff`. |
| `components/evaluation/verifier.py` | Chạy test được cấp từ snapshot tin cậy và benchmark verifier trong các container tách biệt. |
| `components/results/storage.py` | JSON/JSONL atomic, protocol hash, đường dẫn run và study lock. |
| `components/results/report.py` | Xuất CSV từng lượt và Markdown gồm F2P/P2P, reward, test được cấp và nhóm ghép cặp. |
| `prompt/system_prompt.md` | System prompt chung cho agent; instruction cụ thể vẫn lấy từ `data/<task>/instruction.md`. |

## Chuẩn bị cấu hình

1. Cài Docker Desktop và chuyển sang **Linux containers**.
2. Cài `uv`, sau đó tại thư mục repo chạy:

   ```powershell
   uv sync --python 3.12 --cache-dir .uv-cache
   ```

   Lệnh cài đúng các phiên bản trong `uv.lock` vào `.venv`.
3. `.env` cần có `MODEL` và `GEMINI_API_KEY`. Không chép key vào YAML, command line, prompt hoặc issue.
4. Điền `quota_project` cùng `quota.rpm`, `quota.tpm`, `quota.rpd` trong `config/study.yaml` theo đúng project/model trong AI Studio. Để trống/null thì `doctor` và `run-next` không cho chạy.
5. Đặt task, nhánh, seed và budget trước `plan-runs`. Sau khi đóng protocol, đổi cấu hình/model/prompt thì dùng `study_id` mới.

## Lệnh chạy

Có thể gọi từ thư mục repo bằng `uv --cache-dir .uv-cache run deepswe-exp`. Nếu lệnh đó chưa có trên PATH, dùng `uv --cache-dir .uv-cache run python -m deepswe_exp`.

```powershell
uv --cache-dir .uv-cache run deepswe-exp doctor
uv --cache-dir .uv-cache run deepswe-exp check-model
uv --cache-dir .uv-cache run deepswe-exp prepare
uv --cache-dir .uv-cache run deepswe-exp validate
uv --cache-dir .uv-cache run deepswe-exp plan-runs
uv --cache-dir .uv-cache run deepswe-exp run-next
uv --cache-dir .uv-cache run deepswe-exp summarize
```

`doctor` không gọi Gemini. Lệnh kiểm tra riêng Python 3.12, model/key, quota, prompt, task và Docker Linux engine; thiếu điều kiện sẽ trả mã lỗi khác 0.

`check-model` gửi **một request duy nhất** qua đúng client LangChain được dùng trong agent, lưu kết quả vào `runs/<study_id>/connectivity/`. Đây là phép thử kết nối riêng, không tạo run trong schedule và không tính vào số liệu nghiên cứu. Không gọi lại liên tiếp khi có lỗi API/quota.

`prepare` pull/resolve task image, build verifier image, thu inventory và tạo snapshot test đã lọc. `validate` chạy base và reference patch ba lần; đây là bước chạy verifier thật, do đó chỉ chạy khi bạn muốn bắt đầu phần thử nghiệm trên Docker.

`plan-runs` chỉ lập lịch khi mọi task được chọn đã chuẩn bị và validation hợp lệ. Lịch lưu protocol hash, `run_id`, `attempt_id` và số request tối đa.

Mỗi lệnh `run-next` chạy **một lượt** từ trạng thái pending kế tiếp: tạo container sạch → gọi model/tool đến khi dừng → thu patch → chạy test được cấp từ snapshot → chạy evaluator riêng → lưu kết quả. Gọi `run-next` lặp lại để chạy tiếp các nhánh. Lượt hết budget vẫn được chấm với patch đã tạo.

`evaluate` chỉ chấm lại patch đã lưu, không gọi model. `summarize` không cần Docker; nó đọc schedule và artifact để ghi `summary/runs.csv` và `summary/summary.md`. Lượt `smoke_test` vẫn hiện trong bảng chi tiết nhưng bị loại khỏi thống kê ghép cặp và trung bình theo loại task.

Có thể thêm `--config config/study.yaml` trước hoặc sau tên lệnh. Với nhiều task, thêm `--task <task-id>` cho `prepare`, `validate`, `plan-runs`, `run-next` hoặc `evaluate`.

## Tiếp tục sau khi quota hoặc dịch vụ làm gián đoạn

- Nếu `run-next` báo `waiting_for_daily_quota`, chưa có request mới được gửi. Đợi quota reset rồi gọi lại.
- Nếu agent bị lỗi API/quota/Docker sau khi bắt đầu, schedule đánh dấu lượt `interrupted` và vô hiệu nhóm cùng task/seed/attempt để không đưa nhóm thiếu nhánh vào phân tích.
- Lấy `run_id` trong `runs/<study_id>/schedule.json`, rồi tạo attempt mới cho **cả nhóm**:

  ```powershell
  uv --cache-dir .uv-cache run deepswe-exp run-next --restart-group --run-id <run_id>
  ```

- Gọi `uv --cache-dir .uv-cache run deepswe-exp run-next` tiếp để thực hiện các lượt pending của attempt mới. Artifact attempt cũ được giữ nguyên.
- Nếu agent đã hoàn tất nhưng evaluator lỗi, dùng `uv --cache-dir .uv-cache run deepswe-exp evaluate --run-id <run_id>` để chấm lại patch; không gọi model lần nữa.
- Lỗi model/service không được tính là patch fail. Chỉ các run có evaluator status `pass` hoặc `fail_test` mới có điểm hành vi.

## Artifact được lưu ở đâu?

```text
runs/
  quota.jsonl                         # request ledger dùng chung giữa các study
  <study_id>/
    schedule.json
    connectivity/model-check-*.json   # kết quả check-model độc lập
    summary/runs.csv
    summary/summary.md
    <task_id>/
      inventory.json
      prepared.json
      base/                            # snapshot base đã bỏ Git history/cache/test ẩn
      prepared/<branch>/<seed>/
        manifest.json
        snapshot/                      # source + test đã lọc, dùng cho agent; hash kiểm tra tính toàn vẹn
      validation/{base,reference}/    # ba lần kiểm tra mỗi loại
      <branch>/<seed>/<attempt_id>/
        run.json, system_prompt.md, instruction.md
        trajectory.json, commands.jsonl, errors.jsonl
        final/                         # toàn bộ worktree agent trả về
        full_worktree.diff             # mọi thay đổi agent làm
        agent_patch.diff               # chỉ source sản phẩm được evaluator chấm
        visible-tests-result.json      # test từ snapshot chuẩn được cấp
        evaluation/result.json         # reward/F2P/P2P của benchmark evaluator
        evaluation/verifier/           # CTRF và report framework
```

`data/<task>/` là dữ liệu benchmark nguồn và không được pipeline sửa. Thư mục `runs/` và `.env` được Git ignore.
