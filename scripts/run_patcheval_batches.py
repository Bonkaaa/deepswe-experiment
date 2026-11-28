#!/usr/bin/env python3
"""Run PatchEval-Verified (230 CVEs) in sequential batches with per-batch cleanup and resume.

Usage:
  uv run --frozen python scripts/run_patcheval_batches.py --profile saturn
  uv run --frozen python scripts/run_patcheval_batches.py --profile saturn --master-id patcheval-230_20261128_130000
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from deepswe_exp.components.configuration.settings import load_settings
from deepswe_exp.components.execution.docker_env import DockerBackend
from deepswe_exp.components.execution.preparation import prepare
from deepswe_exp.components.execution.runner import plan_runs, run_all
from deepswe_exp.components.execution.validation import validate_task
from deepswe_exp.components.results.report import FIELDS, _paired_groups, summarize
from deepswe_exp.components.results.storage import append_event, utc_now
from deepswe_exp.components.tasks.tasks import load_task


def chunk_tasks(task_ids: list[str], batch_size: int) -> list[list[str]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return [task_ids[i : i + batch_size] for i in range(0, len(task_ids), batch_size)]


def cleanup_batch_artifacts(batch_root: Path, image_refs: list[str], remove_images: bool = True) -> None:
    """Delete heavy snapshot directories and only this batch's PatchEval Docker images."""
    for task_dir in batch_root.iterdir():
        if not task_dir.is_dir() or task_dir.name == "summary":
            continue
        shutil.rmtree(task_dir / "base", ignore_errors=True)
        for snap in task_dir.glob("prepared/*/*/snapshot"):
            shutil.rmtree(snap, ignore_errors=True)
        for final_dir in task_dir.glob("*/*/*/final"):
            shutil.rmtree(final_dir, ignore_errors=True)

    if remove_images and image_refs:
        unique_images = sorted(set(img for img in image_refs if img))
        for img in unique_images:
            subprocess.run(["docker", "rmi", "-f", img], check=False, capture_output=True)


def merge_batch_summaries(master_root: Path, branches: list[str]) -> tuple[Path, Path]:
    summary_dir = master_root / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    for batch_dir in sorted(master_root.glob("batch_*")):
        csv_path = batch_dir / "summary" / "runs.csv"
        if not csv_path.is_file():
            continue
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            all_rows.extend(csv.DictReader(f))

    all_csv = summary_dir / "all_runs.csv"
    with all_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    # Convert reward strings to int for paired-group transition counting
    norm_rows = []
    for r in all_rows:
        nr = dict(r)
        nr["group_valid"] = str(r.get("group_valid", "")).lower() == "true"
        nr["reward"] = int(r["reward"]) if str(r.get("reward", "")).strip() in ("0", "1") else None
        norm_rows.append(nr)

    complete, incomplete, transitions = _paired_groups(norm_rows, branches)
    status_counts = Counter(r.get("evaluation_status", "") for r in norm_rows)
    by_branch: dict[str, list[dict]] = defaultdict(list)
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for r in norm_rows:
        if r.get("reward") in (0, 1):
            by_branch[str(r.get("branch", ""))].append(r)
            by_lang[str(r.get("language") or "unknown")].append(r)

    skipped_count = 0
    skipped_path = master_root / "skipped_cves.jsonl"
    if skipped_path.is_file():
        skipped_count = sum(1 for line in skipped_path.read_text(encoding="utf-8").splitlines() if line.strip())

    lines = [
        f"# PatchEval-Verified 230 CVEs — Tổng hợp ({master_root.name})",
        "",
        f"- Tổng số lượt chạy (runs): **{len(norm_rows)}**",
        f"- Nhóm ghép cặp đủ {len(branches)} nhánh hợp lệ: **{complete}**",
        f"- Nhóm chưa đủ lượt / lỗi kỹ thuật: **{incomplete}**",
        f"- Số CVE bị bỏ qua ở `prepare`/`validate`: **{skipped_count}**",
        f"- Phân loại kết quả đánh giá: `{dict(sorted(status_counts.items()))}`",
        "",
        "## Pass rate theo nhánh",
        "",
        "| Nhánh | Lượt có điểm | Pass | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    for b in branches:
        items = by_branch.get(b, [])
        passed = sum(1 for r in items if r["reward"] == 1)
        rate = f"{(passed / len(items) * 100):.2f}%" if items else "—"
        lines.append(f"| {b} | {len(items)} | {passed} | {rate} |")

    lines.extend([
        "",
        "## Pass rate theo ngôn ngữ",
        "",
        "| Ngôn ngữ | Lượt có điểm | Pass | Pass rate |",
        "|---|---:|---:|---:|",
    ])
    for lang, items in sorted(by_lang.items()):
        passed = sum(1 for r in items if r["reward"] == 1)
        rate = f"{(passed / len(items) * 100):.2f}%" if items else "—"
        lines.append(f"| {lang} | {len(items)} | {passed} | {rate} |")

    lines.extend(["", "## Các cặp chuyển đổi kết quả (Paired Transitions)", ""])
    if transitions:
        lines.extend(f"- `{k}`: {v} cặp" for k, v in sorted(transitions.items()))
    else:
        lines.append("Chưa có cặp nhánh hợp lệ nào cho thấy chuyển đổi pass/fail.")
    lines.append("")

    all_md = summary_dir / "all_summary.md"
    all_md.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return all_csv, all_md


def _ensure_vllm_running(repo_root: Path, profile: str) -> None:
    subprocess.run(
        ["bash", str(repo_root / "scripts" / "start_vllm_neptune.sh"), "--profile", profile],
        check=True,
    )


def _run_batch(
    *,
    repo_root: Path,
    base_config: dict,
    profile: str,
    master_root: Path,
    batch_idx: int,
    batch_cves: list[str],
    workers: int,
    cleanup: bool,
) -> None:
    batch_name = f"batch_{batch_idx:02d}"
    batch_root = master_root / batch_name
    done_marker = batch_root / ".batch_done"
    if done_marker.is_file():
        print(f"[{batch_name}] Already completed (.batch_done found); skipping.", flush=True)
        return

    print(f"\n==================== {batch_name} ({len(batch_cves)} CVEs) ====================", flush=True)
    _ensure_vllm_running(repo_root, profile)

    batch_root.mkdir(parents=True, exist_ok=True)
    batch_cfg_path = batch_root / "batch_config.yaml"
    skipped_path = master_root / "skipped_cves.jsonl"

    def _write_batch_config(task_list: list[str]) -> Path:
        cfg = {
            **base_config,
            "study_id": batch_name,
            "output_root": str(master_root),
            "tasks": task_list,
            "workers": workers,
            "continue_on_error": True,
        }
        cfg.pop("tasks_file", None)
        batch_cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return batch_cfg_path

    _write_batch_config(batch_cves)
    settings = load_settings(config_path=batch_cfg_path, profile=profile)
    image_refs = []
    for cve in batch_cves:
        try:
            image_refs.append(load_task(settings.task_path(cve)).image)
        except Exception:
            pass

    backend = DockerBackend()
    try:
        # Step 1: Prepare each CVE (skip failures instead of aborting batch)
        prepared_cves: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(prepare, settings, cve, backend): cve for cve in batch_cves}
            for fut in as_completed(futs):
                cve = futs[fut]
                try:
                    res = fut.result()
                    prepared_cves.append(cve)
                    print(f"[{batch_name}] Prepared {cve} ({res.get('language')}, tests={res.get('total_unit_tests')})", flush=True)
                except Exception as exc:
                    print(f"[{batch_name}] SKIP prepare {cve}: {type(exc).__name__}: {exc}", flush=True)
                    append_event(skipped_path, {"batch": batch_name, "cve": cve, "stage": "prepare", "error": f"{type(exc).__name__}: {exc}", "at": utc_now()})

        # Step 2: Validate each prepared CVE (skip failures instead of aborting batch)
        valid_cves: list[str] = []
        if prepared_cves:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(validate_task, settings, cve, backend): cve for cve in prepared_cves}
                for fut in as_completed(futs):
                    cve = futs[fut]
                    try:
                        val = fut.result()
                        if val.get("valid"):
                            valid_cves.append(cve)
                            print(f"[{batch_name}] Validated {cve}: OK", flush=True)
                        else:
                            print(f"[{batch_name}] SKIP validate {cve}: invalid baseline", flush=True)
                            append_event(skipped_path, {"batch": batch_name, "cve": cve, "stage": "validate", "error": "validation returned valid=False", "at": utc_now()})
                    except Exception as exc:
                        print(f"[{batch_name}] SKIP validate {cve}: {type(exc).__name__}: {exc}", flush=True)
                        append_event(skipped_path, {"batch": batch_name, "cve": cve, "stage": "validate", "error": f"{type(exc).__name__}: {exc}", "at": utc_now()})

        # Preserve original order
        valid_set = set(valid_cves)
        ordered_valid = [cve for cve in batch_cves if cve in valid_set]

        if ordered_valid:
            _write_batch_config(ordered_valid)
            valid_settings = load_settings(config_path=batch_cfg_path, profile=profile)
            sched = plan_runs(valid_settings, ordered_valid)
            print(f"[{batch_name}] Planned {len(sched['runs'])} runs for {len(ordered_valid)}/{len(batch_cves)} valid CVEs", flush=True)
            batch_res = run_all(valid_settings, backend=backend, workers=workers, continue_on_error=True)
            print(f"[{batch_name}] run-all result: {batch_res.get('status')} (completed={batch_res.get('completed')})", flush=True)
            summarize(valid_settings)
        else:
            print(f"[{batch_name}] No valid CVEs in this batch; skipping run-all.", flush=True)
    finally:
        backend.client.close()

    if cleanup:
        print(f"[{batch_name}] Cleaning up temporary snapshots and {len(image_refs)} PatchEval Docker images...", flush=True)
        cleanup_batch_artifacts(batch_root, image_refs, remove_images=True)

    done_marker.write_text(utc_now() + "\n", encoding="utf-8")
    print(f"[{batch_name}] DONE.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PatchEval-Verified 230 CVEs in automated batches.")
    parser.add_argument("--profile", default="saturn", help="Server profile (default: saturn)")
    parser.add_argument("--config", default="config/study.saturn.patcheval230.yaml", help="Base study YAML")
    parser.add_argument("--batch-size", type=int, default=23, help="CVEs per batch (default: 23 -> 10 batches)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--master-id", default=None, help="Master run directory name (reuse to resume)")
    parser.add_argument("--start-batch", type=int, default=1, help="1-based start batch index")
    parser.add_argument("--end-batch", type=int, default=None, help="1-based end batch index (inclusive)")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep snapshots and Docker images after each batch")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    base_settings = load_settings(config_path=repo_root / args.config, profile=args.profile)
    all_cves = list(base_settings.task_ids)
    batches = chunk_tasks(all_cves, args.batch_size)
    total_batches = len(batches)
    end_batch = args.end_batch or total_batches

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    master_id = args.master_id or f"{base_settings.study_id}_{timestamp}"
    master_root = (repo_root / base_settings.config["output_root"] / master_id).resolve()
    master_root.mkdir(parents=True, exist_ok=True)

    print(f"Master ID:     {master_id}", flush=True)
    print(f"Master Root:   {master_root}", flush=True)
    print(f"Total CVEs:    {len(all_cves)} -> {total_batches} batches x {args.batch_size} CVEs", flush=True)
    print(f"Workers:       {args.workers}", flush=True)
    print(f"Running range: batch_{args.start_batch:02d} .. batch_{end_batch:02d}", flush=True)

    started = time.monotonic()
    for idx in range(args.start_batch, min(end_batch, total_batches) + 1):
        _run_batch(
            repo_root=repo_root,
            base_config=base_settings.config,
            profile=args.profile,
            master_root=master_root,
            batch_idx=idx,
            batch_cves=batches[idx - 1],
            workers=args.workers,
            cleanup=not args.no_cleanup,
        )
        # Keep merged summary updated after every batch so progress is always visible
        merge_batch_summaries(master_root, list(base_settings.config["branches"]))

    all_csv, all_md = merge_batch_summaries(master_root, list(base_settings.config["branches"]))
    archive_path = master_root.parent / f"{master_id}.tar.gz"
    print(f"\nPackaging final archive: {archive_path}", flush=True)
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(master_root, arcname=master_id)

    elapsed_h = (time.monotonic() - started) / 3600
    print(f"\nALL DONE in {elapsed_h:.2f} hours!", flush=True)
    print(f"Merged CSV:     {all_csv}", flush=True)
    print(f"Merged Summary: {all_md}", flush=True)
    print(f"Archive:        {archive_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
