# Kế hoạch tích hợp Ollama và chạy thực nghiệm trên Saturn, Neptune

## 1. Mục tiêu và hiện trạng

Cùng một codebase DeepSWE hỗ trợ hai server với model và cấu hình vận hành riêng. Mỗi lượt chạy phải xác định được profile, model thực tế, quantization, context và budget. Người vận hành có một lệnh chạy thực nghiệm cho mỗi server, kể cả khi mở terminal mới.

| Thành phần | Hiện trạng |
|---|---|
| Saturn | Đã setup `qwen3.6:35b-a3b-coding` portable dưới `.local/ollama/` theo thông tin vận hành |
| Neptune | Đã có script setup/CLI/activation/stop cho `qwen3.8:27b`, runtime dưới `.local/ollama-neptune/` |
| Python provider | Settings, factory, doctor, check-model, quota và runner hiện chỉ tích hợp Google |
| Protocol | Hash hiện gồm study config, tên model, prompt, prepared và validation |
| Chạy batch | CLI hiện có `run-next`, cần bổ sung `run-all` |
| Triển khai thực tế | Cần xác nhận runtime paths, model metadata và smoke run trực tiếp trên mỗi server |

Agent dùng tool `run_shell(command)` trong Docker sandbox hiện có. Ollama chạy trên host, nhận request từ runner. Google tiếp tục được hỗ trợ qua lựa chọn provider.

Đây là kế hoạch triển khai. Các file ghi “cần thêm” bên dưới chưa tồn tại tại thời điểm viết plan. Khi triển khai, cập nhật [implementation-plan-one-click-thuc-nghiem-A.md](implementation-plan-one-click-thuc-nghiem-A.md) và [setup-ollama-neptune.md](setup-ollama-neptune.md) theo giao diện thống nhất trong tài liệu này.

## 2. Hai profile và cấu hình study

### 2.1. Giá trị khởi đầu

| Trường | Saturn | Neptune |
|---|---|---|
| Profile ID | `saturn` | `neptune` |
| Provider | `ollama` | `ollama` |
| Model tag | `qwen3.6:35b-a3b-coding` | `qwen3.8:27b` |
| Quantization | Đọc từ model đang cài trên Saturn | Đọc từ model đã tải trên Neptune |
| Endpoint | `http://127.0.0.1:11434` | `http://127.0.0.1:11435` |
| Runtime root trong repo | `.local/ollama/` | `.local/ollama-neptune/` |
| Binary dự kiến | `.local/ollama/bin/ollama` | `.local/ollama-neptune/runtime/bin/ollama` |
| Model store | `.local/ollama/models/` | `.local/ollama-neptune/models/` |
| Context khởi đầu | `32768` | `32768` |
| Study config cần thêm | `config/study.saturn.yaml` | `config/study.neptune.yaml` |
| Study ID khởi đầu | `pilot-a-saturn-qwen36` | `pilot-a-neptune-qwen38` |
| Artifact root | `runs/pilot-a-saturn-qwen36/` | `runs/pilot-a-neptune-qwen38/` |

Xác nhận binary, model store và endpoint thực tế trên từng server trước khi hoàn thiện profile. Profile Saturn phải trỏ đúng runtime đã cài nếu đường dẫn thực tế khác bảng trên. Hai máy có filesystem riêng, mỗi profile tham chiếu đường dẫn thuộc checkout của chính máy đó.

Neptune tiếp tục dùng tag `qwen3.8:27b` theo setup hiện tại. Nếu chọn `qwen3.8:27b-bf16`, tạo study mới và ghi quantization/digest tương ứng. 234 GB là dung lượng ổ đĩa Neptune. VRAM, số GPU và RAM được thu thập riêng từ server.

### 2.2. Nguồn cấu hình

Cần thêm `config/profiles/saturn.yaml` và `config/profiles/neptune.yaml`. Mỗi profile là nguồn cấu hình duy nhất cho runtime Ollama và model của server. Cấu trúc Neptune dự kiến như sau.

```yaml
profile_id: neptune
provider: ollama
study_config: config/study.neptune.yaml
model: qwen3.8:27b
ollama:
  base_url: http://127.0.0.1:11435
  binary: .local/ollama-neptune/runtime/bin/ollama
  models_dir: .local/ollama-neptune/models
  state_dir: .local/ollama-neptune
  num_ctx: 32768
  temperature: 0.0
  num_parallel: 1
  max_loaded_models: 1
```

Thinking/reasoning và các sampling option khác được xác định qua kiểm tra integration, phiên bản Ollama và model thực tế trên mỗi máy. Bổ sung option được hỗ trợ vào profile, truyền tường minh khi gọi model và lưu cấu hình đã resolve. Option dùng mặc định runtime phải ghi rõ trạng thái này cùng phiên bản Ollama.

Study YAML chứa tasks, branches, seeds, prompt, budget, study ID và output root. Hai study khởi đầu lấy cùng task/branch/seed/budget từ `config/study.yaml`. Budget hiện có gồm 25 model requests, 1.800 giây mỗi lượt, 4.096 output tokens, 8.000 ký tự tool output và 120 giây mỗi tool command. Giới hạn output phải tính đến reasoning theo semantics thực tế của provider.

Profile/study YAML được commit cùng code. Binary, weights, PID, log và cấu hình máy cục bộ nằm trong `.local/` đã được Git ignore. Python dependencies dùng `.venv/`, uv cache dùng `.uv-cache/` trong repo.

### 2.3. Chọn profile và ưu tiên cấu hình

- Bổ sung `--profile saturn|neptune` cho CLI và tham số tương ứng cho `load_settings`. Hỗ trợ nhất quán với `--config` trên tất cả subcommand.
- Khi có `--profile`, nạp model/provider/Ollama options từ profile. Đối chiếu các biến shell như `MODEL`, `MODEL_PROVIDER`, `OLLAMA_BASE_URL`, `OLLAMA_HOST`, `OLLAMA_MODELS` và context. Nếu mâu thuẫn, báo trường xung đột và hướng dẫn bỏ biến cũ trước khi chạy.
- `--config` tường minh có thể chọn study khác trong khi profile cung cấp runtime/model. Ví dụ study BF16 mới cần profile được cập nhật cùng model BF16 trước khi freeze. In rõ profile, study và cấu hình cuối cùng.
- Khi bỏ `--profile`, giữ cơ chế Google hiện hành với environment ưu tiên hơn `.env`. Các script chạy hai server luôn truyền profile tường minh.
- Bash và Python dùng chung logic resolve profile. Nếu cần export cho script, cung cấp đầu ra cấu trúc hoặc helper nhỏ dùng lại settings loader. Validate dữ liệu và quoting khi thiết lập môi trường subprocess.
- Validate provider, URL, số nguyên dương và đường dẫn runtime/model/state đã resolve thuộc repo. Cấu hình model được resolve một lần cho mỗi invocation và dùng xuyên suốt pipeline.

## 3. Thay đổi Python cần triển khai

### 3.1. Dependency, settings và provider factory

Các file chính gồm `pyproject.toml`, `uv.lock`, `src/deepswe_exp/components/configuration/settings.py` và `src/deepswe_exp/components/agent/agent.py`.

1. Thêm `langchain-ollama` tương thích LangChain/Python 3.12 đang dùng. Cập nhật lockfile qua uv, triển khai bằng `uv sync --frozen`.
2. Mở rộng settings với provider, profile ID, endpoint, runtime paths, context và generation options. Giữ khả năng đọc cấu hình cũ và caller đang tạo `Settings` trực tiếp.
3. Tạo `ChatOllama` bằng cấu hình đã resolve. Ánh xạ output budget sang `num_predict`, context sang `num_ctx`, đặt timeout theo thời gian còn lại. Xác nhận thinking/timeout và các option được integration hỗ trợ.
4. Bind chung schema `run_shell(command: string)`. Giữ đầy đủ `AIMessage`, `ToolMessage`, tool-call ID, reasoning metadata và usage nếu có.
5. Đặt retry tường minh theo budget. Ghi lỗi mạng/HTTP/model load và phản hồi tool call lỗi vào artifact chẩn đoán.

### 3.2. Doctor và kiểm tra model

Các file chính gồm `src/deepswe_exp/__main__.py` và `src/deepswe_exp/components/agent/connection.py`.

- `doctor` hiển thị profile, study ID, provider, model, endpoint, context, paths và trạng thái Docker/Python/GPU. Đọc Ollama metadata qua `/api/version`, `/api/tags`, `/api/show`. Request sinh nội dung thuộc `check-model`.
- Nhánh Ollama yêu cầu cấu hình local. Nhánh Google tiếp tục kiểm tra API key và quota.
- `check-model` dùng đúng factory của thực nghiệm. Yêu cầu model gọi `run_shell` với lệnh cố định, trả tool result giả lập trong bộ kiểm tra rồi xác minh model trả lời tiếp. Đếm/ghi từng request riêng và cho phép cấu hình timeout phù hợp thời gian load model.
- Thu Ollama version, model digest từ tags, parameter size, quantization từ metadata và generation options. Đọc tag custom Saturn tại server để biết nội dung thực tế.
- Artifact lưu dưới `runs/<study_id>/connectivity/` với timestamp/ID riêng. Phân biệt lỗi endpoint, tag thiếu, load/OOM, timeout và tool-call format.

### 3.3. Budget, quota và trạng thái lỗi

Các file chính gồm `components/agent/quota.py`, `components/agent/agent.py` và `components/execution/runner.py` dưới `src/deepswe_exp/`.

- Google dùng `QuotaLedger`. Ollama có ledger local với giao diện request/usage phù hợp, ghi theo study và áp budget thực nghiệm.
- Rà tất cả chỗ gọi `ledger.reserve`, `remaining`, `usage` trong agent, runner và connectivity check để tách quota dịch vụ Google khỏi Ollama đầy đủ.
- Usage thiếu được lưu `null` hoặc trạng thái không khả dụng. Cập nhật report để đọc được dữ liệu này.
- Lỗi provider, OOM, endpoint mất kết nối và tool-call format không tương thích làm lượt/group gián đoạn. Rà `format_error` hiện có để phân loại đúng lỗi tích hợp model.
- `apply_failure` hoặc verifier fail với patch đã thu hợp lệ là kết quả chấm. Lỗi hạ tầng/evaluation được ghi riêng để xử lý và chấm lại.

### 3.4. Protocol và artifact

Các file chính gồm `components/results/storage.py`, `schemas.py`, `report.py` và `components/execution/runner.py` dưới `src/deepswe_exp/`.

- Tạo snapshot cấu hình model đã resolve. Protocol Ollama gồm profile ID, provider, model tag, digest, quantization, endpoint, context, generation options, Ollama version và study/prompt/prepared/validation hiện có.
- Lưu snapshot đọc được trong protocol artifact và tham chiếu từ schedule, `run.json`, trajectory và summary. Ghi hostname, GPU/VRAM, runtime paths, code/dependency version làm provenance. Đường dẫn tuyệt đối theo máy thuộc metadata vận hành để hash có thể dùng khi chuyển checkout.
- Lúc lập schedule và trước mỗi lượt chạy/resume, kiểm tra profile và model hiện tại với snapshot. Tag giữ nguyên nhưng digest thay đổi phải được phát hiện.
- Version hóa schema Ollama. Giữ cách xác minh hash Google cũ để tiếp tục study hiện có. Artifact Ollama cũ thiếu định danh được đọc/report và cần study mới khi chạy với protocol mới.
- Mỗi server có study ID/output riêng. Khi gom về một máy, giữ hai cây artifact và profile/model identity trong report.
- Đổi model, quantization, context, prompt, budget hoặc generation options sau khi freeze yêu cầu study mới. Báo cáo so sánh hai server ghi các khác biệt model, runtime, quantization và phần cứng.

## 4. Các script vận hành

### 4.1. File hiện có cần điều chỉnh

| File | Việc cần làm |
|---|---|
| `scripts/setup_ollama_neptune.sh` | Đọc profile Neptune, phục vụ cài/tải lần đầu hoặc cập nhật có chủ đích, ghi version/model metadata |
| `scripts/ollama_neptune.sh` | Dùng cùng loader, đặt binary/endpoint/model store cho subprocess |
| `scripts/activate_ollama_neptune.sh` | Đồng bộ profile, hỗ trợ source lặp lại trong Bash |
| `scripts/stop_ollama_neptune.sh` | Xác minh process repo trước khi dừng và báo trạng thái đúng |

### 4.2. File cần thêm

| File | Trách nhiệm |
|---|---|
| `scripts/run_experiment_a.sh` | Entry point chung nhận `--profile` và tùy chọn `--config`, chạy pipeline |
| `scripts/run_experiment_a_saturn.sh` | Wrapper chuyển tiếp tham số, chọn `--profile saturn` |
| `scripts/run_experiment_a_neptune.sh` | Wrapper chuyển tiếp tham số, chọn `--profile neptune` |
| `scripts/start_ollama_local.sh` | Nhận profile, chạy binary có sẵn và chờ healthcheck |
| `scripts/stop_ollama_local.sh` | Nhận profile, dừng process đã xác minh khi người vận hành gọi |

Tái sử dụng logic start/stop Neptune trong script chung, giữ entry point Neptune cũ dưới dạng wrapper khi phù hợp. Saturn dùng runtime đã cài tại đường dẫn được xác nhận. Với server Saturn được khởi chạy thủ công, xác minh executable, endpoint và model store qua thông tin process có quyền đọc trước khi dùng lại. Khi chưa xác minh được, báo hướng dẫn thao tác cụ thể.

Script tự tìm repo root từ vị trí file, hỗ trợ đường dẫn có khoảng trắng, ghi LF, dùng `set -euo pipefail` trong script chạy độc lập và exit code rõ ràng. Script thực nghiệm tự thiết lập environment cho subprocess để gọi được từ terminal mới.

Runtime, weights, PID, log, download tạm và các file phụ Ollama phát sinh phải ở trong repo. Rà key/config/cache mặc định của phiên bản đang dùng và cơ chế cấu hình đường dẫn được hỗ trợ. Các cache/thư mục tạm cần thiết đặt dưới `.local/` theo phạm vi process. Giữ `HOME` và `CODEX_HOME` đúng ý nghĩa hệ thống.

## 5. Luồng chạy thực nghiệm

### 5.1. Chuẩn bị runtime

1. Wrapper chọn profile tường minh. Resolve study config, in model, endpoint, context, budget và artifact root.
2. Dùng uv/Python 3.12 theo quy trình repo, với `.venv` và `.uv-cache` cục bộ. Nếu cần tải Python managed, đặt vị trí trong repo. Thiếu công cụ/Docker/NVIDIA driver thì báo dependency cần chuẩn bị.
3. Kiểm tra binary, model store, model tag, Docker và tài nguyên thực tế. Tính dung lượng theo model/profile. Ghi GPU dùng cho model vào metadata.
4. Start script đặt `OLLAMA_HOST`, `OLLAMA_MODELS` và runtime options từ profile, khởi động binary hiện có và chờ healthcheck có timeout. Kiểm tra process còn sống, in log khi startup thất bại.
5. Dùng PID, start-time, executable và cấu hình process để xác minh ownership. Khóa start theo runtime để hai invocation tránh tạo server trùng. Xác minh service sẵn có trên endpoint trước khi dùng.

### 5.2. Study mới

Mọi CLI invocation trong pipeline dùng cùng profile và study config đã resolve.

```text
resolve profile + kiểm tra prerequisites
  → khởi chạy hoặc xác minh Ollama của profile
  → doctor
  → check-model (gồm tool calling)
  → prepare
  → validate
  → plan-runs (freeze model snapshot + protocol)
  → run-all
  → summarize
```

Sau khi triển khai, người dùng gọi wrapper tương ứng trên từng máy.

```bash
# Trên Saturn
bash scripts/run_experiment_a_saturn.sh

# Trên Neptune
bash scripts/run_experiment_a_neptune.sh
```

Setup/pull model là bước triển khai riêng. Trong thực nghiệm, Ollama version và model digest được giữ theo protocol đã freeze. Mỗi invocation tự nạp profile nên người dùng có thể chạy wrapper trong terminal mới.

### 5.3. `run-all`, resume và interruption

- Thêm subcommand Python `run-all` dùng lại `run_next`, task selection và study lock. Chốt một cấp giữ lock để tránh khóa lồng nhau khi gọi runner. Dùng kết quả cấu trúc của runner để điều khiển vòng lặp.
- In tiến độ task/branch/seed/attempt/profile và trạng thái từng lượt. Chạy tuần tự trong mỗi study, model concurrency khởi đầu 1.
- Batch hoàn thành khi mọi group task/seed đã có attempt hợp lệ được chấm đủ các nhánh. Khi gặp `no_pending_runs`, kiểm tra group còn thiếu cùng các trạng thái `running`, `interrupted`, `agent_done`, `evaluation_error` để quyết định exit code. Attempt invalid đã có attempt thay thế hoàn tất chỉ thuộc lịch sử.
- `interrupted`, `evaluation_error`, lỗi format/provider hoặc trạng thái chờ quota Google làm batch dừng với mã lỗi và hướng dẫn tiếp tục. Verifier `pass`, `fail_test`, `apply_failure` cho phép tiếp tục lượt kế tiếp.
- Khi chạy lại wrapper có schedule, xác minh snapshot/protocol và tiếp tục schedule hiện tại với prepared/validation đã freeze. Artifact thiếu/lệch được báo để xử lý bằng lệnh tương ứng.
- Group gián đoạn cần thao tác tường minh bằng cơ chế `--restart-group` hiện có. Tạo attempt mới, giữ artifact và trạng thái invalid của attempt trước.
- Khi evaluation lỗi nhưng đã có patch, dùng `evaluate` để chấm lại. Resume đọc trạng thái schedule sau khi chấm.
- `Ctrl+C`/`SIGTERM` ghi trạng thái bền vững, đóng backend và dọn container do invocation tạo. Khóa tồn dư sau hard kill được xử lý sau khi xác minh process giữ khóa đã kết thúc.

Ví dụ các lệnh CLI dự kiến sau triển khai để tiếp tục một study Saturn như sau. Profile tự chọn study config mặc định của máy.

```bash
uv run --frozen deepswe-exp --profile saturn run-next --restart-group --run-id <run-id>
uv run --frozen deepswe-exp --profile saturn evaluate --run-id <run-id>
bash scripts/run_experiment_a_saturn.sh
```

Với Neptune dùng `--profile neptune` và wrapper Neptune. Khi dùng study config tùy chỉnh, truyền cùng `--config` cho mọi lệnh liên quan.

### 5.4. Process và log

Ollama chạy nền để người dùng mở terminal khác vẫn gọi API. Mặc định kết thúc thực nghiệm giữ server hoạt động. Stop là lệnh riêng có kiểm tra ownership. Nếu thêm tùy chọn dừng lúc thoát, chỉ áp dụng cho process invocation đó đã khởi tạo.

Log Ollama lưu trong `state_dir` của profile. Pipeline log lưu theo timestamp dưới `runs/<study_id>/logs/`, bảo toàn exit code khi dùng `tee`. Nếu chưa tạo được log, stderr nêu bước thất bại. Cuối pipeline in đường dẫn summary/artifact thực do `summarize` tạo và trạng thái các lượt còn thiếu nếu có.

## 6. Kiểm tra khi triển khai

### 6.1. Trong workspace local

- Kiểm tra hai profile, đường dẫn, environment xung đột, caller `Settings` cũ và cấu hình Google hiện hành.
- Fake model cho từng profile thực hiện vòng tool call/ToolMessage/trả lời tiếp và xử lý usage thiếu.
- Kiểm tra ledger Ollama với budget, và quota Google bằng fake clock/client.
- Kiểm tra đổi digest/quantization/context/options bị phát hiện, output hai study riêng, schedule Google cũ xác minh được.
- Kiểm tra `run-all`, resume, restart group và retry evaluation giữ đúng trạng thái/attempt/artifact.
- Syntax check/lint Bash bằng công cụ sẵn có. Mock binary/endpoint để kiểm tra port bận, PID stale/reuse, start đồng thời, terminal mới, startup lỗi và đường dẫn có khoảng trắng.

### 6.2. Trên từng server

1. Thu model metadata, Ollama version, runtime/model paths và cấu hình GPU thực tế. Chốt profile theo dữ liệu này.
2. Chạy doctor và check-model của profile, xác nhận tool calling và context/output limits.
3. Dùng study smoke riêng, chạy một group đủ các nhánh đã chọn. Kiểm tra Docker, trajectory, patch, verifier và report.
4. Mở terminal mới, gọi wrapper và xác nhận environment được nạp, resume đúng study.
5. Dùng study kiểm tra riêng để kiểm tra lỗi endpoint và model digest thay đổi trước batch chính thức.
6. Chạy batch, thu artifact riêng của Saturn và Neptune.

## 7. Trình tự và tiêu chí bàn giao

| Bước | Deliverable |
|---|---|
| 1 | Profile/schema settings, hai study config, dependency và lockfile |
| 2 | Provider factory Ollama, ledger local, doctor/check-model |
| 3 | Snapshot/protocol metadata, tương thích artifact và phân loại lỗi |
| 4 | `run-all`, start/stop chung, hai wrapper chạy thực nghiệm |
| 5 | Kiểm tra local, cập nhật tài liệu one-click và hướng dẫn Neptune |
| 6 | Xác nhận metadata hai server, smoke theo profile và chạy batch |

Hoàn thành khi hai wrapper chạy được từ terminal mới với đúng profile, tạo artifact theo study riêng và tiếp tục được schedule theo protocol đã freeze. Metadata phải ghi model digest, quantization, context, reasoning/sampling settings và budget mỗi server. Kết quả kiểm tra local và smoke trên từng server phải được ghi nhận trước khi bàn giao luồng batch.
