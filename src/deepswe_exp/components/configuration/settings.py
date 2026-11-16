from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values

BRANCHES = ("100", "70-random", "50-random", "30-random", "0", "50-related")
BUDGET_KEYS = (
    "max_model_requests",
    "wall_time_seconds",
    "max_output_tokens",
    "max_tool_output_chars",
    "tool_timeout_seconds",
)
MAX_BUDGET = {
    "max_model_requests": 25,
    "wall_time_seconds": 1800,
    "max_output_tokens": 4096,
    "max_tool_output_chars": 8000,
    "tool_timeout_seconds": 120,
}


def find_root(start: str | Path | None = None) -> Path:
    anchors = (Path(start).resolve() if start else Path.cwd(), Path(__file__).resolve())
    for anchor in anchors:
        for candidate in (anchor, *anchor.parents):
            if (candidate / "pyproject.toml").is_file() and (candidate / "config").is_dir():
                return candidate
    raise ValueError("Cannot locate project root; supply --config")


@dataclass(frozen=True)
class Settings:
    root: Path
    config_path: Path
    config: dict
    model: str
    api_key: str = field(repr=False)

    @property
    def task_ids(self) -> list[str]:
        return self.config["tasks"]

    @property
    def task_dir(self) -> Path:
        return self.task_path(self.task_ids[0])

    def task_path(self, task: str) -> Path:
        return self.resolve_path(self.config["data_root"]) / task

    @property
    def output_root(self) -> Path:
        return self.resolve_path(self.config["output_root"]) / self.config["study_id"]

    @property
    def prompt_path(self) -> Path:
        return self.resolve_path(self.config.get("system_prompt", "prompt/system_prompt.md"))

    @property
    def budget(self) -> dict:
        return self.config["budget"]

    @property
    def seeds(self) -> list[int]:
        return self.config.get("seeds", [self.config.get("sample_seed", 42)])

    @property
    def quota_ready(self) -> bool:
        quota = self.config.get("quota", {})
        project = self.config.get("quota_project")
        return (isinstance(project, str) and bool(project.strip())
                and all(type(quota.get(key)) is int and quota[key] > 0 for key in ("rpm", "tpm", "rpd")))

    def resolve_path(self, configured: str | Path) -> Path:
        path = Path(configured)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def redact(self, value: object) -> str:
        text = str(value)
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text


def _validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a YAML mapping")
    for key in ("study_id", "data_root", "output_root", "tasks", "branches", "budget"):
        if key not in config:
            raise ValueError(f"Missing configuration: {key}")
    for key in ("study_id", "data_root", "output_root"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"Configuration {key} must be a non-empty path or identifier")
    if Path(config["study_id"]).name != config["study_id"] or config["study_id"] in (".", ".."):
        raise ValueError("Invalid study_id")
    if not isinstance(config["tasks"], list) or not config["tasks"]:
        raise ValueError("tasks must be a non-empty list")
    for task in config["tasks"]:
        if not isinstance(task, str) or not task or Path(task).name != task or task in (".", ".."):
            raise ValueError("Invalid task identifier")
    if len(set(config["tasks"])) != len(config["tasks"]):
        raise ValueError("Duplicate task identifiers")
    if not isinstance(config["branches"], list) or not config["branches"]:
        raise ValueError("branches must be a non-empty list")
    if any(not isinstance(branch, str) or branch not in BRANCHES for branch in config["branches"]):
        raise ValueError(f"branches must use supported values: {', '.join(BRANCHES)}")
    if len(set(config["branches"])) != len(config["branches"]):
        raise ValueError("Duplicate branch identifiers")
    budget = config["budget"]
    if not isinstance(budget, dict):
        raise ValueError("budget must be a mapping")
    for key in BUDGET_KEYS:
        if type(budget.get(key)) is not int or budget[key] <= 0:
            raise ValueError(f"Invalid budget.{key}; expected a positive integer")
        if budget[key] > MAX_BUDGET[key]:
            raise ValueError(f"budget.{key} cannot exceed the approved limit of {MAX_BUDGET[key]}")
    quota = config.get("quota", {})
    if not isinstance(quota, dict):
        raise ValueError("quota must be a mapping")
    for key in ("rpm", "tpm", "rpd"):
        n = quota.get(key)
        if n is not None and (type(n) is not int or n <= 0):
            raise ValueError(f"Invalid quota.{key}; expected a positive integer or null")
    if config.get("quota_project") is not None and (
        not isinstance(config["quota_project"], str) or not config["quota_project"].strip()
    ):
        raise ValueError("quota_project must be a non-empty project identifier or null")
    seeds = config.get("seeds", [config.get("sample_seed", 42)])
    if not isinstance(seeds, list) or not seeds or any(type(seed) is not int for seed in seeds):
        raise ValueError("seeds must be a non-empty list of integers")
    if len(set(seeds)) != len(seeds):
        raise ValueError("Duplicate seeds")
    options = config.get("task_options", {})
    if not isinstance(options, dict) or any(task not in config["tasks"] for task in options):
        raise ValueError("task_options must be a mapping keyed by configured task IDs")
    return config


def load_settings(config_path: str | Path | None = None) -> Settings:
    if config_path is None:
        root = find_root()
        path = root / "config" / "study.yaml"
    else:
        path = Path(config_path).expanduser().resolve()
        root = find_root(path.parent)
    config = _validate_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    env = dotenv_values(root / ".env")
    model = os.environ.get("MODEL", env.get("MODEL") or "").strip()
    key = os.environ.get("GEMINI_API_KEY", env.get("GEMINI_API_KEY") or "").strip()
    return Settings(root=root, config_path=path, config=config, model=model, api_key=key)
