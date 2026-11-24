from __future__ import annotations

import asyncio
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from deepswe_exp.components.agent.agent import build_model, run_agent
from deepswe_exp.components.agent.quota import LocalLedger
from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.results.storage import scratch_directory as create_scratch_directory

TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-temp"


def scratch_directory():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return create_scratch_directory(TEMP_ROOT)


class FakeBox:
    def execute(self, command: str, timeout: float = 30.0):
        return 0, f"mock output for {command}"


class Phase1Phase2Tests(unittest.TestCase):
    def test_load_profile_saturn(self):
        settings = load_settings(profile="saturn")
        self.assertEqual(settings.profile_id, "saturn")
        self.assertEqual(settings.model, "qwen3.6:35b-a3b-coding")
        self.assertEqual(settings.model_provider, "ollama")
        self.assertEqual(settings.ollama_base_url, "http://127.0.0.1:11434")
        self.assertEqual(settings.ollama_num_ctx, 262144)
        self.assertEqual(settings.output_root.name, "pilot-multilingual-gin-1805-v10-saturn-qwen36")
        self.assertTrue(settings.quota_ready)

    def test_load_profile_neptune(self):
        settings = load_settings(profile="neptune")
        self.assertEqual(settings.profile_id, "neptune")
        self.assertEqual(settings.model, "Qwen/Qwen3.8-27B-FP8")
        self.assertEqual(settings.model_provider, "vllm")
        self.assertEqual(settings.ollama_base_url, "http://127.0.0.1:11435/v1")
        self.assertEqual(settings.ollama_num_ctx, 262144)
        self.assertEqual(settings.output_root.name, "pilot-multilingual-gin-1805-v10-neptune-qwen38")
        self.assertTrue(settings.quota_ready)

    def test_backward_compatible_google(self):
        settings = load_settings()
        self.assertIsNone(settings.profile_id)
        self.assertEqual(settings.model_provider, "google")

    def test_env_var_conflict_warning(self):
        with patch.dict("os.environ", {"MODEL": "conflicting-model-tag"}):
            stderr_capture = io.StringIO()
            with patch("sys.stderr", stderr_capture):
                settings = load_settings(profile="saturn")
            self.assertEqual(settings.model, "qwen3.6:35b-a3b-coding")
            self.assertIn("[WARNING]", stderr_capture.getvalue())
            self.assertIn("conflicting-model-tag", stderr_capture.getvalue())

    def test_env_var_no_false_positive_warning(self):
        repo_root = Path(__file__).resolve().parents[1]
        abs_models = str(repo_root / ".local" / "ollama-neptune" / "models")
        with patch.dict("os.environ", {"OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": abs_models}):
            stderr_capture = io.StringIO()
            with patch("sys.stderr", stderr_capture):
                settings = load_settings(profile="neptune")
            self.assertNotIn("OLLAMA_HOST", stderr_capture.getvalue())
            self.assertNotIn("OLLAMA_MODELS", stderr_capture.getvalue())

    def test_local_ledger(self):
        with scratch_directory() as temp_dir:
            ledger_path = temp_dir / "quota.jsonl"
            ledger = LocalLedger(ledger_path, model="qwen3.6:35b-a3b-coding", project="test")
            self.assertEqual(ledger.remaining(), 999_999)

            ledger.reserve(100, deadline=10.0)
            self.assertTrue(ledger_path.exists())
            lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["type"], "request")
            self.assertEqual(event["tokens"], 100)

            ledger.usage({"input_tokens": 50, "output_tokens": 20})
            lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            usage_event = json.loads(lines[1])
            self.assertEqual(usage_event["type"], "usage")
            self.assertEqual(usage_event["usage"]["input_tokens"], 50)

    def test_build_model_ollama(self):
        settings = load_settings(profile="saturn")
        model = build_model(settings)
        # Should be RunnableBinding wrapping ChatOllama
        self.assertTrue(hasattr(model, "bound") or hasattr(model, "model"))

    def test_run_agent_with_mock_ollama(self):
        settings = load_settings(profile="saturn")
        mock_model = MagicMock()

        async def fake_ainvoke(messages, **kwargs):
            # Simulate Ollama response with tool call and no usage metadata
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "run_shell", "args": {"command": "echo test"}, "id": "call_1"}],
            )
            msg.usage_metadata = None
            return msg

        mock_model.ainvoke = fake_ainvoke

        with scratch_directory() as out:
            ledger = LocalLedger(out / "quota.jsonl", model=settings.model)
            box = FakeBox()
            result = asyncio.run(
                run_agent(
                    settings=settings,
                    instruction="Test instruction",
                    box=box,
                    output_dir=out,
                    ledger=ledger,
                    model=mock_model,
                )
            )
            # Since mock always returns tool calls, it should stop on request_budget
            self.assertEqual(result["stop_reason"], "request_budget")
            self.assertEqual(result["model_requests"], settings.budget["max_model_requests"])
            self.assertTrue((out / "trajectory.json").is_file())
            traj = json.loads((out / "trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(traj["stop_reason"], "request_budget")


if __name__ == "__main__":
    unittest.main()
