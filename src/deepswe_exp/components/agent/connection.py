from __future__ import annotations

import asyncio
import time
import uuid

from langchain_core.messages import HumanMessage, ToolMessage

from ..results.storage import append_event, fetch_ollama_metadata, write_json, utc_now
from .agent import build_model


def check_model_connection(settings, timeout_seconds: int = 45, model=None) -> dict:
    """Send connectivity check and tool verification requests to verify the model."""
    provider = getattr(settings, "model_provider", "google")
    if provider == "google" and (not settings.model or not settings.api_key):
        raise ValueError("MODEL and GEMINI_API_KEY are required for check-model")
    elif provider == "ollama" and not settings.model:
        raise ValueError("MODEL is required for check-model")

    started = time.monotonic()
    result = {
        "schema_version": 1,
        "kind": "connectivity_check",
        "provider": provider,
        "profile_id": getattr(settings, "profile_id", None),
        "model": settings.model,
        "requested_at": utc_now(),
        "request_count": 0,
        "tool_calling_verified": False,
    }
    if provider == "ollama":
        meta = fetch_ollama_metadata(settings.ollama_base_url, settings.model)
        result["ollama_metadata"] = meta

    try:
        active_model = model or build_model(settings)
        project = settings.config.get("quota_project") or "default"
        ledger = settings.output_root.parent / "quota.jsonl"
        append_event(ledger, {
            "type": "request", "time": time.time(), "model": settings.model,
            "project": project, "tokens": 256, "kind": "connectivity_check",
        })
        result["request_count"] = 1

        invoke_kwargs = {}
        if provider == "google":
            invoke_kwargs = {"max_retries": 0, "timeout": timeout_seconds}

        messages = [
            HumanMessage(content="You are an assistant verifying shell tool access. Execute the command 'echo OK' using the run_shell tool.")
        ]
        response = asyncio.run(asyncio.wait_for(
            active_model.ainvoke(messages, **invoke_kwargs),
            timeout=timeout_seconds + 2,
        ))

        tool_calls = getattr(response, "tool_calls", None) or []
        shell_call = next((call for call in tool_calls if call.get("name") == "run_shell"), None)

        if shell_call:
            messages.append(response)
            messages.append(ToolMessage(content="exit_code=0\nOK", tool_call_id=shell_call["id"]))
            result["request_count"] += 1
            append_event(ledger, {
                "type": "request", "time": time.time(), "model": settings.model,
                "project": project, "tokens": 256, "kind": "connectivity_check_round2",
            })
            final_response = asyncio.run(asyncio.wait_for(
                active_model.ainvoke(messages, **invoke_kwargs),
                timeout=timeout_seconds + 2,
            ))
            result.update(
                status="success",
                tool_calling_verified=True,
                response_characters=len(str(final_response.content)),
                usage=getattr(final_response, "usage_metadata", None) or {},
            )
        else:
            result.update(
                status="tool_calling_failed",
                tool_calling_verified=False,
                response_characters=len(str(response.content)),
                usage=getattr(response, "usage_metadata", None) or {},
                error_type="ToolCallingError",
                detail="Model responded with text but did not invoke run_shell tool.",
            )

        append_event(ledger, {
            "type": "usage", "time": time.time(), "model": settings.model,
            "project": project, "kind": "connectivity_check",
            "usage": result.get("usage", {}),
        })
    except Exception as exc:
        result.update(
            status="error",
            error_type=type(exc).__name__,
            detail=settings.redact(str(exc))[:1500],
        )

    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    out = settings.output_root / "connectivity"
    path = out / f"model-check-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    write_json(path, result)
    result["artifact"] = str(path)
    return result
