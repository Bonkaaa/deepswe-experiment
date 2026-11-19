# Rà soát luồng chạy Ollama trên Saturn và Neptune

**Ngày rà soát:** 2026-11-19  
**Phiên bản code:** `5b44d8a` (`local-model`)  
**Phạm vi:** cấu hình profile, khởi chạy Ollama, `doctor`/`check-model`, adapter Ollama, lập lịch chạy và artifact hiện có.

## Tóm tắt

Code hiện tại đã có adapter Ollama và hỗ trợ hai profile Saturn/Neptune. Lỗi truyền `max_retries` cho Ollama đã được chặn trong code hiện tại; hai lượt gọi trong `check-model` cũng đã được gom vào cùng một event loop.

Tuy vậy, artifact Saturn gần nhất trong workspace ghi nhận model chưa có trên endpoint đang dùng (`404 model not found`). Chưa có artifact xác nhận một lượt chạy thực nghiệm Saturn hoặc Neptune đã bắt đầu thành công sau đó. Ngoài ra, bước `doctor` hiện chỉ kiểm tra server Ollama còn phản hồi, chưa xác nhận model cấu hình có trên server.

Rà soát này dùng đọc code và artifact đã lưu. Không có request nào được gửi tới Ollama trên hai server; trạng thái hiện tại của server chưa được xác minh.

## Phát hiện

### 1. Saturn từng không tìm thấy model cấu hình

Artifact mới nhất trong thư mục Saturn là:

`runs/pilot-a-saturn-qwen36/connectivity/model-check-20261118-003631-5fb186.json`

Artifact ghi:

- Provider: `ollama`
- Model: `qwen3.6:35b-a3b-coding`
- Kết quả: `ResponseError`
- Chi tiết: `model 'qwen3.6:35b-a3b-coding' not found (status code: 404)`

Tag này có trong [thư viện model Ollama](https://registry.ollama.ai/library/qwen3.6/tags). Vì vậy, lỗi tại thời điểm ghi artifact nhiều khả năng do model chưa được tải vào model store của daemon đang nghe ở endpoint Saturn, hoặc lệnh đang kết nối tới một daemon khác với daemon chứa model. Đây là suy luận từ tag trong registry và phản hồi 404 của endpoint; artifact không xác định nguyên nhân cụ thể.

`run_experiment_a.sh` dùng `set -e` và chạy `check-model` trước `prepare`/`run-all`. Do đó lỗi này làm pipeline dừng ở bước kiểm tra model, trước khi bắt đầu lượt thực nghiệm. Artifact được ghi ngày 18/11; chưa có dữ liệu để kết luận lỗi còn xảy ra sau khi setup mới nhất hoàn tất.

### 2. `doctor` chưa xác minh model có trên endpoint

Trong `src/deepswe_exp/__main__.py` (hàm `_doctor`), trạng thái Ollama được đánh dấu OK khi `/api/version` trả về version. Hàm chưa kiểm tra `digest` của model được cấu hình trong profile. Tương tự, `scripts/start_ollama_local.sh` xem endpoint là sẵn sàng nếu `/api/tags` trả HTTP thành công, nhưng không kiểm tra tag model mong đợi.

Hệ quả: server có thể vượt qua `doctor` và bước khởi động dù model chưa có; lỗi chỉ xuất hiện ở `check-model` dưới dạng 404. Pipeline hiện vẫn dừng an toàn trước `run-all`, nhưng chẩn đoán chưa chỉ đúng nguyên nhân ở bước đầu.

**Đề xuất:** để `doctor` kiểm tra model tag cấu hình có trong `/api/tags` và báo lỗi rõ ràng nếu thiếu. Nên xác minh daemon Saturn là tiến trình/runtime mong đợi trước khi coi endpoint đang hoạt động là Ollama của profile đó.

### 3. Lỗi `max_retries` cũ đã được xử lý trong code hiện tại

Artifact trước đó, `runs/pilot-a-saturn-qwen36/connectivity/model-check-20261118-002526-e7e3fe.json`, ghi lỗi:

`AsyncClient.chat() got an unexpected keyword argument 'max_retries'`

Trong code hiện tại, `src/deepswe_exp/components/agent/connection.py` và `src/deepswe_exp/components/agent/agent.py` chỉ truyền `max_retries`/`timeout` khi provider là Google. Nhánh Ollama không nhận các tham số đó, nên lỗi cụ thể này đã được loại khỏi đường gọi hiện tại.

Thay đổi gần nhất trong `connection.py` cũng đặt cả hai lượt `ainvoke` bên trong một hàm coroutine duy nhất rồi gọi `asyncio.run` một lần. Cách này xử lý đúng trường hợp client async bị tái sử dụng qua hai event loop khác nhau.

### 4. Hai option server được khai báo nhưng chưa áp dụng

`config/profiles/saturn.yaml` và `config/profiles/neptune.yaml` khai báo `num_parallel: 1` và `max_loaded_models: 1`. Tuy nhiên:

- `build_model()` chỉ truyền `temperature` cùng `num_ctx`/`num_predict` vào `ChatOllama`.
- `scripts/start_ollama_local.sh` chỉ đặt `OLLAMA_HOST` và `OLLAMA_MODELS` trước khi chạy Ollama.
- `scripts/setup_ollama_neptune.sh` cũng chưa đặt biến môi trường cho hai option này.
- `protocol()` vẫn lưu toàn bộ `ollama_options` vào hash, khiến protocol có thể ghi nhận option không thực sự được áp dụng.

Ollama có các biến server tương ứng là `OLLAMA_NUM_PARALLEL` và `OLLAMA_MAX_LOADED_MODELS` ([mã cấu hình môi trường Ollama](https://github.com/ollama/ollama/blob/main/envconfig/config.go)). Runner hiện xử lý tuần tự nên thiếu chúng chưa chứng minh sẽ làm lượt chạy đơn bị lỗi; đây là sai khác giữa cấu hình đã lưu và cấu hình runtime, có thể ảnh hưởng tài nguyên hoặc khả năng tái lập nếu có request đồng thời.

**Đề xuất:** hoặc áp dụng các giá trị này khi khởi chạy daemon, hoặc bỏ khỏi profile/protocol nếu không dùng.

### 5. Cảnh báo xung đột giả khi kích hoạt profile Neptune

`scripts/activate_ollama_neptune.sh` đặt `OLLAMA_HOST=127.0.0.1:11435` và đặt `OLLAMA_MODELS` bằng đường dẫn tuyệt đối. Trong khi đó profile dùng `http://127.0.0.1:11435` và đường dẫn tương đối. `load_settings()` so sánh chuỗi nguyên dạng và có thể cảnh báo hai biến đang xung đột dù chúng trỏ tới cùng endpoint/thư mục.

Điều này không làm profile đổi sang giá trị môi trường vì profile vẫn là nguồn cấu hình được dùng, nhưng sẽ tạo cảnh báo gây nhiễu khi chạy CLI từ terminal đã kích hoạt script.

**Đề xuất:** chuẩn hóa URL và resolve đường dẫn trước khi so sánh.

### 6. Hướng dẫn Neptune có nội dung cũ

`docs/setup-ollama-neptune.md` hiện nói provider Ollama chưa được tích hợp và chưa thể chạy `deepswe-exp`. Nội dung này mâu thuẫn với code hiện tại, vốn có `ChatOllama`, profile và CLI Ollama. Người vận hành có thể hiểu nhầm trạng thái triển khai.

**Đề xuất:** cập nhật đoạn giới hạn triển khai trong tài liệu sau khi xác nhận smoke check trên server.

## Trạng thái artifact trong workspace

- `runs/pilot-a-saturn-qwen36/`: có validation và hai artifact `check-model`; cả hai lần kiểm tra model đều lỗi. Chưa thấy `schedule.json` hoặc run artifact cho study này.
- `runs/pilot-a-neptune-qwen38/`: có validation, chưa thấy artifact `check-model`, schedule hoặc run artifact.
- Các trạng thái trên mô tả workspace hiện tại; chúng không khẳng định tình trạng trên filesystem của máy Saturn/Neptune nếu kết quả ở server chưa được đồng bộ về đây.

## Kiểm tra trước khi chạy batch

Trên từng server, trước khi chạy toàn bộ pipeline:

1. Xác nhận daemon đang dùng đúng endpoint và model store của profile.
2. Kiểm tra model tag cấu hình xuất hiện trong danh sách model của daemon.
3. Chạy `deepswe-exp doctor --profile saturn` hoặc `--profile neptune`.
4. Chạy `deepswe-exp check-model --profile saturn` hoặc `--profile neptune`; chỉ tiếp tục khi trạng thái là `success` và tool calling được xác nhận.
5. Sau khi pipeline chạy, kiểm tra `schedule.json`, `agent_result.json`, `evaluation/result.json` và `error.json` nếu một run bị ngắt.

## Kết luận

Lỗi local mode liên quan đến tham số `max_retries` và vòng đời event loop đã được xử lý trong code hiện tại. Lỗi còn thấy trong artifact Saturn là model trả 404 tại thời điểm kiểm tra; cần một `check-model` mới trên server sau setup hiện tại để xác nhận đã hết. Trước khi coi quy trình sẵn sàng, nên làm cho `doctor` xác minh model tag, đồng bộ các option runtime với profile và sửa cảnh báo so sánh cấu hình Neptune.
