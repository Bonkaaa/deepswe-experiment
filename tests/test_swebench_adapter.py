from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from deepswe_exp.components.evaluation.patches import collect_diff
from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.tasks.variants import source_hash
from deepswe_exp.components.tasks.tasks import load_task


TASK_ID = "gin-gonic__gin-1805"
BASE_COMMIT = "70a0aba3e423246be37462cfdaedd510c26c566e"
IMAGE = "swebench/sweb.eval.x86_64.gin-gonic_1776_gin-1805:latest"
TEST_NAMES = [
    "TestCreateDefaultRouter",
    "TestRouterGroupBasic",
    "TestRouterGroupBasicHandle",
    "TestRouterGroupInvalidStatic",
    "TestRouterGroupInvalidStaticFile",
    "TestRouterGroupTooManyHandlers",
    "TestRouterGroupBadMethod",
    "TestRouterGroupPipeline",
    "TestRouterMethod",
    "TestRouterGroupRouteOK",
    "TestRouterNotFound",
    "TestRouterStaticFSNotFound",
    "TestRouterStaticFSFileNotFound",
    "TestRouterMiddlewareAndStatic",
]


def benchmark_record() -> dict:
    return {
        "instance_id": TASK_ID,
        "repo": "gin-gonic/gin",
        "base_commit": BASE_COMMIT,
        "image": IMAGE,
        "version": "1805",
        "problem_statement": "Default engine produces two log lines with StaticFS for 404",
        "patch": "diff --git a/routergroup.go b/routergroup.go\n",
        "test_patch": "diff --git a/routes_test.go b/routes_test.go\n",
        "eval_script": "cd /testbed\ngo test -run '^Test.*Router' .",
        "eval_type": "pass_and_fail",
        "log_parser": "parse_log_gotest",
        "FAIL_TO_PASS": ["TestMiddlewareCalledOnceByRouterStaticFSNotFound"],
        "PASS_TO_PASS": ["TestRouterNotFound"],
    }


def benchmark_source(record: dict) -> dict:
    return {
        "benchmark": {
            "name": "SWE-bench Multilingual",
            "dataset_revision": "846e647b9f33c0b51b739d005d13d85493c9af09",
            "harness_revision": "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e",
            "image_ref": IMAGE,
            "task_options": {"test_pattern": "^Test.*Router", "product_paths": ["routergroup.go"]},
        },
        "instance": record,
    }


class SwebenchTaskTests(unittest.TestCase):
    def test_go_filter_helper_lives_inside_the_installable_package(self):
        from deepswe_exp.components.tasks import go_tests

        self.assertTrue(Path(go_tests.FILTER_SCRIPT).is_relative_to(Path(go_tests.__file__).parent))

    def test_preparation_audit_uses_the_pattern_recorded_in_the_manifest(self):
        from deepswe_exp.components.execution.preparation import audit_go

        box = MagicMock()
        box.cwd = "/testbed"
        task = MagicMock()
        task.benchmark = {"task_options": {}}
        manifest = {
            "kept_groups": ["routes_test.go::TestRouterKept"],
            "test_pattern": "^TestRouterKept$",
        }
        with patch(
            "deepswe_exp.components.execution.preparation.collect_go_tests",
            return_value=[{"function": "TestRouterKept"}],
        ) as collect:
            audit_go(box, task, manifest)

        collect.assert_called_once_with(box, "^TestRouterKept$", allow_empty=True, package=".")

    def test_go_subpackage_and_test_file_exclusion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            final = root / "final"
            for d in (base, final):
                (d / "internal" / "terraform").mkdir(parents=True)
                (d / "internal" / "terraform" / "eval_import.go").write_text("package terraform\nvar v = 1\n", encoding="utf-8")
                (d / "internal" / "terraform" / "context_plan_import_test.go").write_text("package terraform\nfunc TestA() {}\n", encoding="utf-8")

            # Editing *_test.go must NOT change source_hash or leak into agent_patch.diff
            h_before = source_hash(base, ["internal/terraform"])
            (final / "internal" / "terraform" / "context_plan_import_test.go").write_text("package terraform\n", encoding="utf-8")
            self.assertEqual(source_hash(final, ["internal/terraform"]), h_before)

            (final / "internal" / "terraform" / "eval_import.go").write_text("package terraform\nvar v = 2\n", encoding="utf-8")
            full_diff, product_diff = collect_diff(base, final, root / "out", ["internal/terraform"])
            self.assertIn("context_plan_import_test.go", full_diff.read_text(encoding="utf-8"))
            self.assertNotIn("context_plan_import_test.go", product_diff.read_text(encoding="utf-8"))
            self.assertIn("eval_import.go", product_diff.read_text(encoding="utf-8"))

    def test_swebench_prepare_resets_root_user_before_filtering_go_tests(self):
        from deepswe_exp.components.execution.preparation import _prepare_swebench

        with TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "data" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "instance.json").write_text(json.dumps(benchmark_source(benchmark_record())), encoding="utf-8")
            task = load_task(task_dir)

            settings = MagicMock()
            settings.output_root = root / "runs"
            settings.seeds = [42]
            settings.config = {
                "branches": ["100", "0"],
                "task_options": {TASK_ID: {"test_pattern": "^Test.*Router", "product_paths": ["routergroup.go"]}},
            }

            box = MagicMock()
            box.cwd = "/testbed"
            box.user = "root"
            box.__enter__.return_value = box
            box.__exit__.return_value = False
            backend = MagicMock()
            backend.resolve.return_value = IMAGE
            backend.prepare_swebench_image.return_value = "prepared-image"
            backend.sandbox.return_value = box

            users_during_filter: list[str] = []

            def fake_clean(target_box):
                target_box.user = "10001"

            def fake_export(target_box, dest):
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "routergroup.go").write_text("package gin\n", encoding="utf-8")

            def fake_filter(target_box, manifest, pattern, workdir, package="."):
                users_during_filter.append(target_box.user)

            rows = [{"node_id": "./::TestRouterNotFound", "file": "go_test_scope", "function": "TestRouterNotFound", "class": None}]
            with (
                patch("deepswe_exp.components.execution.preparation.checkout_base"),
                patch("deepswe_exp.components.execution.preparation.collect_go_tests", return_value=rows),
                patch("deepswe_exp.components.execution.preparation.clean", side_effect=fake_clean),
                patch("deepswe_exp.components.execution.preparation.export_app", side_effect=fake_export),
                patch("deepswe_exp.components.execution.preparation.filter_go_tests", side_effect=fake_filter),
                patch("deepswe_exp.components.execution.preparation.audit_go"),
            ):
                _prepare_swebench(settings, task, backend)

            self.assertEqual(users_during_filter, ["root", "root"])

    def test_execution_integrity_check_rejects_changed_variant_snapshots(self):
        from deepswe_exp.components.execution.preparation import verify_prepared_variant
        from deepswe_exp.components.results.storage import digest, write_json
        from deepswe_exp.components.tasks.variants import snapshot_hash, source_hash

        with TemporaryDirectory() as directory:
            root = Path(directory)
            variant = root / "prepared" / "50-random" / "42"
            snapshot = variant / "snapshot"
            snapshot.mkdir(parents=True)
            product = snapshot / "routergroup.go"
            product.write_text("package gin\nvar value = 1\n", encoding="utf-8")
            manifest = {
                "product_paths": ["routergroup.go"],
                "snapshot_hash": snapshot_hash(snapshot),
            }
            source_digest = source_hash(snapshot, manifest["product_paths"])
            manifest["source_hash"] = source_digest
            manifest_hash = digest(manifest)
            write_json(variant / "manifest.json", {**manifest, "manifest_hash": manifest_hash})
            prepared = {
                "source_hash": source_digest,
                "manifests": {"50-random/42": manifest_hash},
            }

            self.assertEqual(
                verify_prepared_variant(root, prepared, "50-random", 42),
                {**manifest, "manifest_hash": manifest_hash},
            )
            product.write_text("package gin\nvar value = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                verify_prepared_variant(root, prepared, "50-random", 42)

    def test_cached_deepswe_preparation_validates_manifest_hash(self):
        from deepswe_exp.components.execution.preparation import prepare
        from deepswe_exp.components.results.storage import digest, write_json
        from deepswe_exp.components.tasks.variants import snapshot_hash, source_hash

        with TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "cached-deepswe-task"
            settings = MagicMock()
            settings.output_root = root
            settings.seeds = [42]
            settings.config = {
                "branches": ["100"],
                "task_options": {task_id: {"product_paths": ["src/adaptix"]}},
            }
            settings.task_path.return_value = root / "task-source"
            task = MagicMock()
            task.benchmark = None

            base = root / task_id / "base" / "src" / "adaptix"
            base.mkdir(parents=True)
            (base / "module.py").write_text("value = 1\n", encoding="utf-8")
            variant = root / task_id / "prepared" / "100" / "42"
            snapshot = variant / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "src" / "adaptix").mkdir(parents=True)
            (snapshot / "src" / "adaptix" / "module.py").write_text("value = 1\n", encoding="utf-8")

            product_hash = source_hash(snapshot, ["src/adaptix"])
            manifest = {
                "product_paths": ["src/adaptix"],
                "source_hash": product_hash,
                "snapshot_hash": snapshot_hash(snapshot),
            }
            manifest_hash = digest(manifest)
            write_json(variant / "manifest.json", {**manifest, "manifest_hash": manifest_hash})
            task_root = root / task_id
            groups, all_nodes = [], []
            inventory_snapshot_hash = snapshot_hash(task_root / "base")
            write_json(task_root / "inventory.json", {
                "groups": groups, "all_nodes": all_nodes, "source_hash": product_hash,
                "snapshot_hash": inventory_snapshot_hash,
                "inventory_hash": digest({
                    "groups": groups, "all_nodes": all_nodes, "source_hash": product_hash,
                    "snapshot_hash": inventory_snapshot_hash,
                }),
            })
            old = {
                "source_hash": product_hash,
                "manifests": {"100/42": manifest_hash},
                "source_config_hash": digest({
                    "branches": ["100"], "seeds": [42],
                    "task_config": {"product_paths": ["src/adaptix"]},
                }),
            }
            write_json(task_root / "prepared.json", old)

            with patch("deepswe_exp.components.execution.preparation.load_task", return_value=task):
                self.assertEqual(prepare(settings, task_id, MagicMock()), old)

    def test_benchmark_record_digest_rejects_edits_after_preparation(self):
        from deepswe_exp.components.tasks.tasks import benchmark_record_digest, verify_benchmark_record
        from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY,benchmark_container_limits

        task = MagicMock()
        task.metadata = benchmark_record()
        task.benchmark = benchmark_source(benchmark_record())["benchmark"]
        prepared = {
            "benchmark_instance_hash": benchmark_record_digest(task),
            "harness_revision": task.benchmark["harness_revision"],
            "grader_container_policy": {
                **BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata),
            },
        }

        verify_benchmark_record(task, prepared)
        task.metadata["problem_statement"] = "changed after preparation"
        with self.assertRaisesRegex(ValueError, "benchmark instance changed"):
            verify_benchmark_record(task, prepared)

    def test_benchmark_record_verification_rejects_an_unpinned_installed_harness(self):
        from deepswe_exp.components.tasks.tasks import benchmark_record_digest, verify_benchmark_record
        from deepswe_exp.components.execution.docker_env import BENCHMARK_CONTAINER_POLICY, benchmark_container_limits

        task = MagicMock()
        task.metadata = benchmark_record()
        task.benchmark = {
            **benchmark_source(benchmark_record())["benchmark"],
            "harness_revision": "not-the-installed-revision",
        }
        prepared = {
            "benchmark_instance_hash": benchmark_record_digest(task),
            "grader_container_policy": {
                **BENCHMARK_CONTAINER_POLICY, **benchmark_container_limits(task.metadata),
            },
            "harness_revision": "not-the-installed-revision",
        }
        with patch(
            "deepswe_exp.components.tasks.tasks.installed_swebench_revision",
            return_value="02e7a74ffd0b707aab73d203fe87bdc7c76afc8e",
        ):
            with self.assertRaisesRegex(ValueError, "Installed SWE-bench harness revision"):
                verify_benchmark_record(task, prepared)

    def test_loads_benchmark_record_without_changing_existing_task_contract(self):
        with TemporaryDirectory() as directory:
            task_dir = Path(directory)
            (task_dir / "instance.json").write_text(json.dumps(benchmark_source(benchmark_record())))

            task = load_task(task_dir)

        self.assertEqual(task.task_id, TASK_ID)
        self.assertEqual(task.base_commit, BASE_COMMIT)
        self.assertEqual(task.image, IMAGE)
        self.assertEqual(task.instruction, benchmark_record()["problem_statement"])
        self.assertEqual(task.metadata["FAIL_TO_PASS"], benchmark_record()["FAIL_TO_PASS"])

    def test_go_test_selection_is_seeded_and_keeps_requested_case_count(self):
        from deepswe_exp.components.tasks.variants import choose, inventory

        rows = [
            {"node_id": f"./::{name}", "file": "routes_test.go", "function": name}
            for name in TEST_NAMES
        ]
        groups = inventory(rows, scope_part=None)

        half = choose(groups, "50-random", 42)
        repeated = choose(groups, "50-random", 42)
        empty = choose(groups, "0", 42)

        self.assertEqual(half["kept_cases"], 7)
        self.assertEqual(half["kept_groups"], repeated["kept_groups"])
        self.assertEqual(empty["kept_cases"], 0)

    def test_go_inventory_uses_go_test_regex_search_semantics(self):
        from deepswe_exp.components.tasks.go_tests import collect_go_tests

        box = MagicMock()
        box.execute.return_value = (0, "TestCreateDefaultRouter\nTestRouterNotFound\nok package\n")

        rows = collect_go_tests(box, "^Test.*Router")

        self.assertEqual([row["function"] for row in rows], ["TestCreateDefaultRouter", "TestRouterNotFound"])

    def test_go_test_json_counts_expected_top_level_tests_only(self):
        from deepswe_exp.components.tasks.go_tests import parse_go_test_results, run_selected_go_tests

        output = "\n".join(json.dumps(event) for event in (
            {"Action": "pass", "Package": "example.test/pkg", "Test": "TestKept"},
            {"Action": "pass", "Package": "example.test/pkg", "Test": "TestKept/subtest"},
            {"Action": "fail", "Package": "example.test/pkg", "Test": "TestBroken"},
            {"Action": "skip", "Package": "example.test/pkg", "Test": "TestSkipped"},
            {"Action": "pass", "Package": "example.test/pkg"},
        ))

        results = parse_go_test_results(output, ["TestKept", "TestBroken", "TestSkipped"])

        self.assertEqual(results, {
            "TestKept": "pass", "TestBroken": "fail", "TestSkipped": "skip",
        })

        box = MagicMock()
        box.execute.return_value = (0, output)
        rc, actual = run_selected_go_tests(
            box, {"kept_groups": ["routes_test.go::TestKept"]}, timeout=60
        )
        self.assertEqual((rc, actual), (0, output))
        self.assertIn("go test -json -run", box.execute.call_args.args[0])

    def test_go_filter_removes_unselected_functions_and_unused_imports(self):
        from deepswe_exp.components.execution.docker_env import DockerBackend
        from deepswe_exp.components.tasks.go_tests import filter_go_tests

        settings = load_settings()
        task = load_task(settings.task_path(TASK_ID))
        backend = DockerBackend()
        try:
            with backend.sandbox(task.image, task) as box:
                source = Path(settings.output_root.parent) / "go-filter-fixture"
                source.mkdir(parents=True, exist_ok=True)
                (source / "go.mod").write_text("module fixture\n\ngo 1.20\n", encoding="utf-8")
                (source / "routes_test.go").write_text(
                    'package fixture\n\nimport ("testing"; f "fmt"; "strings"; "math/rand/v2")\n\n'
                    'func TestRouterKept(t *testing.T) { fmt := struct{ Println func(...any) }{Println: t.Log}; fmt.Println("keep"); t.Log(rand.Int()) }\n\n'
                    'func TestRouterRemoved(t *testing.T) { f.Println(strings.TrimSpace("remove")) }\n\n'
                    'func TestUnrelated(t *testing.T) { t.Log("stay") }\n',
                    encoding="utf-8",
                )
                (source / "browser_js_test.go").write_text(
                    '//go:build js && wasm\n\npackage fixture\n\n'
                    'import ("testing"; "syscall/js")\n\n'
                    'func TestRouterBrowser(t *testing.T) { _ = js.Global() }\n',
                    encoding="utf-8",
                )
                (source / "inline_test.go").write_text(
                    'package fixture\n\nimport "testing"\n\n'
                    'func TestRouterInlineKept(t *testing.T) { t.Log("keep") }; '
                    'func TestRouterInlineRemoved(t *testing.T) { t.Log("remove") }\n',
                    encoding="utf-8",
                )
                box.checked("mkdir -p /tmp/go-filter-fixture")
                box.put_tree(source, "/tmp/go-filter-fixture")
                filter_go_tests(
                    box,
                    {"kept_groups": [
                        "routes_test.go::TestRouterKept",
                        "inline_test.go::TestRouterInlineKept",
                    ]},
                    "^TestRouter",
                    "/tmp/go-filter-fixture",
                )
                rc, output = box.execute(
                    "cd /tmp/go-filter-fixture && go test -list '^Test' .", timeout=120
                )
                self.assertEqual(rc, 0, output)
                names = {line.strip() for line in output.splitlines() if line.startswith("Test")}
                self.assertEqual(names, {"TestRouterKept", "TestRouterInlineKept", "TestUnrelated"})
                result = box.checked("cat /tmp/go-filter-fixture/routes_test.go")
                self.assertNotIn('"fmt"', result)
                self.assertNotIn('"strings"', result)
                self.assertIn('"math/rand/v2"', result)
                inline = box.checked("cat /tmp/go-filter-fixture/inline_test.go")
                self.assertIn("TestRouterInlineKept", inline)
                self.assertNotIn("TestRouterInlineRemoved", inline)
        finally:
            backend.client.close()

    def test_source_hash_accepts_a_single_product_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "routergroup.go"
            product.write_text("package gin\nvar value = 1\n", encoding="utf-8")

            first = source_hash(root, ["routergroup.go"])
            product.write_text("package gin\nvar value = 2\n", encoding="utf-8")
            second = source_hash(root, ["routergroup.go"])

        self.assertNotEqual(first, second)

    def test_collect_diff_accepts_product_file_allowlist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            final = root / "final"
            base.mkdir()
            final.mkdir()
            (base / "routergroup.go").write_text("package gin\n\nvar before = 1\n")
            (final / "routergroup.go").write_text("package gin\n\nvar after = 2\n")
            (base / "routes_test.go").write_text("package gin\n")
            (final / "routes_test.go").write_text("package gin\n\nfunc TestAgentAdded() {}\n")

            with patch.dict("os.environ", {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "",
            }):
                _, product = collect_diff(base, final, root / "run", ["routergroup.go"])

            diff = product.read_text(encoding="utf-8")
        self.assertIn("routergroup.go", diff)
        self.assertNotIn("routes_test.go", diff)


class SwebenchVerifierTests(unittest.TestCase):
    def test_official_grader_client_enforces_agent_sandbox_limits(self):
        from deepswe_exp.components.evaluation.verifier import _restricted_docker_client

        client = MagicMock()
        restricted = _restricted_docker_client(client, {"cpus": 1.5, "memory_mb": 2048})
        restricted.containers.create(
            image=IMAGE, cap_add=["SYS_ADMIN"], network_disabled=False,
        )

        kwargs = client.containers.create.call_args.kwargs
        self.assertTrue(kwargs["network_disabled"])
        self.assertEqual(kwargs["network_mode"], "none")
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertEqual(kwargs["cap_add"], ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"])
        self.assertEqual(kwargs["security_opt"], ["no-new-privileges"])
        self.assertEqual(kwargs["nano_cpus"], 1_500_000_000)
        self.assertEqual(kwargs["mem_limit"], "2048m")

    def test_unapplied_patch_report_without_terminal_marker_is_infrastructure_error(self):
        from deepswe_exp.components.evaluation.verifier import parse_swebench_report

        result = parse_swebench_report({TASK_ID: {
            "patch_successfully_applied": False,
            "infra_failure": False,
        }}, TASK_ID)

        self.assertEqual(result["status"], "infrastructure_error")
        self.assertIsNone(result["reward"])

    def test_go_compile_errors_after_patch_application_are_candidate_failures(self):
        from deepswe_exp.components.evaluation.verifier import _has_go_compile_failure

        with TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "run_instance.log").write_text(
                "2026-01-01 00:00:00 - INFO - >>>>> Applied Patch:\n", encoding="utf-8"
            )
            (log_dir / "test_output.txt").write_text(
                "# github.com/gin-gonic/gin\n"
                "./routergroup.go:12:2: undefined: missingSymbol\n"
                "FAIL\tgithub.com/gin-gonic/gin [build failed]\n",
                encoding="utf-8",
            )
            self.assertTrue(_has_go_compile_failure(log_dir))

            (log_dir / "run_instance.log").write_text("", encoding="utf-8")
            self.assertFalse(_has_go_compile_failure(log_dir))

    def test_go_package_startup_panic_after_patch_application_is_candidate_failure(self):
        from deepswe_exp.components.evaluation.verifier import _has_go_candidate_startup_panic

        with TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "run_instance.log").write_text(
                "2026-01-01 00:00:00 - INFO - >>>>> Applied Patch:\n", encoding="utf-8"
            )
            (log_dir / "test_output.txt").write_text(
                "panic: startup failure\n\n"
                "goroutine 1 [running]:\n"
                "github.com/gin-gonic/gin.init.0()\n"
                "\t/testbed/routergroup.go:12 +0x2a\n"
                "FAIL\tgithub.com/gin-gonic/gin 0.002s\n",
                encoding="utf-8",
            )
            self.assertTrue(_has_go_candidate_startup_panic(log_dir))

            (log_dir / "run_instance.log").write_text("", encoding="utf-8")
            self.assertFalse(_has_go_candidate_startup_panic(log_dir))

    def test_go_setup_failure_is_candidate_error_only_in_patched_source(self):
        from deepswe_exp.components.evaluation.verifier import _has_go_candidate_setup_failure

        with TemporaryDirectory() as directory:
            log_dir = Path(directory)
            patch_file = Path(directory) / "candidate.patch"
            patch_file.write_text(
                "diff --git a/routergroup.go b/routergroup.go\n"
                "--- a/routergroup.go\n+++ b/routergroup.go\n@@ -1 +1 @@\n",
                encoding="utf-8",
            )
            (log_dir / "run_instance.log").write_text(
                "2026-01-01 00:00:00 - INFO - >>>>> Applied Patch: verified already applied\n",
                encoding="utf-8",
            )
            output = (
                "# github.com/gin-gonic/gin\n"
                "./routergroup.go:12:8: no required module provides package example.com/missing\n"
                "FAIL\tgithub.com/gin-gonic/gin [setup failed]\n"
            )
            (log_dir / "test_output.txt").write_text(output, encoding="utf-8")
            self.assertTrue(_has_go_candidate_setup_failure(log_dir, patch_file))

            (log_dir / "test_output.txt").write_text(
                output.replace("routergroup.go:12", "routes_test.go:12"), encoding="utf-8"
            )
            self.assertFalse(_has_go_candidate_setup_failure(log_dir, patch_file))

            patch_file.write_text(
                "diff --git a/routergroup.go b/routergroup.go\n"
                "--- a/routergroup.go\n+++ b/routergroup.go\n@@ -1 +1 @@\n"
                "+++ b/routes_test.go\n",
                encoding="utf-8",
            )
            (log_dir / "test_output.txt").write_text(
                output.replace("./routergroup.go:12", "./routes_test.go:12"),
                encoding="utf-8",
            )
            self.assertFalse(_has_go_candidate_setup_failure(log_dir, patch_file))

            (log_dir / "test_output.txt").write_text(
                output.replace("./routergroup.go:12", "/opt/toolchain/routergroup.go:12"),
                encoding="utf-8",
            )
            self.assertFalse(_has_go_candidate_setup_failure(log_dir, patch_file))

    def test_only_terminal_patch_rejection_is_classified_as_apply_failure(self):
        from deepswe_exp.components.evaluation.verifier import (
            _classify_harness_error, _has_terminal_patch_rejection,
        )

        self.assertEqual(
            _classify_harness_error("Failed to apply patch to container: git apply --reject", False),
            "infrastructure_error",
        )
        self.assertEqual(
            _classify_harness_error(">>>>> Patch Apply Failed!\ninvalid hunk", False),
            "infrastructure_error",
        )
        self.assertEqual(_classify_harness_error("invalid hunk", True), "apply_failure")

        with TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "candidate-output.txt").write_text(">>>>> Patch Apply Failed:\n", encoding="utf-8")
            self.assertFalse(_has_terminal_patch_rejection(log_dir))
            (log_dir / "run_instance.log").write_text(
                "2026-01-01 00:00:00 - INFO - >>>>> Patch Apply Failed:\n", encoding="utf-8"
            )
            self.assertTrue(_has_terminal_patch_rejection(log_dir))

    def test_benchmark_validation_does_not_reuse_results_for_old_prepared_inputs(self):
        from deepswe_exp.components.execution.validation import _evaluate_swebench_with_retries
        from deepswe_exp.components.results.storage import write_json

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            write_json(folder / "result.json", {
                "status": "pass", "reward": 1, "protocol_hash": "old-prepared-hash",
            })
            with patch(
                "deepswe_exp.components.execution.validation.evaluate_patch",
                return_value={"status": "pass", "reward": 1, "protocol_hash": "current-prepared-hash"},
            ) as evaluate:
                result = _evaluate_swebench_with_retries(
                    MagicMock(), MagicMock(), IMAGE, Path("candidate.patch"), folder,
                    protocol_hash="current-prepared-hash",
                )

        self.assertEqual(result["protocol_hash"], "current-prepared-hash")
        evaluate.assert_called_once()

    def test_validation_rewrites_reference_patch_and_binds_cache_to_patch_bytes(self):
        import hashlib
        from deepswe_exp.components.execution.validation import _validate_swebench_task
        from deepswe_exp.components.results.storage import digest

        task = MagicMock()
        task.task_id = TASK_ID
        task.metadata = {**benchmark_record(), "patch": "fresh reference patch\n"}
        prepared = {"verifier_image": IMAGE}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for repeat in range(3):
                folder = root / "reference" / str(repeat)
                folder.mkdir(parents=True)
                (folder / "input.patch").write_text("stale reference patch\n", encoding="utf-8")
            observed = []

            def evaluate(_backend, _task, _image, patch, folder, protocol_hash):
                patch_bytes = Path(patch).read_bytes()
                observed.append((Path(folder).parent.name, patch_bytes, protocol_hash))
                if Path(folder).parent.name == "base":
                    return {"status": "fail_test", "reward": 0, "benchmark": {
                        "p2p_passed": 1, "p2p_total": 1, "f2p_passed": 0, "f2p_total": 1,
                    }}
                return {"status": "pass", "reward": 1}

            with patch(
                "deepswe_exp.components.execution.validation._evaluate_swebench_with_retries",
                side_effect=evaluate,
            ):
                _validate_swebench_task(MagicMock(), task, prepared, MagicMock(), root)

        expected_bytes = b"fresh reference patch\n"
        expected_protocol_hash = digest({
            "prepared_hash": digest(prepared),
            "patch_sha256": hashlib.sha256(expected_bytes).hexdigest(),
        })
        references = [row for row in observed if row[0] == "reference"]
        self.assertEqual([row[1] for row in references], [expected_bytes] * 3)
        self.assertEqual([row[2] for row in references], [expected_protocol_hash] * 3)

    def test_benchmark_validation_retries_transient_cached_failures(self):
        from deepswe_exp.components.execution.validation import _evaluate_swebench_with_retries
        from deepswe_exp.components.results.storage import write_json

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            write_json(folder / "result.json", {"status": "infrastructure_error"})
            with patch(
                "deepswe_exp.components.execution.validation.evaluate_patch",
                side_effect=[{"status": "timeout"}, {"status": "pass", "reward": 1}],
            ) as evaluate:
                result = _evaluate_swebench_with_retries(
                    MagicMock(), MagicMock(), IMAGE, Path("candidate.patch"), folder,
                )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(evaluate.call_count, 2)

    def test_official_harness_logs_are_study_local_and_restored(self):
        from swebench.harness import run_evaluation
        from deepswe_exp.components.evaluation.verifier import _run_official_harness

        task = MagicMock()
        task.task_id = TASK_ID
        task.metadata = {**benchmark_record(), "eval_script": "cd /testbed\ngo test ./...\n"}
        task.path = Path("data") / TASK_ID
        previous_log_dir = run_evaluation.RUN_EVALUATION_LOG_DIR
        with TemporaryDirectory() as directory:
            root = Path(directory)
            patch_file = root / "patch.diff"
            patch_file.write_text("", encoding="utf-8")
            observed = {}

            def inspect_log_dir(*args, **kwargs):
                observed["log_dir"] = Path(run_evaluation.RUN_EVALUATION_LOG_DIR)
                return TASK_ID, {}

            with patch("swebench.harness.run_evaluation.run_instance", side_effect=inspect_log_dir):
                _run_official_harness(
                    task=task, image=IMAGE, patch=patch_file, client=MagicMock(), run_id="local-log-test",
                    output_dir=root, dataset_revision="dataset", harness_revision="harness", timeout=60,
                )
            self.assertEqual(observed["log_dir"], root / "harness")
            self.assertEqual(run_evaluation.RUN_EVALUATION_LOG_DIR, previous_log_dir)

    def test_visible_go_evaluation_runs_json_tests_inside_sandbox(self):
        from deepswe_exp.components.evaluation.verifier import evaluate_supplied_tests

        events = "\n".join(json.dumps({
            "Action": "pass", "Package": "github.com/gin-gonic/gin", "Test": name,
        }) for name in ("TestRouterOne", "TestRouterTwo"))
        backend = MagicMock()
        box = MagicMock()
        box.cwd = "/testbed"
        backend.sandbox.return_value.__enter__.return_value = box
        sandbox_closed = {"value": False}

        def close_sandbox(*args):
            sandbox_closed["value"] = True

        def run_go_tests(*args, **kwargs):
            if sandbox_closed["value"]:
                raise RuntimeError("Go tests ran after their Docker sandbox closed")
            return 0, events

        backend.sandbox.return_value.__exit__.side_effect = close_sandbox
        task = MagicMock()
        task.benchmark = {"task_options": {"test_pattern": "^TestRouter"}}
        task.workdir = "/testbed"
        manifest = {
            "kept_node_ids": ["./::TestRouterOne", "./::TestRouterTwo"],
            "kept_groups": ["routes_test.go::TestRouterOne", "routes_test.go::TestRouterTwo"],
        }

        with TemporaryDirectory() as directory:
            patch_file = Path(directory) / "candidate.patch"
            patch_file.write_bytes(b"")
            with patch("deepswe_exp.components.execution.preparation.clean"), \
                 patch("deepswe_exp.components.evaluation.verifier.run_selected_go_tests", side_effect=run_go_tests):
                result = evaluate_supplied_tests(
                    backend, task, "gin-image", Path(directory) / "snapshot", manifest,
                    patch_file, Path(directory) / "visible",
                )

        self.assertEqual(result["status"], "pass")
        self.assertEqual((result["passed"], result["failed"], result["total"]), (2, 0, 2))
        self.assertTrue(sandbox_closed["value"])

    def test_maps_official_harness_report_into_stable_result_schema(self):
        from deepswe_exp.components.evaluation.verifier import parse_swebench_report

        report = {
            TASK_ID: {
                "patch_exists": True,
                "patch_successfully_applied": True,
                "resolved": True,
                "infra_failure": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": ["issue-test"], "failure": []},
                    "PASS_TO_PASS": {"success": ["regression-test"], "failure": []},
                },
            }
        }

        result = parse_swebench_report(report, TASK_ID)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reward"], 1)
        self.assertEqual(result["benchmark"]["f2p_passed"], 1)
        self.assertEqual(result["benchmark"]["p2p_total"], 1)

    def test_classifies_empty_patch_as_a_benchmark_failure_not_apply_failure(self):
        from deepswe_exp.components.evaluation.verifier import parse_swebench_report

        result = parse_swebench_report(
            {TASK_ID: {
                "patch_exists": True,
                "patch_successfully_applied": True,
                "resolved": False,
                "infra_failure": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": [], "failure": ["issue-test"]},
                    "PASS_TO_PASS": {"success": ["regression-test"], "failure": []},
                },
            }},
            TASK_ID,
        )

        self.assertEqual(result["status"], "fail_test")
        self.assertEqual(result["benchmark"]["f2p_failed"], 1)

    def test_uses_run_scoped_harness_logs_and_pinned_test_spec(self):
        from deepswe_exp.components.evaluation.verifier import run_swebench_patch

        backend = MagicMock()
        task = MagicMock()
        task.task_id = TASK_ID
        task.metadata = benchmark_record()
        task.benchmark = benchmark_source(benchmark_record())["benchmark"]
        task.path = Path("data") / TASK_ID
        report = {TASK_ID: {
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": False,
            "infra_failure": False,
            "tests_status": {
                "FAIL_TO_PASS": {"success": [], "failure": ["issue-test"]},
                "PASS_TO_PASS": {"success": ["regression-test"], "failure": []},
            },
        }}

        with TemporaryDirectory() as directory:
            patch_file = Path(directory) / "patch.diff"
            patch_file.write_text("", encoding="utf-8")
            with patch(
                "deepswe_exp.components.evaluation.verifier._run_official_harness",
                return_value=report,
            ) as run_harness:
                result = run_swebench_patch(
                    backend, task, IMAGE, patch_file, Path(directory) / "evaluation", "protocol"
                )

        self.assertEqual(result["status"], "fail_test")
        kwargs = run_harness.call_args.kwargs
        self.assertEqual(kwargs["dataset_revision"], "846e647b9f33c0b51b739d005d13d85493c9af09")
        self.assertEqual(kwargs["harness_revision"], "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e")

    def test_official_test_spec_normalizes_benchmark_script_line_endings(self):
        from deepswe_exp.components.evaluation.verifier import _run_official_harness

        task = MagicMock()
        task.task_id = TASK_ID
        task.metadata = {**benchmark_record(), "eval_script": "cd /testbed\r\ngo test ./...\r\n"}
        task.path = Path("data") / TASK_ID
        with TemporaryDirectory() as directory:
            patch_file = Path(directory) / "patch.diff"
            patch_file.write_text("", encoding="utf-8")
            def write_harness_script(*args, **kwargs):
                (Path(directory) / "eval.sh").write_text("one\ntwo\n", encoding="utf-8")
                return TASK_ID, {}

            with patch("swebench.harness.run_evaluation.run_instance", side_effect=write_harness_script) as run_instance:
                _run_official_harness(
                    task=task,
                    image=IMAGE,
                    patch=patch_file,
                    client=MagicMock(),
                    run_id="test-run",
                    output_dir=Path(directory),
                    dataset_revision="dataset-revision",
                    harness_revision="harness-revision",
                    timeout=60,
                )
                script = "\n".join(run_instance.call_args.args[0].eval_script_list)
                self.assertNotIn("\r", script)
                self.assertIn("go test ./...", script)
                self.assertEqual((Path(directory) / "eval.sh").read_bytes(), b"one\ntwo\n")

    def test_official_harness_scores_startup_panic_even_when_report_marks_infrastructure(self):
        from swebench.harness import run_evaluation
        from deepswe_exp.components.evaluation.verifier import _run_official_harness
        from deepswe_exp.components.results.storage import write_json

        task = MagicMock()
        task.task_id = TASK_ID
        task.metadata = {**benchmark_record(), "eval_script": "cd /testbed\ngo test ./...\n"}
        task.path = Path("data") / TASK_ID
        with TemporaryDirectory() as directory:
            root = Path(directory)
            patch_file = root / "patch.diff"
            patch_file.write_text("", encoding="utf-8")

            def write_infra_report(*args, **kwargs):
                log_dir = Path(run_evaluation.RUN_EVALUATION_LOG_DIR) / "panic-run" / "deepswe-experiment" / TASK_ID
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "run_instance.log").write_text(
                    "2026-01-01 00:00:00 - INFO - >>>>> Applied Patch: verified already applied\n",
                    encoding="utf-8",
                )
                (log_dir / "test_output.txt").write_text(
                    "panic: Failed to launch router\n\n"
                    "goroutine 1 [running]:\n"
                    "github.com/gin-gonic/gin.init.0()\n"
                    "\t/testbed/routergroup.go:12 +0x2a\n"
                    "FAIL\tgithub.com/gin-gonic/gin 0.002s\n",
                    encoding="utf-8",
                )
                write_json(log_dir / "report.json", {TASK_ID: {
                    "patch_successfully_applied": False, "infra_failure": True,
                }})
                return TASK_ID, {}

            with patch("swebench.harness.run_evaluation.run_instance", side_effect=write_infra_report):
                result = _run_official_harness(
                    task=task, image=IMAGE, patch=patch_file, client=MagicMock(), run_id="panic-run",
                    output_dir=root, dataset_revision="dataset", harness_revision="harness", timeout=60,
                )

        self.assertEqual(result["_error"], "fail_test")

    def test_default_studies_use_multilingual_and_deepswe_configs_are_preserved(self):
        root = Path(__file__).resolve().parents[1]
        for config in ("study.yaml", "study.saturn.yaml", "study.neptune.yaml"):
            settings = load_settings(config_path=root / "config" / config)
            self.assertEqual(settings.task_ids, ["hashicorp__terraform-35543"])
            self.assertEqual(settings.config["branches"], ["100", "50-random", "0"])
        for config in ("study.deepswe.yaml", "study.saturn.deepswe.yaml", "study.neptune.deepswe.yaml"):
            settings = load_settings(config_path=root / "config" / config)
            self.assertEqual(settings.task_ids, ["adaptix-name-mapping-aliases"])


if __name__ == "__main__":
    unittest.main()
