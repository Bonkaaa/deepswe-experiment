#!/usr/bin/env python3
"""Fetch one or more Go tasks from SWE-bench Multilingual into data/<instance_id>/instance.json
and print the ready-to-paste YAML task_options block.

Usage:
  uv run python scripts/add_swebench_task.py prometheus__prometheus-15142 gohugoio__hugo-12448
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import urllib.request
from pathlib import Path

DATASET_NAME = "SWE-bench Multilingual"
DATASET_REPO = "SWE-bench/SWE-bench_Multilingual"
DATASET_REVISION = "846e647b9f33c0b51b739d005d13d85493c9af09"
HARNESS_REVISION = "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e"


def fetch_all_rows() -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for offset in (0, 100, 200):
        url = (
            f"https://datasets-server.huggingface.co/rows"
            f"?dataset={DATASET_REPO}&config=default&split=test&offset={offset}&length=100"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "deepswe-experiment/add-task"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        for item in payload.get("rows", []):
            row = item["row"]
            by_id[row["instance_id"]] = row
    return by_id


def infer_go_test_scope(eval_script: str) -> tuple[str, str, list[str]]:
    """Extract (test_package, test_pattern, warnings) from a Go eval_script."""
    warnings: list[str] = []
    go_test_lines = [
        line.strip()
        for line in eval_script.splitlines()
        if "go test" in line and not line.strip().startswith("#")
    ]
    if not go_test_lines:
        warnings.append("No 'go test' line found in eval_script; defaulting to '.' and '^Test'.")
        return ".", "^Test", warnings
    if len(go_test_lines) > 1:
        warnings.append(f"Found {len(go_test_lines)} 'go test' lines; using the first one: {go_test_lines[0]}")

    line = go_test_lines[0].split("|", 1)[0].strip().strip("()")
    tokens = shlex.split(line)
    pkg = "."
    pattern = "^Test"
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if tok in ("-run", "--run") and i + 1 < len(tokens):
            pattern = tokens[i + 1]
            skip_next = True
        elif tok.startswith("-run="):
            pattern = tok.split("=", 1)[1]
        elif tok in ("-timeout", "-count", "-parallel", "-tags", "-mod", "-coverprofile") and i + 1 < len(tokens):
            skip_next = True
        elif tok == "." or tok.startswith("./"):
            pkg = tok
    return pkg, pattern, warnings


def infer_product_paths(patch_text: str) -> list[str]:
    dirs: set[str] = set()
    root_files: set[str] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        rel = line.split(" b/", 1)[-1].strip()
        if rel.endswith("_test.go"):
            continue
        parent = Path(rel).parent.as_posix()
        if parent == ".":
            root_files.add(rel)
        else:
            dirs.add(parent)
    return sorted(dirs) or sorted(root_files) or ["."]


def main() -> int:
    parser = argparse.ArgumentParser(description="Add SWE-bench Multilingual Go task(s) to data/.")
    parser.add_argument("instance_ids", nargs="+", help="One or more SWE-bench Multilingual instance IDs")
    parser.add_argument("--data-root", default="data", help="Destination data root (default: data)")
    parser.add_argument("--cpus", type=int, default=4, help="Container CPU limit (default: 4)")
    parser.add_argument("--memory-mb", type=int, default=16384, help="Container RAM limit in MB (default: 16384)")
    parser.add_argument("--role", default="experiment", choices=("experiment", "smoke_test"))
    args = parser.parse_args()

    rows = fetch_all_rows()
    repo_root = Path(__file__).resolve().parents[1]
    data_root = (repo_root / args.data_root).resolve()

    yaml_tasks: list[str] = []
    yaml_options: list[str] = []

    for iid in args.instance_ids:
        row = rows.get(iid)
        if row is None:
            print(f"ERROR: instance_id '{iid}' not found in {DATASET_REPO}.", file=sys.stderr)
            return 1
        if row.get("log_parser") != "parse_log_gotest":
            print(
                f"ERROR: '{iid}' uses {row.get('log_parser')} (not Go 'parse_log_gotest'). "
                "Only Go tasks are supported by the current AST test filter.",
                file=sys.stderr,
            )
            return 1

        pkg, pattern, warnings = infer_go_test_scope(row.get("eval_script", ""))
        product_paths = infer_product_paths(row.get("patch", ""))
        for w in warnings:
            print(f"[WARNING] {iid}: {w}", file=sys.stderr)

        instance = dict(row)
        instance["environment"] = {"cpus": args.cpus, "memory_mb": args.memory_mb}
        payload = {
            "benchmark": {
                "name": DATASET_NAME,
                "dataset_revision": DATASET_REVISION,
                "harness_revision": HARNESS_REVISION,
                "image_ref": row["image"],
                "task_options": {
                    "test_package": pkg,
                    "test_pattern": pattern,
                    "product_paths": product_paths,
                },
            },
            "instance": instance,
        }
        dest = data_root / iid / "instance.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(
            f"Wrote {dest} (P2P={len(row.get('PASS_TO_PASS', []))}, F2P={len(row.get('FAIL_TO_PASS', []))}, "
            f"pkg={pkg}, pattern={pattern})"
        )

        yaml_tasks.append(f'  - "{iid}"')
        paths_block = "\n".join(f"      - {p}" for p in product_paths)
        yaml_options.append(
            f"  {iid}:\n"
            f"    role: {args.role}\n"
            f'    test_package: "{pkg}"\n'
            f'    test_pattern: "{pattern}"\n'
            f"    product_paths:\n"
            f"{paths_block}"
        )

    print("\n--- Paste into config/study.*.yaml ---")
    print("tasks:")
    print("\n".join(yaml_tasks))
    print("\ntask_options:")
    print("\n".join(yaml_options))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
