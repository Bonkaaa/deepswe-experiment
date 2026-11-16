from __future__ import annotations

import asyncio
import time
import uuid

from langchain_core.messages import HumanMessage

from ..results.storage import append_event, write_json, utc_now
from .agent import build_model


def check_model_connection(settings, timeout_seconds: int = 45) -> dict:
    """Send exactly one non-study request to verify the configured LangChain model."""
    if not settings.model or not settings.api_key:
        raise ValueError("MODEL and GEMINI_API_KEY are required for check-model")
    started = time.monotonic()
    result = {
        "schema_version": 1,
        "kind": "connectivity_check",
        "model": settings.model,
        "requested_at": utc_now(),
        "request_count": 0,
    }
    try:
        model = build_model(settings)
        project=settings.config.get("quota_project") or "default"
        ledger=settings.output_root.parent/"quota.jsonl"
        append_event(ledger,{"type":"request","time":time.time(),"model":settings.model,
                             "project":project,"tokens":256,"kind":"connectivity_check"})
        result["request_count"]=1
        response = asyncio.run(asyncio.wait_for(
            model.ainvoke([HumanMessage(content="Reply with the single word OK.")],
                          max_retries=0, timeout=timeout_seconds),
            timeout=timeout_seconds + 2,
        ))
        result.update(
            status="success",
            response_characters=len(str(response.content)),
            usage=response.usage_metadata or {},
        )
        append_event(ledger,{"type":"usage","time":time.time(),"model":settings.model,
                             "project":project,"kind":"connectivity_check",
                             "usage":response.usage_metadata or {}})
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__,
                      detail=settings.redact(str(exc))[:1500])
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    out = settings.output_root / "connectivity"
    path = out / f"model-check-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    write_json(path, result)
    result["artifact"] = str(path)
    return result

