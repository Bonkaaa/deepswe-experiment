# Kế hoạch Triển khai (Bản hoàn thiện): Tối ưu Context Window, Cơ chế Tự động Compact Đa tầng & Chống Dừng Sai cho Agent Ollama

**Trạng thái:** Sẵn sàng triển khai (Đã tối ưu hóa dựa trên thực nghiệm `pilot-a-saturn-qwen36` trên server Saturn ngày 19/11/2026)  
**Phạm vi:** Module Agent (`agent.py`, `context.py`), Settings, Runner, System Prompt và Config Profiles  
**Mục tiêu:** 
1. Khai thác hiệu quả 48 GB VRAM của RTX 6000 Ada mà vẫn duy trì tốc độ sinh và độ chính xác suy luận.
2. Thiết kế cơ chế **Auto-Compaction 4 tầng (4-Tier Hierarchical Compaction)** kết hợp High/Low Watermark nhằm bảo toàn bộ nhớ đệm KV cache (Prefix Caching) của Ollama.
3. Loại bỏ dứt điểm lỗi nhận diện sai `model_finished` khi chạm trần ngữ cảnh (`done_reason == "length"`).
4. Định hướng lại hành vi trong System Prompt để Agent dứt khoát chuyển từ đọc file sang sửa code và kiểm thử, tạo ra `agent_patch.diff` hợp lệ.

---

## 1. Phân tích Dữ liệu Thực nghiệm Saturn (`pilot-a-saturn-qwen36`)

Qua phân tích 3 run (`0`, `50-random`, `100`) trong archive thực tế kéo về từ server Saturn:
- **Hiện tượng tràn ngữ cảnh**: Cả 3 run dừng chính xác tại lượt 18–19. Trajectory cho thấy `prompt_eval_count: 32732`, `total_tokens: 32767 / 32768`.
- **Nguyên nhân phình to**: Trong 34 lệnh shell được gọi, Agent thực hiện nhiều lệnh `cat` đọc toàn bộ các file code lớn:
  - `cat component.py` (16.967 ký tự)
  - `cat facade/provider.py` (19.731 ký tự)
  - `cat loader_gen.py` (31.962 ký tự)
  - `cat dumper_gen.py` (34.177 ký tự)
  - `cat loader_provider.py` (10.758 ký tự)
  Mỗi lệnh `cat` bị trần cũ cắt ở 8.000 ký tự (~2.000 tokens), khiến context 32K nhanh chóng cạn kiệt.
- **Lỗi hiểu nhầm `model_finished`**: Khi chạm trần 32.768 tokens, Ollama trả về HTTP 200 với metadata `"done_reason": "length"` và chỉ sinh được 35 tokens thì bị cắt đứt. Do bị cắt giữa chừng, phản hồi không chứa `tool_calls`. Đoạn mã:
  ```python
  if not response.tool_calls:
      stop = 'model_finished' if not response.invalid_tool_calls else 'format_error'
      break
  ```
  đã gán nhầm `stop_reason = 'model_finished'`. Hệ thống tưởng Agent đã xong việc nên đem snapshot đi tính diff, dẫn đến `agent_patch.diff` rỗng (0 bytes) và toàn bộ 44 test F2P bị 0 điểm (`fail_test`).

---

## 2. Các Quyết định Kỹ thuật Cốt lõi (Core Architectural Decisions)

### 2.1. Cấu hình Context Window trên RTX 6000 Ada (48 GB VRAM)
Mô hình triển khai: `qwen3.6:35b-a3b-coding` (kiến trúc `qwen35moe`, Q4_K_M, 35.5B tham số).
- **Trọng số mô hình**: ~20.5 GB VRAM.
- **Tiêu thụ KV Cache** (kiến trúc GQA: `head_count_kv=2`, `head_dim=256`, `block_count=41`):
  $$\text{KV Cache / token} = 2 \times 41 \times 2 \times 256 \times 2\text{ bytes (fp16)} = 83.968\text{ bytes} \approx 82\text{ KB/token}$$
- **So sánh 64K vs 128K**:
  - **Ở mức 64K (`num_ctx: 65536`)**:
    - KV Cache: $65.536 \times 82\text{ KB} \approx \mathbf{5.12\text{ GB}}$.
    - Tổng VRAM tiêu thụ: $20.5\text{ GB} + 5.1\text{ GB} + 2.0\text{ GB (compute)} = \mathbf{\sim 27.6\text{ GB} / 48\text{ GB}}$ (dư tới 20.4 GB an toàn tuyệt đối).
    - Thời gian prompt evaluation: ~15–25s / lượt. Chất lượng bám ngữ cảnh (attention fidelity) rất cao.
  - **Ở mức 128K (`num_ctx: 131072`)**:
    - KV Cache: $131.072 \times 82\text{ KB} \approx \mathbf{10.25\text{ GB}}$.
    - Tổng VRAM tiêu thụ: $20.5\text{ GB} + 10.25\text{ GB} + 2.5\text{ GB} = \mathbf{\sim 33.3\text{ GB} / 48\text{ GB}}$ (dư 14.7 GB).
    - Thời gian prompt evaluation: ~45–75s / lượt (chậm hơn đáng kể, dễ gây timeout vòng lặp nếu agent chạy 50–100 lượt).
- **Quyết định phù hợp nhất**:
  - Cấu hình mặc định trên Saturn là **`num_ctx: 65536` (64K)** — đây là "điểm ngọt" (sweet spot) hoàn hảo giữa tốc độ, độ ổn định và dung lượng ngữ cảnh gấp đôi bản cũ.
  - Vẫn hỗ trợ `131072` (128K) qua profile cấu hình nếu người dùng muốn mở rộng tối đa.
  - Dự phòng: `context_safety_tokens = 4096`.

### 2.2. Cơ chế Auto-Compaction 4 tầng (4-Tier Hierarchical Compaction)
Thay vì chỉ xóa mù quáng cả lượt (turn) khiến mô hình mất sạch ngữ cảnh và quên mất các file vừa duyệt, hệ thống áp dụng 4 tầng nén có trật tự:

1. **Tầng 1 - Cắt gọt tại nguồn (Proactive Output Truncation)**:
   - Hạ `max_tool_output_chars` từ 8.000 xuống **4.000 ký tự**.
   - Cắt theo tỷ lệ **Head 60% + Tail 40%** kèm marker `\n[... TRUNCATED X CHARS ...]\n`. Phần đầu giữ lại lệnh và cấu trúc bắt đầu, phần đuôi giữ lại kết quả kiểm tra hoặc traceback lỗi quan trọng nhất.
2. **Tầng 2 - Nén mềm lịch sử Tool (Historical Tool Folding)**:
   - Khi tổng token vượt ngưỡng kích hoạt (High Watermark), quét các `ToolMessage` thuộc các lượt cũ (cách hiện tại $\ge 4$ lượt).
   - Rút gọn nội dung các `ToolMessage` cũ này về dạng tóm tắt:
     `exit_code=0\n[Output: <N> chars omitted from earlier history; command: '<command>']`.
   - **Lợi ích**: Tiết kiệm ngay 80–90% token của lịch sử cũ, trong khi Agent vẫn biết mình đã từng chạy lệnh đó, không bị rơi vào vòng lặp khám phá lại từ đầu.
3. **Tầng 3 - Cắt tỉa theo Lượt nguyên tử (Atomic Turn Pruning - FIFO)**:
   - Nếu sau Tầng 2 mà context vẫn vượt `input_budget`: Bỏ dần các cụm lượt cũ nhất.
   - Một cụm lượt bao gồm: 1 `AIMessage` và toàn bộ các `ToolMessage` tương ứng với các `tool_call_id`.
   - Luôn giữ bất biến: `SystemMessage` (index 0), `HumanMessage` (index 1), và tối thiểu 3 lượt gần nhất (`min_recent_turns = 3`).
4. **Tầng 4 - Ngưỡng High/Low Watermark (Bảo vệ Prefix Caching)**:
   - Trong Ollama (llama.cpp), KV cache được tái sử dụng nếu tiền tố (prefix) các token từ đầu chuỗi không đổi. Nếu mỗi lượt ta cắt 1 ít ở đầu, cache bị xóa sạch và mô hình phải tính toán lại từ đầu (re-eval) gây nghẽn nghiêm trọng.
   - **Giải pháp**: Chỉ kích hoạt nén khi chạm **High Watermark (85% input_budget)**, và nén sâu xuống **Low Watermark (65% input_budget)**. Nhờ đó, trong 8–15 lượt tiếp theo, hội thoại hoàn toàn là nối tiếp ở cuối (append-only), tận dụng 100% KV cache đã tính, giúp thời gian phản hồi chỉ mất 1–2 giây!

### 2.3. Bắt chính xác `done_reason == "length"` & Cơ chế Thử lại Khẩn cấp
- Trước khi kiểm tra `response.tool_calls`:
  ```python
  resp_meta = getattr(response, "response_metadata", {}) or {}
  is_length_overflow = (resp_meta.get("done_reason") == "length" or resp_meta.get("finish_reason") == "length")
  ```
- Nếu `is_length_overflow`:
  - Tuyệt đối **không** gán `stop = 'model_finished'`.
  - Nếu chưa từng thử lại khẩn cấp ở lượt này: Kích hoạt nén sâu khẩn cấp (Emergency Compaction xuống 50% budget) và gọi lại `model.ainvoke` 1 lần duy nhất.
  - Nếu đã thử lại mà vẫn tràn, hoặc cạn kiệt budget: Gán `stop = 'context_overflow'` và ghi nhận sự kiện vào `errors.jsonl`.
- Trong `runner.py`: Thêm `context_overflow` vào danh sách gây `interrupted` để bỏ qua chấm điểm các run bị nghẽn context, không tính điểm oan.

### 2.4. Tính toàn vẹn của Dữ liệu Nghiên cứu (Audit & Protocol Integrity)
- `messages` (gốc): Giữ nguyên vẹn 100% tất cả các turn thực tế từ đầu đến cuối và lưu vào `trajectory.json`. Các nhà nghiên cứu vẫn xem được trọn vẹn toàn bộ quá trình agent hành động.
- `request_messages`: Chỉ là bản sao (view) đã nén được chuyển cho `model.ainvoke`.
- Thêm `context_stats` vào metadata từng bước trong `trajectory.json` để theo dõi tỷ lệ nén.

### 2.5. Chỉnh lý Prompt Hành vi (`prompt/system_prompt.md`)
- Thêm điều cấm dùng `cat` trên toàn bộ file:
  > *"Inspect files economically: NEVER dump whole large files using `cat`. Use `head -n 50`, `tail -n 50`, `sed -n 'X,Yp'`, or `grep -n` to examine targeted regions."*
- Bắt buộc chuyển pha dứt khoát:
  > *"Move decisively from inspection to implementation within your first 5–8 turns. An agent that only inspects without writing code will fail the task."*
- Kiểm tra kết quả trước khi kết thúc:
  > *"Before finishing, run `git status` and `git diff` to verify that your modifications are present and minimal."*

---

## 3. Công thức Ngân sách Ngữ cảnh (Token Budgeting)

$$\text{input\_budget} = \text{num\_ctx} - \text{max\_output\_tokens} - \text{context\_safety\_tokens}$$

- **Cấu hình Saturn (Mặc định 64K Context)**:
  - `num_ctx`: 65.536
  - `max_output_tokens`: 4.096
  - `context_safety_tokens`: 4.096
  - $\rightarrow$ **`input_budget` = 57.344 tokens**
  - High Watermark (85%): ~48.700 tokens (kích hoạt nén)
  - Low Watermark (65%): ~37.200 tokens (mục tiêu sau nén)
- **Cấu hình Saturn (Tùy chọn 128K Context)**:
  - `num_ctx`: 131.072
  - `max_output_tokens`: 4.096
  - `context_safety_tokens`: 8.192
  - $\rightarrow$ **`input_budget` = 118.784 tokens**
- **Cấu hình Neptune (32K Context)**:
  - `num_ctx`: 32.768
  - `max_output_tokens`: 4.096
  - `context_safety_tokens`: 4.096
  - $\rightarrow$ **`input_budget` = 24.576 tokens**

---

## 4. Chi tiết Thay đổi theo từng File (File-by-File Changes)

### 4.1. Tạo mới: `src/deepswe_exp/components/agent/context.py`
Chứa toàn bộ thuật toán xử lý context:
1. `truncate_output(raw: str, max_chars: int = 4000, head_ratio: float = 0.6) -> str`:
   - Cắt chuỗi giữ 60% đầu + marker `\n[... TRUNCATED {omitted} CHARS ...]\n` + 40% đuôi.
2. `estimate_message_tokens(msg: BaseMessage) -> int`:
   - Ước lượng token nhanh và chính xác: `math.ceil(len(content) / 3.2)` cộng thêm overhead cho role và tool calls.
3. `group_atomic_turns(messages: list[BaseMessage]) -> list[tuple[AIMessage, list[ToolMessage]]]`:
   - Gom nhóm 1 `AIMessage` và danh sách các `ToolMessage` tương ứng với `tool_call_id`.
4. `fold_historical_tool_outputs(turns: list, keep_recent: int = 4) -> None`:
   - Thực hiện Tầng 2: Thay thế nội dung các `ToolMessage` cũ bằng bản tóm tắt ngắn.
5. `build_request_context(messages: list[BaseMessage], input_budget: int, high_watermark_ratio: float = 0.85, low_watermark_ratio: float = 0.65, min_recent_turns: int = 3) -> tuple[list[BaseMessage], dict]`:
   - Điều phối 4 tầng nén: kiểm tra ngưỡng, thực hiện nén mềm, nén cứng nếu cần, trả về danh sách `request_messages` sẵn sàng gửi model kèm thống kê `context_stats`.

### 4.2. Chỉnh sửa: `src/deepswe_exp/components/agent/agent.py`
1. **Áp dụng Tầng 1**: Tại dòng 90–91, thay thế phép cắt đơn giản `raw[:cap]` bằng `truncate_output(raw, cap, head_ratio=0.6)`.
2. **Áp dụng Tầng 2 & 3 trước khi gọi Model**:
   ```python
   request_messages, ctx_stats = build_request_context(
       messages,
       input_budget=settings.input_budget,
       min_recent_turns=3,
   )
   response = await asyncio.wait_for(
       model.ainvoke(request_messages, **invoke_kwargs),
       timeout=max(.01, deadline - clock()),
   )
   ```
3. **Bắt `done_reason == "length"`**:
   ```python
   resp_meta = getattr(response, "response_metadata", {}) or {}
   is_length = resp_meta.get("done_reason") == "length" or resp_meta.get("finish_reason") == "length"
   
   if is_length and not response.tool_calls:
       if not emergency_retry_done:
           emergency_retry_done = True
           # Thử lại 1 lần với ngữ cảnh được nén sâu
           continue
       stop = 'context_overflow'
       append_event(output_dir/'errors.jsonl', {'type': 'ContextOverflow', 'message': 'Ollama context window exhausted'})
       break
   
   if not response.tool_calls:
       stop = 'model_finished' if not response.invalid_tool_calls else 'format_error'
       break
   ```
4. **Ghi nhận `context_stats`**: Lưu vết trạng thái nén ngữ cảnh vào `trajectory.json`.

### 4.3. Chỉnh sửa: `src/deepswe_exp/components/configuration/settings.py`
1. Thêm thuộc tính `context_safety_tokens: int = 4096` vào `Settings`.
2. Bổ sung property `input_budget`:
   ```python
   @property
   def input_budget(self) -> int:
       if self.model_provider != "ollama":
           return 1_000_000  # Google Gemini có context 1M-2M tokens
       return max(1024, self.ollama_num_ctx - self.budget.get("max_output_tokens", 4096) - self.context_safety_tokens)
   ```
3. Nạp `context_safety_tokens` từ section `ollama` của profile YAML (mặc định 4096).

### 4.4. Chỉnh sửa: `src/deepswe_exp/components/execution/runner.py`
Tại dòng 204:
```python
if agent_result["stop_reason"] in (
    "provider_error", "quota_interrupted", "docker_error", "internal_error", "format_error", "context_overflow"
):
    run["status"] = "interrupted"
```
Đảm bảo các run bị gián đoạn do context được đánh dấu `interrupted`, bảo toàn tính chính xác của bảng kết quả.

### 4.5. Cập nhật file cấu hình
1. **`config/profiles/saturn.yaml`**:
   ```yaml
   ollama:
     num_ctx: 65536           # 64K điểm ngọt (có thể tăng lên 131072 nếu cần)
     context_safety_tokens: 4096
   ```
2. **`config/profiles/neptune.yaml`**:
   ```yaml
   ollama:
     num_ctx: 32768
     context_safety_tokens: 4096
   ```
3. **`config/study.saturn.yaml` & `config/study.neptune.yaml`**:
   ```yaml
   budget:
     max_tool_output_chars: 4000
   ```

### 4.6. Cập nhật `prompt/system_prompt.md`
Bổ sung các nguyên tắc rõ ràng vào Section 4:
- Không dùng `cat` trên file lớn; ưu tiên `grep -n`, `head`, `tail`, `sed`.
- Chuyển dứt khoát sang sửa file trong 5–8 lượt đầu.
- Luôn kiểm tra `git diff` trước khi hoàn tất nhiệm vụ.

---

## 5. Kế hoạch Kiểm thử Toàn diện (Unit Tests Plan)

Tạo file kiểm thử chuyên biệt: `tests/test_agent_context.py` (chạy hoàn toàn offline bằng mock, không yêu cầu GPU hay Ollama thật):
1. **`test_truncate_output_head_tail`**: Kiểm tra chuỗi dài > 4000 ký tự được cắt đúng 60% đầu và 40% đuôi, có marker thông báo chính xác số ký tự bị lược. Chuỗi $\le 4000$ ký tự giữ nguyên 100%.
2. **`test_atomic_turn_grouping`**: Kiểm tra `AIMessage` có 3 tool calls được gom nhóm chặt chẽ với đúng 3 `ToolMessage` tương ứng theo `tool_call_id`.
3. **`test_soft_compaction_historical_tools`**: Kiểm tra các `ToolMessage` của các lượt cũ được rút gọn nội dung thành công mà không làm mất cấu trúc `tool_call_id`.
4. **`test_hard_pruning_fifo`**: Đưa vào danh sách tin nhắn vượt quá budget; kiểm tra `SystemMessage` và `HumanMessage` đầu tiên luôn được bảo vệ, các lượt cũ nhất bị lược bỏ trước, các lượt mới nhất được giữ lại.
5. **`test_high_low_watermark`**: Kiểm tra cơ chế không nén khi dưới High Watermark, và khi vượt ngưỡng thì nén về đúng Low Watermark để bảo toàn tiền tố cho các lượt kế tiếp.
6. **`test_done_reason_length_handling`**: Giả lập mô hình trả về `done_reason: 'length'` và `tool_calls: []`. Đảm bảo `agent.py` gán `context_overflow`, không bao giờ gán nhầm `model_finished`.
7. **`test_google_provider_unaffected`**: Kiểm tra khi `model_provider == 'google'`, cơ chế không làm biến đổi thông điệp gửi đi.
8. **Kiểm tra hồi quy toàn diện**: Chạy toàn bộ test suite `.venv\Scripts\python.exe -m unittest discover tests` đảm bảo tất cả 42+ bài test đều PASS.

---

## 6. Tiêu chí Nghiệm thu (Definition of Done)

| Tiêu chí | Mục tiêu cụ thể | Kiểm tra xác nhận |
| :--- | :--- | :--- |
| **Độ bao phủ Test** | 100% unit tests pass trên Windows | Chạy `unittest discover tests` không có lỗi. |
| **An toàn VRAM** | VRAM tiêu thụ trên Saturn $< 35\text{ GB} / 48\text{ GB}$ | Kiểm tra bằng `nvidia-smi` khi chạy experiment. |
| **Không nghẽn Context** | Agent chạy vượt mốc 20 lượt mà không bị tràn context 32K | Kiểm tra `trajectory.json` của lượt chạy mới trên Saturn. |
| **Không báo sai `model_finished`** | Không có trường hợp `done_reason: 'length'` bị ghi nhận là hoàn thành | Kiểm tra trường `stop_reason` và `errors.jsonl`. |
| **Sinh Patch thực tế** | Agent thực hiện lệnh chỉnh sửa và sinh `agent_patch.diff` có dung lượng $> 0$ bytes | Kiểm tra file `agent_patch.diff` sau run. |
