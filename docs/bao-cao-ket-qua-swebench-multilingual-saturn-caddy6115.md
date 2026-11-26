# Báo cáo phân tích kết quả thực nghiệm — `pilot-multilingual-caddy-6115-v10-saturn-qwen38_20261126_011234`

- **Server / Profile:** `saturn` — vLLM (`Qwen/Qwen3.8-27B-FP8`, port `11436`, `num_ctx = 262144`)
- **Benchmark:** `SWE-bench Multilingual` (Go — *bug fixing / program repair*)
- **Task:** `caddyserver__caddy-6115` (`role: experiment`, `seed: 42`, `attempt-001`)
- **Mô tả bài toán:** *Sticky cookie should be Secure and SameSite=None by default* (`modules/caddyhttp/reverseproxy/selectionpolicies.go`)
- **Budget cấu hình:** `max_model_requests = 200`, `wall_time_seconds = 7200`, `max_output_tokens = 24576`, `tool_timeout_seconds = 120`
- **Task options:** `test_package = "./modules/caddyhttp/reverseproxy"`, `test_pattern = "^Test"`, `product_paths = ["modules/caddyhttp/reverseproxy"]`
- **Dữ liệu nguồn:** `runs/saturn_results/pilot-multilingual-caddy-6115-v10-saturn-qwen38_20261126_011234/` (`summary/runs.csv`, `summary/summary.md`, `validation/validation.json`, `validation/reference/0/input.patch`, `caddyserver__caddy-6115/<branch>/42/attempt-001/{run.json,agent_result.json,agent_patch.diff,full_worktree.diff,visible-tests-result.json,evaluation/result.json,commands.jsonl}`)

---

## 1. Bảng tổng hợp kết quả 3 nhánh

| Chỉ số | Nhánh `0` | Nhánh `50-random` | Nhánh `100` |
|---|---:|---:|---:|
| Unit test khớp `^Test` được cấp | `0 / 16` (0%) | `8 / 16` (50%) | `16 / 16` (100%) |
| Trạng thái dừng của agent | `submitted` | `submitted` | `submitted` |
| Số model requests | `40` | **`24`** | `30` |
| Thời gian chạy | `1,224.8s` (~20.4 phút) | **`265.9s` (~4.4 phút)** | `338.7s` (~5.6 phút) |
| Input tokens (tổng / peak mỗi req) | `1,416,960` / `60,435` | **`404,532` / `25,541`** | `593,707` / `31,688` |
| Output tokens (tổng / peak mỗi req) | `51,259` / `8,436` | **`9,731` / `1,684`** | `11,718` / `1,446` |
| Chạm trần `24,576` output tokens | `0 lần` | `0 lần` | `0 lần` |
| Số lệnh bash / số lần chạy `go test` | `47` / `11` | **`28` / `7`** | `37` / `9` |
| Kích thước `agent_patch.diff` / `full_worktree.diff` | `1,254 B` / `1,254 B` | **`657 B`** / `2,872 B` | **`657 B`** / `1,768 B` |
| **Visible tests (harness chấm lại)** | `not_applicable` (`0/0`) | **pass `8 / 8`** | **pass `16 / 16`** |
| **Agent-seen pass/fail (official eval)** | `0 / 0` | **8 pass / 0 fail** | **16 pass / 0 fail** |
| `seen_pass_but_eval_fail` | `False` | `False` | `False` |
| **F2P (Official SWE-bench Eval)** | **`1 / 1` (100%)** | **`1 / 1` (100%)** | **`1 / 1` (100%)** |
| **P2P (Official SWE-bench Eval)** | **`16 / 16` (100%)** | **`16 / 16` (100%)** | **`16 / 16` (100%)** |
| **Official SWE-bench Eval / Reward** | **`pass` / `1`** | **`pass` / `1`** | **`pass` / `1`** |

**Tổng kết nhanh:**
- **Chạy sạch 100%, không bất kỳ sự cố kỹ thuật nào:** `validation.json` xác nhận `valid = true, stable = true`. Cả 3/3 nhánh đều `submitted` sạch sẽ, không dính `RepeatedFormatError`, không chạm `request_budget`, không chạm trần `24,576` output tokens. Tổng thời gian cả 3 nhánh chỉ **~30.5 phút**.
- **Cả 3/3 nhánh đều PASS hoàn toàn (`reward = 1`, F2P `1/1`, P2P `16/16`)**, và `Visible tests` trùng khớp 100% với `Agent-seen` (không còn hiện tượng lệch như `terraform-35543`).

---

## 2. Phân tích sâu các bản vá (`agent_patch.diff`) so với Reference Patch

### 2.1. Reference Patch (`validation/reference/0/input.patch`)
Patch chuẩn của Caddy xử lý kỹ 2 chi tiết quan trọng:
1. Kiểm tra HTTPS theo **cả 2 nguồn**: kết nối TLS trực tiếp (`req.TLS != nil`) **hoặc** đi qua trusted proxy (`caddyhttp.TrustedProxyVarKey`) có header `X-Forwarded-Proto: https`.
2. **Chỉ bật `SameSite = http.SameSiteNoneMode` khi `Secure == true`** (vì theo spec trình duyệt hiện đại, `SameSite=None` bắt buộc đi kèm `Secure`; với HTTP thường thì giữ `Secure: false` và không set `SameSite=None`).

```go
cookie := &http.Cookie{
	Name:   s.Name,
	Value:  sha,
	Path:   "/",
	Secure: false,
}
isProxyHttps := false
if trusted, ok := caddyhttp.GetVar(req.Context(), caddyhttp.TrustedProxyVarKey).(bool); ok && trusted {
	xfp, xfpOk, _ := lastHeaderValue(req.Header, "X-Forwarded-Proto")
	isProxyHttps = xfpOk && xfp == "https"
}
if req.TLS != nil || isProxyHttps {
	cookie.Secure = true
	cookie.SameSite = http.SameSiteNoneMode
}
http.SetCookie(w, cookie)
```

### 2.2. Nhánh `50-random` và Nhánh `100`: Gọn gàng, giống nhau từng ký tự (`657 B`)
Cả 2 nhánh đều tạo ra bản vá giống hệt nhau, chỉ sửa đúng 2 dòng trong `CookieHashSelection.Select()`:

```diff
 		http.SetCookie(w, &http.Cookie{
-			Name:   s.Name,
-			Value:  sha,
-			Path:   "/",
-			Secure: false,
+			Name:     s.Name,
+			Value:    sha,
+			Path:     "/",
+			Secure:   req.TLS != nil,
+			SameSite: http.SameSiteNoneMode,
 		})
```

### 2.3. Nhánh `0`: "Sáng tạo thừa" (*over-engineering / scope creep*, `1,254 B`)
Nhánh `0` ngoài việc sửa `Select()` còn **tự ý thêm một trường public mới** vào struct cấu hình `CookieHashSelection`:

```diff
 type CookieHashSelection struct {
 	Name string `json:"name,omitempty"`
 	// Secret to hash (Hmac256) chosen upstream in cookie
 	Secret string `json:"secret,omitempty"`
+	// SameSite mode for the cookie.
+	SameSite http.SameSite `json:"samesite,omitempty"`
```

Và bên trong `Select()`:
```go
cookie := &http.Cookie{
	Name:   s.Name,
	Value:  sha,
	Path:   "/",
	Secure: req.TLS != nil,
}
if s.SameSite != 0 {
	cookie.SameSite = s.SameSite
} else {
	cookie.SameSite = http.SameSiteNoneMode
}
```

Đây là thay đổi API/JSON schema **không được yêu cầu** trong đề bài (và không kèm Caddyfile parser hay unit test).

---

## 3. Các insight quan trọng thu được từ lượt chạy này

### Insight 1 — Đảo ngược hoàn toàn xu hướng chi phí: Nhánh `0` chậm gấp 4.6 lần và tốn gấp 5.3 lần output tokens so với Nhánh `50-random`
- Ở `gin-1805`, Nhánh `0` từng nhanh nhất (13.8 phút vs ~28 phút).
- Nhưng ở **`caddy-6115`**, kết quả **đảo chiều mạnh**:
  - **Nhánh `50-random` nhanh và tiết kiệm nhất:** `4.4 phút`, `24` req, `404.5K` input tokens, `9.7K` output tokens.
  - **Nhánh `100` đứng thứ hai:** `5.6 phút`, `30` req, `593.7K` input tokens, `11.7K` output tokens.
  - **Nhánh `0` chậm và tốn kém nhất:** `20.4 phút` (**gấp 4.6×** `50-random`), `40` req, `1.42M` input tokens (**gấp 3.5×**), `51.3K` output tokens (**gấp 5.3×**).
- **Ý nghĩa:** Khi không có sẵn unit test làm điểm neo (*grounding*), agent không hẳn lúc nào cũng "nhanh hơn" — ở bài cần tìm hiểu cách module hoạt động như `caddy-6115`, việc thiếu file test mẫu khiến agent phải đọc rộng hơn và suy luận dài hơn nhiều (`max_out = 8,436` tokens/req so với `~1,500` ở 2 nhánh có test).

### Insight 2 — Mặt trái của "tự do sáng tạo" ở Nhánh `0`: Scope creep & không viết test
- Trả lời trực tiếp câu hỏi ở lượt trước về việc *"không có unit test giúp agent sáng tạo hơn hay không"*: ở `caddy-6115`, **không có unit test dẫn đến "sáng tạo thừa" (*over-engineering*)**:
  - Nhánh `0` tự thêm field public `SameSite http.SameSite` vào struct `CookieHashSelection`, làm phình patch gấp đôi (`1,254 B` vs `657 B`).
  - Nhánh `0` cũng **không viết thêm bất kỳ unit test nào** (`full_worktree.diff == agent_patch.diff`), trong khi Nhánh `50-random` tự viết hẳn hàm `TestCookieHashPolicy` mới (test cả HTTPS lẫn HTTP) và Nhánh `100` bổ sung assertion kiểm tra `SameSite` vào 2 test hiện có (không sửa đề / không test-tampering).
- Ngược lại, **Nhánh `50-random` đạt điểm cân bằng tối ưu (*sweet spot*)**: vừa có đủ test mẫu để hiểu nhanh convention của package (ít nhiễu hơn `100%`), vừa tạo patch tối giản nhất và chạy nhanh nhất.

### Insight 3 — "Pass nhưng chưa trọn vẹn" (*under-specified benchmark tests*)
- Dù cả 3 nhánh đều đạt **Reward = `1`** (`F2P 1/1`, `P2P 16/16`), so sánh trực tiếp với Reference Patch cho thấy **cả 3 nhánh đều thiếu 2 điều kiện biên thực tế**:
  1. Không xử lý trường hợp đứng sau reverse proxy (`TrustedProxyVarKey` + `X-Forwarded-Proto: https`).
  2. Set `SameSite: http.SameSiteNoneMode` kể cả trên request HTTP thường (`Secure: false`), dù đề bài không nêu rõ chi tiết proxy và bộ test F2P chính thức (`TestCookieHashPolicyWithSecureRequest`) chỉ kiểm tra ca `req.TLS != nil`.
- Đây là một quan sát quan trọng về **giới hạn của điểm nhị phân `reward`** trong SWE-bench: điểm `pass` chỉ đảm bảo đạt bộ test hiện có, không đồng nghĩa bản vá đã bao phủ 100% logic của Reference Patch.

### Insight 4 — Xác nhận giá trị của tiêu chí chọn task "sạch"
- Khác với `terraform-35543` (`test_patch` sửa ruột test cũ gây `seen_pass_but_eval_fail = True`), ở `caddy-6115` (`test_patch +54/-0` chỉ thêm test mới):
  - `Visible tests` khớp 100% với `Agent-seen` (`8/8` và `16/16`).
  - `product_paths = ["modules/caddyhttp/reverseproxy"]` bao trọn toàn bộ sửa đổi sản phẩm.
  - Không có hành vi test-tampering.

---

## 4. Bức tranh tổng hợp sau 3 task pilot (Go — `Qwen/Qwen3.8-27B-FP8` trên Saturn)

| Task | Patch chuẩn (File / Churn) | Reward (`0` / `50` / `100`) | F2P (`0` / `50` / `100`) | Thời gian (`0` / `50` / `100`) | Nhánh hiệu quả nhất | Ghi chú nổi bật |
|---|---:|---|---|---|---|---|
| `gin-gonic__gin-1805` | 1 / 2 dòng | `1` / `1` / `1` | `1/1` / `1/1` / `1/1` | `13.8m` / `27.7m` / `28.3m` | `0` (nhanh nhất) | Dễ (ceiling effect) |
| `caddyserver__caddy-6115` | 1 / 14 dòng | `1` / `1` / `1` | `1/1` / `1/1` / `1/1` | `20.4m` / **`4.4m`** / `5.6m` | **`50-random`** (nhanh & gọn nhất) | Nhánh `0` chậm 4.6× & over-engineer |
| `hashicorp__terraform-35543` | 8 / 377 dòng | `0` / `0` / `0` | `6/6` / `3/6` / `0/6` | `59.4m` / `68.1m` / `21.4m`* | `0` (F2P cao nhất) | Rất khó (floor effect) + `100` dính `format_error` |

---

## 5. Đề xuất bước tiếp theo

Để bắt được **cặp chuyển đổi `pass / fail` thực sự giữa 3 nhánh** (tránh cả *ceiling effect* như `gin-1805`, `caddy-6115` lẫn *floor effect* như `terraform-35543`), nên dịch lên nấc **30–90 dòng churn** với các task Go xác định (không dính concurrency/race như `prometheus-15142`):
1. **`gohugoio__hugo-12171`** (1 file, **35 dòng** churn, 12 P2P, `test_patch +30/-0` sạch).
2. **`gin-gonic__gin-1957`** (3 file Go, **83 dòng** churn, 13 P2P, `test_patch +69/-0` sạch).
3. **`hashicorp__terraform-34900`** (3 file, **83 dòng** churn, 15 P2P `Sensitive`, `test_patch +144/-1` sạch — chỉ cần sửa 1 dòng trong `scripts/add_swebench_task.py`).
