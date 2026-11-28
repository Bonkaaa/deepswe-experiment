from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import litellm
import yaml
from minisweagent import package_dir
from minisweagent.models.litellm_model import LitellmModel


def _load_mini_model_config() -> dict[str, Any]:
    config_path = package_dir / "config" / "mini.yaml"
    if not config_path.is_file():
        config_path = package_dir / "config" / "default.yaml"
    if config_path.is_file():
        return yaml.safe_load(config_path.read_text(encoding="utf-8")).get("model", {}) or {}
    return {}


def build_mini_swe_model(settings: Any) -> LitellmModel:
    """Build a LitellmModel instance configured for the study settings."""
    provider = getattr(settings, "model_provider", "google")
    budget = getattr(settings, "budget", {}) or {}
    max_tokens = int(budget.get("max_output_tokens", 4096))
    wall_time = int(budget.get("wall_time_seconds", 1800))
    req_timeout = max(60, min(300 if wall_time <= 1800 else 1200, wall_time))

    base_cfg = _load_mini_model_config()
    base_kwargs = dict(base_cfg.get("model_kwargs", {}) or {})
    extra_config = {
        k: v for k, v in base_cfg.items()
        if k in ("observation_template", "format_error_template", "multimodal_regex", "set_cache_control")
    }

    if provider == "ollama":
        model_name = settings.model
        if not model_name.startswith("ollama_chat/"):
            model_name = f"ollama_chat/{model_name}"
        ollama_opts = getattr(settings, "ollama_options", {}) or {}
        temperature = float(ollama_opts.get("temperature", 0.0))
        model_kwargs = {
            **base_kwargs,
            "api_base": settings.ollama_base_url,
            "temperature": temperature,
            "num_ctx": settings.ollama_num_ctx,
            "max_tokens": max_tokens,
            "timeout": req_timeout,
        }
    elif provider == "vllm":
        model_name = settings.model
        if not model_name.startswith("openai/"):
            model_name = f"openai/{model_name}"
        vllm_opts = getattr(settings, "ollama_options", {}) or {}
        temperature = float(vllm_opts.get("temperature", 0.0))
        model_kwargs = {
            **base_kwargs,
            "api_base": settings.ollama_base_url,
            "api_key": "EMPTY",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": req_timeout,
        }
    elif provider == "google":
        model_name = settings.model
        if not model_name.startswith("gemini/"):
            model_name = f"gemini/{model_name}"
        model_kwargs = {
            **base_kwargs,
            "api_key": settings.api_key,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "timeout": req_timeout,
        }
    else:
        raise ValueError(f"Unsupported model provider: {provider}")

    model = LitellmModel(
        model_name=model_name,
        model_kwargs=model_kwargs,
        cost_tracking="ignore_errors",
        **extra_config,
    )
    if litellm.exceptions.BadRequestError not in model.abort_exceptions:
        model.abort_exceptions = [*model.abort_exceptions, litellm.exceptions.BadRequestError]
    return model
