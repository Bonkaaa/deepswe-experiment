from __future__ import annotations

import os
import sys
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
    "max_model_requests": 200,
    "wall_time_seconds": 7200,
    "max_output_tokens": 16384,
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
    profile_id: str | None = None
    model_provider: str = "google"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_num_ctx: int = 32768
    ollama_context_safety_tokens: int = 4096
    ollama_binary: Path | None = None
    ollama_models_dir: Path | None = None
    ollama_state_dir: Path | None = None
    ollama_options: dict = field(default_factory=dict)

    @property
    def input_budget(self) -> int:
        if self.model_provider != "ollama":
            return 1_000_000
        max_output = int(self.budget.get("max_output_tokens", 4096))
        return max(1024, self.ollama_num_ctx - max_output - self.ollama_context_safety_tokens)

    @property
    def study_id(self) -> str:
        return str(self.config.get("study_id", ""))

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
        if self.model_provider != "google":
            return True
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


def _resolve_profile_path(root_dir: Path, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    p = Path(raw_path)
    return p.resolve() if p.is_absolute() else (root_dir / p).resolve()


def _validate_profile(config: object) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Profile configuration must be a YAML mapping")
    for key in ("profile_id", "provider", "model", "study_config"):
        if key not in config or not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"Profile configuration missing or invalid: {key}")
    if config["provider"] not in ("ollama", "google"):
        raise ValueError(f"Unsupported provider: {config['provider']}")
    if config["provider"] == "ollama":
        ollama = config.get("ollama")
        if not isinstance(ollama, dict):
            raise ValueError("Ollama profile must include an 'ollama' mapping")
        for key in ("base_url", "binary", "models_dir", "state_dir", "num_ctx"):
            if key not in ollama:
                raise ValueError(f"Profile ollama section missing: {key}")
        if type(ollama["num_ctx"]) is not int or ollama["num_ctx"] <= 0:
            raise ValueError("ollama.num_ctx must be a positive integer")
    return config


def load_settings(
    config_path: str | Path | None = None,
    profile: str | None = None,
    study_id: str | None = None,
) -> Settings:
    root = find_root(Path(config_path).expanduser().resolve().parent if config_path else None)
    env = dotenv_values(root / ".env")

    if profile is not None:
        prof_file = root / "config" / "profiles" / f"{profile}.yaml"
        if not prof_file.is_file():
            raise ValueError(f"Unknown profile: {profile}; cannot find {prof_file}")
        prof_config = _validate_profile(yaml.safe_load(prof_file.read_text(encoding="utf-8")))

        def _norm_endpoint(val: str) -> str:
            s = str(val).strip().lower()
            for prefix in ("http://", "https://"):
                if s.startswith(prefix):
                    s = s[len(prefix):]
            return s.rstrip("/")

        def _norm_path(val: str) -> str:
            p = Path(str(val).strip())
            if not p.is_absolute():
                p = (root / p).resolve()
            else:
                p = p.resolve()
            return str(p).lower()

        checks = [
            ("MODEL", prof_config.get("model"), "str"),
            ("MODEL_PROVIDER", prof_config.get("provider"), "str"),
            ("OLLAMA_BASE_URL", prof_config.get("ollama", {}).get("base_url"), "endpoint"),
            ("OLLAMA_HOST", prof_config.get("ollama", {}).get("base_url"), "endpoint"),
            ("OLLAMA_MODELS", prof_config.get("ollama", {}).get("models_dir"), "path"),
            ("OLLAMA_NUM_CTX", str(prof_config.get("ollama", {}).get("num_ctx", "")), "str"),
        ]
        for var_name, prof_val, kind in checks:
            env_val = os.environ.get(var_name) or env.get(var_name)
            if env_val and prof_val:
                conflict = False
                if kind == "endpoint":
                    conflict = _norm_endpoint(env_val) != _norm_endpoint(prof_val)
                elif kind == "path":
                    conflict = _norm_path(env_val) != _norm_path(prof_val)
                else:
                    conflict = str(env_val).strip() != str(prof_val).strip()
                if conflict:
                    sys.stderr.write(
                        f"[WARNING] Environment {var_name}='{env_val}' conflicts with profile '{profile}' value '{prof_val}'. "
                        f"Please 'unset {var_name}' or remove it from .env before running.\n"
                    )

        if config_path is None:
            study_path = (root / prof_config["study_config"]).resolve()
        else:
            study_path = Path(config_path).expanduser().resolve()

        study_config = _validate_config(yaml.safe_load(study_path.read_text(encoding="utf-8")))
        if study_id:
            study_config = {**study_config, "study_id": str(study_id).strip()}
            _validate_config(study_config)
        provider = prof_config["provider"]
        model = prof_config["model"]
        key = os.environ.get("GEMINI_API_KEY", env.get("GEMINI_API_KEY") or "").strip() if provider == "google" else ""
        ollama = prof_config.get("ollama", {})

        return Settings(
            root=root,
            config_path=study_path,
            config=study_config,
            model=model,
            api_key=key,
            profile_id=prof_config["profile_id"],
            model_provider=provider,
            ollama_base_url=str(ollama.get("base_url", "http://127.0.0.1:11434")),
            ollama_num_ctx=int(ollama.get("num_ctx", 32768)),
            ollama_context_safety_tokens=int(ollama.get("context_safety_tokens", 4096)),
            ollama_binary=_resolve_profile_path(root, ollama.get("binary")),
            ollama_models_dir=_resolve_profile_path(root, ollama.get("models_dir")),
            ollama_state_dir=_resolve_profile_path(root, ollama.get("state_dir")),
            ollama_options=ollama,
        )

    if config_path is None:
        path = root / "config" / "study.yaml"
    else:
        path = Path(config_path).expanduser().resolve()
    config = _validate_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    if study_id:
        config = {**config, "study_id": str(study_id).strip()}
        _validate_config(config)
    model = os.environ.get("MODEL", env.get("MODEL") or "").strip()
    key = os.environ.get("GEMINI_API_KEY", env.get("GEMINI_API_KEY") or "").strip()
    provider = os.environ.get("MODEL_PROVIDER", env.get("MODEL_PROVIDER") or "google").strip()
    base_url = os.environ.get("OLLAMA_BASE_URL", env.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").strip()
    try:
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", env.get("OLLAMA_NUM_CTX") or 32768))
    except (ValueError, TypeError):
        num_ctx = 32768
    try:
        safety_tokens = int(os.environ.get("OLLAMA_CONTEXT_SAFETY_TOKENS", env.get("OLLAMA_CONTEXT_SAFETY_TOKENS") or 4096))
    except (ValueError, TypeError):
        safety_tokens = 4096

    return Settings(
        root=root,
        config_path=path,
        config=config,
        model=model,
        api_key=key,
        model_provider=provider,
        ollama_base_url=base_url,
        ollama_num_ctx=num_ctx,
        ollama_context_safety_tokens=safety_tokens,
    )
