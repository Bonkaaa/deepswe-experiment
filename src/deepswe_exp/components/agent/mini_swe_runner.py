from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml
from litellm.exceptions import ContextWindowExceededError
from minisweagent import package_dir
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, LimitsExceeded, Submitted, TimeExceeded

from ..results.schemas import AgentResult
from .mini_swe_env import SandboxEnvironment
from .mini_swe_model import build_mini_swe_model


def run_mini_swe_agent(
    *,
    settings: Any,
    instruction: str,
    box: Any,
    output_dir: Path,
    ledger: Any = None,
    protocol_hash: str = "",
) -> AgentResult:
    """Run mini-SWE-agent loop in the provided Docker sandbox and normalize output to AgentResult."""
    started = time.monotonic()
    timeout = settings.budget.get("tool_timeout_seconds", 120)
    env = SandboxEnvironment(box=box, cwd="/app", timeout=timeout)
    model = build_mini_swe_model(settings)

    # Load agent configuration (prefer mini.yaml, fallback to default.yaml)
    config_path = package_dir / "config" / "mini.yaml"
    if not config_path.is_file():
        config_path = package_dir / "config" / "default.yaml"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")).get("agent", {})
    # Override budget constraints from study settings
    config["step_limit"] = settings.budget.get("max_model_requests", 100)
    config["cost_limit"] = 0.0  # Disable monetary cost ceiling in favor of step/time budget
    if "wall_time_seconds" in settings.budget:
        config["wall_time_limit_seconds"] = settings.budget["wall_time_seconds"]

    agent = DefaultAgent(model=model, env=env, **config)

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
            stop_reason = "format_error"
        else:
            stop_reason = "submitted"
    except Submitted:
        stop_reason = "submitted"
    except LimitsExceeded:
        stop_reason = "request_budget"
    except TimeExceeded:
        stop_reason = "time_budget"
    except ContextWindowExceededError:
        stop_reason = "context_overflow"
    except Exception as exc:
        exc_str = str(exc)
        if "context" in exc_str.lower() and "window" in exc_str.lower():
            stop_reason = "context_overflow"
        else:
            stop_reason = "internal_error"
            error_detail = exc_str

    elapsed = time.monotonic() - started

    # Save trajectory to output directory
    if hasattr(agent, "save"):
        try:
            agent.save(output_dir / "trajectory.json")
        except Exception:
            pass

    return {
        "schema_version": 1,
        "protocol_hash": protocol_hash,
        "stop_reason": stop_reason,
        "model_requests": getattr(agent, "n_calls", 0),
        "elapsed_seconds": elapsed,
        "usage": [],
        "commands": [],
        "context_stats": {},
        "error_detail": error_detail,
    }
