# Kế hoạch Triển khai (Bản hoàn thiện): Phân biệt Output Truncation & Context Overflow, Cơ chế Phục hồi Tự động và Đặt tên Folder có Timestamp

**Trạng thái:** Đã phê duyệt và đang triển khai  
**Phạm vi:** Local Ollama agent trên Saturn/Neptune, Settings, Runner, CLI và Scripts thực nghiệm  
**Mục tiêu:** 
1. Phân loại chuẩn xác nguyên nhân kết thúc giữa **Output Truncation** (`eval_count >= max_output_tokens`) và **Context Overflow** (`prompt_eval_count + eval_count >= num_ctx - 100`).
2. Tự động phục hồi khi Agent bị cắt cụt output (cho phép 1 lần retry với prompt hướng dẫn sửa code cục bộ thay vì ngắt run ngay lập tức).
3. Nâng trần `max_output_tokens` lên **8.192** trên Saturn để mở rộng gấp đôi không gian sinh code (~30.000 ký tự).
4. Tự động gắn **Timestamp** vào tên folder study mỗi lần chạy (ví dụ `runs/pilot-a-saturn-qwen36_20261120_011000/`), đảm bảo mỗi lần chạy là một folder riêng biệt, không đè kết quả cũ và không cần xóa tay `rm -rf`.

---

## 1. Phân tích Dữ liệu Thực nghiệm Lượt 29 trên Saturn

Ở lượt chạy vừa qua trên Saturn:
- **Ngữ cảnh đầu vào hoàn toàn không tràn**: `prompt_eval_count: 28.791` / 65.536 tokens (chưa tới 45% context window 64K).
- **Nguyên nhân chạm cờ `done_reason: 'length'`**: `eval_count: 4096` = đúng trần `max_output_tokens: 4096`.
- **Hành vi Agent**: Sau khi tạo nhánh Git `alias-support` và đọc `name_mapping.py`, Agent cố gắng sinh ra một lệnh shell ghi đè file code lớn, dẫn đến việc token sinh ra chạm trần 4.096 trước khi đóng thẻ JSON của tool call.
- **Hạn chế cũ**: `agent.py` coi mọi `done_reason == "length"` là `context_overflow`, cố nén context đầu vào (vốn không hề tràn) rồi ngắt run với trạng thái `interrupted`.

---

## 2. Các Quyết định Kỹ thuật Cốt lõi Đã Chốt

### 2.1. Phân loại chính xác Response (`classify_response`)
Thêm hàm phân loại độc lập sau mỗi lượt gọi model:
1. `context_overflow`: Khi `(prompt_tokens + 100 >= num_ctx)` hoặc `(prompt_tokens >= input_budget)`.
2. `output_truncated`: Khi `(output_tokens >= max_output_tokens - 10)` và response bị cắt đứt do `length`, trong khi ngữ cảnh đầu vào vẫn còn dư.
3. `truncation_unknown`: Khi có cờ `length` nhưng thiếu counters đo lường.
4. `normal`: Hoàn thành bình thường hoặc có tool calls hợp lệ.

### 2.2. Cơ chế Phục hồi khi bị Output Truncation (1-Shot Recovery Retry)
- Khi phân loại là `output_truncated`:
  - **Không thực thi** bất kỳ tool call dở dang nào.
  - **Không đưa** tool call JSON bị lỗi vào context gửi cho lượt retry (nhưng vẫn lưu vào `trajectory.json` để audit).
  - Tối đa **1 lần retry liên tiếp**.
  - Trước khi retry, nạp vào một tin nhắn hướng dẫn khẩn cấp:
    > *"Warning: Lệnh/phản hồi trước của bạn vượt quá giới hạn sinh mã trong một lượt (8.192 tokens) và bị cắt cụt. Vui lòng KHÔNG ghi đè toàn bộ file lớn trong một lệnh shell. Hãy dùng các lệnh sửa cục bộ có mục tiêu như `sed -i`, `python3 -c '...'`, hoặc viết một patch nhỏ để sửa đúng các dòng code cần thiết."*
  - Nếu retry thành công sinh tool call hợp lệ: tiếp tục chu trình bình thường!
  - Nếu retry vẫn tiếp tục bị cắt output: dừng an toàn với `stop_reason = 'output_truncated'` và đánh dấu `interrupted`.

### 2.3. Nâng trần `max_output_tokens: 8192` cho Saturn
- Trong `settings.py`: Nâng `MAX_BUDGET["max_output_tokens"]` từ 4.096 lên **8.192**.
- Trong `config/study.saturn.yaml`: Cấu hình `max_output_tokens: 8192`.
- Tính toán lại `input_budget`:
  $$\text{input\_budget} = 65.536 - 8.192 - 4.096 = \mathbf{53.248\text{ tokens}}$$
  (Vẫn quá rộng rãi so với mức 28.791 tokens thực tế ở lượt 29).

### 2.4. Tự động Gắn Timestamp vào Thư mục Thực nghiệm
- Bổ sung tham số `--study-id` vào CLI `deepswe-exp` và hàm `load_settings(...)`.
- Trong `scripts/run_experiment_a.sh`:
  - Nếu người dùng không chỉ định explicit study ID, script sẽ tự động tạo study ID có timestamp:
    `STUDY_ID="${BASE_STUDY_ID}_$(date +%Y%m%d_%H%M%S)"`
  - Truyền `--study-id "${STUDY_ID}"` qua tất cả các step (prepare, validate, plan-runs, run-all, summarize).
  - Mỗi lần bấm chạy sẽ tạo ra thư mục riêng biệt trong `runs/` (ví dụ `runs/pilot-a-saturn-qwen36_20261120_011500/`), không bao giờ bị ghi đè hay xung đột lịch sử.

### 2.5. Bảo toàn Ngân sách và Dữ liệu Audit (Strict Accounting)
- Mỗi lượt gọi model (kể cả recovery retry) đều được kiểm tra `max_model_requests`, `deadline`, reserve qua `ledger.reserve(...)`, tăng `model_requests`, và lưu riêng `usage` vào trajectory.
- Trong `runner.py`: Thêm `output_truncated` và `truncation_unknown` vào danh sách gây `interrupted`.

---

## 3. Chi tiết Thay đổi theo từng File

| File | Thay đổi chính |
| :--- | :--- |
| `src/deepswe_exp/components/agent/context.py` | Bổ sung hàm `classify_response(response, num_ctx, max_output_tokens, input_budget)` phân biệt chuẩn xác 4 trạng thái kết thúc. |
| `src/deepswe_exp/components/agent/agent.py` | Triển khai luồng 1-shot recovery retry cho output truncation; bảo đảm kiểm tra quota/deadline/ledger trước mỗi request. |
| `src/deepswe_exp/components/configuration/settings.py` | Nâng `MAX_BUDGET["max_output_tokens"] = 8192`; hỗ trợ override `study_id` trong `load_settings`. |
| `src/deepswe_exp/__main__.py` | Bổ sung cờ `--study-id` vào CLI parser và truyền vào `load_settings`. |
| `src/deepswe_exp/components/execution/runner.py` | Bổ sung `output_truncated` và `truncation_unknown` vào danh sách `interrupted`. |
| `config/study.saturn.yaml` | Nâng `max_output_tokens: 8192`. |
| `prompt/system_prompt.md` | Bổ sung quy tắc cấm dump toàn bộ file lớn khi sửa code; bắt buộc dùng lệnh sửa cục bộ. |
| `scripts/run_experiment_a.sh` | Tự động sinh `STUDY_ID` kèm timestamp `_$(date +%Y%m%d_%H%M%S)` và truyền `--study-id`. |
| `tests/test_agent_context.py` | Viết unit tests offline cho logic phân loại, recovery retry 1-shot, quota tracking, và timestamp study. |

---

## 4. Kế hoạch Kiểm thử & Nghiệm thu

1. **Unit Tests Offline**: Toàn bộ unit tests pass 100% trên Windows mà không cần server GPU hay Docker.
2. **Kiểm tra Phân loại**:
   - `prompt_tokens=28791, eval_count=4096, num_ctx=65536` $\rightarrow$ phân loại chính xác `output_truncated`.
   - `prompt_tokens=65000, eval_count=20, num_ctx=65536` $\rightarrow$ phân loại chính xác `context_overflow`.
3. **Kiểm tra Recovery**: Lượt 1 bị output truncation $\rightarrow$ kích hoạt retry kèm hướng dẫn sửa code $\rightarrow$ lượt 2 thành công tiếp tục chu trình mà không ngắt run.
4. **Kiểm tra Timestamp**: Chạy script tạo ra folder `runs/<study_id>_<timestamp>`, các run kế tiếp tạo folder mới độc lập.
