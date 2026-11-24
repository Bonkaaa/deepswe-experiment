from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import yaml
from litellm.exceptions import ContextWindowExceededError
from minisweagent import package_dir
from minisweagent.agents.default import DefaultAgent as _BaseDefaultAgent
from minisweagent.exceptions import FormatError, LimitsExceeded, Submitted, TimeExceeded

from ..execution.docker_env import DockerFailure
from ..results.schemas import AgentResult
from ..results.storage import append_event
from .mini_swe_env import SandboxEnvironment
from .mini_swe_model import build_mini_swe_model


class _RedactingAgent(_BaseDefaultAgent):
    """DefaultAgent subclass that redacts sensitive credentials before writing trajectory to disk."""

    def __init__(self, *args, redact_fn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._redact_fn = redact_fn

    def save(self, path: Path | None, *extra_dicts) -> dict:
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, indent=2)
            if self._redact_fn:
                text = self._redact_fn(text)
            path.write_text(text, encoding="utf-8")
        return data


DefaultAgent = _RedactingAgent


def _is_context_overflow_message(text: str) -> bool:
    low = text.lower()
    return any(
        phrase in low
        for phrase in (
            "context length",
            "context window",
            "prompt too long",
            "maximum context",
            "exceeded max context",
            "context_length_exceeded",
        )
    )


def _classify_repeated_format_error(messages: list[dict[str, Any]], settings: Any) -> str:
    num_ctx = int(getattr(settings, "ollama_num_ctx", 32768) or 32768)
    input_budget = int(getattr(settings, "input_budget", num_ctx) or num_ctx)
    budget = getattr(settings, "budget", {}) or {}
    max_output = int(budget.get("max_output_tokens", 4096) or 4096)

    for msg in reversed(messages or []):
        resp = msg.get("extra", {}).get("response") if isinstance(msg, dict) else None
        if not isinstance(resp, dict):
            continue
        choices = resp.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

        if total_tokens >= num_ctx - 64 or prompt_tokens >= input_budget:
            return "context_overflow"
        if finish_reason in ("length", "max_tokens") or completion_tokens >= max_output - 16:
            return "output_truncated"
        break
    return "format_error"


def _extract_usage(messages: list[dict[str, Any]], ledger: Any = None) -> list[dict[str, int]]:
    usage_list: list[dict[str, int]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        resp = msg.get("extra", {}).get("response") if isinstance(msg.get("extra"), dict) else None
        if not isinstance(resp, dict):
            continue
        u = resp.get("usage")
        if not isinstance(u, dict):
            continue
        prompt_tokens = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        completion_tokens = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        total_tokens = int(u.get("total_tokens") or (prompt_tokens + completion_tokens))
        entry = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        usage_list.append(entry)
        if ledger is not None and hasattr(ledger, "usage"):
            try:
                ledger.usage(entry)
            except Exception:
                pass
    return usage_list


def run_mini_swe_agent(
    *,
    settings: Any,
    instruction: str,
    box: Any,
    output_dir: Path,
    ledger: Any = None,
    protocol_hash: str = "",
    cwd: str = "/app",
) -> AgentResult:
    """Run mini-SWE-agent loop in the provided Docker sandbox and normalize output to AgentResult."""
    started = time.monotonic()
    budget = getattr(settings, "budget", {}) or {}
    timeout = int(budget.get("tool_timeout_seconds", 120))
    max_output_chars = int(budget.get("max_tool_output_chars", 8000))
    redact_fn = settings.redact if hasattr(settings, "redact") else None

    env = SandboxEnvironment(
        box=box,
        cwd=cwd,
        timeout=timeout,
        max_output_chars=max_output_chars,
        redact_fn=redact_fn,
    )
    model = build_mini_swe_model(settings)

    config_path = package_dir / "config" / "mini.yaml"
    if not config_path.is_file():
        config_path = package_dir / "config" / "default.yaml"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")).get("agent", {})
    config["step_limit"] = int(budget.get("max_model_requests", 100))
    config["cost_limit"] = 0.0
    if "wall_time_seconds" in budget:
        config["wall_time_limit_seconds"] = int(budget["wall_time_seconds"])

    traj_path = output_dir / "trajectory.json"
    config["output_path"] = traj_path

    agent = DefaultAgent(model=model, env=env, **config)
    if hasattr(agent, "_redact_fn"):
        agent._redact_fn = redact_fn

    stop_reason = "internal_error"
    error_detail = None

    try:
        run_output = agent.run(instruction)
        exit_status = run_output.get("exit_status") if isinstance(run_output, dict) else None
        if exit_status == "Submitted":
            stop_reason = "submitted"
        elif exit_status == "LimitsExceeded":
            stop_reason = "request_budget"
        elif exit_status == "TimeExceeded":
            stop_reason = "time_budget"
        elif exit_status == "RepeatedFormatError":
            stop_reason = _classify_repeated_format_error(getattr(agent, "messages", []), settings)
        else:
            stop_reason = "internal_error"
            error_detail = f"Unexpected agent exit_status: {exit_status}"
    except Submitted:
        stop_reason = "submitted"
    except TimeExceeded:
        stop_reason = "time_budget"
    except LimitsExceeded:
        stop_reason = "request_budget"
    except DockerFailure as exc:
        stop_reason = "docker_error"
        error_detail = redact_fn(str(exc)) if redact_fn else str(exc)
    except ContextWindowExceededError as exc:
        stop_reason = "context_overflow"
        error_detail = redact_fn(str(exc)) if redact_fn else str(exc)
    except Exception as exc:
        exc_str = str(exc)
        redacted_exc = redact_fn(exc_str) if redact_fn else exc_str
        if _is_context_overflow_message(exc_str):
            stop_reason = "context_overflow"
            error_detail = redacted_exc
        else:
            stop_reason = "internal_error"
            error_detail = redacted_exc

    elapsed = time.monotonic() - started

    if hasattr(agent, "save"):
        try:
            agent.save(traj_path)
            if traj_path.is_file() and redact_fn:
                raw_traj = traj_path.read_text(encoding="utf-8")
                traj_path.write_text(redact_fn(raw_traj), encoding="utf-8")
        except Exception:
            pass

    usage = _extract_usage(getattr(agent, "messages", []), ledger=ledger)
    for cmd_item in env.commands:
        try:
            append_event(output_dir / "commands.jsonl", cmd_item)
        except Exception:
            pass

    return {
        "schema_version": 1,
        "protocol_hash": protocol_hash,
        "stop_reason": stop_reason,
        "model_requests": getattr(agent, "n_calls", 0),
        "elapsed_seconds": elapsed,
        "usage": usage,
        "commands": env.commands,
        "context_stats": {},
        "error_detail": error_detail,
    }
