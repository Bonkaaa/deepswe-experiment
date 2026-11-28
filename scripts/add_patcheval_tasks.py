#!/usr/bin/env python3
"""Materialize PatchEval-Verified (230 CVEs) tasks into data/<CVE-ID>/instance.json.

Usage:
  uv run python scripts/add_patcheval_tasks.py --all
  uv run python scripts/add_patcheval_tasks.py --cve CVE-2021-23376 CVE-2020-26215
  uv run python scripts/add_patcheval_tasks.py --lang Python --limit 4
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

PATCHEVAL_VERIFIED_URL = (
    "https://raw.githubusercontent.com/bytedance/PatchEval/main/"
    "patcheval/datasets/patcheval_verified.json"
)
DATASET_NAME = "PatchEval-Verified"


def ensure_dataset_file(path: Path) -> list[dict]:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            PATCHEVAL_VERIFIED_URL,
            headers={"User-Agent": "deepswe-experiment/add-patcheval"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            path.write_bytes(resp.read())
    return json.loads(path.read_text(encoding="utf-8"))


def build_instruction(sample: dict) -> str:
    cve_id = str(sample.get("cve_id") or "").strip()
    desc = str(sample.get("cve_description") or "").strip()
    lang = str(sample.get("programming_language") or "").strip()
    cwe_info = sample.get("cwe_info") or {}
    cwe_lines = []
    for cwe_id, detail in sorted(cwe_info.items()):
        name = detail.get("name", "") if isinstance(detail, dict) else ""
        cwe_lines.append(f"- {cwe_id}: {name}" if name else f"- {cwe_id}")
    cwe_block = ("\nCWE Classification:\n" + "\n".join(cwe_lines) + "\n") if cwe_lines else ""
    return (
        f"Please fix the security vulnerability ({cve_id}, {lang}) in this code repository "
        f"based on the following vulnerability description:\n\n"
        f"{desc}\n"
        f"{cwe_block}\n"
        "Requirements:\n"
        "- Investigate the repository in the current working directory and implement a minimal, "
        "correct security fix.\n"
        "- Ensure existing functionality and tests continue to pass.\n"
        "- Do not attempt to access external networks or search online for this CVE.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize PatchEval-Verified CVE tasks into data/.")
    parser.add_argument("--dataset", default="data/patcheval/patcheval_verified.json", help="Path to patcheval_verified.json")
    parser.add_argument("--data-root", default="data", help="Destination data root (default: data)")
    parser.add_argument("--cve", nargs="+", default=None, help="Specific CVE IDs to materialize")
    parser.add_argument("--lang", nargs="+", choices=("Python", "Go", "JavaScript"), default=None, help="Filter by language")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of tasks to materialize")
    parser.add_argument("--all", action="store_true", help="Materialize all 230 CVEs")
    parser.add_argument("--cpus", type=int, default=2, help="Container CPU limit (default: 2)")
    parser.add_argument("--memory-mb", type=int, default=8192, help="Container RAM limit in MB (default: 8192)")
    args = parser.parse_args()

    if not args.all and not args.cve and not args.lang and args.limit is None:
        args.all = True

    repo_root = Path(__file__).resolve().parents[1]
    dataset_path = (repo_root / args.dataset).resolve()
    data_root = (repo_root / args.data_root).resolve()
    rows = ensure_dataset_file(dataset_path)

    selected = rows
    if args.cve:
        wanted = set(args.cve)
        selected = [r for r in selected if r["cve_id"] in wanted]
        missing = sorted(wanted - {r["cve_id"] for r in selected})
        if missing:
            raise SystemExit(f"Unknown CVE IDs: {', '.join(missing)}")
    if args.lang:
        langs = set(args.lang)
        selected = [r for r in selected if r.get("programming_language") in langs]
    if args.limit is not None:
        selected = selected[: args.limit]

    written_ids: list[str] = []
    for row in selected:
        cve_id = row["cve_id"]
        lang = row["programming_language"]
        cwe_ids = sorted((row.get("cwe_info") or {}).keys())
        primary_cwe = cwe_ids[0] if cwe_ids else "CWE-unknown"
        instance = {
            "instance_id": cve_id,
            "cve_id": cve_id,
            "cve_description": row.get("cve_description", ""),
            "cwe_info": row.get("cwe_info", {}),
            "cwe_ids": cwe_ids,
            "category": f"{lang}:{primary_cwe}",
            "repo": row.get("repo", ""),
            "programming_language": lang,
            "image": row["image_url"],
            "image_url": row["image_url"],
            "problem_statement": build_instruction(row),
            "environment": {"cpus": args.cpus, "memory_mb": args.memory_mb},
        }
        payload = {
            "benchmark": {
                "type": "patcheval",
                "name": DATASET_NAME,
                "language": lang,
                "image_ref": row["image_url"],
            },
            "instance": instance,
        }
        dest = data_root / cve_id / "instance.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written_ids.append(cve_id)

    manifest_path = data_root / "patcheval" / "all_tasks.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if args.all:
        manifest_path.write_text(json.dumps(written_ids, indent=2) + "\n", encoding="utf-8")
    print(f"Materialized {len(written_ids)} PatchEval tasks under {data_root}")
    if args.all:
        print(f"Wrote task list manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
