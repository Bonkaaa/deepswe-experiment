from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import threading
import uuid

from ..agent.agent import run_agent
from ..agent.quota import QuotaLedger, LocalLedger
from ..evaluation.patches import collect_diff
from ..evaluation.verifier import evaluate_patch, evaluate_supplied_tests
from ..results.storage import digest, protocol, read_json, run_dir, utc_now, write_json
from ..results.schemas import RunSpecification, RunState
from ..tasks.tasks import load_task, verify_benchmark_record, is_patcheval_task
from .preparation import create_agent, export_app, verify_prepared_variant


def plan_runs(settings, task_ids=None):
    selected = list(task_ids or settings.task_ids)
    if not settings.model:
        raise ValueError("Set MODEL before freezing the study protocol")
    if not selected or any(task not in settings.task_ids for task in selected):
        raise ValueError("Invalid task selection")
    prepared = {task: read_json(settings.output_root / task / "prepared.json") for task in selected}
    for task in selected:
        task_record=load_task(settings.task_path(task))
        verify_benchmark_record(task_record,prepared[task])
        validation = read_json(settings.output_root / task / "validation/validation.json")
        if not validation.get("valid"):
            raise ValueError(f"Task not validated: {task}")
        if task_record.benchmark and validation.get("prepared_hash")!=digest(prepared[task]):
            raise ValueError(f"Task validation does not match prepared inputs; rerun validate: {task}")
    model_metadata = None
    provider = getattr(settings, "model_provider", "google")
    if provider == "ollama":
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
    elif provider == "vllm":
        from ..results.storage import fetch_vllm_metadata
        model_metadata = fetch_vllm_metadata(settings.ollama_base_url, settings.model)
        write_json(settings.output_root / "protocol_snapshot.json", {
            "study_id": settings.config["study_id"],
            "profile_id": getattr(settings, "profile_id", None),
            "model": settings.model,
            "vllm_base_url": settings.ollama_base_url,
            "vllm_num_ctx": settings.ollama_num_ctx,
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


def _build_ledger(settings):
    provider = getattr(settings, "model_provider", "google")
    if provider == "google":
        if not settings.model or not settings.api_key or not settings.quota_ready:
            raise ValueError("MODEL, GEMINI_API_KEY and quota RPM/TPM/RPD are required")
        return QuotaLedger(
            settings.output_root.parent / "quota.jsonl",
            settings.config["quota"],
            settings.model,
            settings.config.get("quota_project", "default"),
        )
    if not settings.model:
        raise ValueError("MODEL is required for Ollama/vLLM provider")
    return LocalLedger(
        settings.output_root.parent / "quota.jsonl",
        settings.model,
        settings.profile_id or provider,
    )


def _execute_run(settings, backend, run, value, schedule_path, ledger, lock=None):
    def _update_status(new_status, invalidate_group=False):
        def _apply():
            run["status"] = new_status
            if invalidate_group:
                for sibling in value["runs"]:
                    if (sibling["task_id"], sibling["seed"], sibling["attempt_id"]) == (
                        run["task_id"], run["seed"], run["attempt_id"],
                    ):
                        sibling["group_valid"] = False
            write_json(schedule_path, value)
        if lock is not None:
            with lock:
                _apply()
        else:
            _apply()

    out = run_dir(settings, run)
    try:
        task = load_task(settings.task_path(run["task_id"]))
        task_root = settings.output_root / run["task_id"]
        prepared = read_json(task_root / "prepared.json")
        verify_benchmark_record(task, prepared)
        variant = task_root / "prepared" / run["branch"] / str(run["seed"])
        manifest = verify_prepared_variant(task_root, prepared, run["branch"], run["seed"])
        if out.exists():
            raise FileExistsError(f"Run artifact path already exists: {out}")

        _update_status("running")
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "run.json", {
            **run,
            "model": settings.model,
            "manifest": manifest,
            "budget": settings.budget,
            "started": utc_now(),
        })
        harness_name = getattr(settings, "harness", "mini-swe-agent")
        if harness_name == "mini-swe-agent":
            try:
                import os
                os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
                import yaml
                from minisweagent import package_dir
                mini_cfg = package_dir / "config" / "mini.yaml"
                if not mini_cfg.is_file():
                    mini_cfg = package_dir / "config" / "default.yaml"
                agent_cfg = yaml.safe_load(mini_cfg.read_text(encoding="utf-8")).get("agent", {})
                sys_tmpl = agent_cfg.get("system_template", "").strip()
                inst_tmpl = agent_cfg.get("instance_template", "").strip()
                prompt_content = f"{sys_tmpl}\n\n{inst_tmpl}".strip()
            except Exception:
                prompt_content = ""
            (out / "system_prompt.md").write_text(prompt_content or settings.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            (out / "system_prompt.md").write_text(settings.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        (out / "instruction.md").write_text(task.instruction, encoding="utf-8")

        workdir = prepared.get("workdir", task.workdir) if is_patcheval_task(task) else task.workdir
        with create_agent(backend, task, prepared, variant / "snapshot", manifest) as box:
            if harness_name == "mini-swe-agent":
                from ..agent.mini_swe_runner import run_mini_swe_agent
                agent_result = run_mini_swe_agent(
                    settings=settings,
                    instruction=task.instruction,
                    box=box,
                    output_dir=out,
                    ledger=ledger,
                    protocol_hash=run["protocol_hash"],
                    cwd=workdir,
                )
            else:
                agent_result = asyncio.run(run_agent(
                    settings=settings,
                    instruction=task.instruction,
                    box=box,
                    output_dir=out,
                    ledger=ledger,
                    protocol_hash=run["protocol_hash"],
                    cwd=workdir,
                ))
            write_json(out / "agent_result.json", agent_result)
            export_app(box, out / "final", skip_unsafe_symlinks=is_patcheval_task(task))
        collect_diff(
            variant / "snapshot",
            out / "final",
            out,
            manifest["product_paths"],
            language=task.language if is_patcheval_task(task) else None,
        )

        if agent_result["stop_reason"] in (
            "provider_error", "quota_interrupted", "docker_error", "internal_error"
        ):
            _update_status("interrupted", invalidate_group=True)
        else:
            visible = evaluate_supplied_tests(
                backend, task, prepared["image"], variant / "snapshot", manifest,
                out / "agent_patch.diff", out / "visible-tests", protocol_hash=run["protocol_hash"],
            )
            write_json(out / "visible-tests-result.json", visible)
            _update_status("agent_done")
            evaluation = evaluate_patch(
                backend, task, prepared["verifier_image"], out / "agent_patch.diff", out / "evaluation",
                protocol_hash=run["protocol_hash"],
            )
            final_status = "scored" if evaluation["status"] in ("pass", "fail_test", "apply_failure") else "evaluation_error"
            _update_status(final_status)
    except Exception as exc:
        try:
            out.mkdir(parents=True, exist_ok=True)
            write_json(out / "error.json", {"type": type(exc).__name__, "detail": settings.redact(str(exc))})
        except OSError:
            pass
        has_patch = (out / "agent_result.json").is_file() and (out / "agent_patch.diff").is_file()
        err_status = "evaluation_error" if has_patch else "interrupted"
        _update_status(err_status, invalidate_group=(err_status == "interrupted"))
    return run


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
    ledger = _build_ledger(settings)
    if provider == "google" and ledger.remaining() < settings.budget["max_model_requests"]:
        return {"status": "waiting_for_daily_quota"}
    if backend is None:
        raise ValueError("Docker backend is required to execute a repair run")
    return _execute_run(settings, backend, pending[0], value, schedule_path, ledger)


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
        verify_benchmark_record(task,prepared)
        variant = settings.output_root / run["task_id"] / "prepared" / run["branch"] / str(run["seed"])
        manifest = verify_prepared_variant(
            settings.output_root / run["task_id"],prepared,run["branch"],run["seed"]
        )
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


def run_all(settings, backend=None, task_id=None, workers=None, continue_on_error=None):
    effective_workers = max(1, int(workers if workers is not None else getattr(settings, "workers", 1)))
    effective_continue = bool(continue_on_error if continue_on_error is not None else getattr(settings, "continue_on_error", False))
    completed = 0
    failed_runs = []

    if effective_workers == 1 and not effective_continue:
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
            print(f"[Progress] Completed run {completed}: task={res.get('task_id')} branch={res.get('branch')} seed={res.get('seed')} attempt={res.get('attempt_id')} status={status}", flush=True)
    else:
        value = schedule(settings)
        schedule_path = settings.output_root / "schedule.json"
        for run in value["runs"]:
            if run["status"] == "running" and run.get("group_valid", True):
                raise ValueError(f"Previous process stopped; restart group with run_id {run['run_id']}")
        pending = [
            run for run in value["runs"]
            if run["status"] == "pending" and run["group_valid"]
            and (task_id is None or run["task_id"] == task_id)
        ]
        if pending:
            if backend is None:
                raise ValueError("Docker backend is required to execute a repair run")
            ledger = _build_ledger(settings)
            if getattr(settings, "model_provider", "google") == "google" and ledger.remaining() < settings.budget["max_model_requests"]:
                return {"status": "waiting_for_daily_quota", "completed": completed, "detail": "Daily quota exhausted"}
            lock = threading.Lock()
            total = len(pending)
            print(f"[Batch] Starting {total} pending runs with workers={effective_workers} (continue_on_error={effective_continue})", flush=True)
            first_failure = None
            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = {
                    pool.submit(_execute_run, settings, backend, run, value, schedule_path, ledger, lock): run
                    for run in pending
                }
                for fut in as_completed(futures):
                    orig_run = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        if not effective_continue:
                            for pending_fut in futures:
                                pending_fut.cancel()
                            raise
                        res = {**orig_run, "status": "interrupted", "error": f"{type(exc).__name__}: {exc}"}
                    status = res.get("status")
                    if status in ("interrupted", "evaluation_error"):
                        failed_runs.append({
                            "run_id": res.get("run_id"),
                            "task_id": res.get("task_id"),
                            "branch": res.get("branch"),
                            "seed": res.get("seed"),
                            "attempt_id": res.get("attempt_id"),
                            "status": status,
                        })
                        print(f"[Progress] FAILED run: task={res.get('task_id')} branch={res.get('branch')} seed={res.get('seed')} status={status}", flush=True)
                        if not effective_continue and first_failure is None:
                            first_failure = res
                            for pending_fut in futures:
                                pending_fut.cancel()
                    else:
                        completed += 1
                        print(f"[Progress] Completed {completed}/{total}: task={res.get('task_id')} branch={res.get('branch')} seed={res.get('seed')} status={status}", flush=True)
            if first_failure is not None:
                return {
                    "status": first_failure.get("status"),
                    "completed": completed,
                    "failed_run_id": first_failure.get("run_id"),
                    "task_id": first_failure.get("task_id"),
                    "branch": first_failure.get("branch"),
                    "seed": first_failure.get("seed"),
                    "attempt_id": first_failure.get("attempt_id"),
                }

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
            "failed_runs": failed_runs,
            "incomplete_groups": incomplete_groups,
            "detail": f"Incomplete task/seed groups: {incomplete_groups}. Run restart-group or evaluate to resolve.",
        }

    return {"status": "completed", "completed": completed}

