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

### 3.1. Ý tưởng và phạm vi so sánh

Thực nghiệm A kiểm tra một câu hỏi cụ thể: **khi agent có ít test repo hơn để tham khảo và chạy trong lúc sửa lỗi, chất lượng patch trên một bộ chấm độc lập thay đổi thế nào?** Biến được thay đổi là lượng test repo *trong phạm vi liên quan đến task* mà agent được xem và chạy. Prompt, mã nguồn ban đầu, model, công cụ, ngân sách, môi trường và bộ test chấm cuối được giữ cố định giữa các nhánh của cùng task.

Đơn vị phân tích là **task**, còn đơn vị được lấy mẫu để giữ hoặc bỏ là **nhóm test case**. Không dùng số file làm mẫu số: một file có thể chứa nhiều test độc lập, hoặc nhiều test chỉ là các biến thể của cùng một hành vi. Trước khi chạy model, lập danh mục các test case kiểm tra hành vi thuộc phạm vi task; gộp các biến thể dùng chung logic thành nhóm. Ghi rõ test ID, hành vi được kiểm tra, file, fixture/helper phụ thuộc và nhóm của nó. Danh mục và quy tắc phân nhóm phải được chốt trước khi xem patch do model tạo.

Các nhánh trả lời hai ý khác nhau:

- So sánh các mức ngẫu nhiên với **100%** cho biết đường xu hướng khi lần lượt giảm test khả dụng.
- So sánh **50% liên quan** với **50% ngẫu nhiên** cho biết, ở cùng lượng test, việc giữ lại test gần với mô tả lỗi có giúp patch đạt điểm chấm ẩn tốt hơn không.

Trong pilot chỉ chạy ba mức 100%, 50% ngẫu nhiên và 0% để kiểm tra quy trình, chưa kết luận về đường xu hướng đầy đủ hay lợi thế của chọn test liên quan. Hai câu hỏi đó được kiểm tra ở thực nghiệm chính.

| Nhánh | Tỷ lệ test repo giữ lại |
|---|---:|
| Baseline | 100% |
| Bỏ 30% | 70% |
| Bỏ 50% | 50% |
| Bỏ 70% | 30% |
| Bỏ toàn bộ | 0% |
| Giữ test liên quan | 50% |

### 3.2. Định nghĩa tập test và cách tạo các nhánh

Với mỗi task, xác định trước **tập test trong phạm vi**: những test repo kiểm tra trực tiếp hành vi cần sửa, kể cả test tương đương nằm ở nhiều file. Test tổng quát không liên quan vẫn có thể giữ trong mọi nhánh, nhưng phải được ghi riêng và không tính vào mẫu số. Nếu một test cần fixture, helper hoặc dữ liệu dùng chung, giữ các phần phụ thuộc đó để test được chọn vẫn chạy; không tính fixture/helper là test. Rà soát cả cấu hình CI, script chạy test và các test khác có thể làm lộ cùng hành vi.

Gọi tập test trong phạm vi là `T`, có `N` test case thuộc các nhóm đã chốt. Tỷ lệ mục tiêu là tỷ lệ trên **test case**, không phải tỷ lệ nhóm hoặc file. Vì các nhóm có thể khác kích thước, công cụ lấy mẫu cần tiến gần tỷ lệ mục tiêu theo test case; luôn ghi cả số lượng thực tế. Nếu không thể đạt đúng tỷ lệ mà không tách một nhóm logic, ghi sai lệch và lý do thay vì tách nhóm sau khi thấy kết quả.

- **100%:** agent được xem và chạy toàn bộ `T`.
- **70%, 50%, 30% ngẫu nhiên:** dùng một hoán vị ngẫu nhiên có seed cố định của các nhóm test. Các tập được giữ phải lồng nhau: tập 30% nằm trong tập 50%, tập 50% nằm trong tập 70%. Nhờ vậy, khác biệt giữa mức giữ lại không bị chi phối bởi việc đổi hoàn toàn test được xem. Tạo hoán vị mới cho seed tiếp theo.
- **0%:** agent không được đọc hoặc chạy test case thuộc `T`. Agent vẫn được đọc/sửa mã nguồn và có thể tự viết test; mọi test do agent tạo hoặc thay đổi đều phải được lưu lại và phân loại riêng. Test ngoài phạm vi tiếp tục theo quy tắc cố định đã công bố.
- **50% liên quan:** xếp hạng các nhóm trước khi chạy repair, chỉ dựa trên mô tả task, mã nguồn và test ở base commit. Giữ các nhóm có thứ hạng cao nhất sao cho số test case gần 50% của `T`. Người xếp hạng không được xem patch tham chiếu, test chấm ẩn hoặc kết quả repair. Ghi tiêu chí xếp hạng và cách xử lý đồng hạng. So sánh nhánh này với 50% ngẫu nhiên trên chính cùng task.

Với từng task và seed, lưu manifest liệt kê test case được phép, bị giữ lại khỏi agent, các fixture/helper được giữ, tỷ lệ thực tế và hash của manifest. Bộ chấm ẩn không nằm trong checkout, image, Git history hay thư mục test mà agent có thể truy cập. Việc chỉ yêu cầu agent “không xem” test không được xem là ablation hợp lệ; các test bị bỏ phải thực sự không thể đọc/chạy trong môi trường agent.

### 3.3. Quy trình cho một task trong thực nghiệm A

1. **Chốt task và phạm vi.** Ghim revision benchmark, base commit, image và phiên bản runner. Lập danh mục `T`, nhóm test, dependency và tiêu chí phân loại trước khi tạo patch. Lưu lý do bao gồm/loại test.
2. **Xác thực task.** Trên checkout sạch, xác nhận test chấm tái hiện lỗi trên base code, bản sửa tham chiếu giải quyết lỗi, và test hồi quy được chọn vẫn pass. Chạy lặp để nhận diện flaky test. Task không đạt thì sửa lỗi harness hoặc loại task theo tiêu chí đã định trước; không chỉnh tiêu chí sau khi xem patch của model.
3. **Tạo các bản môi trường.** Tạo checkout/container mới từ cùng base commit cho từng nhánh và seed. Chỉ khác phần test repo được phép truy cập. Giữ cùng phiên bản dependency, biến môi trường, quyền mạng, lệnh chạy test và cấu hình agent.
4. **Chạy repair ghép cặp.** Cấp cùng prompt và ngân sách cho cùng model/agent ở mọi nhánh của task. Mỗi lượt bắt đầu từ trạng thái sạch. Xáo thứ tự chạy các nhánh để giảm ảnh hưởng của thay đổi tải máy hoặc dịch vụ theo thời gian. Không mang patch, log hay bộ nhớ từ lượt trước sang lượt sau.
5. **Ghi nhận hành vi test.** Lưu test agent đã chạy, test agent tự thêm/sửa, lệnh chạy, log, thời gian/token/chi phí và patch cuối. Ghi riêng nếu agent tìm cách truy cập test bị loại hoặc môi trường bị lỗi.
6. **Chấm trong evaluator riêng.** Áp phần diff mã nguồn sản phẩm lên checkout sạch; không đưa test do agent tạo vào bộ chấm ẩn. Chạy cùng một bộ test ẩn và regression suite cố định cho mọi nhánh của task. Lưu kết quả từng test và loại lỗi.
7. **Phân tích theo cặp.** So sánh kết quả các nhánh trên cùng task, không chỉ so tỷ lệ gộp. Báo pass toàn bộ bộ chấm ẩn, số task chuyển pass↔fail, test được cấp mà patch pass nhưng test ẩn fail, cùng nguyên nhân lỗi. Nhánh 0% vẫn được chấm bằng cùng evaluator.

### 3.4. Điều kiện hợp lệ và diễn giải

Kết quả của A chỉ đo ảnh hưởng của **quyền tiếp cận test repo đã định nghĩa**, không đo ảnh hưởng của việc bỏ test khỏi quy trình phát triển nói chung. Nếu agent còn truy cập được test bị bỏ từ một bản sao khác, cache, lịch sử Git, image hoặc đường dẫn khác, lượt đó bị đánh dấu nhiễm test và không tính như một quan sát ablation hợp lệ. Nếu patch không áp dụng được, timeout hoặc hạ tầng hỏng, ghi riêng theo quy tắc ở mục 5; không tự coi mọi trường hợp đó là lỗi hành vi.

Tỷ lệ 50% ngẫu nhiên và 50% liên quan phải có cùng số test case hữu hiệu hoặc sai lệch được báo rõ. “Liên quan” là tiêu chí xếp hạng dựa trên thông tin có sẵn trước khi repair, không phải lựa chọn test sau khi biết patch nào sẽ pass. Pilot dùng để tìm lỗi quy trình và ước lượng độ khả thi/chi phí; cỡ mẫu pilot nhỏ không đủ để khẳng định tác động ổn định.

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

### Phase 1 — Kiểm chứng harness và pilot thực nghiệm A

Mục tiêu của phase này là chứng minh rằng một lần chạy có thể tạo ra ba điều kiện khác nhau (đủ test, giữ một phần test, không có test trong phạm vi), nhưng vẫn chấm các patch trên cùng một bộ test độc lập. Đây là bước kiểm tra cách đo, chưa phải kiểm định kết luận nghiên cứu.

#### Bước 1: Dùng instance adaptix làm smoke test kỹ thuật

Bắt đầu bằng `adaptix-name-mapping-aliases` làm **một instance thử harness**, vì đây là task Python đã tải. Trước khi đưa vào pilot, kiểm tra riêng xem nó đủ điều kiện thực nghiệm không: có tập test repo trong phạm vi đủ xác định để lấy mẫu hay không; test chấm có làm base fail và reference pass theo đúng kỳ vọng hay không; có thể tách test agent khỏi test evaluator hay không. Không mặc định task đủ điều kiện chỉ vì đã tải được folder. File lời giải và test patch trong bản tải dùng cho khâu kiểm tra/đối chiếu của evaluator; phải loại chúng khỏi image và mọi dữ liệu agent có thể đọc.

Với instance này, tạo ba môi trường sạch ở các nhánh 100%, 50% ngẫu nhiên và 0%. Xác nhận manifest và quyền truy cập đúng ở cả ba; chạy base/reference trong evaluator. Đây là smoke test kỹ thuật, chưa gọi model repair. Nếu task đạt tiêu chí sau khi giao thức được đóng băng, đưa nó vào pilot như một task trong nhóm 5–8; không tính thành một bộ chạy thêm ngoài pilot. Nếu task không đạt hoặc quá khó kiểm soát, sửa harness trước hoặc chọn task Python khác.

#### Bước 2: Đóng băng giao thức pilot

Trước khi gọi model, ghi lại: revision benchmark; danh sách task và lý do loại; base commit/image; tập test trong phạm vi và các nhóm test; cách tạo 50% ngẫu nhiên; prompt; model/phiên bản; cấu hình sampling; ngân sách token/thời gian; lệnh test; evaluator; tiêu chí lỗi hạ tầng/flaky/nhiễm test; và schema lưu kết quả. Không thay đổi các mục này giữa các nhánh của cùng task. Nếu cần sửa giao thức sau smoke test, ghi phiên bản giao thức mới và bắt đầu pilot từ đầu với cùng quy tắc cho mọi nhánh.

Chọn **5–8 task Python đủ điều kiện** cho pilot, nếu metadata và tính khả thi cho phép; lưu lý do chọn/loại trước khi xem patch của model. Nếu chưa tìm được đủ task, ghi số khả dụng và lý do, không lấp chỗ bằng task không đạt điều kiện. Mỗi task được chạy ghép cặp ở 100%, 50% ngẫu nhiên và 0%, với một model: tổng cộng 15–24 lượt repair nếu có đủ 5–8 task. Dùng một seed trong pilot để kiểm tra luồng; chưa cần nhiều seed.

#### Bước 3: Lập danh mục và tạo bản test theo nhánh

Hai người rà soát độc lập (nếu nguồn lực cho phép) phân loại test trong phạm vi và nhóm các biến thể cùng logic; giải quyết bất đồng trước khi chạy model. Tạo manifest cho 100%, 50% ngẫu nhiên và 0%. Kiểm tra tự động rằng số test thực tế khớp manifest và kiểm tra thủ công rằng fixture/helper cần thiết không bị mất.

Kiểm tra khả năng truy cập từ phía agent: thử tìm file/test ID bằng lệnh tìm kiếm thông thường, thử gọi runner nhắm vào test bị bỏ, kiểm tra `git log`/refs và các thư mục/caches trong image. Với 0%, test trong phạm vi không được hiện diện ở bất kỳ đường dẫn nào mà agent có thể đọc hoặc chạy. Thực hiện kiểm tra tương tự với bộ chấm ẩn và patch tham chiếu để xác nhận chúng chỉ có trong evaluator.

#### Bước 4: Xác thực base, reference và độ ổn định của bộ chấm

Cho từng task, chạy base và reference trong evaluator trên checkout sạch. Kỳ vọng: test fail-to-pass tái hiện lỗi trên base và pass với reference; test pass-to-pass tiếp tục pass ở cả hai. Chạy mỗi cấu hình ít nhất ba lần để phát hiện kết quả không ổn định. Ghi hash image, lệnh, mã thoát và kết quả test từng lần. Nếu có flaky test, timeout hay sai lệch ngoài dự kiến, dừng task đó để điều tra; không dùng patch của model để quyết định test nào được giữ.

#### Bước 5: Chạy pilot ghép cặp

Với từng task, tạo ba container/checkout độc lập từ cùng base. Chạy cùng một prompt, model, agent và ngân sách; chỉ thay quyền xem/chạy test trong phạm vi. Xáo thứ tự ba nhánh. Bắt đầu mỗi lượt mới hoàn toàn, không chia sẻ hội thoại, bộ nhớ, patch hoặc workspace. Cho phép agent viết test riêng theo cùng chính sách ở cả ba nhánh; lưu test agent tạo/chỉnh và không đưa chúng vào evaluator.

Thu tối thiểu: task ID; nhánh; seed; danh sách test được cấp và tỷ lệ thực tế; prompt/config/model; thứ tự chạy; thời gian và token/chi phí; lệnh và test agent đã chạy; patch; kết quả ẩn theo từng test; lỗi hạ tầng/timeout/nhiễm test; và ghi chú nguyên nhân. Bảo toàn log gốc để có thể truy nguyên kết quả.

#### Bước 6: Đọc kết quả pilot và quyết định có mở rộng

Tạo bảng theo từng task với ba cột kết quả 100%, 50% ngẫu nhiên và 0%; đánh dấu pass toàn bộ bộ chấm ẩn, lỗi chưa sửa, regression mới và lượt không hợp lệ. Tóm tắt chênh lệch ghép cặp giữa 100%–50%, 50%–0%, số patch pass test agent nhìn thấy nhưng fail evaluator, và thời gian/chi phí mỗi lượt. Không diễn giải chênh lệch từ 5–8 task như ước lượng chắc chắn cho toàn benchmark.

Chỉ chuyển sang thực nghiệm chính khi: (a) manifest thực sự tạo ra đúng các mức truy cập; (b) không phát hiện đường rò test ẩn hoặc test bị bỏ; (c) base/reference cho kết quả kỳ vọng và lặp lại được; (d) thu patch, log và kết quả evaluator đầy đủ; (e) lỗi hạ tầng/flaky có quy tắc xử lý áp dụng nhất quán. Nếu chưa đạt, sửa harness/giao thức và chạy lại pilot cho tất cả nhánh bị ảnh hưởng. Sau pilot, mới quyết định số seed và cỡ mẫu chính dựa trên số task hợp lệ, độ biến động ghép cặp và chi phí.

| Giai đoạn | Chạy gì | Điều kiện để chuyển bước |
|---|---|---|
| Smoke test + pilot A | Instance Python thử harness; sau đó 5–8 task × 1 model × 3 nhánh giữ 100%, 50% ngẫu nhiên, 0% (15–24 lượt repair) | Đạt các cổng kiểm tra ở Bước 6; xác nhận test được ẩn, chấm ổn định và thu đủ dữ liệu |
| Thực nghiệm A chính | Thêm mức giữ 70%, 30%, 50% liên quan và nhiều seed; mọi nhánh ghép cặp theo task | Có manifest cố định, không nhiễm test, đủ lượt hợp lệ và ngân sách đồng nhất |
| Thực nghiệm phụ | Chạy ba nhánh nguồn test trên các task đủ điều kiện | Có kết quả chấm ẩn, tỷ lệ sinh test hợp lệ và kết quả chạy chéo |
| Kiểm tra độ bền | Lặp các so sánh chính với model thứ hai | Báo cáo hiệu ứng theo model và các giới hạn của tập task |

Lưu tối thiểu cho mỗi lượt: task ID, revision, nhánh, seed, danh sách test được cấp, model và cấu hình, patch, chi phí hoặc token, kết quả từng test và nguyên nhân lỗi. Chốt cỡ mẫu chính sau pilot dựa trên số task đủ điều kiện, chi phí và mức chênh lệch cần phát hiện.

## Tài liệu tham khảo

- [DeepSWE và hướng dẫn chạy](https://github.com/datacurve-ai/deep-swe)
- [Metadata 113 task DeepSWE v1.1](https://deepswe.datacurve.ai/artifacts/v1.1/tasks.json)
- [Audit DeepSWE v1.1 của Epoch AI](https://epoch.ai/benchmarks/deepswe/review)
- [e-Otter++ và reproduction-test generation](https://arxiv.org/abs/2508.06365)
- [Investigating Test Overfitting on SWE-bench](https://arxiv.org/abs/2511.16858)
