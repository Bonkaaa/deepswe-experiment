from __future__ import annotations

import tomllib
import json
from importlib.metadata import distribution
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    path: Path
    metadata: dict
    instruction: str
    benchmark: dict | None = None

    @property
    def benchmark_type(self) -> str:
        if not isinstance(self.benchmark, dict):
            return "deepswe"
        explicit = str(self.benchmark.get("type") or "").strip().lower()
        if explicit:
            return explicit
        name = str(self.benchmark.get("name") or "").strip().lower()
        if "patcheval" in name:
            return "patcheval"
        return "swebench"

    @property
    def is_swebench(self) -> bool:
        return self.benchmark_type == "swebench"

    @property
    def is_patcheval(self) -> bool:
        return self.benchmark_type == "patcheval"

    @property
    def language(self) -> str:
        if self.is_patcheval:
            return str(self.metadata.get("programming_language") or self.benchmark.get("language") or "")
        if self.is_swebench:
            return str(self.metadata.get("programming_language") or "Go")
        return "Python"

    @property
    def task_id(self) -> str:
        return self.metadata.get("instance_id", self.metadata.get("cve_id", self.metadata.get("metadata", {}).get("task_id", self.path.name)))

    @property
    def base_commit(self) -> str:
        return self.metadata.get("base_commit", self.metadata.get("metadata", {}).get("base_commit_hash", ""))

    @property
    def image(self) -> str:
        return self.benchmark.get("image_ref", self.metadata.get("image", self.metadata.get("image_url", ""))) if self.benchmark else self.metadata["environment"]["docker_image"]

    @property
    def workdir(self) -> str:
        if self.metadata.get("workdir"):
            return str(self.metadata["workdir"])
        if self.is_patcheval:
            return "/workspace"
        return "/testbed" if self.benchmark else "/app"


def load_task(path: Path) -> Task:
    instance_file = path / "instance.json"
    if instance_file.is_file():
        source = json.loads(instance_file.read_text(encoding="utf-8"))
        return Task(path, source["instance"], source["instance"]["problem_statement"], source["benchmark"])

    task_file = path / "task.toml"
    instruction_file = path / "instruction.md"
    if not task_file.is_file() or not instruction_file.is_file():
        raise FileNotFoundError(f"Not a DeepSWE task directory: {path}")
    return Task(path, tomllib.loads(task_file.read_text(encoding="utf-8")), instruction_file.read_text(encoding="utf-8"))


def benchmark_record_digest(task: Task) -> str | None:
    if not isinstance(task.benchmark, dict):
        return None
    from ..results.storage import digest

    return digest(task.metadata)


def installed_swebench_revision() -> str:
    direct_url = distribution("swebench").read_text("direct_url.json")
    if not direct_url:
        raise ValueError("Installed SWE-bench distribution has no VCS revision metadata")
    try:
        revision = json.loads(direct_url).get("vcs_info", {}).get("commit_id")
    except (TypeError, ValueError) as exc:
        raise ValueError("Could not read installed SWE-bench revision metadata") from exc
    if not revision:
        raise ValueError("Installed SWE-bench distribution has no pinned Git commit")
    return revision


def is_patcheval_task(task) -> bool:
    bench = getattr(task, "benchmark", None)
    if not isinstance(bench, dict):
        return False
    explicit = str(bench.get("type") or "").strip().lower()
    if explicit:
        return explicit == "patcheval"
    return "patcheval" in str(bench.get("name") or "").strip().lower()


def verify_benchmark_record(task: Task, prepared: dict) -> None:
    if not isinstance(task.benchmark, dict):
        return
    if prepared.get("benchmark_instance_hash") != benchmark_record_digest(task):
        raise ValueError("Task benchmark instance changed after preparation; use a new study_id")
    from ..execution.docker_env import BENCHMARK_CONTAINER_POLICY,benchmark_container_limits

    expected_policy={**BENCHMARK_CONTAINER_POLICY,**benchmark_container_limits(task.metadata)}
    if prepared.get("grader_container_policy") != expected_policy:
        raise ValueError("Benchmark grader container policy changed after preparation; use a new study_id")
    if is_patcheval_task(task):
        return
    expected_harness = task.benchmark.get("harness_revision")
    if prepared.get("harness_revision") != expected_harness:
        raise ValueError("Prepared SWE-bench harness revision changed; use a new study_id")
    installed_harness = installed_swebench_revision()
    if installed_harness != expected_harness:
        raise ValueError(
            f"Installed SWE-bench harness revision {installed_harness} does not match "
            f"the task pin {expected_harness}; reinstall the pinned dependency"
        )
