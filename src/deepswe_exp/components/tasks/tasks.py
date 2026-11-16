from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    path: Path
    metadata: dict
    instruction: str

    @property
    def task_id(self) -> str:
        return self.metadata.get("metadata", {}).get("task_id", self.path.name)

    @property
    def base_commit(self) -> str:
        return self.metadata["metadata"]["base_commit_hash"]

    @property
    def image(self) -> str:
        return self.metadata["environment"]["docker_image"]


def load_task(path: Path) -> Task:
    task_file = path / "task.toml"
    instruction_file = path / "instruction.md"
    if not task_file.is_file() or not instruction_file.is_file():
        raise FileNotFoundError(f"Not a DeepSWE task directory: {path}")
    return Task(path, tomllib.loads(task_file.read_text(encoding="utf-8")), instruction_file.read_text(encoding="utf-8"))
