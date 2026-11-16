# Implementation plan — Thực nghiệm A

## Mục tiêu

Tạo một pipeline chạy task DeepSWE qua agent dùng LangChain + Gemini, thay đổi số test repo agent được thấy, thu patch, chấm patch bằng verifier riêng và lưu kết quả có thể truy lại. Adaptix là smoke test kỹ thuật; vì reference và verifier của task này đã được xem trong quá trình chuẩn bị, không tính nó là một task độc lập cho kết luận nghiên cứu.

## Các quyết định đã chốt

- Windows + Docker Desktop Linux containers; Python 3.12 và `uv`.
- Gọi Gemini qua `langchain-google-genai` / `ChatGoogleGenerativeAI`; lấy `MODEL` và `GEMINI_API_KEY` từ `.env`.
- Dùng một tool `run_shell` thực thi lệnh bên trong container agent. Runner trên máy chủ điều khiển vòng lặp và không đưa API key vào container.
- Task sửa lỗi và task thêm tính năng đều có thể tham gia; ghi category và phân tích riêng.
- Ba nhánh đầu tiên: 100%, 50% ngẫu nhiên, 0%; một lượt tại một thời điểm.
- Mỗi lượt tối đa 25 request gửi tới API và 30 phút. Tắt retry tự động; lỗi quota/dịch vụ không được chấm như lỗi patch.
- Dữ liệu gốc trong `data/` không bị sửa. Artifact chạy lưu riêng trong `runs/<study>/<task>/<branch>/<seed>/`.

## Phase triển khai

### 0. Môi trường và project

Tạo Python project bằng `uv`, ghim Python 3.12 và dependency. Thêm `.gitignore` cho `.env`, cache và `runs/`. `doctor` kiểm tra Python, Docker Linux engine, task folder, tên biến môi trường và quota mà không in key. Docker CLI hiện có nhưng engine chưa kết nối; phải khởi động Docker Desktop trước khi chạy container. File `Dockerfile` gốc đang rỗng, không dùng nó làm cấu hình.

### 1. Xác thực task/verifier, không gọi model

Đọc `task.toml`, image và commit nền. Chạy verifier trên base và reference patch ba lần, kiểm tra F2P/P2P và ghi riêng timeout, lỗi build, flaky test. Adaptix grader khai báo 44 F2P và 2.738 P2P node IDs; đây là test verifier, không phải tập test repo được ablate.

### 2. Danh mục test và nhánh

Checkout repo tại base commit trước khi áp `tests/test.patch`. Lập danh sách test repo quanh module task, gắn test ID, hành vi, file, nhóm và fixture. Giữ toàn bộ biến thể tham số hóa trong cùng nhóm. Tạo manifest 100%, 50% ngẫu nhiên và 0%, dùng seed smoke `42`; ghi số test case thực tế và hash manifest.

### 3. Sandbox agent

Khởi tạo container từ task image. Trước khi agent bắt đầu, áp snapshot test theo manifest, xóa Git history cũ rồi tạo baseline Git mới. Không mount `.env`, verifier, `solution/`, test patch hoặc Docker socket. Kiểm tra rằng test bị bỏ không tồn tại trong filesystem agent hoặc Git history và source sản phẩm giống nhau giữa các nhánh.

### 4. LangChain agent

Tạo `ChatGoogleGenerativeAI` từ `.env`, tắt retry tự động, gắn tool `run_shell` bằng `bind_tools`. Vòng lặp giữ nguyên toàn bộ `AIMessage`, thực thi tool call tuần tự trong container, trả output có giới hạn 8.000 ký tự và dừng khi model kết thúc, đạt 25 request hoặc 30 phút. Mỗi lệnh terminal tối đa 120 giây. Lưu trajectory sau từng bước.

### 5. Thu patch và verifier

Lưu full diff; tạo patch chấm chỉ gồm đường dẫn mã nguồn sản phẩm cho phép (Adaptix ban đầu: `src/adaptix/`). Không đưa test do agent thêm/sửa vào verifier. Áp patch trên checkout base thật trong container verifier riêng, chạy `tests/test.sh`, lưu report. Hết budget vẫn chấm patch hiện có; lỗi API/Docker/apply patch được phân loại riêng.

### 6. Smoke test Adaptix

Chạy 100%, 50% ngẫu nhiên, 0% tuần tự với container và hội thoại mới. Chấm bằng cùng verifier. Điều kiện đạt: ba manifest khác nhau đúng dự kiến, không rò test, thu đủ patch/log và có thể chấm lại patch mà không gọi model.

### 7. Pilot mở rộng

Chọn năm task Python hợp lệ trước khi xem patch model, gồm task sửa lỗi và thêm tính năng; ghi category. Chạy ba nhánh mỗi task, một model, một lần: 15 lượt, tối đa 375 request. Nếu quota free tier không đủ, tiếp tục theo ngày; không đổi model hoặc budget giữa các task.

### 8. Thực nghiệm A đầy đủ

Thêm 70%, 30% ngẫu nhiên và 50% liên quan để có sáu nhánh. Chạy seed `11`, `23`, `37`; cùng task/seed chạy đủ nhánh, mỗi lượt có sandbox mới. Năm task × sáu nhánh × ba seed = 90 lượt, tối đa 2.250 request. Xếp hạng test liên quan chỉ dựa trên prompt, source và test base, ghi thứ hạng trước repair. Báo pass evaluator, F2P/P2P, lượt gián đoạn, usage và kết quả ghép cặp; phân tích riêng task sửa lỗi và thêm tính năng.

## Cấu trúc code và lệnh

`src/deepswe_exp/__main__.py` là CLI. Các module được nhóm trong `src/deepswe_exp/components/`: `configuration/` nạp cấu hình và che key; `tasks/` đọc task và tạo biến thể test; `agent/` điều khiển LangChain loop/tool/budget; `execution/` quản lý container và lượt chạy; `evaluation/` thu patch và chạy grader; `results/` lưu state/artifact và xuất báo cáo.

```text
doctor → prepare → validate → plan-runs → run-next → evaluate → summarize
```

`run-next` chạy một lượt. `evaluate` chấm lại patch đã lưu, không gọi model. Khi code xong, tạo `docs/pipeline-thuc-nghiem-A.md` để giải thích thứ tự file, dữ liệu vào/ra, lệnh chạy và cách tiếp tục sau quota interruption.

## Kiểm tra nghiệm thu

Trước khi gọi Gemini, dùng model giả để kiểm tra tool loop, giới hạn request/thời gian, lọc test, thu patch, verifier và chấm lại. Chỉ chạy smoke test thật sau khi Docker engine và quota cấu hình sẵn sàng. API key không xuất hiện trong log, artifact hoặc Git.
