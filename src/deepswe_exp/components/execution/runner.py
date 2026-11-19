from __future__ import annotations

import asyncio
import random
import uuid

from ..agent.agent import run_agent
from ..agent.quota import QuotaLedger, LocalLedger
from ..evaluation.patches import collect_diff
from ..evaluation.verifier import evaluate_patch, evaluate_supplied_tests
from ..results.storage import protocol, read_json, run_dir, utc_now, write_json
from ..results.schemas import RunSpecification, RunState
from ..tasks.tasks import load_task
from .preparation import create_agent, export_app


def plan_runs(settings, task_ids=None):
    selected = list(task_ids or settings.task_ids)
    if not settings.model:
        raise ValueError("Set MODEL before freezing the study protocol")
    if not selected or any(task not in settings.task_ids for task in selected):
        raise ValueError("Invalid task selection")
    prepared = {task: read_json(settings.output_root / task / "prepared.json") for task in selected}
    for task in selected:
        validation = read_json(settings.output_root / task / "validation/validation.json")
        if not validation.get("valid"):
            raise ValueError(f"Task not validated: {task}")
    model_metadata = None
    if getattr(settings, "model_provider", "google") == "ollama":
        from ..results.storage import fetch_ollama_metadata
        model_metadata = fetch_ollama_metadata(settings.ollama_base_url, settings.model)
        write_json(settings.output_root / "protocol_snapshot.json", {
            "study_id": settings.config["study_id"],
            "profile_id": getattr(settings, "profile_id", None),
            "model": settings.model,
            "ollama_base_url": settings.ollama_base_url,
            "ollama_num_ctx": settings.ollama_num_ctx,
            "metadata": model_metadata,
            "frozen_at": utc_now(),
        })
    protocol_hash = protocol(settings, prepared, selected, model_metadata=model_metadata)
    path = settings.output_root / "schedule.json"
    if path.exists():
        old = read_json(path)
        if old.get("protocol_hash") != protocol_hash:
            raise ValueError("Protocol changed; use a new study_id")
        if old.get("task_ids", settings.task_ids) != selected:
            raise ValueError("Schedule already exists for a different task selection")
        return old

    rows = []
    for task in selected:
        for seed in settings.seeds:
            branches = list(settings.config["branches"])
            random.Random(seed).shuffle(branches)
            for branch in branches:
                if f"{branch}/{seed}" not in prepared[task]["manifests"]:
                    raise ValueError(f"Branch not prepared: {branch}; add relevance scores before planning")
                specification: RunSpecification = {
                    "schema_version": 1, "task_id": task, "branch": branch, "seed": seed,
                    "attempt_id": "attempt-001", "run_id": uuid.uuid4().hex,
                    "protocol_hash": protocol_hash,
                }
                state: RunState = {**specification, "status": "pending", "group_valid": True}
                rows.append(state)
    result = {
        "schema_version": 1,
        "protocol_hash": protocol_hash,
        "model_metadata": model_metadata,
        "task_ids": selected,
        "runs": rows,
        "max_requests": len(rows) * settings.budget["max_model_requests"],
    }
    write_json(path, result)
    return result


def schedule(settings):
    value = read_json(settings.output_root / "schedule.json")
    selected=value.get("task_ids",settings.task_ids)
    prepared = {task: read_json(settings.output_root / task / "prepared.json") for task in selected}
    model_metadata = value.get("model_metadata")
    if value.get("protocol_hash") != protocol(settings, prepared, selected, model_metadata=model_metadata):
        raise ValueError("Frozen protocol changed")

    if getattr(settings, "model_provider", "google") == "ollama" and model_metadata and model_metadata.get("digest"):
        from ..results.storage import fetch_ollama_metadata
        current_meta = fetch_ollama_metadata(settings.ollama_base_url, settings.model, timeout=1.5)
        if current_meta.get("digest") and current_meta["digest"] != model_metadata["digest"]:
            raise ValueError(f"Model digest changed from {model_metadata['digest']} to {current_meta['digest']}; use a new study_id")
    return value


def restart_group(value, run_id):
    matches = [run for run in value["runs"] if run["run_id"] == run_id]
    if not matches:
        raise ValueError(f"Unknown run_id: {run_id}")
    selected = matches[0]
    group = [run for run in value["runs"] if
             (run["task_id"], run["seed"], run["attempt_id"]) ==
             (selected["task_id"], selected["seed"], selected["attempt_id"])]
    if not any(run["status"] in ("interrupted", "running") for run in group):
        raise ValueError("Only interrupted groups may restart")
    attempt = "attempt-" + uuid.uuid4().hex[:10]
    new_rows = []
    for run in group:
        run["group_valid"] = False
        if run["status"] == "pending":
            run["status"] = "superseded"
        elif run["status"] == "running":
            run["status"] = "interrupted"
        new_rows.append({
            **run,
            "run_id": uuid.uuid4().hex,
            "attempt_id": attempt,
            "status": "pending",
            "group_valid": True,
        })
    value["runs"].extend(new_rows)


def run_next(settings, backend=None, task_id=None, run_id=None, restart=False):
    value = schedule(settings)
    schedule_path = settings.output_root / "schedule.json"
    if restart:
        if not run_id:
            raise ValueError("--restart-group requires --run-id")
        restart_group(value, run_id)
        write_json(schedule_path, value)
        return {"status": "restarted"}

    # A killed runner leaves a durable running record. Restart the whole paired
    # task/seed group explicitly instead of silently starting at another branch.
    for run in value["runs"]:
        if run["status"] == "running" and run.get("group_valid", True):
            raise ValueError(f"Previous process stopped; restart group with run_id {run['run_id']}")
    pending = [run for run in value["runs"]
               if run["status"] == "pending" and run["group_valid"]
               and (task_id is None or run["task_id"] == task_id)
               and (run_id is None or run["run_id"] == run_id)]
    if not pending:
        return {"status": "no_pending_runs"}
    provider = getattr(settings, "model_provider", "google")
    if provider == "google":
        if not settings.model or not settings.api_key or not settings.quota_ready:
            raise ValueError("MODEL, GEMINI_API_KEY and quota RPM/TPM/RPD are required")
        run = pending[0]
        ledger = QuotaLedger(
            settings.output_root.parent / "quota.jsonl",
            settings.config["quota"],
            settings.model,
            settings.config.get("quota_project", "default"),
        )
        if ledger.remaining() < settings.budget["max_model_requests"]:
            return {"status": "waiting_for_daily_quota"}
    else:
        if not settings.model:
            raise ValueError("MODEL is required for Ollama provider")
        run = pending[0]
        ledger = LocalLedger(
            settings.output_root.parent / "quota.jsonl",
            settings.model,
            settings.profile_id or "ollama",
        )
    if backend is None:
        raise ValueError("Docker backend is required to execute a repair run")

    task = load_task(settings.task_path(run["task_id"]))
    task_root = settings.output_root / run["task_id"]
    prepared = read_json(task_root / "prepared.json")
    variant = task_root / "prepared" / run["branch"] / str(run["seed"])
    manifest = read_json(variant / "manifest.json")
    out = run_dir(settings, run)
    if out.exists():
        raise FileExistsError(f"Run artifact path already exists: {out}")

    run["status"] = "running"
    write_json(schedule_path, value)
    try:
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "run.json", {
            **run,
            "model": settings.model,
            "manifest": manifest,
            "budget": settings.budget,
            "started": utc_now(),
        })
        (out / "system_prompt.md").write_text(settings.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        (out / "instruction.md").write_text(task.instruction, encoding="utf-8")

        with create_agent(backend, task, prepared, variant / "snapshot", manifest) as box:
            agent_result = asyncio.run(run_agent(
                settings=settings,
                instruction=task.instruction,
                box=box,
                output_dir=out,
                ledger=ledger,
                protocol_hash=run["protocol_hash"],
            ))
            export_app(box, out / "final")
        collect_diff(variant / "snapshot", out / "final", out, manifest["product_paths"])
        write_json(out / "agent_result.json", agent_result)

        if agent_result["stop_reason"] in (
            "provider_error", "quota_interrupted", "docker_error", "internal_error", "format_error",
            "context_overflow", "output_truncated", "truncation_unknown"
        ):
            run["status"] = "interrupted"
        else:
            visible = evaluate_supplied_tests(
                backend, task, prepared["image"], variant / "snapshot", manifest,
                out / "agent_patch.diff", out / "visible-tests", protocol_hash=run["protocol_hash"],
            )
            write_json(out / "visible-tests-result.json", visible)
            run["status"] = "agent_done"
            write_json(schedule_path, value)
            evaluation = evaluate_patch(
                backend, task, prepared["verifier_image"], out / "agent_patch.diff", out / "evaluation",
                protocol_hash=run["protocol_hash"],
            )
            run["status"] = "scored" if evaluation["status"] in ("pass", "fail_test", "apply_failure") else "evaluation_error"
    except Exception as exc:
        write_json(out / "error.json", {"type": type(exc).__name__, "detail": settings.redact(str(exc))})
        has_patch = (out / "agent_result.json").is_file() and (out / "agent_patch.diff").is_file()
        run["status"] = "evaluation_error" if has_patch else "interrupted"

    if run["status"] == "interrupted":
        for sibling in value["runs"]:
            if (sibling["task_id"], sibling["seed"], sibling["attempt_id"]) == (
                run["task_id"], run["seed"], run["attempt_id"],
            ):
                sibling["group_valid"] = False
    write_json(schedule_path, value)
    return run


def evaluate_pending(settings, backend, run_id=None, task_id=None):
    value = schedule(settings)
    statuses = ("agent_done", "evaluation_error", "scored") if run_id else ("agent_done", "evaluation_error")
    rows = [run for run in value["runs"]
            if run["status"] in statuses
            and (run_id is None or run["run_id"] == run_id)
            and (task_id is None or run["task_id"] == task_id)]
    if run_id and not rows:
        raise ValueError("Run has no evaluable patch")
    for run in rows:
        out = run_dir(settings, run)
        task = load_task(settings.task_path(run["task_id"]))
        prepared = read_json(settings.output_root / run["task_id"] / "prepared.json")
        variant = settings.output_root / run["task_id"] / "prepared" / run["branch"] / str(run["seed"])
        manifest = read_json(variant / "manifest.json")
        visible = evaluate_supplied_tests(
            backend, task, prepared["image"], variant / "snapshot", manifest,
            out / "agent_patch.diff", out / "visible-tests-recheck", protocol_hash=run["protocol_hash"],
        )
        write_json(out / "visible-tests-recheck-result.json", visible)
        index = 0
        while (out / f"evaluation-{index}").exists():
            index += 1
        result = evaluate_patch(
            backend, task, prepared["verifier_image"], out / "agent_patch.diff", out / f"evaluation-{index}",
            protocol_hash=run["protocol_hash"],
        )
        run["evaluation_dir"] = f"evaluation-{index}"
        run["status"] = "scored" if result["status"] in ("pass", "fail_test", "apply_failure") else "evaluation_error"
        write_json(settings.output_root / "schedule.json", value)
    return {"evaluated": len(rows)}


def run_all(settings, backend=None, task_id=None):
    completed = 0
    while True:
        res = run_next(settings, backend=backend, task_id=task_id)
        status = res.get("status")
        if status == "no_pending_runs":
            break
        if status == "waiting_for_daily_quota":
            return {"status": "waiting_for_daily_quota", "completed": completed, "detail": "Daily quota exhausted"}
        if status in ("interrupted", "evaluation_error"):
            return {
                "status": status,
                "completed": completed,
                "failed_run_id": res.get("run_id"),
                "task_id": res.get("task_id"),
                "branch": res.get("branch"),
                "seed": res.get("seed"),
                "attempt_id": res.get("attempt_id"),
            }
        completed += 1
        print(f"[Progress] Completed run {completed}: task={res.get('task_id')} branch={res.get('branch')} seed={res.get('seed')} attempt={res.get('attempt_id')} status={status}")

    sched = schedule(settings)
    from collections import defaultdict
    grouped = defaultdict(list)
    for r in sched["runs"]:
        grouped[(r["task_id"], r["seed"], r["attempt_id"])].append(r)

    expected_branches = set(settings.config["branches"])
    task_seed_pairs = set((r["task_id"], r["seed"]) for r in sched["runs"] if task_id is None or r["task_id"] == task_id)
    incomplete_groups = []
    for pair in sorted(task_seed_pairs):
        has_completed_attempt = any(
            set(r["branch"] for r in runs) == expected_branches and all(r["status"] == "scored" for r in runs)
            for (t, s, a), runs in grouped.items() if (t, s) == pair
        )
        if not has_completed_attempt:
            incomplete_groups.append(f"{pair[0]}:seed_{pair[1]}")

    if incomplete_groups:
        return {
            "status": "incomplete",
            "completed": completed,
            "incomplete_groups": incomplete_groups,
            "detail": f"Incomplete task/seed groups: {incomplete_groups}. Run restart-group or evaluate to resolve.",
        }

    return {"status": "completed", "completed": completed}

