from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace
from pathlib import Path

from langchain_core.messages import AIMessage

from deepswe_exp.components.agent.agent import run_agent
from deepswe_exp.components.agent.quota import QuotaLedger, QuotaUnavailable
from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.evaluation.patches import collect_diff
from deepswe_exp.components.evaluation.verifier import parse_result
from deepswe_exp.components.results.report import _paired_groups, _test_visibility, summarize
from deepswe_exp.components.results.storage import write_json, scratch_directory as create_scratch_directory
from deepswe_exp.components.tasks.variants import choose, filter_snapshot, inventory, snapshot_hash
from deepswe_exp.components.execution.runner import restart_group
from deepswe_exp.components.execution.docker_env import docker_status

TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-temp"


def scratch_directory():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return create_scratch_directory(TEMP_ROOT)


class FakeLedger:
    def __init__(self):
        self.requests = 0
        self.usages = []

    def reserve(self, estimate, deadline, clock):
        self.requests += 1

    def usage(self, usage):
        self.usages.append(usage)


class FakeModel:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "run_shell", "args": {"command": "echo smoke"},
                "id": "tool-smoke-1", "type": "tool_call",
            }])
        return AIMessage(content="Done")


class FakeBox:
    def __init__(self):
        self.commands = []

    def execute(self, command, timeout):
        self.commands.append((command, timeout))
        return 0, "smoke output"


class ComponentSmokeTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings()

    def test_settings_load_and_hide_api_key(self):
        rendered = repr(self.settings)
        self.assertNotIn(self.settings.api_key, rendered)
        self.assertNotIn("GEMINI_API_KEY", rendered)
        self.assertTrue(self.settings.prompt_path.is_file())

    def test_seeded_manifest_and_ast_filter_remove_test_functions(self):
        rows = [
            {"node_id": "tests/unit/morphing/test_name.py::test_one[1]", "file": "tests/unit/morphing/test_name.py", "function": "test_one", "class": None, "fixtures": ["tmp_path"]},
            {"node_id": "tests/unit/morphing/test_name.py::test_one[2]", "file": "tests/unit/morphing/test_name.py", "function": "test_one", "class": None, "fixtures": ["tmp_path"]},
            {"node_id": "tests/unit/morphing/test_name.py::TestNames::test_two", "file": "tests/unit/morphing/test_name.py", "function": "test_two", "class": "TestNames", "fixtures": []},
        ]
        groups = inventory(rows)
        first = choose(groups, "50-random", 42)
        second = choose(groups, "50-random", 42)
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        self.assertEqual(len(groups), 2)  # The parametrized cases are one group.
        empty = choose(groups, "0", 42)
        self.assertEqual(empty["kept_cases"], 0)
        with scratch_directory() as tmp:
            root = Path(tmp)
            test_file = root / rows[0]["file"]
            test_file.parent.mkdir(parents=True)
            test_file.write_text(
                "import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\ndef test_one(x):\n    assert x\n\nclass TestNames:\n    def test_two(self):\n        assert True\n",
                encoding="utf-8",
            )
            filter_snapshot(root, empty)
            text = test_file.read_text(encoding="utf-8")
            self.assertNotIn("def test_one", text)
            self.assertNotIn("class TestNames", text)
            compile(text, str(test_file), "exec")

    def test_snapshot_hash_changes_when_visible_test_file_changes(self):
        with scratch_directory() as tmp:
            root = Path(tmp) / "snapshot"
            path = root / "tests/integration/morphing/test_names.py"
            path.parent.mkdir(parents=True)
            path.write_text("def test_name():\n    assert True\n", encoding="utf-8")
            first = snapshot_hash(root)
            path.write_text("def test_name():\n    assert False\n", encoding="utf-8")
            self.assertNotEqual(first, snapshot_hash(root))

    def test_related_variant_records_scores_and_keeps_exact_random_case_count(self):
        rows = [
            {"node_id": "tests/integration/morphing/test_a.py::test_a", "file": "tests/integration/morphing/test_a.py", "function": "test_a", "class": None},
            {"node_id": "tests/integration/morphing/test_b.py::test_b", "file": "tests/integration/morphing/test_b.py", "function": "test_b", "class": None},
            {"node_id": "tests/integration/morphing/test_c.py::test_c", "file": "tests/integration/morphing/test_c.py", "function": "test_c", "class": None},
        ]
        groups = inventory(rows)
        scores = {group["id"]: float(index) for index, group in enumerate(groups)}
        random_variant = choose(groups, "50-random", 42)
        related = choose(groups, "50-related", 42, scores)
        self.assertEqual(related["kept_cases"], random_variant["kept_cases"])
        self.assertEqual(related["relevance_ranking"][0], groups[-1]["id"])
        self.assertEqual(related["selected_relevance_score"], max(scores.values()))

    def test_patch_diff_includes_add_delete_and_filters_product_scope(self):
        with scratch_directory() as tmp:
            root = Path(tmp)
            base = root / "base"
            final = root / "final"
            base.mkdir()
            (base / "src/pkg").mkdir(parents=True)
            (base / "src/pkg/old.py").write_text("old = True\n", encoding="utf-8")
            (base / "src/pkg/change.py").write_text("value = 1\n", encoding="utf-8")
            (base / "README.md").write_text("base\n", encoding="utf-8")
            final.mkdir()
            (final / "src/pkg").mkdir(parents=True)
            (final / "src/pkg/change.py").write_text("value = 2\n", encoding="utf-8")
            (final / "src/pkg/new.py").write_text("new = True\n", encoding="utf-8")
            (final / "README.md").write_text("agent note\n", encoding="utf-8")
            full, product = collect_diff(base, final, root / "out", ["src/pkg/"])
            full_text = full.read_text(encoding="utf-8")
            product_text = product.read_text(encoding="utf-8")
            self.assertIn("src/pkg/new.py", product_text)
            self.assertIn("src/pkg/old.py", product_text)
            self.assertIn("README.md", full_text)
            self.assertNotIn("README.md", product_text)

    def test_verifier_result_parser_keeps_infrastructure_errors_separate(self):
        with scratch_directory() as tmp:
            root = Path(tmp)
            (root / "reward.json").write_text(json.dumps({"reward": 0, "f2p_total": 1, "f2p_passed": 0}), encoding="utf-8")
            self.assertEqual(parse_result(root, 0)["status"], "infrastructure_error")
            (root / "ctrf.json").write_text(json.dumps({"results": {"tests": [{"name": "[f2p] test", "status": "failed"}]}}), encoding="utf-8")
            self.assertEqual(parse_result(root, 0)["status"], "fail_test")
            (root / "reward.json").write_text(json.dumps({"reward": 0, "apply_failed": 1}), encoding="utf-8")
            self.assertEqual(parse_result(root, 0)["status"], "apply_failure")
            self.assertEqual(parse_result(root, 124, timed_out=True)["status"], "timeout")

    def test_evaluator_node_names_match_pytest_node_ids(self):
        report = {"results": {"tests": [{
            "name": "[p2p] tests.integration.morphing.test_names.test_alias[param]",
            "status": "passed",
        }]}}
        manifest = {"kept_node_ids": [
            "tests/integration/morphing/test_names.py::test_alias[param]",
        ]}
        self.assertEqual(_test_visibility({"_ctrf": report}, manifest), (1, 1, 0))

    def test_restart_supersedes_stale_running_attempt(self):
        group = [
            {"task_id": "t", "seed": 42, "attempt_id": "attempt-001", "branch": branch,
             "run_id": str(index), "status": status, "group_valid": True}
            for index, (branch, status) in enumerate((
                ("100", "running"), ("50-random", "pending"), ("0", "scored"),
            ))
        ]
        schedule = {"runs": group}
        restart_group(schedule, "0")
        old = schedule["runs"][:3]
        new = schedule["runs"][3:]
        self.assertEqual([row["status"] for row in old], ["interrupted", "superseded", "scored"])
        self.assertTrue(all(not row["group_valid"] for row in old))
        self.assertTrue(all(row["status"] == "pending" and row["group_valid"] for row in new))

    def test_paired_summary_excludes_smoke_and_apply_failures(self):
        branches = ["100", "50-random", "0"]
        rows = [
            {"task_id": "t", "seed": 42, "attempt_id": "attempt-001", "branch": branch,
             "group_valid": True, "run_status": "scored", "evaluation_status": "pass", "reward": 1,
             "experiment_role": "experiment"}
            for branch in branches
        ]
        rows[0]["evaluation_status"] = "apply_failure"
        self.assertEqual(_paired_groups(rows, branches)[:2], (0, 1))
        for row in rows:
            row["evaluation_status"] = "pass"
            row["experiment_role"] = "smoke_test"
        self.assertEqual(_paired_groups(rows, branches)[:2], (0, 0))

    def test_quota_ledger_persists_reserved_requests(self):
        with scratch_directory() as tmp:
            current = [1_800_000_000.0]

            def sleep(seconds):
                current[0] += seconds

            ledger = QuotaLedger(Path(tmp) / "quota.jsonl", {"rpm": 10, "tpm": 10000, "rpd": 1},
                                 "fake-model", clock=lambda: current[0], sleep=sleep)
            ledger.reserve(100, 1e20, lambda: 0.0)
            self.assertEqual(ledger.remaining(), 0)
            with self.assertRaises(QuotaUnavailable):
                ledger.reserve(100, 1e20, lambda: 0.0)
            self.assertEqual(len(ledger.events()), 1)

    def test_langchain_loop_uses_fake_model_and_keeps_tool_message(self):
        with scratch_directory() as tmp:
            config = {**self.settings.config, "budget": {**self.settings.budget, "max_model_requests": 1}}
            settings = replace(self.settings, config=config, model="fake-model", api_key="smoke-secret")
            model = FakeModel()
            box = FakeBox()
            ledger = FakeLedger()
            result = asyncio.run(run_agent(settings=settings, instruction="Implement the task", box=box,
                                           output_dir=Path(tmp), ledger=ledger, model=model))
            self.assertEqual(result["stop_reason"], "request_budget")
            self.assertEqual(result["model_requests"], 1)
            self.assertEqual(model.calls, 1)
            self.assertEqual(ledger.requests, 1)
            self.assertEqual(len(box.commands), 1)
            trajectory = (Path(tmp) / "trajectory.json").read_text(encoding="utf-8")
            self.assertIn("tool-smoke-1", trajectory)
            self.assertIn("tool", trajectory.lower())
            self.assertNotIn("smoke-secret", trajectory)

    def test_summary_writes_csv_and_markdown_from_saved_records(self):
        with scratch_directory() as tmp:
            root = Path(tmp)
            settings = replace(self.settings, root=root, config={**self.settings.config, "output_root": "runs"})
            output = settings.output_root
            rows = []
            for branch in settings.config["branches"]:
                run = {"task_id": settings.task_ids[0], "branch": branch, "seed": 42,
                       "attempt_id": "attempt-001", "run_id": branch, "status": "scored", "group_valid": True}
                rows.append(run)
                folder = output / run["task_id"] / branch / "42" / "attempt-001"
                write_json(folder / "run.json", {**run, "manifest": {"kept_cases": 2, "actual_ratio": 0.5}})
                write_json(folder / "agent_result.json", {"stop_reason": "model_finished", "model_requests": 1, "usage": []})
                write_json(folder / "visible-tests-result.json", {"status": "pass", "passed": 2, "failed": 0})
                write_json(folder / "evaluation/result.json", {"status": "pass", "reward": 1,
                           "benchmark": {"f2p_passed": 1, "f2p_total": 1, "p2p_passed": 1, "p2p_total": 1}})
            write_json(output / "schedule.json", {"task_ids": settings.task_ids, "runs": rows})
            self.assertEqual(summarize(settings), 0)
            self.assertTrue((output / "summary/runs.csv").is_file())
            markdown = (output / "summary/summary.md").read_text(encoding="utf-8")
            self.assertIn("Nhóm ghép cặp", markdown)
            self.assertIn("100", markdown)

    def test_docker_status_returns_expected_status(self):
        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            del mock_client.__enter__
            mock_client.info.return_value = {"OSType": "linux"}
            mock_from_env.return_value = mock_client
            self.assertEqual(docker_status(), "available")
            mock_client.close.assert_called_once()

        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            del mock_client.__enter__
            mock_client.info.return_value = {"OSType": "windows"}
            mock_from_env.return_value = mock_client
            self.assertEqual(docker_status(), "Linux engine required")
            mock_client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
