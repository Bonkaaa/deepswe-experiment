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
        self.assertEqual(vars_dict.get("system"), "Linux")
        self.assertEqual(vars_dict.get("machine"), "x86_64")
        self.assertIn("release", vars_dict)
        self.assertIn("version", vars_dict)

    def test_serialize(self):
        box = MagicMock()
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)
        serialized = env.serialize()
        self.assertIsInstance(serialized, dict)
        self.assertIn("info", serialized)
        self.assertEqual(serialized["info"]["config"]["environment"]["cwd"], "/app")


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

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_return_dict_statuses(self, mock_agent_cls):
        cases = [
            ({"exit_status": "Submitted", "submission": "done"}, "submitted", None),
            ({"exit_status": "LimitsExceeded", "submission": ""}, "request_budget", None),
            ({"exit_status": "TimeExceeded", "submission": ""}, "time_budget", None),
            ({"exit_status": "RepeatedFormatError", "submission": ""}, "format_error", None),
            ({"exit_status": "UnknownStatus", "submission": ""}, "internal_error", "Unexpected agent exit_status"),
        ]
        import tempfile
        from pathlib import Path
        for return_dict, expected_reason, expected_error_sub in cases:
            mock_agent = MagicMock()
            mock_agent.run.return_value = return_dict
            mock_agent_cls.return_value = mock_agent

            with tempfile.TemporaryDirectory() as td:
                res = run_mini_swe_agent(
                    settings=self.settings,
                    instruction="Test",
                    box=self.box,
                    output_dir=Path(td),
                )
                self.assertEqual(res["stop_reason"], expected_reason)
                if expected_error_sub:
                    self.assertIn(expected_error_sub, res["error_detail"])

    @patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent")
    def test_run_redacts_api_key_in_trajectory_and_error(self, mock_agent_cls):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("Failed with key secret-12345")
        mock_agent_cls.return_value = mock_agent

        settings = DummySettings(
            model_provider="google",
            model="gemini-2.5-flash",
            api_key="secret-12345",
            budget={"tool_timeout_seconds": 30, "max_model_requests": 15},
            redact=lambda s: str(s).replace("secret-12345", "[REDACTED]"),
        )

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            # simulate trajectory saved with secret key
            def fake_save(p):
                p.write_text('{"api_key": "secret-12345"}', encoding="utf-8")
            mock_agent.save.side_effect = fake_save

            res = run_mini_swe_agent(
                settings=settings,
                instruction="Test",
                box=self.box,
                output_dir=out_dir,
            )

            self.assertIn("[REDACTED]", res["error_detail"])
            self.assertNotIn("secret-12345", res["error_detail"])

            saved_traj = (out_dir / "trajectory.json").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", saved_traj)
            self.assertNotIn("secret-12345", saved_traj)

    def test_contract_real_default_agent_rendering_and_serialization(self):
        """Verify real DefaultAgent with SandboxEnvironment renders Jinja templates and serializes without error."""
        from minisweagent.agents.default import DefaultAgent
        import yaml
        from minisweagent import package_dir

        box = MagicMock()
        box.execute.return_value = (0, "echo hi")
        env = SandboxEnvironment(box=box, cwd="/app", timeout=60)

        # Minimal mock model satisfying minisweagent.Model protocol
        mock_model = MagicMock()
        mock_model.format_message.side_effect = lambda role, content: {"role": role, "content": content}
        mock_model.get_template_vars.return_value = {}

        config = yaml.safe_load((package_dir / "config" / "mini.yaml").read_text(encoding="utf-8"))["agent"]
        config["step_limit"] = 1
        agent = DefaultAgent(model=mock_model, env=env, **config)
        agent.extra_template_vars["task"] = "Fix the login bug"

        # Test Jinja rendering of instance template does not fail with 'system' is undefined
        rendered_instance = agent._render_template(config["instance_template"])
        self.assertIn("Linux", rendered_instance)
        self.assertIn("x86_64", rendered_instance)

        # Test agent serialize works without AttributeError on env.serialize
        serialized = agent.serialize()
        self.assertIsInstance(serialized, dict)
        self.assertIn("info", serialized)


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
            with patch(
                "deepswe_exp.components.execution.runner.verify_prepared_variant",
                return_value={"product_paths": []},
            ):
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
            with patch(
                "deepswe_exp.components.execution.runner.verify_prepared_variant",
                return_value={"product_paths": []},
            ):
                run = run_next(settings, backend=MagicMock())

        mock_custom_runner.assert_called_once()
        self.assertEqual(run["status"], "scored")

    def test_model_loads_mini_yaml_templates_and_max_tokens(self):
        import litellm
        settings = DummySettings(
            model_provider="ollama",
            model="qwen3.6:35b-a3b-coding",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_num_ctx=32768,
            ollama_options={"temperature": 0.0},
            budget={"max_output_tokens": 8192, "wall_time_seconds": 1800},
        )
        model = build_mini_swe_model(settings)
        self.assertIn("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", model.config.format_error_template)
        self.assertIn("finish_reason", model.config.format_error_template)
        self.assertIn("output_head", model.config.observation_template)
        self.assertEqual(model.config.model_kwargs.get("max_tokens"), 8192)
        self.assertTrue(model.config.model_kwargs.get("drop_params"))
        self.assertIsNotNone(model.config.model_kwargs.get("timeout"))
        self.assertIn(litellm.exceptions.BadRequestError, model.abort_exceptions)

    def test_env_truncates_long_output_and_handles_timeout_and_cwd(self):
        box = MagicMock()
        box.execute.return_value = (0, "A" * 500 + "secret-xyz" + "B" * 500)
        env = SandboxEnvironment(
            box=box,
            cwd="/app",
            timeout=60,
            max_output_chars=200,
            redact_fn=lambda s: s.replace("secret-xyz", "[REDACTED]"),
        )
        res = env.execute({"command": "cat bigfile"}, cwd="/app/subdir")
        box.execute.assert_called_once_with("cd /app/subdir && cat bigfile", timeout=60)
        self.assertLessEqual(len(res["output"]), 200)
        self.assertIn("[... TRUNCATED", res["output"])
        self.assertNotIn("secret-xyz", res["output"])
        self.assertEqual(len(env.commands), 1)
        self.assertEqual(env.commands[0]["command"], "cat bigfile")
        self.assertEqual(env.commands[0]["exit_code"], 0)

        # Timeout rc=124 sets exception_info
        box.execute.return_value = (124, "partial output")
        res_timeout = env.execute({"command": "sleep 999"})
        self.assertEqual(res_timeout["returncode"], 124)
        self.assertIn("timed out after 60 seconds", res_timeout["exception_info"])

    def test_runner_extracts_token_usage_and_writes_commands_jsonl(self):
        import json
        import tempfile
        from pathlib import Path
        from deepswe_exp.components.agent.mini_swe_runner import _RedactingAgent

        settings = DummySettings(
            model_provider="google",
            model="gemini-2.5-flash",
            api_key="secret-api-key",
            ollama_num_ctx=32768,
            input_budget=24000,
            budget={"tool_timeout_seconds": 30, "max_model_requests": 15, "max_output_tokens": 4096, "max_tool_output_chars": 4000},
            redact=lambda s: str(s).replace("secret-api-key", "[REDACTED]"),
        )
        box = MagicMock()
        box.execute.side_effect = [
            (0, "file1.py"),
            (0, "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndone"),
        ]
        ledger = MagicMock()

        def fake_run(self_agent, instruction):
            self_agent.n_calls = 2
            self_agent.env.execute({"command": "ls"})
            self_agent.messages = [
                {
                    "role": "assistant",
                    "content": "Listing files",
                    "extra": {"response": {"usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}}},
                },
                {
                    "role": "assistant",
                    "content": "Submitting",
                    "extra": {"response": {"usage": {"prompt_tokens": 150, "completion_tokens": 35, "total_tokens": 185}}},
                },
            ]
            # Verify incremental save redacts api_key immediately
            self_agent.save(self_agent.config.output_path)
            return {"exit_status": "Submitted", "submission": "done"}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            with patch.object(_RedactingAgent, "run", fake_run):
                result = run_mini_swe_agent(
                    settings=settings,
                    instruction="Fix bug",
                    box=box,
                    output_dir=out_dir,
                    ledger=ledger,
                    protocol_hash="p1",
                )

            self.assertEqual(result["stop_reason"], "submitted")
            self.assertEqual(len(result["usage"]), 2)
            self.assertEqual(result["usage"][0]["input_tokens"], 100)
            self.assertEqual(result["usage"][0]["output_tokens"], 25)
            self.assertEqual(result["usage"][1]["input_tokens"], 150)
            self.assertEqual(result["usage"][1]["output_tokens"], 35)
            self.assertEqual(ledger.usage.call_count, 2)
            self.assertEqual(len(result["commands"]), 1)
            self.assertTrue((out_dir / "commands.jsonl").is_file())
            traj_text = (out_dir / "trajectory.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-api-key", traj_text)
            self.assertIn("[REDACTED]", traj_text)

    def test_runner_reclassifies_repeated_format_error_and_exceptions(self):
        import tempfile
        from pathlib import Path
        from litellm.exceptions import BadRequestError
        from minisweagent.exceptions import TimeExceeded
        from deepswe_exp.components.execution.docker_env import DockerFailure

        settings = DummySettings(
            model_provider="ollama",
            model="qwen3.6:35b-a3b-coding",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_num_ctx=32768,
            input_budget=24576,
            budget={"tool_timeout_seconds": 30, "max_model_requests": 15, "max_output_tokens": 4096, "max_tool_output_chars": 4000},
            redact=lambda s: str(s),
        )
        box = MagicMock()

        # 1. RepeatedFormatError due to context overflow (total_tokens near ollama_num_ctx)
        with tempfile.TemporaryDirectory() as td, \
             patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.run.return_value = {"exit_status": "RepeatedFormatError", "submission": ""}
            mock_agent.messages = [{
                "role": "user",
                "extra": {
                    "response": {
                        "choices": [{"finish_reason": "length"}],
                        "usage": {"prompt_tokens": 32750, "completion_tokens": 18, "total_tokens": 32768},
                    }
                },
            }]
            mock_cls.return_value = mock_agent
            res = run_mini_swe_agent(settings=settings, instruction="Fix", box=box, output_dir=Path(td))
            self.assertEqual(res["stop_reason"], "context_overflow")

        # 2. RepeatedFormatError due to output_truncated (completion_tokens hit max_output_tokens)
        with tempfile.TemporaryDirectory() as td, \
             patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.run.return_value = {"exit_status": "RepeatedFormatError", "submission": ""}
            mock_agent.messages = [{
                "role": "user",
                "extra": {
                    "response": {
                        "choices": [{"finish_reason": "length"}],
                        "usage": {"prompt_tokens": 2000, "completion_tokens": 4096, "total_tokens": 6096},
                    }
                },
            }]
            mock_cls.return_value = mock_agent
            res = run_mini_swe_agent(settings=settings, instruction="Fix", box=box, output_dir=Path(td))
            self.assertEqual(res["stop_reason"], "output_truncated")

        # 3. BadRequestError("context length exceeded") -> context_overflow
        with tempfile.TemporaryDirectory() as td, \
             patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.run.side_effect = BadRequestError("OllamaError: context length exceeded", model="qwen", llm_provider="ollama")
            mock_cls.return_value = mock_agent
            res = run_mini_swe_agent(settings=settings, instruction="Fix", box=box, output_dir=Path(td))
            self.assertEqual(res["stop_reason"], "context_overflow")

        # 4. DockerFailure -> docker_error
        with tempfile.TemporaryDirectory() as td, \
             patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.run.side_effect = DockerFailure("Command timeout: container killed")
            mock_cls.return_value = mock_agent
            res = run_mini_swe_agent(settings=settings, instruction="Fix", box=box, output_dir=Path(td))
            self.assertEqual(res["stop_reason"], "docker_error")

        # 5. TimeExceeded exception -> time_budget (not shadowed by LimitsExceeded)
        with tempfile.TemporaryDirectory() as td, \
             patch("deepswe_exp.components.agent.mini_swe_runner.DefaultAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.run.side_effect = TimeExceeded()
            mock_cls.return_value = mock_agent
            res = run_mini_swe_agent(settings=settings, instruction="Fix", box=box, output_dir=Path(td))
            self.assertEqual(res["stop_reason"], "time_budget")

    def test_check_model_connection_with_mini_swe_harness_and_report_error_detail(self):
        import tempfile
        from pathlib import Path
        from deepswe_exp.components.agent.connection import check_model_connection
        from deepswe_exp.components.results.report import _record
        from deepswe_exp.components.results.storage import write_json, protocol

        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td) / "runs"
            settings = DummySettings(
                harness="mini-swe-agent",
                model_provider="ollama",
                model="qwen3.6:35b-a3b-coding",
                ollama_base_url="http://127.0.0.1:11434",
                ollama_num_ctx=32768,
                ollama_options={},
                output_root=out_root,
                config={"study_id": "test"},
                budget={"max_output_tokens": 4096, "wall_time_seconds": 600},
                redact=lambda s: str(s),
            )
            fake_litellm_model = MagicMock()
            fake_litellm_model.query.side_effect = [
                {
                    "role": "assistant",
                    "content": "Running echo OK",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "echo OK"}'}}],
                    "extra": {
                        "actions": [{"command": "echo OK", "tool_call_id": "call_1"}],
                        "response": {"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
                    },
                },
                {
                    "role": "assistant",
                    "content": "Confirmed",
                    "extra": {
                        "actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "tool_call_id": "call_2"}],
                        "response": {"usage": {"prompt_tokens": 70, "completion_tokens": 12, "total_tokens": 82}},
                    },
                },
            ]
            fake_litellm_model.format_observation_messages.return_value = [
                {"role": "tool", "tool_call_id": "call_1", "content": '{"returncode": 0, "output": "OK"}'}
            ]

            with patch("deepswe_exp.components.agent.connection.fetch_ollama_metadata", return_value={"version": "0.6.0", "digest": "sha256:123"}), \
                 patch("deepswe_exp.components.agent.connection.build_mini_swe_model", return_value=fake_litellm_model):
                chk = check_model_connection(settings)
                self.assertEqual(chk["status"], "success")
                self.assertTrue(chk["tool_calling_verified"])
                self.assertEqual(chk["request_count"], 2)

            # Test report._record includes agent_result["error_detail"]
            run_row = {
                "task_id": "t1", "branch": "100", "seed": 42, "attempt_id": "att1",
                "run_id": "r1", "group_valid": False, "status": "interrupted",
            }
            attempt_dir = out_root / "t1" / "100" / "42" / "att1"
            write_json(attempt_dir / "agent_result.json", {
                "stop_reason": "context_overflow",
                "model_requests": 12,
                "elapsed_seconds": 45.2,
                "usage": [{"input_tokens": 30000, "output_tokens": 2000}],
                "error_detail": "Prompt exceeded max context length 32768",
            })
            rec = _record(settings, run_row)
            self.assertEqual(rec["input_tokens"], 30000)
            self.assertEqual(rec["output_tokens"], 2000)
            self.assertIn("Prompt exceeded max context length 32768", rec["technical_detail"])

    def test_parse_result_scores_collection_failure_missing_from_report_as_fail_test(self):
        import json
        import tempfile
        from pathlib import Path
        from deepswe_exp.components.evaluation.verifier import parse_result

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "reward.json").write_text(
                json.dumps({
                    "reward": 0,
                    "f2p_total": 44,
                    "f2p_passed": 0,
                    "p2p_total": 2738,
                    "p2p_passed": 0,
                    "f2p": 0.0,
                    "p2p": 0.0,
                    "partial": 0.0,
                }),
                encoding="utf-8",
            )
            (root / "ctrf.json").write_text(
                json.dumps({
                    "results": {
                        "tests": [
                            {
                                "name": "[p2p] tests.unit.test_example",
                                "status": "failed",
                                "message": "ValueError: Cannot create schema because overlay contains omitted values",
                            },
                            {
                                "name": "[f2p] tests.integration.morphing.test_aliases.test_alias_style",
                                "status": "failed",
                                "message": "missing from report (test did not run or produced no result — see raw output)",
                            },
                        ]
                    }
                }),
                encoding="utf-8",
            )
            res = parse_result(root, rc=0, timed_out=False)
            self.assertEqual(res["status"], "fail_test")
            self.assertEqual(res["reward"], 0)

    def test_vllm_provider_model_settings_doctor_and_connection(self):
        import io
        import json
        import tempfile
        from pathlib import Path
        from deepswe_exp.__main__ import _doctor
        from deepswe_exp.components.agent.connection import check_model_connection
        from deepswe_exp.components.results.storage import fetch_vllm_metadata, protocol, write_json

        # 1. Profile neptune loads vLLM configuration
        neptune = load_settings(profile="neptune")
        self.assertEqual(neptune.model_provider, "vllm")
        self.assertEqual(neptune.model, "Qwen/Qwen3.8-27B-FP8")
        self.assertEqual(neptune.ollama_base_url, "http://127.0.0.1:11435/v1")
        self.assertEqual(neptune.ollama_num_ctx, 262144)
        self.assertEqual(neptune.input_budget, 221184)
        self.assertTrue(neptune.quota_ready)

        # 2. build_mini_swe_model builds openai/<model> with api_base and api_key="EMPTY"
        model = build_mini_swe_model(neptune)
        self.assertEqual(model.config.model_name, "openai/Qwen/Qwen3.8-27B-FP8")
        self.assertEqual(model.config.model_kwargs.get("api_base"), "http://127.0.0.1:11435/v1")
        self.assertEqual(model.config.model_kwargs.get("api_key"), "EMPTY")
        self.assertEqual(model.config.model_kwargs.get("max_tokens"), 24576)

        # 3. fetch_vllm_metadata parses /v1/models
        def fake_urlopen(req, timeout=2.0):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "data": [{"id": "Qwen/Qwen3.8-27B-FP8", "max_model_len": 262144}]
            }).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            meta = fetch_vllm_metadata("http://127.0.0.1:11435/v1", "Qwen/Qwen3.8-27B-FP8")
            self.assertTrue(meta["reachable"])
            self.assertTrue(meta["model_available"])
            self.assertEqual(meta["max_model_len"], 262144)

        # 4. _doctor passes for vllm when server serves the model
        with patch("deepswe_exp.components.execution.docker_env.docker_status", return_value="available"), \
             patch("deepswe_exp.components.results.storage.fetch_vllm_metadata", return_value={"reachable": True, "model_available": True, "max_model_len": 262144}):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                code = _doctor(neptune)
            self.assertEqual(code, 0)
            self.assertIn("Provider: vllm", buf.getvalue())
            self.assertIn("vLLM Model Served", buf.getvalue())

        # 5. check_model_connection supports vllm without requiring GEMINI_API_KEY
        fake_litellm = MagicMock()
        fake_litellm.query.side_effect = [
            {"role": "assistant", "content": "", "extra": {"actions": [{"command": "echo OK", "tool_call_id": "c1"}]}},
            {"role": "assistant", "content": "OK", "extra": {"response": {"usage": {"prompt_tokens": 20, "completion_tokens": 5}}}},
        ]
        fake_litellm.format_observation_messages.return_value = [{"role": "tool", "tool_call_id": "c1", "content": "OK"}]
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td) / "runs"
            with patch.object(type(neptune), "output_root", new_callable=lambda: property(lambda self: out_root)), \
                 patch("deepswe_exp.components.agent.connection.fetch_vllm_metadata", return_value={"reachable": True, "model_available": True}), \
                 patch("deepswe_exp.components.agent.connection.build_mini_swe_model", return_value=fake_litellm):
                chk = check_model_connection(neptune)
                self.assertEqual(chk["status"], "success")
                self.assertTrue(chk["tool_calling_verified"])


if __name__ == "__main__":
    unittest.main()
