# Kế hoạch thực nghiệm về patch overfitting trên DeepSWE v1.1

## 1. Mục tiêu và câu hỏi nghiên cứu

- **RQ1.** Khi giảm số test có sẵn mà agent được đọc và chạy, tỷ lệ patch pass bộ test ẩn thay đổi thế nào?
- **RQ2.** Với cùng số test, giữ test liên quan đến lỗi có giúp patch tổng quát hóa tốt hơn giữ test ngẫu nhiên không?
- **RQ3.** Khi thiếu test repo, test tái hiện lỗi do LLM sinh giúp repair đến đâu so với test người viết? Hai nguồn bỏ sót những hành vi nào?

Đơn vị phân tích là task. Mỗi task chạy qua tất cả nhánh áp dụng được, với cùng base commit, mô tả, model, agent và ngân sách. Chấm mỗi patch bằng cùng bộ test ẩn đã cố định cho task đó.

## 2. Chọn và xác thực task

Dùng tập con sửa lỗi chức năng trong DeepSWE v1.1. Pilot ưu tiên một ngôn ngữ và các repo có thể chạy riêng test quanh module liên quan. Ghim revision benchmark, base commit, image, test runner và phiên bản agent. Lưu danh sách task được chọn và lý do loại trước khi xem patch của model.

Một task đủ điều kiện cho thực nghiệm chính khi có test repo để ablate, test ẩn tái hiện đúng hành vi lỗi trên base code, và bản sửa tham chiếu pass bộ chấm. Chạy lại các kiểm tra này để phát hiện flaky tests và lỗi môi trường. Với thực nghiệm phụ, task còn cần test người viết dùng được trong lúc repair và test ẩn độc lập kiểm tra hành vi khác. Giữ nguyên các fixture và helper cần thiết cho những test được cấp.

## 3. Thực nghiệm A — Giảm lượng test repo

| Nhánh | Tỷ lệ test repo giữ lại |
|---|---:|
| Baseline | 100% |
| Bỏ 30% | 70% |
| Bỏ 50% | 50% |
| Bỏ 70% | 30% |
| Bỏ toàn bộ | 0% |
| Giữ test liên quan | 50% |

Mẫu số là tập test repo quanh module liên quan, xác định trước cho từng task. Gộp các biến thể cùng logic thành nhóm và báo cáo cả tỷ lệ mục tiêu lẫn tỷ lệ thực tế theo test case. Trong mỗi seed, dùng các tập giữ lại lồng nhau ở những mức lấy mẫu ngẫu nhiên. Với nhánh giữ test liên quan, xếp hạng bằng mô tả task và code trước fix, rồi đối chiếu với nhánh ngẫu nhiên cùng số lượng.

Tạo môi trường agent mới cho từng lượt. Kiểm tra test đã ẩn không thể đọc hoặc chạy qua checkout, Git history hay dữ liệu còn trong image. Áp dụng cùng quy tắc cho test agent tự viết và lưu lại các trường hợp này. Mức 0% áp dụng cho tập test đã định nghĩa quanh module. Bộ chấm nằm ở môi trường riêng.

## 4. Thực nghiệm B — Test người viết và test LLM sinh

Cố định tập test repo ở mức giữ 50%. Trên cùng task, chạy ba nhánh: không cấp thêm reproduction test, cấp test người viết, và cấp test LLM sinh. Dùng cùng số test hữu hiệu nếu có thể và cùng ngân sách repair.

Sinh test LLM từ mô tả và base code, rồi cố định test trước khi gọi repair agent. Evaluator xác nhận các test được cấp fail đúng hành vi trên base code và pass với bản sửa tham chiếu. Ghi riêng tỷ lệ task sinh được test hợp lệ. Báo cáo so sánh ghép cặp trên các task đủ điều kiện và hiệu quả của nhánh generated trên toàn bộ tập dự kiến, với quy tắc quay về nhánh không cấp thêm test khi sinh thất bại.

Chạy chéo test từ hai nguồn trên cùng các patch ứng viên. Rà soát các trường hợp một nguồn bắt được lỗi mà nguồn kia bỏ sót hoặc đánh fail một patch đáp ứng mô tả.

## 5. Chấm và phân tích

1. Áp phần diff mã nguồn sản phẩm của từng patch vào checkout sạch. Chạy bộ test ẩn và regression suite đã cố định. Giữ diff đầy đủ để phân tích. Ghi riêng lỗi áp patch, lỗi hạ tầng, timeout và lỗi hành vi theo quy tắc định trước.
2. Chỉ số chính là tỷ lệ task pass toàn bộ bộ chấm ẩn. Báo cáo chênh lệch theo cặp, khoảng tin cậy ở cấp task khi mở rộng, và số ca chuyển pass → fail hoặc fail → pass. Tách lỗi chưa sửa khỏi regression mới.
3. Với nhánh có test được cấp, báo cáo số patch pass test đó nhưng fail một test ẩn hợp lệ. Trình bày cả số ca trên tổng task và tỷ lệ trong số patch pass test được cấp. Với nhánh 0%, dùng kết quả bộ chấm ẩn để so sánh chất lượng patch.
4. Đối chiếu ca bất đồng với mô tả task để phân biệt patch bỏ sót edge case, patch gây regression và verifier quá đặc thù. Audit DeepSWE đã ghi nhận xung đột giữa test agent thêm hoặc sửa với verifier, nên cần kiểm tra riêng lỗi chấm do test.

## 6. Thứ tự triển khai

| Giai đoạn | Chạy gì | Điều kiện để chuyển bước |
|---|---|---|
| Kiểm tra harness | Chạy base và bản sửa tham chiếu trên một task | Kết quả lặp lại được và hai bản cho kết quả đúng kỳ vọng |
| Pilot | 5–8 task × 1 model × 3 nhánh giữ 100%, 50%, 0%, tức 15–24 lượt repair | Xác nhận ẩn test hiệu quả, chấm ổn định và xác định số task đủ điều kiện cho thực nghiệm B |
| Thực nghiệm chính | Thêm mức bỏ 30%, 70%, nhánh giữ 50% test liên quan và các seed lấy mẫu | Ghi đủ mẫu ghép cặp, test thực tế mỗi nhánh và ngân sách mỗi lượt |
| Thực nghiệm phụ | Chạy ba nhánh nguồn test trên các task đủ điều kiện | Có kết quả chấm ẩn, tỷ lệ sinh test hợp lệ và kết quả chạy chéo |
| Kiểm tra độ bền | Lặp các so sánh chính với model thứ hai | Báo cáo hiệu ứng theo model và các giới hạn của tập task |

Lưu tối thiểu cho mỗi lượt: task ID, revision, nhánh, seed, danh sách test được cấp, model và cấu hình, patch, chi phí hoặc token, kết quả từng test và nguyên nhân lỗi. Chốt cỡ mẫu chính sau pilot dựa trên số task đủ điều kiện, chi phí và mức chênh lệch cần phát hiện.

## Tài liệu tham khảo

- [DeepSWE và hướng dẫn chạy](https://github.com/datacurve-ai/deep-swe)
- [Metadata 113 task DeepSWE v1.1](https://deepswe.datacurve.ai/artifacts/v1.1/tasks.json)
- [Audit DeepSWE v1.1 của Epoch AI](https://epoch.ai/benchmarks/deepswe/review)
- [e-Otter++ và reproduction-test generation](https://arxiv.org/abs/2508.06365)
- [Investigating Test Overfitting on SWE-bench](https://arxiv.org/abs/2511.16858)
