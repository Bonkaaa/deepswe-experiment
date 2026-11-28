from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.evaluation.patches import collect_diff
from deepswe_exp.components.evaluation.verifier import _classify_patcheval_error, run_patcheval_patch
from deepswe_exp.components.execution.runner import run_all
from deepswe_exp.components.execution.validation import validate_task
from deepswe_exp.components.tasks.patcheval_tests import (
    discover_patcheval_tests,
    filter_patcheval_snapshot,
)
from deepswe_exp.components.tasks.tasks import load_task, verify_benchmark_record
from deepswe_exp.components.tasks.variants import choose, source_hash


def _write_patcheval_task(root: Path, cve_id: str, language: str = "Python") -> Path:
    task_dir = root / cve_id
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "benchmark": {
            "type": "patcheval",
            "name": "PatchEval-Verified",
            "language": language,
            "image_ref": f"ghcr.io/patcheval-cve/patcheval-cve:{cve_id.lower()}",
        },
        "instance": {
            "instance_id": cve_id,
            "cve_id": cve_id,
            "cve_description": "Test vulnerability description",
            "cwe_info": {"CWE-78": {"name": "OS Command Injection"}},
            "cwe_ids": ["CWE-78"],
            "category": f"{language}:CWE-78",
            "repo": "https://github.com/example/sample-repo",
            "programming_language": language,
            "image": f"ghcr.io/patcheval-cve/patcheval-cve:{cve_id.lower()}",
            "problem_statement": "Fix the vulnerability.",
            "environment": {"cpus": 2, "memory_mb": 8192},
        },
    }
    (task_dir / "instance.json").write_text(json.dumps(payload), encoding="utf-8")
    return task_dir


def test_load_patcheval_task_and_verify_record(tmp_path: Path):
    task_dir = _write_patcheval_task(tmp_path, "CVE-2024-99999", "Go")
    task = load_task(task_dir)
    assert task.is_patcheval
    assert not task.is_swebench
    assert task.benchmark_type == "patcheval"
    assert task.language == "Go"
    assert task.workdir == "/workspace"
    assert task.image == "ghcr.io/patcheval-cve/patcheval-cve:cve-2024-99999"

    from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY, benchmark_container_limits
    from deepswe_exp.components.tasks.tasks import benchmark_record_digest

    prepared = {
        "benchmark_instance_hash": benchmark_record_digest(task),
        "grader_container_policy": {**BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata)},
    }
    # Must NOT require installed swebench revision
    verify_benchmark_record(task, prepared)


def test_python_discover_filter_and_source_hash(tmp_path: Path):
    (tmp_path / "app.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        "def test_one():\n    assert 1 == 1\n\n"
        "def test_two():\n    assert 2 == 2\n\n"
        "class TestSuite:\n    def test_three(self):\n        assert 3 == 3\n",
        encoding="utf-8",
    )
    before_hash = source_hash(tmp_path, ["."], language="Python")
    groups = discover_patcheval_tests(tmp_path, "Python")
    assert len(groups) == 3

    manifest_zero = choose(groups, "0", seed=42, allow_empty=True)
    filter_patcheval_snapshot(tmp_path, manifest_zero, "Python")
    assert not (tests_dir / "test_app.py").exists()
    assert source_hash(tmp_path, ["."], language="Python") == before_hash


def test_go_discover_filter_and_prune_imports(tmp_path: Path):
    (tmp_path / "main.go").write_text("package main\nfunc Add(a, b int) int { return a + b }\n", encoding="utf-8")
    (tmp_path / "main_test.go").write_text(
        'package main\n\nimport (\n\t"strings"\n\t"testing"\n)\n\n'
        'func TestAlpha(t *testing.T) {\n\t_ = strings.TrimSpace("a")\n}\n\n'
        'func TestBeta(t *testing.T) {\n\tif Add(1, 2) != 3 { t.Fatal("bad") }\n}\n',
        encoding="utf-8",
    )
    before_hash = source_hash(tmp_path, ["."], language="Go")
    groups = discover_patcheval_tests(tmp_path, "Go")
    assert [g["function"] for g in groups] == ["TestAlpha", "TestBeta"]

    manifest = {
        "groups": groups,
        "kept_groups": ["main_test.go::TestBeta"],
    }
    filter_patcheval_snapshot(tmp_path, manifest, "Go")
    filtered = (tmp_path / "main_test.go").read_text(encoding="utf-8")
    assert "TestAlpha" not in filtered
    assert "TestBeta" in filtered
    assert '"strings"' not in filtered
    assert '"testing"' in filtered
    assert source_hash(tmp_path, ["."], language="Go") == before_hash


def test_js_discover_and_filter_and_collect_diff(tmp_path: Path):
    base = tmp_path / "base"
    final = tmp_path / "final"
    (base / "test").mkdir(parents=True)
    (base / "index.js").write_text("module.exports = (x) => x;\n", encoding="utf-8")
    (base / "test" / "index.test.js").write_text(
        "describe('suite', () => {\n"
        "  it('first', () => { expect(1).toBe(1); });\n"
        "  test('second', () => { expect(2).toBe(2); });\n"
        "});\n",
        encoding="utf-8",
    )
    before_hash = source_hash(base, ["."], language="JavaScript")
    groups = discover_patcheval_tests(base, "JavaScript")
    assert len(groups) == 2

    manifest = choose(groups, "50-random", seed=42, allow_empty=True)
    filter_patcheval_snapshot(base, manifest, "JavaScript")
    assert source_hash(base, ["."], language="JavaScript") == before_hash

    # Verify collect_diff excludes JS test edits from agent_patch.diff
    (final / "test").mkdir(parents=True)
    (final / "index.js").write_text("module.exports = (x) => String(x);\n", encoding="utf-8")
    (final / "test" / "index.test.js").write_text("// modified test\n", encoding="utf-8")
    full_diff, product_diff = collect_diff(base, final, tmp_path / "out", ["."], language="JavaScript")
    assert "index.test.js" in full_diff.read_text(encoding="utf-8")
    assert "index.test.js" not in product_diff.read_text(encoding="utf-8")
    assert "index.js" in product_diff.read_text(encoding="utf-8")


def test_choose_allows_empty_for_zero_test_repos():
    manifest = choose([], "50-random", seed=42, allow_empty=True)
    assert manifest["total_cases"] == 0
    assert manifest["kept_cases"] == 0
    assert manifest["actual_ratio"] == 0.0


def test_classify_patcheval_errors_and_run_patcheval_patch(tmp_path: Path):
    assert _classify_patcheval_error("error: patch does not apply", "Python") == ("apply_failure", "apply_fail")
    assert _classify_patcheval_error("SyntaxError: invalid syntax", "Python") == ("fail_test", "compilation_fail")
    assert _classify_patcheval_error("AssertionError: exploit succeeded", "Python") == ("fail_test", "validation_fail")
    assert _classify_patcheval_error("main.go:12:5: undefined: foo", "Go") == ("fail_test", "compilation_fail")

    task_dir = _write_patcheval_task(tmp_path, "CVE-2024-00001", "Python")
    task = load_task(task_dir)
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")

    class FakeBox:
        def __init__(self, rc, out):
            self.rc = rc
            self.out = out
            self.written = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def put_bytes(self, path, data):
            self.written[path] = data

        def execute(self, cmd, timeout=600):
            return self.rc, self.out

    class FakeBackend:
        def __init__(self, rc, out):
            self.box = FakeBox(rc, out)

        def sandbox(self, image, task, workdir=None):
            assert workdir == "/workspace"
            return self.box

    res_pass = run_patcheval_patch(FakeBackend(0, "All tests passed"), task, task.image, patch_file, tmp_path / "eval_pass")
    assert res_pass["status"] == "pass"
    assert res_pass["reward"] == 1
    assert res_pass["benchmark"]["validation_type"] == "Repair Success"

    res_timeout = run_patcheval_patch(FakeBackend(124, "timed out"), task, task.image, patch_file, tmp_path / "eval_to")
    assert res_timeout["status"] == "timeout"
    assert res_timeout["reward"] is None


def test_settings_tasks_file_and_parallel_run_all(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "system_prompt.md").write_text("sys", encoding="utf-8")
    (tmp_path / "tasks.json").write_text(json.dumps(["CVE-2024-0001", "CVE-2024-0002"]), encoding="utf-8")
    cfg = tmp_path / "config" / "study.yaml"
    cfg.write_text(
        "study_id: test-parallel\n"
        "data_root: data\n"
        "output_root: runs\n"
        "tasks_file: tasks.json\n"
        "branches: ['100', '0']\n"
        "workers: 4\n"
        "continue_on_error: true\n"
        "validation_repeats: 1\n"
        "budget:\n"
        "  max_model_requests: 10\n"
        "  wall_time_seconds: 1800\n"
        "  max_output_tokens: 4096\n"
        "  max_tool_output_chars: 8000\n"
        "  tool_timeout_seconds: 60\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path=cfg)
    assert settings.task_ids == ["CVE-2024-0001", "CVE-2024-0002"]
    assert settings.workers == 4
    assert settings.continue_on_error is True
    assert settings.validation_repeats == 1

    # Verify parallel execution in run_all uses ThreadPoolExecutor concurrently
    sched_data = {
        "schema_version": 1,
        "protocol_hash": "abc",
        "task_ids": ["CVE-2024-0001", "CVE-2024-0002"],
        "runs": [
            {"task_id": "CVE-2024-0001", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r1", "status": "pending", "group_valid": True},
            {"task_id": "CVE-2024-0001", "branch": "0", "seed": 42, "attempt_id": "attempt-001", "run_id": "r2", "status": "pending", "group_valid": True},
            {"task_id": "CVE-2024-0002", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r3", "status": "pending", "group_valid": True},
            {"task_id": "CVE-2024-0002", "branch": "0", "seed": 42, "attempt_id": "attempt-001", "run_id": "r4", "status": "pending", "group_valid": True},
        ],
    }
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_execute(s, backend, run, value, schedule_path, ledger, lock=None):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with active_lock:
            active -= 1
        with lock:
            run["status"] = "scored"
        return run

    with patch("deepswe_exp.components.execution.runner.schedule", return_value=sched_data), \
         patch("deepswe_exp.components.execution.runner._build_ledger", return_value=SimpleNamespace(remaining=lambda: 9999)), \
         patch("deepswe_exp.components.execution.runner._execute_run", side_effect=fake_execute):
        res = run_all(settings, backend=object(), workers=4, continue_on_error=True)

    assert res["status"] == "completed"
    assert res["completed"] == 4
    assert max_active >= 2


def test_clean_patcheval_isolation_and_audit():
    from deepswe_exp.components.execution.preparation import clean_patcheval

    cmds = []

    class FakeBox:
        cwd = "/workspace/sample-repo"
        user = "root"

        def checked(self, cmd, timeout=120):
            cmds.append(cmd)
            return ""

        def execute(self, cmd, timeout=120):
            cmds.append(cmd)
            return 0, ""

    box = FakeBox()
    clean_patcheval(box, "/workspace/sample-repo")
    joined = "\n".join(cmds)
    assert "rm -f /workspace/fix.patch /workspace/fix-run.sh /workspace/prepare.sh" in joined
    assert "! -name sample-repo" in joined
    assert "test ! -e /workspace/fix-run.sh" in joined
    assert box.user == "10001"


def test_validate_patcheval_task_single_repeat(tmp_path: Path):
    from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY, benchmark_container_limits
    from deepswe_exp.components.results.storage import write_json
    from deepswe_exp.components.tasks.tasks import benchmark_record_digest

    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "system_prompt.md").write_text("sys", encoding="utf-8")
    task_dir = _write_patcheval_task(tmp_path / "data", "CVE-2024-11111", "Python")
    task = load_task(task_dir)

    cfg = tmp_path / "config" / "study.yaml"
    cfg.write_text(
        "study_id: test-val\n"
        "data_root: data\n"
        "output_root: runs\n"
        "tasks: ['CVE-2024-11111']\n"
        "branches: ['100', '0']\n"
        "validation_repeats: 1\n"
        "budget:\n"
        "  max_model_requests: 10\n"
        "  wall_time_seconds: 1800\n"
        "  max_output_tokens: 4096\n"
        "  max_tool_output_chars: 8000\n"
        "  tool_timeout_seconds: 60\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path=cfg)
    prepared = {
        "schema_version": 1,
        "task_id": "CVE-2024-11111",
        "verifier_image": task.image,
        "benchmark_instance_hash": benchmark_record_digest(task),
        "grader_container_policy": {**BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata)},
        "benchmark": task.benchmark,
    }
    write_json(settings.output_root / "CVE-2024-11111" / "prepared.json", prepared)

    class FakeBox:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def put_bytes(self, path, data): pass
        def execute(self, cmd, timeout=600): return 1, "PoC exploit succeeded"

    class FakeBackend:
        def sandbox(self, image, task, workdir=None): return FakeBox()

    val = validate_task(settings, "CVE-2024-11111", FakeBackend())
    assert val["valid"] is True
    assert len(val["records"]["base"]) == 1


def test_run_all_continue_on_error_and_30m_timeout_cap(tmp_path: Path):
    from deepswe_exp.components.agent.mini_swe_model import build_mini_swe_model

    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "system_prompt.md").write_text("sys", encoding="utf-8")
    cfg = tmp_path / "config" / "study.yaml"
    cfg.write_text(
        "study_id: test-err\n"
        "data_root: data\n"
        "output_root: runs\n"
        "tasks: ['CVE-1', 'CVE-2']\n"
        "branches: ['100']\n"
        "workers: 2\n"
        "continue_on_error: true\n"
        "budget:\n"
        "  max_model_requests: 10\n"
        "  wall_time_seconds: 1800\n"
        "  max_output_tokens: 4096\n"
        "  max_tool_output_chars: 8000\n"
        "  tool_timeout_seconds: 60\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path=cfg)
    model = build_mini_swe_model(settings)
    assert model.config.model_kwargs["timeout"] <= 300

    sched_data = {
        "schema_version": 1,
        "protocol_hash": "abc",
        "task_ids": ["CVE-1", "CVE-2"],
        "runs": [
            {"task_id": "CVE-1", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r1", "status": "pending", "group_valid": True},
            {"task_id": "CVE-2", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r2", "status": "pending", "group_valid": True},
        ],
    }

    def fake_execute(s, backend, run, value, schedule_path, ledger, lock=None):
        with lock:
            run["status"] = "interrupted" if run["task_id"] == "CVE-1" else "scored"
        return run

    with patch("deepswe_exp.components.execution.runner.schedule", return_value=sched_data), \
         patch("deepswe_exp.components.execution.runner._build_ledger", return_value=SimpleNamespace(remaining=lambda: 9999)), \
         patch("deepswe_exp.components.execution.runner._execute_run", side_effect=fake_execute):
        res = run_all(settings, backend=object(), workers=2, continue_on_error=True)

    assert res["status"] == "incomplete"
    assert res["completed"] == 1
    assert len(res["failed_runs"]) == 1
    assert res["failed_runs"][0]["task_id"] == "CVE-1"


def test_js_edge_cases_regex_chained_timeout_and_nested_test_dirs(tmp_path: Path):
    base = tmp_path / "base"
    final = tmp_path / "final"
    (base / "packages" / "ui" / "test").mkdir(parents=True)
    (base / "pkg" / "tests").mkdir(parents=True)
    (base / "src.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (base / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (base / "pkg" / "tests" / "b.py").write_text("class MyCase:\n    def test_a(self): assert True\n", encoding="utf-8")
    (base / "packages" / "ui" / "test" / "edge.js").write_text(
        "function test(x) { return x; }\n"
        "// it('commented', () => {});\n"
        "describe('edge', () => {\n"
        "  it('first', () => { const r = /\\)/.test(')'); expect(test(1)).toBe(1); }).timeout(5000);\n"
        "  test('second', () => { expect(test(2)).toBe(2); });\n"
        "});\n",
        encoding="utf-8",
    )
    js_groups = discover_patcheval_tests(base, "JavaScript")
    assert len(js_groups) == 2
    filter_patcheval_snapshot(base, {"groups": js_groups, "kept_groups": [js_groups[1]["id"]]}, "JavaScript")
    filtered = (base / "packages" / "ui" / "test" / "edge.js").read_text(encoding="utf-8")
    assert "function test(x)" in filtered
    assert ".timeout(5000)" not in filtered
    assert "expect(test(2)).toBe(2)" in filtered

    py_groups = discover_patcheval_tests(base, "Python")
    assert len(py_groups) == 1
    assert py_groups[0]["class"] == "MyCase"

    # Nested test directories must be excluded from product diff for both Python and JS
    (final / "packages" / "ui" / "test").mkdir(parents=True)
    (final / "pkg" / "tests").mkdir(parents=True)
    (final / "src.js").write_text("module.exports = 2;\n", encoding="utf-8")
    (final / "packages" / "ui" / "test" / "edge.js").write_text("// changed\n", encoding="utf-8")
    (final / "pkg" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (final / "pkg" / "tests" / "b.py").write_text("# changed\n", encoding="utf-8")

    _, js_prod = collect_diff(base, final, tmp_path / "out_js", ["."], language="JavaScript")
    assert "edge.js" not in js_prod.read_text(encoding="utf-8")
    assert "src.js" in js_prod.read_text(encoding="utf-8")

    _, py_prod = collect_diff(base, final, tmp_path / "out_py", ["."], language="Python")
    assert "pkg/tests/b.py" not in py_prod.read_text(encoding="utf-8")
    assert "pkg/app.py" in py_prod.read_text(encoding="utf-8")


def test_go_gopkg_v2_import_kept_and_nested_workspace_isolation(tmp_path: Path):
    from deepswe_exp.components.execution.preparation import _hide_workspace_payload

    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "main_test.go").write_text(
        'package main\n\nimport (\n\t"strings"\n\t"gopkg.in/yaml.v2"\n\t"testing"\n)\n\n'
        'func TestAlpha(t *testing.T) { _ = strings.TrimSpace("a") }\n'
        'func TestBeta(t *testing.T) { _ = yaml.Unmarshal(nil, nil) }\n',
        encoding="utf-8",
    )
    groups = discover_patcheval_tests(tmp_path, "Go")
    filter_patcheval_snapshot(tmp_path, {"groups": groups, "kept_groups": ["main_test.go::TestBeta"]}, "Go")
    filtered = (tmp_path / "main_test.go").read_text(encoding="utf-8")
    assert '"gopkg.in/yaml.v2"' in filtered
    assert '"strings"' not in filtered

    # Verify nested /workspace/org/repo prunes both /workspace and /workspace/org without || true
    cmds = []

    class FakeBox:
        def checked(self, cmd, timeout=120):
            cmds.append(cmd)
            return ""

    _hide_workspace_payload(FakeBox(), "/workspace/org/repo")
    assert any("! -name org" in c and "|| true" not in c for c in cmds)
    assert any("/workspace/org" in c and "! -name repo" in c for c in cmds)

    cmds.clear()
    _hide_workspace_payload(FakeBox(), "/workspace")
    assert any("git -C /workspace clean -fdx" in c for c in cmds)


def test_parallel_run_all_fail_fast_and_execute_run_pre_try_error(tmp_path: Path):
    from deepswe_exp.components.execution.runner import _execute_run

    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "system_prompt.md").write_text("sys", encoding="utf-8")
    cfg = tmp_path / "config" / "study.yaml"
    cfg.write_text(
        "study_id: test-ff\n"
        "data_root: data\n"
        "output_root: runs\n"
        "tasks: ['CVE-MISSING']\n"
        "branches: ['100']\n"
        "workers: 2\n"
        "continue_on_error: false\n"
        "budget:\n"
        "  max_model_requests: 10\n"
        "  wall_time_seconds: 1800\n"
        "  max_output_tokens: 4096\n"
        "  max_tool_output_chars: 8000\n"
        "  tool_timeout_seconds: 60\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path=cfg)
    sched_path = settings.output_root / "schedule.json"
    run = {"task_id": "CVE-MISSING", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r1", "status": "pending", "group_valid": True, "protocol_hash": "abc"}
    value = {"runs": [run]}
    # _execute_run must catch missing task/prepared.json inside try: and mark interrupted instead of crashing pool
    res = _execute_run(settings, object(), run, value, sched_path, object(), lock=threading.Lock())
    assert res["status"] == "interrupted"
    assert res["group_valid"] is False

    # Parallel run_all with continue_on_error=False returns failure dict
    sched_data = {
        "schema_version": 1, "protocol_hash": "abc", "task_ids": ["CVE-MISSING"],
        "runs": [
            {"task_id": "CVE-MISSING", "branch": "100", "seed": 42, "attempt_id": "attempt-001", "run_id": "r1", "status": "pending", "group_valid": True},
        ],
    }
    with patch("deepswe_exp.components.execution.runner.schedule", return_value=sched_data), \
         patch("deepswe_exp.components.execution.runner._build_ledger", return_value=SimpleNamespace(remaining=lambda: 9999)), \
         patch("deepswe_exp.components.execution.runner._execute_run", return_value={**sched_data["runs"][0], "status": "interrupted"}):
        ff_res = run_all(settings, backend=object(), workers=2, continue_on_error=False)
    assert ff_res["status"] == "interrupted"
    assert ff_res["failed_run_id"] == "r1"


def test_safe_extract_skips_unsafe_fixture_symlinks_when_enabled(tmp_path: Path):
    import io, tarfile
    from deepswe_exp.components.execution.docker_env import DockerFailure, safe_extract

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("app.go")
        data = b"package main\n"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        sym = tarfile.TarInfo("testdata/invalid-symlink")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/non/existing/file"
        tf.addfile(sym)
    raw = buf.getvalue()

    with pytest.raises(DockerFailure, match="Unsafe symlink"):
        safe_extract(raw, tmp_path / "strict")

    safe_extract(raw, tmp_path / "relaxed", skip_unsafe_symlinks=True)
    assert (tmp_path / "relaxed" / "app.go").is_file()
    assert not (tmp_path / "relaxed" / "testdata" / "invalid-symlink").exists()


def test_batch_runner_chunking_cleanup_and_merge_summaries(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_patcheval_batches",
        Path(__file__).resolve().parents[1] / "scripts" / "run_patcheval_batches.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 230 CVEs -> 10 batches of 23
    chunks = mod.chunk_tasks([f"CVE-{i}" for i in range(230)], 23)
    assert len(chunks) == 10
    assert all(len(c) == 23 for c in chunks)

    # Cleanup removes base/, final/, prepared/*/*/snapshot but preserves trajectories/diffs/summary
    batch_root = tmp_path / "batch_01"
    (batch_root / "CVE-1" / "base").mkdir(parents=True)
    (batch_root / "CVE-1" / "base" / "big.bin").write_bytes(b"123")
    (batch_root / "CVE-1" / "prepared" / "100" / "42" / "snapshot").mkdir(parents=True)
    (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "final").mkdir(parents=True)
    (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "trajectory.json").write_text("{}", encoding="utf-8")
    (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "agent_patch.diff").write_text("diff", encoding="utf-8")
    (batch_root / "summary").mkdir(parents=True)
    (batch_root / "summary" / "runs.csv").write_text(
        "task_id,language,branch,seed,attempt_id,group_valid,run_status,evaluation_status,reward\n"
        "CVE-1,Go,100,42,attempt-001,True,scored,pass,1\n"
        "CVE-1,Go,50-random,42,attempt-001,True,scored,fail_test,0\n"
        "CVE-1,Go,0,42,attempt-001,True,scored,fail_test,0\n",
        encoding="utf-8-sig",
    )

    mod.cleanup_batch_artifacts(batch_root, [], remove_images=False)
    assert not (batch_root / "CVE-1" / "base").exists()
    assert not (batch_root / "CVE-1" / "prepared" / "100" / "42" / "snapshot").exists()
    assert not (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "final").exists()
    assert (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "trajectory.json").is_file()
    assert (batch_root / "CVE-1" / "100" / "42" / "attempt-001" / "agent_patch.diff").is_file()

    all_csv, all_md = mod.merge_batch_summaries(tmp_path, ["100", "50-random", "0"])
    assert all_csv.is_file()
    md_text = all_md.read_text(encoding="utf-8")
    assert "Tổng số lượt chạy (runs): **3**" in md_text
    assert "| Go | 3 | 1 | 33.33% |" in md_text
    assert "`100=1 → 0=0`: 1 cặp" in md_text




