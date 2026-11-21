from __future__ import annotations

from typing import Any

from minisweagent.models.litellm_model import LitellmModel


def build_mini_swe_model(settings: Any) -> LitellmModel:
    """Build a LitellmModel instance configured for the study settings."""
    provider = getattr(settings, "model_provider", "google")
    if provider == "ollama":
        model_name = settings.model
        if not model_name.startswith("ollama_chat/"):
            model_name = f"ollama_chat/{model_name}"
        ollama_opts = getattr(settings, "ollama_options", {}) or {}
        temperature = float(ollama_opts.get("temperature", 0.0))
        return LitellmModel(
            model_name=model_name,
            model_kwargs={
                "api_base": settings.ollama_base_url,
                "temperature": temperature,
                "num_ctx": settings.ollama_num_ctx,
            },
            cost_tracking="ignore_errors",
        )
    elif provider == "google":
        model_name = settings.model
        if not model_name.startswith("gemini/"):
            model_name = f"gemini/{model_name}"
        return LitellmModel(
            model_name=model_name,
            model_kwargs={
                "api_key": settings.api_key,
                "temperature": 0.0,
            },
            cost_tracking="ignore_errors",
        )
    raise ValueError(f"Unsupported model provider: {provider}")
