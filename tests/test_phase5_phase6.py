from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

from deepswe_exp.components.configuration.settings import load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

EXPECTED_SCRIPTS = [
    "start_ollama_local.sh",
    "stop_ollama_local.sh",
    "run_experiment_a.sh",
    "run_experiment_a_saturn.sh",
    "run_experiment_a_neptune.sh",
]


class TestPhase5Phase6Scripts(unittest.TestCase):
    def test_scripts_exist_and_use_lf_line_endings(self) -> None:
        """Verify that all scripts exist and strictly use Unix LF line endings (no \r)."""
        self.assertTrue(SCRIPTS_DIR.is_dir(), f"Scripts directory missing: {SCRIPTS_DIR}")
        for script_name in EXPECTED_SCRIPTS:
            script_path = SCRIPTS_DIR / script_name
            self.assertTrue(script_path.is_file(), f"Expected script missing: {script_name}")
            raw_bytes = script_path.read_bytes()
            self.assertNotIn(
                b"\r",
                raw_bytes,
                f"Script {script_name} contains CRLF (\\r). It must strictly use LF line endings.",
            )

    def test_scripts_have_shebang_and_strict_mode(self) -> None:
        """Verify that all scripts begin with standard bash shebang and set -euo pipefail."""
        for script_name in EXPECTED_SCRIPTS:
            script_path = SCRIPTS_DIR / script_name
            self.assertTrue(script_path.is_file(), f"Script missing: {script_name}")
            content = script_path.read_text(encoding="utf-8")
            lines = [line.strip() for line in content.split("\n") if line.strip()]
            self.assertGreater(len(lines), 0, f"Script {script_name} is empty")
            self.assertEqual(
                lines[0],
                "#!/usr/bin/env bash",
                f"Script {script_name} must start with '#!/usr/bin/env bash'",
            )
            self.assertTrue(
                any("set -euo pipefail" in line for line in lines[:5]),
                f"Script {script_name} must enable strict mode: set -euo pipefail",
            )

    def test_wrappers_forward_correct_profile(self) -> None:
        """Verify wrapper scripts pass the correct profile argument to run_experiment_a.sh."""
        saturn_path = SCRIPTS_DIR / "run_experiment_a_saturn.sh"
        self.assertTrue(saturn_path.is_file(), "run_experiment_a_saturn.sh missing")
        saturn_content = saturn_path.read_text(encoding="utf-8")
        self.assertIn("run_experiment_a.sh", saturn_content)
        self.assertIn("--profile saturn", saturn_content)
        self.assertIn('"$@"', saturn_content)

        neptune_path = SCRIPTS_DIR / "run_experiment_a_neptune.sh"
        self.assertTrue(neptune_path.is_file(), "run_experiment_a_neptune.sh missing")
        neptune_content = neptune_path.read_text(encoding="utf-8")
        self.assertIn("run_experiment_a.sh", neptune_content)
        self.assertIn("--profile neptune", neptune_content)
        self.assertIn('"$@"', neptune_content)

    def test_run_experiment_a_pipeline_sequence(self) -> None:
        """Verify run_experiment_a.sh runs the complete deepswe pipeline in proper sequence."""
        runner_path = SCRIPTS_DIR / "run_experiment_a.sh"
        self.assertTrue(runner_path.is_file(), "run_experiment_a.sh missing")
        content = runner_path.read_text(encoding="utf-8")

        required_steps = [
            "doctor",
            "check-model",
            "prepare",
            "validate",
            "plan-runs",
            "run-all",
            "summarize",
        ]
        pos = 0
        for step in required_steps:
            idx = content.find(step, pos)
            self.assertNotEqual(
                idx,
                -1,
                f"Step '{step}' missing or not in sequential order in run_experiment_a.sh",
            )
            pos = idx + len(step)

        # Check tarball packaging and scp message
        self.assertIn(".tar.gz", content)
        self.assertIn("scp ", content)
        # Check safe passthrough handling (no unbound variable under set -u)
        self.assertIn("PASSTHROUGH_ARGS", content)
        self.assertNotIn('"${PASSTHROUGH_ARGS[@]:+', content)

    def test_ollama_lifecycle_scripts_content(self) -> None:
        """Verify start_ollama_local.sh and stop_ollama_local.sh handle PID and healthcheck."""
        start_script = SCRIPTS_DIR / "start_ollama_local.sh"
        self.assertTrue(start_script.is_file(), "start_ollama_local.sh missing")
        start_content = start_script.read_text(encoding="utf-8")
        self.assertIn("api/tags", start_content)
        self.assertIn(".pid", start_content)
        self.assertIn("serve", start_content)
        self.assertIn("ollama_base_url", start_content)

        stop_script = SCRIPTS_DIR / "stop_ollama_local.sh"
        self.assertTrue(stop_script.is_file(), "stop_ollama_local.sh missing")
        stop_content = stop_script.read_text(encoding="utf-8")
        self.assertIn(".pid", stop_content)
        self.assertIn("kill", stop_content)
        self.assertIn("ollama_state_dir", stop_content)

    def test_settings_study_id_property(self) -> None:
        """Verify Settings.study_id property correctly mirrors config['study_id']."""
        saturn_settings = load_settings(profile="saturn")
        self.assertEqual(saturn_settings.study_id, "pilot-a-saturn-qwen36")
        self.assertEqual(saturn_settings.study_id, saturn_settings.config["study_id"])

        neptune_settings = load_settings(profile="neptune")
        self.assertEqual(neptune_settings.study_id, "pilot-a-neptune-qwen38")
        self.assertEqual(neptune_settings.study_id, neptune_settings.config["study_id"])

    def test_python_snippets_in_scripts_execute_without_error(self) -> None:
        """Execute the embedded Python snippets used in shell scripts and verify values."""
        # 1. Snippet in run_experiment_a.sh for extracting study_id
        for profile, expected_id in [("saturn", "pilot-a-saturn-qwen36"), ("neptune", "pilot-a-neptune-qwen38")]:
            cmd = [
                sys.executable,
                "-c",
                "import sys; from deepswe_exp.components.configuration.settings import load_settings; s = load_settings(profile=sys.argv[1]); print(s.config['study_id'])",
                profile,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.assertEqual(res.stdout.strip(), expected_id)

        # 2. Snippet in start_ollama_local.sh for extracting ollama config
        for profile, expected_port, expected_model in [
            ("saturn", "11434", "qwen3.6:35b-a3b-coding"),
            ("neptune", "11435", "qwen3.8:27b"),
        ]:
            cmd = [
                sys.executable,
                "-c",
                """
import sys, shlex
from deepswe_exp.components.configuration.settings import load_settings
profile = sys.argv[1]
s = load_settings(profile=profile)
print(f"BASE_URL={shlex.quote(str(s.ollama_base_url))}")
print(f"MODELS_DIR={shlex.quote(str(s.ollama_models_dir) if s.ollama_models_dir else '')}")
print(f"STATE_DIR={shlex.quote(str(s.ollama_state_dir) if s.ollama_state_dir else f'scratch/ollama/{profile}')}")
print(f"BINARY={shlex.quote(str(s.ollama_binary) if s.ollama_binary else 'ollama')}")
print(f"MODEL={shlex.quote(str(s.model))}")
""",
                profile,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.assertIn(f"127.0.0.1:{expected_port}", res.stdout)
            self.assertIn(expected_model, res.stdout)
            if profile == "neptune":
                self.assertIn("ollama-neptune", res.stdout)

        # 3. Snippet in stop_ollama_local.sh for state_dir
        cmd = [
            sys.executable,
            "-c",
            "import sys; from deepswe_exp.components.configuration.settings import load_settings; s = load_settings(profile=sys.argv[1]); print(str(s.ollama_state_dir) if s.ollama_state_dir else f'scratch/ollama/{profile}')",
            "neptune",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("ollama-neptune", res.stdout)

    def test_archive_packaging_and_restoration(self) -> None:
        """Verify tarball creation and extraction preserves directory hierarchy and artifacts."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            study_root = tmp_path / "runs" / "pilot-a-test"
            study_root.mkdir(parents=True, exist_ok=True)

            (study_root / "schedule.json").write_text('{"runs": []}', encoding="utf-8")
            (study_root / "protocol_snapshot.json").write_text('{"digest": "123"}', encoding="utf-8")

            summary_dir = study_root / "summary"
            summary_dir.mkdir()
            (summary_dir / "summary.md").write_text("# Report\nResults", encoding="utf-8")
            (summary_dir / "runs.csv").write_text("task_id,status\n1,scored\n", encoding="utf-8")

            task_dir = study_root / "task_1" / "seed_42" / "attempt_1" / "branch_control"
            task_dir.mkdir(parents=True)
            (task_dir / "trajectory.json").write_text('{"events": []}', encoding="utf-8")
            (task_dir / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")

            archive_path = tmp_path / "runs" / "pilot-a-test.tar.gz"
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(study_root, arcname="pilot-a-test")

            self.assertTrue(archive_path.is_file())

            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=extract_dir)

            restored_study = extract_dir / "pilot-a-test"
            self.assertTrue((restored_study / "schedule.json").is_file())
            self.assertTrue((restored_study / "protocol_snapshot.json").is_file())
            self.assertTrue((restored_study / "summary" / "summary.md").is_file())
            self.assertTrue((restored_study / "summary" / "runs.csv").is_file())
            self.assertTrue(
                (restored_study / "task_1" / "seed_42" / "attempt_1" / "branch_control" / "trajectory.json").is_file()
            )
            self.assertTrue(
                (restored_study / "task_1" / "seed_42" / "attempt_1" / "branch_control" / "patch.diff").is_file()
            )


if __name__ == "__main__":
    unittest.main()
