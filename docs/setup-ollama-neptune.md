# Thêm Qwen3.8 trên Neptune, giữ nguyên Qwen3.6 trên Saturn

Saturn hiện đã có Qwen3.6; các script này chỉ dành cho Neptune. Trên Neptune, runtime, model, cấu hình và log đều nằm dưới `.local/ollama-neptune/` trong repo; `.local/` đã được Git ignore. Ollama trên Neptune dùng cổng loopback `11435`. Script không gọi installer mặc định, không dùng Ollama từ `PATH`, không dùng `sudo`, không tạo service và không sửa hoặc dừng tiến trình Saturn/global.

Model được chọn là `qwen3.8:27b` từ thư viện Ollama chính thức. Bản Ollama đang công bố có dung lượng khoảng 18 GB. [Trang model Qwen3.8 trên Ollama](https://ollama.com/library/qwen3.8)

## Chạy trên Neptune

Trước tiên đồng bộ repo lên Neptune, vào thư mục repo, rồi chạy:

```bash
bash scripts/setup_ollama_neptune.sh
```

Script kiểm tra H100, dung lượng trống, và các công cụ sẵn có (`curl`, `tar`, `zstd`, `nvidia-smi`). Sau đó script tải Ollama Linux AMD64 từ nguồn chính thức vào `.local/ollama-neptune/runtime/`, chạy riêng trên `127.0.0.1:11435`, và tải model vào `.local/ollama-neptune/models/`. Lần đầu cần internet và ít nhất 30 GiB trống trong cùng filesystem với repo.

Kiểm tra model/API:

```bash
bash scripts/ollama_neptune.sh list
bash scripts/ollama_neptune.sh run qwen3.8:27b
curl http://127.0.0.1:11435/api/chat -d '{
  "model": "qwen3.8:27b",
  "messages": [{"role": "user", "content": "Trả lời đúng một từ: OK"}],
  "stream": false,
  "options": {"num_ctx": 32768}
}'
nvidia-smi
```

Để dừng đúng tiến trình Ollama do repo khởi chạy:

```bash
bash scripts/stop_ollama_neptune.sh
```

Script dừng xác minh executable qua `/proc/<pid>/exe`; nếu PID không còn là Ollama của Neptune, nó từ chối gửi tín hiệu. CLI nên luôn chạy qua `scripts/ollama_neptune.sh` để endpoint và model store tiếp tục được giới hạn trong repo. Model vẫn ở `.local/ollama-neptune/models/` sau khi dừng. Không xóa thư mục đó nếu muốn giữ model đã tải.

## Terminal mới

Đóng/mở terminal không làm server nền dừng. Trong terminal mới, từ repo root, chọn môi trường local một lần:

```bash
source scripts/activate_ollama_neptune.sh
ollama list
ollama run qwen3.8:27b
```

File activation chỉ thay đổi biến môi trường và `PATH` của terminal hiện tại; lệnh `ollama` sẽ trỏ vào binary trong `.local/ollama-neptune/`. Nếu máy Neptune vừa reboot nên Ollama server đã dừng, chạy lại `bash scripts/setup_ollama_neptune.sh`; binary/model có sẵn sẽ được dùng lại.

## Trạng thái tích hợp và vận hành

Provider Ollama và profile `--profile neptune` đã được tích hợp đầy đủ vào ứng dụng `deepswe-exp`. 
Sau khi hoàn tất setup Ollama trên Neptune theo hướng dẫn trên, bạn có thể thực hiện kiểm tra và chạy benchmark đầy đủ:

```bash
# Kiểm tra hệ thống và xác nhận model trên Neptune
uv run --frozen python -m deepswe_exp doctor --profile neptune

# Kiểm tra kết nối model và khả năng gọi công cụ
uv run --frozen python -m deepswe_exp check-model --profile neptune

# Khởi động One-Click toàn bộ quy trình thực nghiệm A trên Neptune
bash scripts/run_experiment_a_neptune.sh
```
