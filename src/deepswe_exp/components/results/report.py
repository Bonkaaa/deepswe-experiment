from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from .storage import read_json

FIELDS = [
    "task_id", "task_category", "language", "has_unit_tests", "experiment_role", "branch", "seed", "attempt_id", "run_id", "group_valid", "run_status",
    "agent_stop_reason", "model_requests", "elapsed_seconds", "input_tokens", "output_tokens",
    "visible_test_cases", "visible_test_ratio", "evaluation_status", "patcheval_validation_type", "reward",
    "f2p_passed", "f2p_total", "p2p_passed", "p2p_total", "agent_seen_total", "agent_seen_passed",
    "agent_seen_failed", "technical_detail",
    "seen_tests_status", "seen_tests_passed", "seen_tests_failed", "seen_tests_skipped", "seen_pass_but_eval_fail",
]


def _read_optional(path: Path) -> dict:
    try:
        return read_json(path)
    except (OSError, ValueError, TypeError):
        return {}


def _evaluation(run_dir: Path, run: dict) -> dict:
    preferred = run.get("evaluation_dir")
    paths = [run_dir / preferred / "result.json"] if preferred else []
    paths.extend([run_dir / "evaluation" / "result.json", run_dir / "evaluation-0" / "result.json"])
    for path in paths:
        value = _read_optional(path)
        if value:
            value["_ctrf"] = _read_optional(path.parent / "verifier" / "ctrf.json")
            benchmark=value.get("benchmark",{})
            if isinstance(benchmark.get("f2p_success"),list) and isinstance(benchmark.get("p2p_success"),list):
                value["_swebench"] = True
            return value
    return {}


def _normalize_node_id(node_id: str) -> str:
    parts = node_id.replace("\\", "/").split("::")
    module = parts[0]
    if module.endswith(".py"):
        module = module[:-3]
    return ".".join([module.replace("/", "."), *parts[1:]])


def _test_visibility(evaluation: dict, manifest: dict) -> tuple[int | None, int | None, int | None]:
    if evaluation.get("_swebench"):
        benchmark=evaluation.get('benchmark',{})
        visible={group.rsplit('::',1)[-1] for group in manifest.get('kept_groups',[])}
        passing=set(benchmark.get('f2p_success',[])+benchmark.get('p2p_success',[]))
        failing=set(benchmark.get('f2p_failure',[])+benchmark.get('p2p_failure',[]))
        seen=visible & (passing|failing)
        return len(seen),len(seen&passing),len(seen&failing)
    report = evaluation.get("_ctrf") or {}
    rows = report.get("results", {}).get("tests", [])
    visible = {_normalize_node_id(node_id) for node_id in manifest.get("kept_node_ids", [])}
    passed = failed = 0
    seen = set()
    for row in rows:
        name = str(row.get("name", ""))
        if name.startswith("[f2p] ") or name.startswith("[p2p] "):
            node_id = _normalize_node_id(name.split("] ", 1)[1])
            if node_id in visible and node_id not in seen:
                seen.add(node_id)
                if row.get("status") == "passed":
                    passed += 1
                else:
                    failed += 1
    if not rows:
        return None, None, None
    return len(seen), passed, failed


def _record(settings, run: dict) -> dict:
    folder = settings.output_root / run["task_id"] / run["branch"] / str(run["seed"]) / run["attempt_id"]
    data = _read_optional(folder / "run.json")
    prepared = _read_optional(settings.output_root / run["task_id"] / "prepared.json")
    agent = _read_optional(folder / "agent_result.json")
    evaluation = _evaluation(folder, run)
    visible = _read_optional(folder / "visible-tests-result.json")
    manifest = data.get("manifest", {})
    benchmark = evaluation.get("benchmark") or {}
    seen_total, seen_pass, seen_fail = _test_visibility(evaluation, manifest)
    usage = agent.get("usage") or []
    input_tokens = output_tokens = 0
    for item in usage:
        if not isinstance(item, dict):
            continue
        input_tokens += int(item.get("input_tokens", 0) or 0)
        output_tokens += int(item.get("output_tokens", 0) or 0)
    error = _read_optional(folder / "error.json")
    detail = evaluation.get("detail") or error.get("detail") or agent.get("error_detail") or ""
    return {
        "task_id": run.get("task_id"), "task_category": prepared.get("category", ""),
        "language": prepared.get("language", benchmark.get("language", "")),
        "has_unit_tests": prepared.get("has_unit_tests", ""),
        "experiment_role": prepared.get("role", "experiment"),
        "branch": run.get("branch"), "seed": run.get("seed"),
        "attempt_id": run.get("attempt_id"), "run_id": run.get("run_id"),
        "group_valid": run.get("group_valid", False), "run_status": run.get("status"),
        "agent_stop_reason": agent.get("stop_reason", ""),
        "model_requests": agent.get("model_requests", 0),
        "elapsed_seconds": agent.get("elapsed_seconds", ""),
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "visible_test_cases": manifest.get("kept_cases", ""),
        "visible_test_ratio": manifest.get("actual_ratio", ""),
        "evaluation_status": evaluation.get("status", "not_evaluated"),
        "patcheval_validation_type": benchmark.get("validation_type", ""),
        "reward": evaluation.get("reward", ""),
        "f2p_passed": benchmark.get("f2p_passed", ""), "f2p_total": benchmark.get("f2p_total", ""),
        "p2p_passed": benchmark.get("p2p_passed", ""), "p2p_total": benchmark.get("p2p_total", ""),
        "agent_seen_total": seen_total if seen_total is not None else "",
        "agent_seen_passed": seen_pass if seen_pass is not None else "",
        "agent_seen_failed": seen_fail if seen_fail is not None else "",
        "technical_detail": str(detail).replace("\n", " ")[:500],
        "seen_tests_status": visible.get("status", "not_run"),
        "seen_tests_passed": visible.get("passed", ""),
        "seen_tests_failed": visible.get("failed", ""),
        "seen_tests_skipped": visible.get("skipped", ""),
        "seen_pass_but_eval_fail": visible.get("status") == "pass" and evaluation.get("status") == "fail_test",
    }


def _paired_groups(rows: list[dict], branches: list[str]) -> tuple[int, int, Counter]:
    grouped = defaultdict(list)
    for row in rows:
        key = (row["task_id"], row["seed"], row["attempt_id"])
        grouped[key].append(row)
    complete = incomplete = 0
    transitions: Counter = Counter()
    for members in grouped.values():
        if all(row.get("experiment_role") == "smoke_test" for row in members):
            continue
        valid = {r["branch"]: r for r in members if r.get("group_valid")}
        if len(valid) == len(branches) and all(
            b in valid and valid[b]["run_status"] == "scored"
            and valid[b]["evaluation_status"] in ("pass", "fail_test")
            for b in branches
        ):
            complete += 1
            for left_index, left in enumerate(branches):
                for right in branches[left_index + 1:]:
                    a = valid[left]["reward"]
                    b = valid[right]["reward"]
                    if a in (0, 1) and b in (0, 1) and a != b:
                        transitions[f"{left}={a} → {right}={b}"] += 1
        else:
            incomplete += 1
    return complete, incomplete, transitions


def _category_summary(rows: list[dict]) -> list[tuple[str, int, float, float | None, float | None]]:
    groups = defaultdict(list)
    for row in rows:
        if row.get("experiment_role") == "smoke_test":
            continue
        if row.get("evaluation_status") in ("pass", "fail_test") and row.get("reward") in (0, 1):
            groups[row.get("task_category") or "uncategorized"].append(row)
    result = []
    for category, items in sorted(groups.items()):
        f2p = [r["f2p_passed"] / r["f2p_total"] for r in items if r.get("f2p_total")]
        p2p = [r["p2p_passed"] / r["p2p_total"] for r in items if r.get("p2p_total")]
        result.append((category, len(items), sum(r["reward"] for r in items) / len(items),
                       sum(f2p) / len(f2p) if f2p else None,
                       sum(p2p) / len(p2p) if p2p else None))
    return result


def summarize(settings) -> int:
    root = settings.output_root
    schedule = _read_optional(root / "schedule.json")
    runs = schedule.get("runs", [])
    rows = [_record(settings, run) for run in runs]
    summary_dir = root / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    csv_path = summary_dir / "runs.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["evaluation_status"] for row in rows)
    smoke_count = sum(row.get("experiment_role") == "smoke_test" for row in rows)
    branches = list(settings.config["branches"])
    complete, incomplete, transitions = _paired_groups(rows, branches)
    categories = _category_summary(rows)
    lines = [
        "# Thực nghiệm A — tổng hợp lượt chạy", "",
        f"Study: `{settings.config['study_id']}`", "",
        f"- Tổng số lượt đã lên lịch: **{len(rows)}**",
        f"- Nhóm ghép cặp đủ nhánh và có điểm hợp lệ: **{complete}**",
        f"- Nhóm chưa đủ lượt hoặc có lỗi kỹ thuật: **{incomplete}**",
        f"- Lượt smoke test (không đưa vào phân tích ghép cặp/theo loại task): **{smoke_count}**",
        f"- Phân loại kết quả đánh giá: `{dict(sorted(counts.items()))}`", "",
        "## Kết quả theo lượt", "",
        "| Task | Nhánh | Seed | Attempt | Agent dừng | Request | Test được cấp | Test được cấp pass | Eval | Reward | F2P | P2P |",
        "|---|---|---:|---|---|---:|---:|---|---|---:|---:|---:|",
    ]
    for row in rows:
        f2p = f"{row['f2p_passed']}/{row['f2p_total']}" if row["f2p_total"] != "" else "—"
        p2p = f"{row['p2p_passed']}/{row['p2p_total']}" if row["p2p_total"] != "" else "—"
        lines.append(
            f"| {row['task_id']} ({row['experiment_role']}) | {row['branch']} | {row['seed']} | {row['attempt_id']} | "
            f"{row['agent_stop_reason'] or '—'} | {row['model_requests']} | {row['visible_test_cases']} | "
            f"{row['seen_tests_status']} {row['seen_tests_passed']}/{row['visible_test_cases']} | "
            f"{row['evaluation_status']} | {row['reward']} | {f2p} | {p2p} |"
        )
    lines.extend(["", "## Kết quả theo loại task", "",
                  "| Category | Lượt có điểm hành vi | Reward trung bình | F2P trung bình | P2P trung bình |",
                  "|---|---:|---:|---:|---:|"])
    if categories:
        for category, count, reward, f2p, p2p in categories:
            f2p_value = f"{f2p:.3f}" if f2p is not None else "—"
            p2p_value = f"{p2p:.3f}" if p2p is not None else "—"
            lines.append(f"| {category} | {count} | {reward:.3f} | {f2p_value} | {p2p_value} |")
    else:
        lines.append("Chưa có lượt thí nghiệm (không phải smoke test) đủ điểm để tổng hợp.")
    lines.extend(["", "## Các cặp đổi kết quả", ""])
    if transitions:
        lines.extend(f"- `{transition}`: {count} cặp" for transition, count in sorted(transitions.items()))
    else:
        lines.append("Chưa có cặp nhánh hợp lệ nào cho thấy chuyển đổi pass/fail.")
    lines.extend(["", "Kết quả chi tiết, token usage và lỗi kỹ thuật nằm trong `runs.csv`.", ""])
    md_path = summary_dir / "summary.md"
    md_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")
    return 0
