# Kế hoạch hoàn thiện pipeline Thực nghiệm A

Tài liệu này là đặc tả triển khai cho pipeline trong repo. Mục tiêu là để một người có thể lần theo dữ liệu từ task DeepSWE đến patch, kết quả chấm và báo cáo, đồng thời giữ riêng phần agent được phép nhìn thấy với bộ test evaluator.

## Mục tiêu và giới hạn

- Chạy các task Python trong `data/` với agent sửa code, lưu patch và chấm patch trong container độc lập.
- So sánh các nhánh test `100%`, `70% ngẫu nhiên`, `50% ngẫu nhiên`, `30% ngẫu nhiên`, `0%` và `50% liên quan`.
- Gọi Gemini qua LangChain `ChatGoogleGenerativeAI`; đọc `MODEL` và `GEMINI_API_KEY` từ `.env`.
- Runner chạy trên Windows bằng Python 3.12 và `uv`; Docker Desktop cung cấp Linux containers.
- Mỗi lượt có tối đa 25 request model, 30 phút, 4.096 output token/request, 8.000 ký tự output tool gửi lại model và 120 giây/lệnh.
- Chạy từng lượt tuần tự. API key không được ghi ra artifact hoặc đưa vào container.
- Adaptix là smoke test kỹ thuật. Kết quả Adaptix không được dùng làm kết luận benchmark độc lập.

Việc hoàn thiện code và smoke test component không đồng nghĩa Docker đã được xác thực hoặc thí nghiệm Adaptix đã chạy. Những bước cần Docker hoặc chạy agent/evaluator thật được tách riêng trong pipeline guide để người vận hành chủ động thực hiện.

## Kiến trúc

```text
CLI (__main__.py)
  ├─ configuration/settings.py       YAML, .env, đường dẫn, kiểm tra schema
  ├─ tasks/                           đọc task, thu collection, tạo test manifest
  ├─ execution/                       Docker, snapshot, validate, lịch và lượt chạy
  ├─ agent/                           LangChain, terminal tool, quota ledger
  ├─ evaluation/                      patch sản phẩm, test được cấp, verifier kín
  └─ results/                         JSONL/JSON, CSV và báo cáo Markdown
```

Python runner giữ API key ở host. Agent container không có mạng, API key, Docker socket, thư mục verifier hay artifact của lượt khác. Verifier dùng container mới, image đã ghim, base commit thật và patch sản phẩm đã lọc.

## Các phase triển khai

### Phase 1 — Cấu hình và CLI

1. Dùng `uv` ghim Python 3.12 và các thư viện LangChain, Gemini, Docker SDK, YAML, dotenv, timezone database.
2. Đọc file YAML tùy chọn bằng `--config`; đường dẫn data, prompt và artifact được giải từ root repo, không phụ thuộc thư mục hiện tại.
3. Xác thực nhánh, seed, budget, quota và task ID trước khi chạy.
4. Tách kiểm tra môi trường (`doctor`) khỏi kiểm tra kết nối model (`check-model`). Chỉ `check-model` gửi đúng một request Gemini, ghi kết quả riêng và không tạo lượt nghiên cứu.
5. Che key khỏi log và biểu diễn `Settings`.

**Điều kiện đạt:** lệnh CLI có exit code đúng; cấu hình lỗi dừng trước khi gọi model; `doctor` báo riêng Python, Docker, task, prompt, model, key và quota.

### Phase 2 — Docker và verifier

1. Resolve task image thành digest và build verifier image riêng từ `tests/Dockerfile` tạm, thay image nền bằng digest đã resolve.
2. Tạo container Linux không mạng, giới hạn CPU/RAM, dùng Docker archive API để truyền snapshot và file.
3. Chạy test trong process có timeout; lưu stdout và report kể cả khi verifier lỗi.
4. Chạy base và reference patch ba lần, kiểm tra từng node ID, reward, F2P/P2P và độ lặp lại.
5. Phân loại timeout, apply failure, report thiếu và lỗi Docker/API thành lỗi kỹ thuật, không biến chúng thành test fail.

**Điều kiện đạt:** base fail chức năng nhưng giữ P2P; reference pass F2P và P2P; cả ba lần cho cùng kết quả.

### Phase 3 — Inventory và manifest test

1. Thu test collection từ checkout base trước khi áp benchmark `test.patch`.
2. Ghi node ID, file, class, hàm, mô tả ngắn và fixture dùng bởi test.
3. Gom parameterized cases theo cùng hàm test; phạm vi Adaptix mặc định là file có path component `morphing`.
4. Tạo các nhánh ngẫu nhiên từ cùng thứ tự nhóm đã xáo theo seed. Chọn prefix có số case gần tỷ lệ đích nhất; nếu hòa chọn ít case hơn.
5. Tạo nhánh `50-related` bằng điểm liên quan đã khai báo trước trong `task_options`; nếu thiếu điểm, chỉ nhánh đó bị chặn.
6. Xóa hẳn hàm test không được cấp bằng AST, kể cả decorator; thu collection lại để đối chiếu manifest. Giữ fixture/helper chung.

**Điều kiện đạt:** manifest deterministic, có hash nguồn và hash manifest; collection thực tế đúng với danh sách được cấp; không lọc mơ hồ hoặc âm thầm bỏ qua test.

### Phase 4 — Snapshot và sandbox agent

1. Tạo snapshot riêng cho từng nhánh/seed từ base source; source sản phẩm phải có cùng hash.
2. Xóa Git history cũ, test ngoài snapshot, cache và thư mục solution/reference trước khi giao container.
3. Tạo Git baseline mới từ snapshot đã lọc. Agent chạy bằng user không đặc quyền, không mạng và không có quyền truy cập Docker.
4. Thu cây file cuối cùng và tính diff ở host so với snapshot tin cậy. Lưu cả diff toàn bộ lẫn patch sản phẩm theo allowlist.

**Điều kiện đạt:** ba nhánh độc lập; agent không lấy lại được test đã bỏ qua từ history; file mới/xóa/commit đều xuất hiện trong diff; test agent sửa không lọt vào patch chấm.

### Phase 5 — LangChain agent và quota

1. Dùng `ChatGoogleGenerativeAI` với model/key lấy từ cấu hình, Google Developer API và retry tắt ở client lẫn request.
2. Cung cấp một tool `run_shell(command)` chạy trong `/app` của container agent.
3. Giữ nguyên AIMessage/tool call ID trong hội thoại và serialize toàn bộ trajectory.
4. Ghi quota ledger bền vững trước request; request lỗi cũng được tính. Điều tiết RPM/TPM với 20% dự phòng và kiểm tra RPD trước khi mở lượt.
5. Dừng khi hết request, thời gian, quota hoặc khi model/API lỗi; lưu patch ở trạng thái hiện tại.

**Điều kiện đạt:** request thứ 26 không được gửi; lỗi dịch vụ làm hỏng nhóm task/seed thay vì tính là fail; model giả kiểm tra được vòng tool mà không cần Gemini.

### Phase 6 — Chấm hai loại test tách biệt

1. Chạy test được cấp trên snapshot chuẩn đã lọc, sau khi áp patch agent. Test lấy từ snapshot tin cậy, không lấy từ cây agent có thể chỉnh sửa.
2. Chạy verifier benchmark trong container riêng tại base commit thật; verifier tự áp test patch và patch model theo quy trình benchmark.
3. Lưu trạng thái và report riêng cho test được cấp và evaluator kín.
4. Phân biệt agent dừng do budget với điểm patch. Patch rỗng hoặc patch tạo trước khi hết budget vẫn được chấm.

**Điều kiện đạt:** có thể tái chấm patch không gọi model; lỗi apply/timeout/report/Docker có nhãn kỹ thuật riêng; test agent tạo không được dùng làm evaluator.

### Phase 7 — Lịch, tiếp tục và tổng hợp

1. `plan-runs` khóa protocol bằng hash của cấu hình, model, prompt và các artifact chuẩn bị.
2. Mỗi run có `run_id` và `attempt_id`; không ghi đè attempt cũ. Một file lock giữ một runner tại một thời điểm.
3. Khi lỗi API làm gián đoạn nhóm, đánh dấu nhóm cũ không hợp lệ; `--restart-group --run-id ...` tạo attempt mới cho cả nhóm task/seed.
4. `evaluate` chạy lại verifier trên patch có sẵn; không gọi model.
5. `summarize` xuất CSV từng lượt và Markdown; chỉ nhóm đủ nhánh hợp lệ mới tham gia thống kê ghép cặp.

**Điều kiện đạt:** mọi con số trong báo cáo truy ngược được tới manifest, trajectory, patch và report verifier tương ứng.

## Kiểm tra nghiệm thu

- Component smoke offline: settings/root, lọc AST và collection, manifest xác định, diff patch, parser reward/CTRF, ledger và report.
- Smoke model riêng: đúng một request qua LangChain; không phải agent run.
- Docker xác thực: kiểm tra thực thi, timeout, tách mạng, truyền/thu file, base/reference và audit snapshot.
- Adaptix smoke: ba nhánh, ba agent run độc lập, chấm evaluator riêng.

Các mục Docker và Adaptix chỉ được đánh dấu hoàn thành khi có artifact chứng minh tương ứng. Không suy ra chúng đã đạt chỉ từ việc code import được hoặc Docker CLI có mặt.
