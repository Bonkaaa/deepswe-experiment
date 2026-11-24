from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

FILTER_SCRIPT = Path(__file__).with_name("filter_go_tests.go")


def collect_go_tests(box, pattern: str, allow_empty: bool = False, package: str = ".") -> list[dict]:
    rc, output = box.execute(f"go test -list {shlex.quote(pattern)} {shlex.quote(package)}", timeout=600)
    if rc:
        raise RuntimeError(f"Go test listing failed: {output[-2000:]}")
    names = sorted({line.strip() for line in output.splitlines() if re.search(pattern, line.strip())})
    if not names and not allow_empty:
        raise RuntimeError(f"No Go tests match the configured pattern: {pattern}")
    return [
        {"node_id": f"{package}::{name}", "file": "go_test_scope", "function": name, "class": None}
        for name in names
    ]


def filter_go_tests(box, manifest: dict, pattern: str, workdir: str, package: str = ".") -> None:
    keep_path = "/tmp/experiment-kept-go-tests.json"
    script_path = "/tmp/experiment-filter-go-tests.go"
    names = [group.rsplit("::", 1)[-1] for group in manifest["kept_groups"]]
    box.put_bytes(keep_path, json.dumps(names).encode())
    box.put_bytes(script_path, FILTER_SCRIPT.read_bytes())
    command = " ".join((
        "go run", shlex.quote(script_path), "--root", shlex.quote(workdir),
        "--pkg", shlex.quote(package),
        "--pattern", shlex.quote(pattern), "--keep", shlex.quote(keep_path),
    ))
    box.checked(command, timeout=600)


def run_selected_go_tests(box, manifest: dict, timeout: int, package: str = ".") -> tuple[int, str]:
    names = [group.rsplit("::", 1)[-1] for group in manifest["kept_groups"]]
    if not names:
        return 0, "No in-scope tests were assigned to this branch."
    pattern = "^(" + "|".join(names) + ")$"
    return box.execute(f"go test -json -run {shlex.quote(pattern)} {shlex.quote(package)}", timeout=timeout)


def parse_go_test_results(output: str, expected_names: list[str]) -> dict[str, str]:
    """Return final JSON statuses for expected top-level Go tests."""
    expected = set(expected_names)
    results = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("Test")
        action = event.get("Action")
        if name in expected and action in {"pass", "fail", "skip"}:
            results[name] = action
    return results
