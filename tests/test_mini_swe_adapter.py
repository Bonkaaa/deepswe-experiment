from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from minisweagent.exceptions import Submitted
from deepswe_exp.components.agent.mini_swe_env import SandboxEnvironment
from deepswe_exp.components.agent.mini_swe_model import build_mini_swe_model
from deepswe_exp.components.agent.mini_swe_runner import run_mini_swe_agent
from deepswe_exp.components.configuration.settings import _validate_config, load_settings, Settings


class DummySettings:
    def __init__(self, **kwargs):
        self.profile_id = None
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSandboxEnvironment(unittest.TestCase):
    def test_execute_normal_command(self):
        box = MagicMock()
        box.execute.return_value = (0, "line 1\nline 2")
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)

        result = env.execute({"command": "ls -la"})

        box.execute.assert_called_once_with("ls -la", timeout=60)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"], "line 1\nline 2")
        self.assertEqual(result["exception_info"], "")

    def test_execute_custom_timeout(self):
        box = MagicMock()
        box.execute.return_value = (0, "ok")
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)

        result = env.execute({"command": "pytest"}, timeout=15)

        box.execute.assert_called_once_with("pytest", timeout=15)
        self.assertEqual(result["returncode"], 0)

    def test_execute_raises_submitted_on_token(self):
        box = MagicMock()
        box.execute.return_value = (0, "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nPatch content here")
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)

        with self.assertRaises(Submitted) as ctx:
            env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})

        submission_data = ctx.exception.messages[0]
        self.assertEqual(submission_data.get("role"), "exit")
        self.assertIn("Patch content here", submission_data.get("content", ""))

    def test_execute_does_not_raise_submitted_on_nonzero_rc(self):
        box = MagicMock()
        box.execute.return_value = (1, "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nSome error")
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)

        result = env.execute({"command": "failing_cmd"})
        self.assertEqual(result["returncode"], 1)
        self.assertIn("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", result["output"])

    def test_get_template_vars(self):
        box = MagicMock()
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)
        vars_dict = env.get_template_vars()
        self.assertIsInstance(vars_dict, dict)
        self.assertEqual(vars_dict.get("cwd"), "/app")


class TestMiniSweModel(unittest.TestCase):
    def test_build_google_model(self):
        settings = DummySettings(
            model_provider="google",
            model="gemini-2.5-flash",
            api_key="test-api-key",
        )
        model = build_mini_swe_model(settings)
        self.assertEqual(model.config.model_name, "gemini/gemini-2.5-flash")
        self.assertEqual(model.config.model_kwargs.get("api_key"), "test-api-key")
        self.assertEqual(model.config.cost_tracking, "ignore_errors")

    def test_build_ollama_model(self):
        settings = DummySettings(
            model_provider="ollama",
            model="qwen2.5-coder:32b",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_num_ctx=65536,
            ollama_options={"temperature": 0.2},
        )
        model = build_mini_swe_model(settings)
        self.assertEqual(model.config.model_name, "ollama_chat/qwen2.5-coder:32b")
        self.assertEqual(model.config.model_kwargs.get("api_base"), "http://127.0.0.1:11434")
        self.assertEqual(model.config.model_kwargs.get("num_ctx"), 65536)
        self.assertEqual(model.config.model_kwargs.get("temperature"), 0.2)
        self.assertEqual(model.config.cost_tracking, "ignore_errors")

    def test_build_unsupported_provider(self):
        settings = DummySettings(model_provider="unknown")
        with self.assertRaises(ValueError) as ctx:
            build_mini_swe_model(settings)
        self.assertIn("Unsupported model provider", str(ctx.exception))


class TestMiniSweRunner(unittest.TestCase):
    def setUp(self):
        self.settings = DummySettings(
            model_provider="google",
            model="gemini-2.5-flash",
            api_key="fake-key",
            budget={"tool_timeout_seconds": 30, "max_model_requests": 15},
        )
        self.box = MagicMock()

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_submitted(self, mock_agent_cls):
        mock_agent = MagicMock()
        mock_agent.n_calls = 5
        mock_agent.run.side_effect = Submitted({"role": "exit", "content": "Done"})
        mock_agent_cls.return_value = mock_agent

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            result = run_mini_swe_agent(
                settings=self.settings,
                instruction="Solve bug",
                box=self.box,
                output_dir=out_dir,
                protocol_hash="proto123",
            )

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["stop_reason"], "submitted")
            self.assertEqual(result["protocol_hash"], "proto123")
            self.assertEqual(result["model_requests"], 5)
            mock_agent.save.assert_called_once_with(out_dir / "trajectory.json")

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_limits_exceeded(self, mock_agent_cls):
        from minisweagent.exceptions import LimitsExceeded
        mock_agent = MagicMock()
        mock_agent.n_calls = 15
        mock_agent.run.side_effect = LimitsExceeded("Limit reached")
        mock_agent_cls.return_value = mock_agent

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            result = run_mini_swe_agent(
                settings=self.settings,
                instruction="Solve bug",
                box=self.box,
                output_dir=out_dir,
                protocol_hash="proto123",
            )

            self.assertEqual(result["stop_reason"], "request_budget")
            self.assertEqual(result["model_requests"], 15)

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_context_overflow(self, mock_agent_cls):
        from litellm.exceptions import ContextWindowExceededError
        mock_agent = MagicMock()
        mock_agent.run.side_effect = ContextWindowExceededError("Context window exceeded", model="gemini", llm_provider="google")
        mock_agent_cls.return_value = mock_agent

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            result = run_mini_swe_agent(
                settings=self.settings,
                instruction="Solve bug",
                box=self.box,
                output_dir=out_dir,
            )

            self.assertEqual(result["stop_reason"], "context_overflow")

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_internal_error(self, mock_agent_cls):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("Something crashed")
        mock_agent_cls.return_value = mock_agent

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            result = run_mini_swe_agent(
                settings=self.settings,
                instruction="Solve bug",
                box=self.box,
                output_dir=out_dir,
            )

            self.assertEqual(result["stop_reason"], "internal_error")
            self.assertEqual(result["error_detail"], "Something crashed")


class TestSettingsAndRunnerDispatch(unittest.TestCase):
    def test_validate_config_harness_default(self):
        cfg = {
            "study_id": "study-1",
            "data_root": "data",
            "output_root": "runs",
            "tasks": ["task1"],
            "branches": ["100"],
            "budget": {
                "max_model_requests": 10,
                "wall_time_seconds": 100,
                "max_output_tokens": 1000,
                "max_tool_output_chars": 1000,
                "tool_timeout_seconds": 30,
            },
        }
        res = _validate_config(cfg)
        self.assertEqual(res.get("harness", "mini-swe-agent"), "mini-swe-agent")

    def test_validate_config_harness_custom(self):
        cfg = {
            "study_id": "study-1",
            "data_root": "data",
            "output_root": "runs",
            "tasks": ["task1"],
            "branches": ["100"],
            "harness": "custom",
            "budget": {
                "max_model_requests": 10,
                "wall_time_seconds": 100,
                "max_output_tokens": 1000,
                "max_tool_output_chars": 1000,
                "tool_timeout_seconds": 30,
            },
        }
        res = _validate_config(cfg)
        self.assertEqual(res.get("harness"), "custom")

    def test_validate_config_harness_invalid(self):
        cfg = {
            "study_id": "study-1",
            "data_root": "data",
            "output_root": "runs",
            "tasks": ["task1"],
            "branches": ["100"],
            "harness": "unsupported-agent",
            "budget": {
                "max_model_requests": 10,
                "wall_time_seconds": 100,
                "max_output_tokens": 1000,
                "max_tool_output_chars": 1000,
                "tool_timeout_seconds": 30,
            },
        }
        with self.assertRaises(ValueError) as ctx:
            _validate_config(cfg)
        self.assertIn("harness", str(ctx.exception))

    def test_settings_has_harness_attribute(self):
        s = Settings(
            root=None,
            config_path=None,
            config={},
            model="gemini",
            api_key="",
            harness="mini-swe-agent",
        )
        self.assertEqual(s.harness, "mini-swe-agent")

    @patch("deepswe_exp.components.execution.runner.evaluate_patch")
    @patch("deepswe_exp.components.execution.runner.evaluate_supplied_tests")
    @patch("deepswe_exp.components.execution.runner.write_json")
    @patch("deepswe_exp.components.execution.runner.collect_diff")
    @patch("deepswe_exp.components.execution.runner.export_app")
    @patch("deepswe_exp.components.agent.mini_swe_runner.run_mini_swe_agent")
    @patch("deepswe_exp.components.execution.runner.create_agent")
    @patch("deepswe_exp.components.execution.runner.schedule")
    @patch("deepswe_exp.components.execution.runner.load_task")
    @patch("deepswe_exp.components.execution.runner.read_json")
    def test_run_next_dispatches_mini_swe_agent(
        self, mock_read_json, mock_load_task, mock_schedule, mock_create_agent,
        mock_mini_runner, mock_export_app, mock_collect_diff, mock_write_json,
        mock_eval_tests, mock_eval_patch,
    ):
        from deepswe_exp.components.execution.runner import run_next
        from pathlib import Path

        mock_schedule.return_value = {
            "runs": [{
                "run_id": "r1", "status": "pending", "group_valid": True, "task_id": "t1", "branch": "100",
                "seed": 42, "attempt_id": "att1", "protocol_hash": "hash1"
            }]
        }
        mock_read_json.side_effect = lambda p: {"image": "img", "verifier_image": "vimg", "manifests": {}, "product_paths": []}
        task_mock = MagicMock()
        task_mock.instruction = "Instruction"
        mock_load_task.return_value = task_mock
        mock_box = MagicMock()
        mock_create_agent.return_value.__enter__.return_value = mock_box
        mock_mini_runner.return_value = {
            "stop_reason": "submitted", "model_requests": 3, "elapsed_seconds": 1.0
        }
        mock_eval_tests.return_value = {"passed": True}
        mock_eval_patch.return_value = {"status": "pass"}

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td) / "runs"
            prompt_file = Path(td) / "prompt.md"
            prompt_file.write_text("fake prompt", encoding="utf-8")

            settings = DummySettings(
                harness="mini-swe-agent",
                output_root=out_root,
                task_path=lambda t: Path(td) / t,
                budget={"tool_timeout_seconds": 30, "max_model_requests": 10},
                prompt_path=prompt_file,
                model="qwen",
                api_key="",
                model_provider="ollama",
                quota_ready=True,
                config={"study_id": "test"},
                redact=lambda x: str(x),
            )
            run = run_next(settings, backend=MagicMock())

        mock_mini_runner.assert_called_once()
        self.assertEqual(run["status"], "scored")

    @patch("deepswe_exp.components.execution.runner.evaluate_patch")
    @patch("deepswe_exp.components.execution.runner.evaluate_supplied_tests")
    @patch("deepswe_exp.components.execution.runner.write_json")
    @patch("deepswe_exp.components.execution.runner.collect_diff")
    @patch("deepswe_exp.components.execution.runner.export_app")
    @patch("deepswe_exp.components.execution.runner.run_agent")
    @patch("deepswe_exp.components.execution.runner.create_agent")
    @patch("deepswe_exp.components.execution.runner.schedule")
    @patch("deepswe_exp.components.execution.runner.load_task")
    @patch("deepswe_exp.components.execution.runner.read_json")
    def test_run_next_dispatches_custom_agent(
        self, mock_read_json, mock_load_task, mock_schedule, mock_create_agent,
        mock_custom_runner, mock_export_app, mock_collect_diff, mock_write_json,
        mock_eval_tests, mock_eval_patch,
    ):
        import asyncio
        from deepswe_exp.components.execution.runner import run_next
        from pathlib import Path

        mock_schedule.return_value = {
            "runs": [{
                "run_id": "r2", "status": "pending", "group_valid": True, "task_id": "t1", "branch": "100",
                "seed": 42, "attempt_id": "att1", "protocol_hash": "hash1"
            }]
        }
        mock_read_json.side_effect = lambda p: {"image": "img", "verifier_image": "vimg", "manifests": {}, "product_paths": []}
        task_mock = MagicMock()
        task_mock.instruction = "Instruction"
        mock_load_task.return_value = task_mock
        mock_box = MagicMock()
        mock_create_agent.return_value.__enter__.return_value = mock_box

        async def fake_run_agent(**kwargs):
            return {"stop_reason": "submitted", "model_requests": 2, "elapsed_seconds": 1.0}

        mock_custom_runner.side_effect = fake_run_agent
        mock_eval_tests.return_value = {"passed": True}
        mock_eval_patch.return_value = {"status": "pass"}

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td) / "runs"
            prompt_file = Path(td) / "prompt.md"
            prompt_file.write_text("fake prompt", encoding="utf-8")

            settings = DummySettings(
                harness="custom",
                output_root=out_root,
                task_path=lambda t: Path(td) / t,
                budget={"tool_timeout_seconds": 30, "max_model_requests": 10},
                prompt_path=prompt_file,
                model="qwen",
                api_key="",
                model_provider="ollama",
                quota_ready=True,
                config={"study_id": "test"},
                redact=lambda x: str(x),
            )
            run = run_next(settings, backend=MagicMock())

        mock_custom_runner.assert_called_once()
        self.assertEqual(run["status"], "scored")


if __name__ == "__main__":
    unittest.main()
