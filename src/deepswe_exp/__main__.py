from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from .components.configuration.settings import load_settings
from .components.results.storage import study_lock
from .components.tasks.tasks import load_task


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deepswe-exp")
    parser.add_argument("--config", default=None, help="Study YAML (defaults to this repository's config/study.yaml)")
    parser.add_argument("--profile", default=None, help="Server profile (e.g. saturn, neptune)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "check-model", "prepare", "validate", "plan-runs", "run-next", "run-all", "evaluate", "summarize"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if name in ("prepare", "validate", "plan-runs", "run-next", "run-all", "evaluate"):
            sub.add_argument("--task", help="Limit this command to one configured task")
        if name in ("run-next", "evaluate"):
            sub.add_argument("--run-id", help="Select an already planned run")
        if name == "run-next":
            sub.add_argument("--restart-group", action="store_true", help="Create a fresh attempt for an interrupted task/seed group")
    return parser


def _print_check(label: str, ok: bool, detail: str) -> bool:
    print(f"{'OK' if ok else 'MISSING'}  {label}: {detail}")
    return ok


def _doctor(settings) -> int:
    checks = []
    py = platform.python_version()
    checks.append(_print_check("Python", sys.version_info[:2] == (3, 12), f"{py} (required 3.12.x)"))
    if getattr(settings, "profile_id", None):
        checks.append(_print_check("Profile", True, settings.profile_id))
    provider = getattr(settings, "model_provider", "google")
    checks.append(_print_check("Provider", provider in ("google", "ollama"), provider))
    checks.append(_print_check("MODEL", bool(settings.model), settings.model or "not set in configuration"))

    if provider == "google":
        checks.append(_print_check("Gemini API key", bool(settings.api_key), "configured" if settings.api_key else "not set in .env/environment"))
        checks.append(_print_check("Quota", settings.quota_ready,
                                   "project and RPM/TPM/RPD configured" if settings.quota_ready
                                   else "fill quota_project and quota.rpm/tpm/rpd from AI Studio"))
    elif provider == "ollama":
        checks.append(_print_check("Ollama Base URL", bool(settings.ollama_base_url), settings.ollama_base_url))
        checks.append(_print_check("Ollama Context", bool(settings.ollama_num_ctx), str(settings.ollama_num_ctx)))
        from .components.results.storage import fetch_ollama_metadata
        meta = fetch_ollama_metadata(settings.ollama_base_url, settings.model, timeout=1.5)
        ollama_ok = bool(meta.get("version"))
        detail = f"version {meta['version']}" if ollama_ok else "server not responding"
        checks.append(_print_check("Ollama Server Endpoint", ollama_ok, detail))

    checks.append(_print_check("System prompt", settings.prompt_path.is_file() and bool(settings.prompt_path.read_text(encoding="utf-8").strip()), str(settings.prompt_path)))
    for task_id in settings.task_ids:
        try:
            task = load_task(settings.task_path(task_id))
            valid = len(task.base_commit) == 40 and bool(task.image)
            detail = f"{task.task_id}; commit and image found" if valid else "invalid commit or image"
        except Exception as exc:
            valid, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append(_print_check(f"Task {task_id}", valid, detail))
    from .components.execution.docker_env import docker_status
    docker = docker_status()
    checks.append(_print_check("Docker Linux engine", docker == "available", docker))
    print("No model requests were made.")
    return 0 if all(checks) else 2


def _select_tasks(settings, task_id: str | None) -> list[str]:
    if task_id is None:
        return list(settings.task_ids)
    if task_id not in settings.task_ids:
        raise ValueError(f"Task is not configured: {task_id}")
    return [task_id]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_arg = getattr(args, "config", None)
        profile_arg = getattr(args, "profile", None)
        settings = load_settings(config_path=config_arg, profile=profile_arg)
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "check-model":
            from .components.agent.connection import check_model_connection
            result = check_model_connection(settings)
            print(f"Model connection: {result['status']} ({result.get('elapsed_seconds', 0)} s)")
            print(f"Artifact: {result['artifact']}")
            if result["status"] != "success":
                print(f"{result.get('error_type', 'Error')}: {settings.redact(result.get('detail', ''))}")
                return 2
            print("This was one separate connection-check request, not a study run.")
            return 0

        with study_lock(settings.output_root):
            selected = _select_tasks(settings, getattr(args, "task", None))
            if args.command == "prepare":
                from .components.execution.preparation import prepare
                from .components.execution.docker_env import DockerBackend
                backend = DockerBackend()
                try:
                    for task_id in selected:
                        result = prepare(settings, task_id, backend)
                        print(f"Prepared {task_id}: {len(result['manifests'])} snapshots; {settings.output_root / task_id}")
                finally:
                    backend.client.close()
                return 0
            if args.command == "validate":
                from .components.execution.validation import validate_task
                from .components.execution.docker_env import DockerBackend
                backend = DockerBackend()
                try:
                    results = [validate_task(settings, task_id, backend) for task_id in selected]
                finally:
                    backend.client.close()
                for task_id, result in zip(selected, results):
                    print(f"Validation {task_id}: {'valid' if result['valid'] else 'FAILED'}")
                return 0 if all(item["valid"] for item in results) else 2
            if args.command == "plan-runs":
                from .components.execution.runner import plan_runs
                result = plan_runs(settings, selected)
                print(f"Scheduled {len(result['runs'])} runs; maximum model requests: {result['max_requests']}")
                print(f"Schedule: {settings.output_root / 'schedule.json'}")
                return 0
            if args.command == "run-next":
                from .components.execution.runner import run_next
                from .components.execution.docker_env import DockerBackend
                backend = None if args.restart_group else DockerBackend()
                try:
                    result = run_next(settings, backend, selected[0] if getattr(args, "task", None) else None,
                                      getattr(args, "run_id", None), args.restart_group)
                finally:
                    if backend is not None:
                        backend.client.close()
                print(result)
                return 0 if result.get("status") not in ("interrupted", "evaluation_error") else 2
            if args.command == "run-all":
                from .components.execution.runner import run_all
                from .components.execution.docker_env import DockerBackend
                backend = DockerBackend()
                try:
                    result = run_all(settings, backend, selected[0] if getattr(args, "task", None) else None)
                finally:
                    backend.client.close()
                print(f"Batch execution result: {result.get('status')} (completed: {result.get('completed', 0)})")
                if result.get("status") != "completed":
                    print(f"Failed run details: {result}", file=sys.stderr)
                    return 2
                return 0
            if args.command == "evaluate":
                from .components.execution.runner import evaluate_pending
                from .components.execution.docker_env import DockerBackend
                backend = DockerBackend()
                try:
                    result = evaluate_pending(settings, backend, getattr(args, "run_id", None), selected[0] if getattr(args, "task", None) else None)
                finally:
                    backend.client.close()
                print(f"Evaluated {result['evaluated']} saved patches")
                return 0
            if args.command == "summarize":
                from .components.results.report import summarize
                return summarize(settings)
    except Exception as exc:
        try:
            message = settings.redact(str(exc))
        except UnboundLocalError:
            message = str(exc)
        print(f"error: {type(exc).__name__}: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
