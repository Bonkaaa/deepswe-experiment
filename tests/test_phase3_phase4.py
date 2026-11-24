from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from langchain_core.messages import AIMessage

from deepswe_exp.components.agent.connection import check_model_connection
from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.execution.runner import plan_runs, run_all, schedule
from deepswe_exp.components.results.storage import (
    digest,
    fetch_ollama_metadata,
    protocol,
    read_json,
    scratch_directory as create_scratch_directory,
    write_json,
)
from deepswe_exp.__main__ import _doctor

TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-temp"


def scratch_directory():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return create_scratch_directory(TEMP_ROOT)


class Phase3Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.saturn_settings = load_settings(profile="saturn")
        self.neptune_settings = load_settings(profile="neptune")
        self.google_settings = load_settings()

    def test_protocol_hash_differentiation(self):
        with scratch_directory() as out:
            for settings in (self.saturn_settings, self.neptune_settings, self.google_settings):
                val_dir = settings.output_root / "adaptix-name-mapping-aliases" / "validation"
                write_json(val_dir / "validation.json", {"valid": True})

            prepared = {"adaptix-name-mapping-aliases": {"image": "test-img"}}
            hash_google = protocol(self.google_settings, prepared)
            hash_saturn = protocol(self.saturn_settings, prepared)
            hash_neptune = protocol(self.neptune_settings, prepared)

            self.assertNotEqual(hash_google, hash_saturn)
            self.assertNotEqual(hash_saturn, hash_neptune)
            self.assertNotEqual(hash_google, hash_neptune)

    def test_legacy_google_protocol_hash_unchanged(self):
        # Ensure protocol() payload for Google does not include model_provider or profile_id
        with scratch_directory():
            val_dir = self.google_settings.output_root / "adaptix-name-mapping-aliases" / "validation"
            write_json(val_dir / "validation.json", {"valid": True})
            prepared = {"adaptix-name-mapping-aliases": {"image": "test-img"}}
            
            # Legacy payload structure:
            legacy_payload = {
                'schema_version': 1,
                'config': self.google_settings.config,
                'model': self.google_settings.model,
                'prompt': self.google_settings.prompt_path.read_text(encoding='utf-8'),
                'prepared': prepared,
                'validation': {"adaptix-name-mapping-aliases": {"valid": True}},
            }
            self.assertEqual(protocol(self.google_settings, prepared), digest(legacy_payload))

    def test_protocol_model_snapshot_detects_digest_change(self):
        with scratch_directory():
            val_dir = self.saturn_settings.output_root / "adaptix-name-mapping-aliases" / "validation"
            write_json(val_dir / "validation.json", {"valid": True})
            prepared = {"adaptix-name-mapping-aliases": {"image": "test-img"}}

            hash_digest_1 = protocol(self.saturn_settings, prepared, model_metadata={"digest": "sha256:11111"})
            hash_digest_2 = protocol(self.saturn_settings, prepared, model_metadata={"digest": "sha256:22222"})
            self.assertNotEqual(hash_digest_1, hash_digest_2)

    def test_fetch_ollama_metadata_parser(self):
        def fake_urlopen(req, timeout=2.0):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            mock_resp = MagicMock()
            if "/api/version" in url:
                mock_resp.read.return_value = json.dumps({"version": "0.5.4"}).encode("utf-8")
            elif "/api/show" in url:
                mock_resp.read.return_value = json.dumps({
                    "details": {"quantization_level": "Q4_K_M"},
                    "model_info": {"general.architecture": "qwen2"},
                }).encode("utf-8")
            elif "/api/tags" in url:
                mock_resp.read.return_value = json.dumps({
                    "models": [{
                        "name": "qwen3.6:35b-a3b-coding",
                        "digest": "sha256:abcdef123456",
                        "details": {"quantization_level": "Q4_K_M"},
                    }]
                }).encode("utf-8")
            else:
                raise ValueError(f"Unexpected url: {url}")
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            meta = fetch_ollama_metadata("http://127.0.0.1:11434", "qwen3.6:35b-a3b-coding")
            self.assertEqual(meta["version"], "0.5.4")
            self.assertEqual(meta["quantization"], "Q4_K_M")
            self.assertEqual(meta["digest"], "sha256:abcdef123456")

        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            offline_meta = fetch_ollama_metadata("http://127.0.0.1:11434", "qwen3.6:35b-a3b-coding")
            self.assertIsNone(offline_meta["version"])
            self.assertIsNone(offline_meta["digest"])

    def test_doctor_profile_ollama(self):
        with patch("deepswe_exp.components.execution.docker_env.docker_status", return_value="available"), \
             patch("deepswe_exp.components.results.storage.fetch_ollama_metadata", return_value={"version": "0.5.4", "digest": "sha256:qwen35testdigest"}):
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                code = _doctor(self.saturn_settings)
            output = stdout_buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Profile: saturn", output)
            self.assertIn("Provider: ollama", output)
            self.assertIn("Ollama Base URL", output)
            self.assertIn("Ollama Context", output)
            self.assertIn("version 0.5.4", output)
            self.assertIn("Ollama Model Installed", output)
            self.assertIn("sha256:qwen35testd", output)
            self.assertNotIn("Gemini API key", output)

    def test_doctor_profile_ollama_missing_model(self):
        with patch("deepswe_exp.components.execution.docker_env.docker_status", return_value="available"), \
             patch("deepswe_exp.components.results.storage.fetch_ollama_metadata", return_value={"version": "0.5.4", "digest": None}):
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                code = _doctor(self.saturn_settings)
            output = stdout_buf.getvalue()
            self.assertEqual(code, 2)
            self.assertIn("MISSING  Ollama Model Installed", output)
            self.assertIn("not found on server", output)

    def test_check_model_tool_calling_flow(self):
        call_count = 0

        async def fake_ainvoke(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "run_shell", "args": {"command": "echo OK"}, "id": "call_xyz"}],
                )
            else:
                return AIMessage(content="Execution confirmed OK.")

        mock_model = MagicMock()
        mock_model.ainvoke = fake_ainvoke

        with scratch_directory() as out:
            settings = self.saturn_settings
            with patch.object(type(settings), "output_root", new_callable=PropertyMock, return_value=out), \
                 patch("deepswe_exp.components.agent.connection.fetch_ollama_metadata", return_value={"version": "0.5.4", "digest": "sha256:abc"}):
                result = check_model_connection(settings, model=mock_model)
                self.assertEqual(result["status"], "success")
                self.assertTrue(result["tool_calling_verified"])
                self.assertEqual(call_count, 2)
                self.assertTrue(Path(result["artifact"]).is_file())
                saved = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
                self.assertTrue(saved["tool_calling_verified"])
                self.assertEqual(saved["ollama_metadata"]["digest"], "sha256:abc")

    def test_check_model_fails_when_no_tool_call(self):
        async def fake_ainvoke(messages, **kwargs):
            # Model returns plain text instead of tool call
            return AIMessage(content="I am an LLM but I will not call any tool.")

        mock_model = MagicMock()
        mock_model.ainvoke = fake_ainvoke

        with scratch_directory() as out:
            settings = self.saturn_settings
            with patch.object(type(settings), "output_root", new_callable=PropertyMock, return_value=out), \
                 patch("deepswe_exp.components.agent.connection.fetch_ollama_metadata", return_value={"version": "0.5.4"}):
                result = check_model_connection(settings, model=mock_model)
                self.assertEqual(result["status"], "tool_calling_failed")
                self.assertFalse(result["tool_calling_verified"])

    def test_plan_runs_freezes_snapshot_and_metadata(self):
        with scratch_directory() as out:
            settings = self.saturn_settings
            with patch.object(type(settings), "output_root", new_callable=PropertyMock, return_value=out):
                task_id = settings.task_ids[0]
                task_dir = out / task_id
                prepared = {"image": "img", "manifests": {"100/42": {}, "50-random/42": {}, "0/42": {}}}
                from deepswe_exp.components.tasks.tasks import load_task
                task = load_task(settings.task_path(task_id))
                if isinstance(task.benchmark, dict):
                    from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY, benchmark_container_limits
                    from deepswe_exp.components.tasks.tasks import benchmark_record_digest
                    prepared["benchmark_instance_hash"] = benchmark_record_digest(task)
                    prepared["harness_revision"] = task.benchmark["harness_revision"]
                    prepared["grader_container_policy"] = {
                        **BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata),
                    }
                write_json(task_dir / "prepared.json", prepared)
                validation = {"valid": True}
                if isinstance(task.benchmark, dict):
                    validation["prepared_hash"] = digest(prepared)
                write_json(task_dir / "validation" / "validation.json", validation)

                with patch("deepswe_exp.components.results.storage.fetch_ollama_metadata", return_value={"version": "0.5.4", "digest": "sha256:qwen35"}):
                    sched = plan_runs(settings)
                    self.assertIn("model_metadata", sched)
                    self.assertEqual(sched["model_metadata"]["digest"], "sha256:qwen35")
                    self.assertTrue((out / "protocol_snapshot.json").is_file())
                    snapshot = read_json(out / "protocol_snapshot.json")
                    self.assertEqual(snapshot["metadata"]["digest"], "sha256:qwen35")

    def test_plan_runs_rejects_validation_for_different_prepared_inputs(self):
        with scratch_directory() as out:
            settings = self.saturn_settings
            with patch.object(type(settings), "output_root", new_callable=PropertyMock, return_value=out):
                task_id = settings.task_ids[0]
                task_dir = out / task_id
                from deepswe_exp.components.tasks.tasks import load_task
                from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY, benchmark_container_limits
                from deepswe_exp.components.tasks.tasks import benchmark_record_digest

                task = load_task(settings.task_path(task_id))
                prepared = {
                    "image": "img",
                    "manifests": {"100/42": {}, "50-random/42": {}, "0/42": {}},
                    "benchmark_instance_hash": benchmark_record_digest(task),
                    "harness_revision": task.benchmark["harness_revision"],
                    "grader_container_policy": {
                        **BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata),
                    },
                }
                write_json(task_dir / "prepared.json", prepared)
                write_json(task_dir / "validation" / "validation.json", {
                    "valid": True, "prepared_hash": "stale-prepared-inputs",
                })

                with self.assertRaisesRegex(ValueError, "does not match prepared inputs"):
                    plan_runs(settings)

    def test_schedule_digest_change_detection(self):
        with scratch_directory() as out:
            settings = self.saturn_settings
            with patch.object(type(settings), "output_root", new_callable=PropertyMock, return_value=out):
                task_dir = out / "adaptix-name-mapping-aliases"
                write_json(task_dir / "prepared.json", {"image": "img"})
                write_json(task_dir / "validation" / "validation.json", {"valid": True})
                meta = {"version": "0.5.4", "digest": "sha256:original"}
                proto_hash = protocol(settings, {"adaptix-name-mapping-aliases": {"image": "img"}}, model_metadata=meta)
                write_json(out / "schedule.json", {
                    "protocol_hash": proto_hash,
                    "model_metadata": meta,
                    "task_ids": ["adaptix-name-mapping-aliases"],
                    "runs": [],
                })

                # Server now returns different digest
                with patch("deepswe_exp.components.results.storage.fetch_ollama_metadata", return_value={"digest": "sha256:tampered"}):
                    with self.assertRaises(ValueError) as ctx:
                        schedule(settings)
                    self.assertIn("Model digest changed", str(ctx.exception))

    def test_run_all_executes_all_pending(self):
        runs = [
            {"status": "scored", "run_id": "r1", "task_id": "adaptix", "branch": "100", "seed": 42, "attempt_id": "att1"},
            {"status": "scored", "run_id": "r2", "task_id": "adaptix", "branch": "50-random", "seed": 42, "attempt_id": "att1"},
            {"status": "no_pending_runs"},
        ]

        def fake_run_next(settings, backend=None, task_id=None):
            return runs.pop(0)

        mock_schedule = {
            "runs": [
                {"status": "scored", "task_id": "adaptix", "branch": b, "seed": 42, "attempt_id": "att1"}
                for b in self.saturn_settings.config["branches"]
            ]
        }

        with patch("deepswe_exp.components.execution.runner.run_next", side_effect=fake_run_next), \
             patch("deepswe_exp.components.execution.runner.schedule", return_value=mock_schedule):
            res = run_all(self.saturn_settings, backend=None)
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["completed"], 2)

    def test_run_all_detects_incomplete_schedule(self):
        with patch("deepswe_exp.components.execution.runner.run_next", return_value={"status": "no_pending_runs"}), \
             patch("deepswe_exp.components.execution.runner.schedule", return_value={
                 "runs": [{"status": "interrupted", "group_valid": False, "task_id": "adaptix", "seed": 42, "branch": "100", "attempt_id": "att1"}]
             }):
            res = run_all(self.saturn_settings, backend=None)
            self.assertEqual(res["status"], "incomplete")
            self.assertEqual(res["incomplete_groups"], ["adaptix:seed_42"])

    def test_run_all_stops_immediately_on_interrupted(self):
        runs = [
            {"status": "scored", "run_id": "r1", "task_id": "adaptix", "branch": "100", "seed": 42, "attempt_id": "att1"},
            {"status": "interrupted", "run_id": "r2", "task_id": "adaptix", "branch": "50-random", "seed": 42, "attempt_id": "att1"},
            {"status": "scored", "run_id": "r3", "task_id": "adaptix", "branch": "0", "seed": 42, "attempt_id": "att1"},
        ]

        def fake_run_next(settings, backend=None, task_id=None):
            return runs.pop(0)

        with patch("deepswe_exp.components.execution.runner.run_next", side_effect=fake_run_next):
            res = run_all(self.saturn_settings, backend=None)
            self.assertEqual(res["status"], "interrupted")
            self.assertEqual(res["completed"], 1)
            self.assertEqual(res["failed_run_id"], "r2")
            self.assertEqual(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
