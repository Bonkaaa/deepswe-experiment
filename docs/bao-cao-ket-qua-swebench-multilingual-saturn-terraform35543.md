# Báo cáo phân tích kết quả thực nghiệm — `pilot-multilingual-terraform-35543-v10-saturn-qwen38_20261125_155905`

- **Server / Profile:** `saturn` — vLLM (`Qwen/Qwen3.8-27B-FP8`, port `11436`, `num_ctx = 262144`)
- **Benchmark:** `SWE-bench Multilingual` (Go — *bug fixing / program repair*)
- **Task:** `hashicorp__terraform-35543` (`role: experiment`, `seed: 42`, `attempt-001`, `base_commit: 24833a2b062126a71ef30a86a6d1fc3823afdfdb`)
- **Budget cấu hình:** `max_model_requests = 200`, `wall_time_seconds = 7200`, `max_output_tokens = 24576`, `tool_timeout_seconds = 120`
- **Task options:** `test_package = "./internal/terraform"`, `test_pattern = "^TestContext2Plan_import"`, `product_paths = ["internal/terraform"]`
- **Dữ liệu nguồn:** `runs/saturn_results/pilot-multilingual-terraform-35543-v10-saturn-qwen38_20261125_155905/` (`summary/runs.csv`, `summary/summary.md`, `validation/validation.json`, `validation/reference/0/input.patch`, `hashicorp__terraform-35543/<branch>/42/attempt-001/{run.json,agent_result.json,agent_patch.diff,full_worktree.diff,visible-tests-result.json,evaluation/result.json,commands.jsonl,trajectory.json}`)

---

## 1. Bảng tổng hợp kết quả 3 nhánh

| Chỉ số | Nhánh `0` | Nhánh `50-random` | Nhánh `100` |
|---|---:|---:|---:|
| Unit test khớp `^TestContext2Plan_import` được cấp | `0 / 29` (0.0%) | `14 / 29` (48.3%) | `29 / 29` (100.0%) |
| Trạng thái dừng của agent | `request_budget` | `submitted` | `format_error` (`RepeatedFormatError`) |
| Số model requests | `200 / 200` | `189 / 200` | `50 / 200` |
| Thời gian chạy | `3,565.6s` (~59.4 phút) | `4,086.7s` (~68.1 phút) | `1,281.5s` (~21.4 phút) |
| Input tokens (tổng / peak mỗi req) | `15,195,612` / `140,919` | `12,236,974` / `112,330` | `1,482,897` / `58,709` |
| Output tokens (tổng / chạm trần 24,576) | `97,562` / `1 lần` (req 148) | `108,229` / `2 lần` (req 123, 153) | `50,374` / `0 lần` (peak 10,261) |
| Số lệnh bash / số lần chạy `go test` | `220` / `35` | `193` / `39` | `58` / `0` |
| Kích thước `agent_patch.diff` / `full_worktree.diff` | `6,802 B` / `10,484 B` | `3,560 B` / `8,777 B` | `0 B` / `0 B` |
| **Visible tests (harness chấm trên `base_commit`)** | `not_applicable` (`0/0`) | **pass `14 / 14`** | **pass `29 / 29`** |
| **F2P (Official SWE-bench Eval)** | **`6 / 6` (100%)** | `3 / 6` (50%) | `0 / 6` (0%) |
| **P2P (Official SWE-bench Eval)** | `19 / 29` (65.5%) | `19 / 29` (65.5%) | **`29 / 29` (100%)** |
| **Agent-seen pass/fail (theo official eval)** | `0 / 0` | `12 pass / 2 fail` | `23 pass / 6 fail` |
| `seen_pass_but_eval_fail` | `False` | **`True`** | **`True`** |
| **Official SWE-bench Eval / Reward** | **`fail_test` / `0`** | **`fail_test` / `0`** | **`fail_test` / `0`** |
``
**Tổng kết nhanh:**
- **Hạ tầng & validation ổn định 100%:** `validation.json` xác nhận `stable = true` (3/3 lần chạy `base` đều cho F2P `0/6`, P2P `29/29`; 3/3 lần chạy `reference` đều `pass`, F2P `6/6`, P2P `29/29`). Tổng thời gian chạy cả 3 nhánh là **8,933.8 giây (~2.48 giờ)**.
- **Cả 3/3 nhánh đều `fail_test` (`reward = 0`), nhưng vì 3 nguyên nhân hoàn toàn khác nhau:**
  1. **Nhánh `0`:** Giải được **100% F2P (`6/6`)**, nhưng phá **10/29 P2P** do sai thiết kế ở 2 chỗ (*config generation* và *`for_each` trong `import`*), và cạn ngân sách 200 request đúng lúc đang chạy vòng kiểm thử cuối.
  2. **Nhánh `50-random`:** Hoàn thành và `submitted`, pass `14/14` visible tests, nhưng chỉ đạt **F2P `3/6`** (bỏ sót kiểm tra self-reference) và phá **đúng 10 P2P giống hệt Nhánh `0`**.
  3. **Nhánh `100`:** Dừng sớm ở request 50 do **`RepeatedFormatError`** ngay trong giai đoạn đọc code (chưa sửa dòng nào, chưa chạy `go test` lần nào) → patch rỗng (`0 B`), điểm số trùng khớp `base_commit` (`F2P 0/6`, `P2P 29/29`).

---

## 2. Bài toán `hashicorp__terraform-35543` & Độ khó so với Reference Patch

### 2.1. Mô tả lỗi
Người dùng báo cáo `terraform validate` trả về `Success! The configuration is valid.` ngay cả khi các khối `import {}` chứa:
- `id = local.something_not_defined` (local chưa khai báo)
- `id = var.some_other_undefined_thing` (biến chưa khai báo)
- `to = null_resource.not-a-real-resource` (resource đích không tồn tại trong cấu hình)

### 2.2. Kiến trúc của Reference Patch (`validation/reference/0/input.patch`)
Reference patch dài **535 dòng**, sửa **8 file** trong `internal/terraform/`:
- `context_validate.go`: Truyền `ImportTargets: c.findImportTargets(config)` vào `PlanGraphBuilder` (khi `Operation: walkValidate`). **Không** thêm trường `GenerateConfigPath` vào `ValidateOpts`.
- `transform_config.go`:
  - Trong `transformSingle`: Các `importTargets` không có resource cấu hình tương ứng vẫn được thêm vào graph dưới dạng `NodeAbstractResource` (với `Config == nil`) để node validation tự kiểm tra.
  - Trong `validateImportTargets`: Chỉ kiểm tra `moduleCfg == nil` (module đích không tồn tại), **nhường** kiểm tra resource đích chưa có config cho `validateConfigGen()` (ở bước validate) và `validateExpandedImportTargets()` (ở bước plan).
- `node_resource_validate.go`:
  - Chạy `n.validateImportTargets(ctx)` **trước** kiểm tra `if n.Config == nil { return diags }`.
  - Gọi `n.validateConfigGen(ctx)`: Khi `n.Config == nil`, chỉ báo lỗi nếu resource nằm ngoài root module, hoặc dùng `for_each`/keyed address (không tương thích `-generate-config-out`). **Cho phép** single-instance import ở root module đi qua `Validate()` vì `terraform validate` không biết người dùng có chạy `plan -generate-config-out` sau đó hay không.
  - Gọi `validateImportSelfRef(n.Addr.Resource, imp.Config.ID)` để bắt tự tham chiếu.
  - Nếu `imp.Config.ForEach != nil`: Đánh giá `for_each` trước bằng `newForEachEvaluator(...).ImportValues()` rồi duyệt từng `keyData` (`instances.RepetitionData`) để evaluate `imp.Config.To` và `imp.Config.ID` trong đúng ngữ cảnh `each.key` / `each.value`.
  - Kiểm tra khớp chế độ mở rộng (`count` / `for_each` vs key của `to`) qua `validateImportTargetExpansion`.
- `node_resource_plan.go`, `eval_import.go`, `eval_for_each.go`, `graph_builder_plan.go`, `transform_attach_config_resource.go`: Chuyển báo lỗi thiếu config (khi không có `-generate-config-out`) sang `validateExpandedImportTargets` lúc Plan, tránh panic khi `for_each` không phải collection, và bỏ kiểm tra trùng lặp `validateImportSelfRef`.

> **Nhận xét:** Khác hoàn toàn với bài smoke test `gin-1805` (sửa 1 dòng), `terraform-35543` đòi hỏi hiểu sâu tương tác giữa `Validate` graph, `Plan` graph, `ConfigTransformer`, `-generate-config-out`, và cơ chế `for_each` của Terraform.

---

## 3. Phân tích nguyên nhân gốc khiến từng nhánh thất bại

### 3.1. Vì sao cả Nhánh `0` và Nhánh `50-random` cùng phá ĐÚNG 10 P2P tests?

Danh sách 10 P2P entries bị fail giống hệt nhau ở cả Nhánh `0` và Nhánh `50-random` chia thành **2 nhóm lỗi thiết kế rõ rệt**:

#### Nhóm A — Phá 6 P2P entries liên quan đến Config Generation (`-generate-config-out`)
- `TestContext2Plan_importIntoModuleWithGeneratedConfig`
- `TestContext2Plan_importResourceConfigGen` và `TestContext2Plan_importResourceConfigGen/test_object.a`
- `TestContext2Plan_importResourceConfigGenWithAlias` và `TestContext2Plan_importResourceConfigGenWithAlias/test_object.a`
- `TestContext2Plan_importWithSensitives` *(test này cũng đặt `GenerateConfigPath`)*

**Nguyên nhân gốc:**
Cả Nhánh `0` và Nhánh `50-random` đều tự thiết kế thêm field `ValidateOpts.GenerateConfigPath string`, rồi trong `Context.Validate()` coi mọi `import` block không có resource cấu hình là **lỗi ngay lập tức** khi `opts.GenerateConfigPath == ""`.
- Dù cả 2 nhánh đều đã truyền `op.GenerateConfigOut` từ `internal/backend/local/backend_local.go` (file này sau đó còn bị lọc khỏi `agent_patch.diff` do `product_paths = ["internal/terraform"]`), trong bộ test đánh giá chính thức (`context_plan_import_test.go` sau khi áp `test_patch`), các test gọi trực tiếp `c.Validate(m, nil)` — tức `opts == nil` → `opts.GenerateConfigPath == ""`!
- Kết quả: mọi test config generation đều bị `c.Validate()` chặn với lỗi `"Configuration for import target does not exist"`, trong khi Reference Patch cho phép single-instance root import không có config đi qua `Validate()` và chỉ báo lỗi ở pha `Plan()` nếu không truyền `-generate-config-out`.

#### Nhóm B — Phá 4 P2P tests sử dụng `for_each` trong khối `import`
- `TestContext2Plan_importForEach`
- `TestContext2Plan_importForEachmodule`
- `TestContext2Plan_importForEachPartial`
- `TestContext2Plan_importForEachFromData`

**Nguyên nhân gốc:**
Khi khối `import` có `for_each`, biểu thức `id` thường tham chiếu `each.key` hoặc `each.value`.
- **Nhánh `0`** gọi `evaluateImportIdExpression(importTarget.Config.ID, target, ctx, EvalDataForNoInstanceKey, true)` **vô điều kiện** bằng `EvalDataForNoInstanceKey`, rồi mới đánh giá `ForEach` độc lập phía sau.
- **Nhánh `50-random`** cũng gọi `n.evaluateExpr(ctx, imp.Config.ID, cty.String, nil, EvalDataForNoInstanceKey)` bằng `EvalDataForNoInstanceKey`.
- Do không tạo `RepetitionData` từ `newForEachEvaluator(...).ImportValues()` trước khi đánh giá `id`, mọi biểu thức `id` dùng `each.key`/`each.value` đều báo lỗi khi `Validate()` chạy!

---

### 3.2. Vì sao Nhánh `0` đạt F2P `6/6` (100%) còn Nhánh `50-random` chỉ đạt F2P `3/6` (50%)?

| F2P Test | Nhánh `0` | Nhánh `50-random` | Nhánh `100` |
|---|---|---|---|
| `TestContext2Plan_importResourceConfigGenExpandedResource` | PASS | PASS | FAIL |
| `TestContext2Plan_importGenerateNone` | PASS | PASS | FAIL |
| `TestContext2Plan_importSelfReferenceInModule` | PASS | PASS | FAIL |
| `TestContext2Plan_importSelfReference` | **PASS** | **FAIL** | FAIL |
| `TestContext2Plan_importSelfReferenceInstanceRef` | **PASS** | **FAIL** | FAIL |
| `TestContext2Plan_importSelfReferenceInst` | **PASS** | **FAIL** | FAIL |

**Nguyên nhân chênh lệch F2P:**
- **Nhánh `0`** tái sử dụng hàm có sẵn `evaluateImportIdExpression(...)` trong `eval_import.go`. Hàm này bên trong đã gọi sẵn `validateImportSelfRef(target.Resource.Resource, expr)` nên **tự động bắt được cả 4 test self-reference** → đạt **F2P `6/6`**.
- **Nhánh `50-random`** tự gọi thẳng `n.evaluateExpr(ctx, imp.Config.ID, cty.String, nil, EvalDataForNoInstanceKey)`, **bỏ qua hoàn toàn `evaluateImportIdExpression` và `validateImportSelfRef`** → bỏ lỡ 3 test self-reference ở root module (`importSelfReference`, `importSelfReferenceInstanceRef`, `importSelfReferenceInst`).

---

### 3.3. Vì sao Nhánh `100` dừng sớm ở request 50 với `RepeatedFormatError`?

Kiểm tra `100/42/attempt-001/trajectory.json` và `commands.jsonl`:
- Trong 50 requests (~21.4 phút, 58 lệnh bash), agent mới chỉ đang ở giai đoạn **đọc và tìm hiểu mã nguồn** (`go test` = `0` lần, `agent_patch.diff` = `0 B`, `full_worktree.diff` = `0 B`).
- Từ request 43 trở đi, độ dài output của model tăng vọt bất thường (`10,261`, `4,606`, `7,513`, `4,095`, `4,760` tokens) và model liên tục sinh phản hồi **không kèm tool call `bash`** (các thông báo lỗi format ở message 99, 100, 104, và sau đó **3 lần liên tiếp** ở message 107, 108, 109).
- Harness nhận diện 3 lỗi format liên tiếp và dừng agent với `RepeatedFormatError` (`stop_reason = format_error`).
- Do chưa có chỉnh sửa nào trong worktree, bước đánh giá chạy trên mã gốc `base_commit` → F2P `0/6`, P2P `29/29`.

---

### 3.4. Vì sao Nhánh `0` chạm `request_budget` (200/200) và cả 2 nhánh `0` & `50-random` đều có request chạm trần `24,576` output tokens?

- **Nhánh `0` (200 requests, 59.4 phút, 15.20M input tokens):** Agent tốn nhiều vòng để tự tái hiện bug bằng binary `/tmp/terraform` và chạy `go test ./internal/command/` (nhiều lệnh chạm `tool_timeout_seconds = 120`, exit code `124`). Request 148 chạm trần `max_output_tokens = 24,576`, sau đó phục hồi được nhưng đến request 200 thì hết ngân sách đúng ngay sau khi chạy xong `go test ./internal/terraform/ ./internal/backend/local/` (`ok`), chưa kịp gọi lệnh `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. Dù vậy, toàn bộ bản vá đã nằm trong worktree nên vẫn được trích xuất và chấm điểm đầy đủ.
- **Nhánh `50-random` (189 requests, 68.1 phút, 12.24M input tokens):** Chạm trần `24,576` output tokens **2 lần** (request 123 và 153), tự phục hồi được và `submitted` ở request 189.

---

## 4. Hiện tượng quan trọng về phương pháp thực nghiệm cần lưu ý

### 4.1. Giải mã `seen_pass_but_eval_fail = True` (`visible-tests` pass 100% nhưng Official Eval vẫn fail chính các test đó)

Đây là phát hiện rất đáng giá cho thiết kế thực nghiệm:
- Trong `base_commit`, file `internal/terraform/context_plan_import_test.go` có **30 lệnh gọi `.Plan(...)`** và **0 lệnh gọi `.Validate(...)`**.
- Toàn bộ 29 test khớp `^TestContext2Plan_import` (bao gồm cả **6 test F2P**!) **đều đã tồn tại sẵn tên hàm** trong `base_commit` và đều pass ở `base_commit` vì chúng chưa gọi `c.Validate()`.
- Khi official SWE-bench harness chấm điểm, nó áp `test_patch` — sửa `context_plan_import_test.go` để **bổ sung gọi `c.Validate(...)`** vào chính các hàm `TestContext2Plan_import*` đó.
- Hệ quả trực tiếp:
  1. Ở **Nhánh `50-random`**: Agent nhìn thấy 14 test gốc (chưa có `c.Validate()`), chạy `go test ./internal/terraform/` thấy **pass 14/14** (`visible-tests-result.json = 14/14`), nên hoàn toàn không nhận ra patch của mình phá `TestContext2Plan_importResourceConfigGenWithAlias` và `TestContext2Plan_importWithSensitives` khi official eval thêm `c.Validate()` (`agent_seen_passed = 12`, `agent_seen_failed = 2`).
  2. Ở **Nhánh `100`**: Dù patch rỗng (`0 B`), `visible-tests` pass `29/29`, nhưng trên official eval thì 6 test F2P (vốn nằm trong 29 test được cấp) bị fail → `agent_seen_passed = 23`, `agent_seen_failed = 6`.

### 4.2. Tác động của `product_paths = ["internal/terraform"]`
Cả Nhánh `0` và Nhánh `50-random` đều sửa thêm file `internal/backend/local/backend_local.go` (chỉ ~3 dòng để truyền `op.GenerateConfigOut` vào `ValidateOpts`). Do `product_paths` chỉ liệt kê `["internal/terraform"]`, thay đổi ở `internal/backend/local/backend_local.go` đã bị cắt khỏi `agent_patch.diff` trước khi gửi vào harness.
- Trong riêng task `terraform-35543`, việc cắt này **không làm thay đổi kết quả chấm** (vì Reference Patch chỉ sửa `internal/terraform/` và test official gọi trực tiếp `c.Validate(m, nil)` không đi qua `backend/local`).
- Tuy nhiên, đây là minh chứng thực tế xác nhận lại rủi ro của việc siết `product_paths` quá hẹp nếu có task mà lời giải thay thế hợp lệ chạm sang package khác (như `internal/backend/local` hay `internal/command`).

### 4.3. Không có hành vi "sửa đề" (*test-tampering*)
Kiểm tra `full_worktree.diff` của cả 3 nhánh:
- Nhánh `0` và `50-random` chỉ **thêm mới** các test `TestValidateImportBlock*` trong `internal/command/validate_test.go` và các fixture mới trong `internal/command/testdata/validate-*/`.
- Không nhánh nào sửa hoặc xóa các test hiện có trong `internal/terraform/context_plan_import_test.go`.

---

## 5. So sánh nhanh với lượt chạy `gin-1805` & Đề xuất bước tiếp theo

| Tiêu chí | `gin-gonic__gin-1805` (smoke test) | `hashicorp__terraform-35543` (experiment) |
|---|---|---|
| Độ lớn Reference Patch | 1 dòng / 1 file | 535 dòng / 8 file |
| Kết quả Reward (`0` / `50-random` / `100`) | `1` / `1` / `1` (*ceiling effect*) | `0` / `0` / `0` (*floor effect*) |
| F2P (`0` / `50-random` / `100`) | `1/1` / `1/1` / `1/1` | **`6/6` / `3/6` / `0/6`** |
| P2P (`0` / `50-random` / `100`) | `13/13` / `13/13` / `13/13` | `19/29` / `19/29` / `29/29` |
| Tổng thời gian 3 nhánh | ~1.16 giờ | ~2.48 giờ |
| Vấn đề hành vi agent | Không có | `RepeatedFormatError` (nhánh `100`), `request_budget` (nhánh `0`), chạm trần 24K output tokens (3 lần) |

### Đề xuất hành động
1. **Điều tra & gia cố chống `RepeatedFormatError` cho `Qwen3.8-27B-FP8`:** Nhánh `100` mất trắng ở request 50 khi mới đọc code do model sinh phản hồi dài không có tool call 3 lần liên tiếp. Cần kiểm tra cấu hình tool-call parser trên vLLM (`--tool-call-parser`) hoặc cơ chế nhắc lại/cắt reasoning dài trong mini-swe-agent trước khi chạy batch lớn (để tránh mất dữ liệu nhánh `100`).
2. **Phân biệt rõ task có `test_patch` sửa test cũ vs thêm test mới:** Với `terraform-35543`, 29 test được cấp ở `base_commit` không gọi `c.Validate()` nên tín hiệu unit test được cấp cho agent (ở `50-random` và `100`) yếu hơn kỳ vọng — agent chỉ thấy `Plan` tests, không thấy `Validate` tests. Khi phân tích kết quả Thực nghiệm A trên nhiều task, nên ghi nhận thuộc tính này như một biến kiểm soát (*confounder*).
3. **Cân bằng độ khó tập task cho study chính thức:** `gin-1805` quá dễ (3/3 pass), còn `terraform-35543` rất khó về mặt kiến trúc (0/3 pass, tốn ~2.5 giờ/task). Nên tuyển đa dạng task ở mức độ trung bình–khó (patch chuẩn ~20–150 dòng) để vừa tránh *ceiling effect* vừa tránh *floor effect* khi so sánh `100%` vs `50-random` vs `0%`.
4. **Mở rộng `product_paths` cho các task mới:** Nên đặt `product_paths` bao quát thư mục mã nguồn (ví dụ `["internal"]` cho Terraform thay vì chỉ `["internal/terraform"]`) để không vô tình cắt bỏ thay đổi sản phẩm hợp lệ ở các package liên quan.
