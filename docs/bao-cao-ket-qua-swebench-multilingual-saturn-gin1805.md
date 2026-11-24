# Báo cáo kết quả thực nghiệm — `pilot-multilingual-gin-1805-v10-saturn-qwen38_20261124_212635`

- **Server / Profile:** `saturn` — vLLM (`Qwen/Qwen3.8-27B-FP8`, `--tensor-parallel-size 2` trên 2× NVIDIA RTX 6000 Ada 46GB, port `11436`, `num_ctx = 262144`)
- **Benchmark:** `SWE-bench Multilingual` (Go — thuần *bug fixing / program repair*)
- **Task:** `gin-gonic__gin-1805` (`role: smoke_test`, `seed: 42`, `attempt-001`)
- **Dữ liệu nguồn:** `runs/saturn_results/pilot-multilingual-gin-1805-v10-saturn-qwen38_20261124_212635/` (`summary/runs.csv`, `summary/summary.md`, `evaluation/result.json`, `visible-tests-result.json`, `agent_patch.diff`, `full_worktree.diff`, `commands.jsonl`)

---

## 1. Bảng tổng hợp kết quả 3 nhánh

| Chỉ số | Nhánh `0` | Nhánh `50-random` | Nhánh `100` |
|---|---:|---:|---:|
| Unit test khớp `^Test.*Router` được cấp | `0` (0%) | `7` (50%) | `14` (100%) |
| Trạng thái dừng của agent | `submitted` | `submitted` | `submitted` |
| **F2P (sửa đúng bug #1805)** | **1 / 1 (100%)** | **1 / 1 (100%)** | **1 / 1 (100%)** |
| **P2P (không phá code cũ)** | **13 / 13 (100%)** | **13 / 13 (100%)** | **13 / 13 (100%)** |
| **Visible tests (harness chấm lại)** | `not_applicable` (`0/0`) | **pass `7/7`** | **pass `14/14`** |
| **Official SWE-bench Eval / Reward** | **`pass` / 1** | **`pass` / 1** | **`pass` / 1** |
| Số model requests | **23** | 34 | 40 |
| Thời gian chạy | **829.6s (~13.8 phút)** | 1,661.2s (~27.7 phút) | 1,699.5s (~28.3 phút) |
| Input tokens | **809,757** | 981,384 | 1,803,342 |
| Output tokens | **35,302** | 72,899 | 74,003 |

**Tổng kết nhanh:**
- **Cả 3/3 nhánh đều PASS hoàn toàn (`reward = 1`, F2P `1/1`, P2P `13/13`)**, tổng thời gian chạy cả 3 nhánh là **~70 phút (~1.16 giờ)**.
- Toàn bộ pipeline mới (`SWE-bench Multilingual` + cắt test Go bằng AST + official SWE-bench harness + vLLM TP=2 auto-select GPU trên Saturn) **chạy sạch 100%, không lỗi hạ tầng**.

---

## 2. Phân tích chất lượng bản vá (`agent_patch.diff`) giữa 3 nhánh

### 2.1. Nhánh `0` và Nhánh `50-random`: Trùng khớp tuyệt đối với Reference Patch
Cả 2 nhánh đều tìm ra đúng nguyên nhân gốc trong `routergroup.go` và tạo ra bản vá **giống từng ký tự** với patch chuẩn của `gin-gonic/gin`:

```diff
--- a/routergroup.go
+++ b/routergroup.go
@@ -195,7 +195,7 @@ func (group *RouterGroup) createStaticHandler(relativePath string, fs http.FileS
 		// Check if file exists and/or if we have permission to access it
 		if _, err := fs.Open(file); err != nil {
 			c.Writer.WriteHeader(http.StatusNotFound)
-			c.handlers = group.engine.allNoRoute
+			c.handlers = group.engine.noRoute
 			// Reset index
 			c.index = -1
 			return
```

### 2.2. Nhánh `100`: Lời giải thay thế hợp lệ
Nhánh `100` không reset `c.index = -1` mà nối trực tiếp `group.engine.noRoute` vào sau handler hiện tại rồi gọi `serveError`:

```diff
--- a/routergroup.go
+++ b/routergroup.go
@@ -194,10 +194,8 @@ func (group *RouterGroup) createStaticHandler(relativePath string, fs http.FileS
 		file := c.Param("filepath")
 		// Check if file exists and/or if we have permission to access it
 		if _, err := fs.Open(file); err != nil {
-			c.Writer.WriteHeader(http.StatusNotFound)
-			c.handlers = group.engine.allNoRoute
-			// Reset index
-			c.index = -1
+			c.handlers = append(c.handlers[:c.index+1], group.engine.noRoute...)
+			serveError(c, http.StatusNotFound, default404Body)
 			return
 		}
```

Cách này cũng tránh chạy lại middleware trước đó và vượt qua 100% cả F2P (`1/1`), P2P (`13/13`) lẫn `visible-tests` (`14/14`).

---

## 3. Phân tích hành vi agent & hiệu năng

1. **Tương quan số test được cấp vs chi phí chạy:**
   - Nhánh `0` chạy nhanh nhất (`13.8` phút, `23` requests, `810K` input tokens), trong khi nhánh `50-random` (`27.7` phút, `34` req) và nhánh `100` (`28.3` phút, `40` req, `1.80M` input tokens) tốn thời gian gấp ~2 lần vì agent dành thêm nhiều lượt đọc `routes_test.go`, viết thêm test mới và chạy nhiều lệnh `go test` hơn.
2. **Không xuất hiện hành vi "sửa đề" (test-tampering):**
   - Khác với đợt DeepSWE `adaptix-name-mapping-aliases` trước đó, lần này ở nhánh `50-random` và `100`, agent chỉ **bổ sung test mới** (`staticfs_404_test.go` ở `50-random`, thêm `TestRouterGroupStaticFS404SingleLog` vào `routes_test.go` ở `100`) và không sửa/xóa test cũ để vượt qua kiểm thử.
3. **Giải thích chênh lệch nhỏ giữa `visible_test_cases = 14` và `p2p_total = 13` ở nhánh `100`:**
   - Biểu thức `^Test.*Router` khớp **14** hàm test Go có sẵn ở `base_commit`, trong đó `13` hàm nằm trong danh sách `PASS_TO_PASS` của `instance.json` (`agent_seen_total = 13`). Cả 14/14 visible tests đều pass.
4. **Hiệu ứng trần (*ceiling effect*) ở bài smoke test:**
   - Do bug `gin-1805` là lỗi 1 dòng khá rõ ràng và `Qwen/Qwen3.8-27B-FP8` giải tốt ngay cả ở nhánh `0`, cả 3 nhánh đều đạt `reward = 1` nên chưa xuất hiện cặp chuyển đổi pass/fail. Điều này là bình thường đối với task `smoke_test` (mục tiêu chính là xác nhận pipeline end-to-end hoạt động đúng).

---

## 4. Đề xuất bước tiếp theo

1. **Pipeline đã sẵn sàng mở rộng sang full study:** Cả Neptune (GPU 0, TP=1) và Saturn (2× RTX 6000 Ada, TP=2, auto-select GPU) đều đã chạy cùng 1 model `Qwen/Qwen3.8-27B-FP8` và cùng cấu hình budget.
2. **Tuyển thêm tập task chính thức (`role: experiment`):** Chọn thêm các task Go (hoặc ngôn ngữ khác đã có filter) trong `SWE-bench Multilingual` có độ phức tạp trung bình–khó (sửa nhiều hàm/file, nhiều P2P hơn) để đo rõ sự phân hóa giữa `100%`, `50-random`, và `0%` unit test.
3. **Lưu ý về `product_paths`:** Khi thêm các task mới, nên mở rộng `product_paths` ở cấp package/thư mục sản phẩm (hoặc toàn bộ file non-test) thay vì khóa cứng 1 file duy nhất để tránh vô tình loại bỏ các bản vá hợp lệ sửa sang file sản phẩm khác.
