# Thực nghiệm A — giải thích dễ hiểu

File này giải thích ý tưởng của Thực nghiệm A bằng ngôn ngữ đơn giản. Bản kế hoạch chính vẫn nằm ở `plan-thuc-nghiem-deepswe.md`.

## 1. Mình muốn biết điều gì?

Ta muốn biết: **nếu AI có ít bài test sẵn để tham khảo khi sửa code, nó có sửa kém đi không?**

Khi một lập trình viên sửa code, các bài test trong repo giống như những bài tập cho biết chương trình hiện đang được kỳ vọng hoạt động ra sao. AI cũng có thể đọc và chạy chúng. Nếu AI thấy nhiều test, nó có thêm manh mối. Nếu một số test bị giấu đi, AI phải dựa nhiều hơn vào yêu cầu, mã nguồn và suy luận của mình.

Sau khi AI sửa xong, ta sẽ kiểm tra patch bằng **một bộ test chấm riêng**. Bộ này giống bài thi cuối kỳ: AI không được xem trước, và mọi phiên bản thí nghiệm đều được chấm bằng cùng một bộ. Như vậy, ta không hỏi “AI có làm pass đúng những test mà nó vừa nhìn thấy không?” mà hỏi “patch của nó có làm đúng yêu cầu trong những trường hợp mà ta giữ riêng để chấm không?”.

## 2. Hai loại test cần phân biệt

| Loại test | AI có được xem/chạy không? | Dùng để làm gì? |
|---|---|---|
| Test có sẵn trong repo, thuộc phần đang nghiên cứu | Tùy nhánh thí nghiệm | Là phần ta chủ động cho nhiều hoặc ít đi để xem nó ảnh hưởng tới cách AI sửa code thế nào. |
| Test chấm riêng của benchmark | Không | Chấm patch cuối cùng. Bộ test này phải giống nhau giữa mọi nhánh. |

Khi nói “giảm test”, ta chỉ giảm **test repo thuộc phạm vi đã chọn**. Ta không xóa mã nguồn sản phẩm. Các test không liên quan có thể được giữ giống nhau ở mọi nhánh. Ta cũng cho phép AI tự viết test nếu muốn; cần lưu lại test đó, nhưng không dùng nó thay cho bộ chấm riêng.

## 3. Ví dụ với task Adaptix

Task Adaptix yêu cầu thư viện nhận được nhiều tên khóa đầu vào cho cùng một trường. Ví dụ trường `user_name` có thể được lấy từ `user_name` hoặc `userName`.

Ta tạo các bản sao sạch của cùng một repo ở cùng một commit. Trong mỗi bản, AI nhận cùng yêu cầu và được giao cùng ngân sách. Điều duy nhất thay đổi là có bao nhiêu test repo liên quan được để lại cho AI xem và chạy.

Khi AI làm xong, ta lấy patch ở từng bản sao và đưa sang bộ chấm riêng. Bộ chấm kiểm tra mọi patch theo cùng tiêu chí, ví dụ: alias có được nhận không, trường hợp xung đột có báo lỗi không, và các tính năng cũ có còn hoạt động không.

**Lưu ý:** Adaptix là ví dụ để thử cách làm. Trước khi đưa nó vào số liệu, phải xác nhận task và test của nó đáp ứng điều kiện thí nghiệm. Không nên thấy task có sẵn là mặc định nó phù hợp.

## 4. Các nhánh sẽ chạy

Phần trăm dưới đây là tỷ lệ **test case liên quan** được để lại cho AI, không phải tỷ lệ file. Ví dụ, nếu phạm vi có 20 test case thì mức 50% nghĩa là AI được xem khoảng 10 test.

| Nhánh | AI thấy gì? | Câu hỏi nhánh giúp trả lời |
|---|---|---|
| **100%** | Tất cả test repo liên quan | Đây là mốc đầy đủ để so sánh. |
| **70%** | Khoảng 7 trên 10 test liên quan | Bớt một phần test có làm patch kém đi không? |
| **50% ngẫu nhiên** | Một nửa test được chọn ngẫu nhiên | Nếu chỉ giữ một nửa bất kỳ thì sao? |
| **30%** | Khoảng 3 trên 10 test liên quan | Khi còn rất ít test, kết quả thay đổi thế nào? |
| **0%** | Không thấy test repo thuộc phạm vi | Nếu không có các test đó, AI vẫn sửa được không? |
| **50% liên quan** | Một nửa được chọn vì gần với yêu cầu nhất | Ở cùng số lượng, test hữu ích có tốt hơn test ngẫu nhiên không? |

Hai nhánh 50% giúp so sánh công bằng: chúng có số lượng test gần như nhau, nhưng cách chọn khác nhau. Nếu 50% liên quan cho kết quả tốt hơn 50% ngẫu nhiên, đó là dấu hiệu cho thấy **test nào được giữ lại** cũng quan trọng, chứ không chỉ là giữ bao nhiêu test.

Với các nhánh ngẫu nhiên, ta dùng cách chọn lồng nhau. Chẳng hạn, 30% test được giữ nằm bên trong tập 50%, và tập 50% nằm bên trong tập 70%. Nhờ vậy, khi so sánh 70% với 50%, ta không vô tình đổi toàn bộ test cùng lúc. Mỗi lần chọn đều được ghi lại để có thể dựng lại chính xác.

## 5. Một lượt thí nghiệm diễn ra thế nào?

1. Chọn một task và ghi rõ những test repo nào kiểm tra trực tiếp phần việc đó.
2. Chuẩn bị các bản sao giống nhau của repo tại cùng commit. Mỗi bản chỉ khác danh sách test được phép xem/chạy.
3. Chạy cùng AI với cùng yêu cầu, cấu hình và giới hạn thời gian/token. Mỗi lượt bắt đầu trong môi trường mới để lượt trước không ảnh hưởng lượt sau.
4. Lưu patch, log, test AI đã chạy hoặc tự viết, thời gian và chi phí.
5. Đưa từng patch vào bộ chấm riêng giống hệt nhau. Ghi patch nào pass, patch nào fail và vì sao.

Ví dụ một task chạy đủ sáu nhánh thì tạo sáu phiên sửa code độc lập. Không lấy patch ở nhánh 100% rồi tiếp tục sửa cho nhánh 50%; mỗi nhánh phải bắt đầu từ cùng trạng thái ban đầu.

## 6. Phase 1: thử cách làm trước khi chạy lớn

Phase 1 là giai đoạn kiểm tra xem thí nghiệm có chạy đúng như dự định không. Kết quả của phase này chủ yếu giúp sửa quy trình, chưa đủ để kết luận cho toàn bộ benchmark.

### Bước 1 — Thử harness với Adaptix

Dùng task Adaptix đã tải để kiểm tra việc dựng repo, tách test và chấm patch. Trước tiên, chạy bản gốc và bản sửa tham chiếu trong bộ chấm riêng:

- Bản gốc cần thể hiện được lỗi hoặc thiếu tính năng mà task nói tới.
- Bản sửa tham chiếu cần vượt qua test tính năng và không làm hỏng các test hồi quy.

Sau đó thử dựng ba phiên bản môi trường: 100%, 50% ngẫu nhiên và 0%. Kiểm tra tận nơi xem test bị bỏ thật sự không thể tìm hoặc chạy từ môi trường AI. Đồng thời kiểm tra patch tham chiếu và test chấm riêng không bị đưa vào môi trường AI.

Nếu Adaptix không phù hợp hoặc không dựng được cách tách test đáng tin cậy, dừng ở đây để sửa harness hoặc chọn task Python khác. Không ép task vào thí nghiệm chỉ vì đã tải nó xuống.

### Bước 2 — Chốt cách chạy

Trước khi gọi AI, ghi lại task nào sẽ dùng, test nào được tính là liên quan, cách chọn test ngẫu nhiên, model và cấu hình, giới hạn mỗi lượt, lệnh chạy test, cùng cách xử lý timeout hoặc lỗi môi trường. Sau khi bắt đầu pilot, không đổi riêng một nhánh. Nếu phải thay đổi cách làm, áp dụng lại nhất quán cho các nhánh liên quan.

### Bước 3 — Chọn task cho pilot

Chọn khoảng **5–8 task Python** đủ điều kiện. Adaptix có thể là một task trong danh sách nếu đã qua kiểm tra. Ghi task nào được chọn và vì sao task nào bị loại trước khi xem patch do AI tạo ra.

### Bước 4 — Chạy ba nhánh pilot

Trong pilot chỉ cần ba nhánh:

- 100% test liên quan;
- 50% test liên quan được chọn ngẫu nhiên;
- 0% test liên quan.

Với 5–8 task, mỗi task chạy cả ba nhánh. Tổng cộng là 15–24 lượt sửa code. Dùng một model và một seed để trước hết kiểm tra luồng chạy. Mỗi lượt có repo/container riêng và cùng ngân sách.

### Bước 5 — So sánh và quyết định bước tiếp

Lập bảng cho từng task: kết quả ở ba nhánh, patch có pass bộ chấm riêng không, có lỗi hạ tầng không, AI đã chạy test nào và mỗi lượt tốn bao lâu/bao nhiêu token.

Chỉ mở rộng khi ta xác nhận được rằng test được giữ/bỏ đúng như dự kiến, bộ chấm riêng cho kết quả ổn định, không có test bị lộ sang môi trường AI, và log/patch của mọi lượt đều thu được. Nếu chưa đạt, sửa cách dựng môi trường rồi chạy lại các nhánh bị ảnh hưởng.

Sau khi pilot ổn, thực nghiệm chính mới thêm các mức 70%, 30% và 50% liên quan, rồi chạy thêm các lần chọn ngẫu nhiên khác. Khi đó ta mới có dữ liệu để xem đường xu hướng rõ hơn và đánh giá cách chọn test liên quan.

## 7. Kết quả cuối cùng sẽ trông như thế nào?

Ta sẽ có một hàng cho mỗi task và một cột cho mỗi nhánh. Mỗi ô cho biết patch có vượt qua bộ chấm riêng không, cùng ghi chú lỗi chính. Từ bảng đó, ta có thể trả lời:

- Khi giảm test từ 100% xuống 50% rồi 0%, có ít patch tốt hơn không?
- Hai nhánh giữ 50% có khác nhau không: giữ ngẫu nhiên hay giữ test gần với yêu cầu?
- Patch có vượt qua các test AI được thấy nhưng vẫn trượt bộ chấm riêng không?

Ý nghĩa của kết quả là giúp hiểu **test có sẵn hỗ trợ AI sửa code ra sao**. Nó không tự động chứng minh rằng mọi test đều tốt hoặc mọi task sẽ có cùng kết quả; kết luận cần dựa trên số lượng task và độ ổn định của các lần chạy.
