from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Sequence

from ..results.schemas import TestGroup

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".tox",
    ".eggs",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    ".nyc_output",
}

JS_EXTS = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}
JS_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
JS_TEST_CALLEES = {"it", "test", "specify"}
JS_TEST_MODIFIERS = {"only", "skip", "concurrent", "todo"}
GO_TEST_FUNC_RE = re.compile(r"^func\s+(Test[A-Za-z0-9_]*)\s*\(", re.MULTILINE)


def _is_ignored(rel: Path) -> bool:
    return any(part in IGNORED_DIRS for part in rel.parts)


def is_test_file(rel_path: str | Path, language: str) -> bool:
    rel = Path(str(rel_path).replace("\\", "/"))
    if _is_ignored(rel):
        return False
    name = rel.name
    lang = (language or "").strip().lower()
    if lang == "go":
        return name.endswith("_test.go")
    if lang == "python":
        if not name.endswith(".py") or name in ("__init__.py", "conftest.py"):
            return False
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        return any(part in ("tests", "test") for part in rel.parts[:-1])
    if lang in ("javascript", "js", "typescript", "ts"):
        if rel.suffix.lower() not in JS_EXTS:
            return False
        low = name.lower()
        if any(marker in low for marker in (".test.", ".spec.")):
            return True
        if any(part.lower() in JS_TEST_DIRS for part in rel.parts[:-1]):
            if not any(
                part.lower() in ("fixtures", "fixture", "__fixtures__", "__mocks__", "mocks", "helpers", "utils")
                for part in rel.parts[:-1]
            ):
                return True
        return False
    return False


def test_exclude_pathspecs(language: str) -> list[str]:
    lang = (language or "").strip().lower()
    if lang == "go":
        return [":(exclude)*_test.go"]
    if lang == "python":
        return [
            ":(exclude,glob)**/test_*.py",
            ":(exclude,glob)**/*_test.py",
            ":(exclude,glob)**/tests/**",
            ":(exclude,glob)**/test/**",
        ]
    if lang in ("javascript", "js", "typescript", "ts"):
        return [
            ":(exclude,glob)**/*.test.*",
            ":(exclude,glob)**/*.spec.*",
            ":(exclude,glob)**/test/**",
            ":(exclude,glob)**/tests/**",
            ":(exclude,glob)**/__tests__/**",
            ":(exclude,glob)**/spec/**",
            ":(exclude,glob)**/specs/**",
        ]
    return []


def _discover_python_tests(root: Path) -> list[TestGroup]:
    groups: list[TestGroup] = []
    seen_ids: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if not is_test_file(rel, "Python"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        rel_posix = rel.as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                gid = f"{rel_posix}::{node.name}"
                if gid not in seen_ids:
                    seen_ids.add(gid)
                    groups.append({
                        "id": gid, "file": rel_posix, "class": None, "function": node.name,
                        "node_ids": [gid], "fixtures": [],
                    })
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        gid = f"{rel_posix}::{node.name}::{item.name}"
                        if gid not in seen_ids:
                            seen_ids.add(gid)
                            groups.append({
                                "id": gid, "file": rel_posix, "class": node.name, "function": item.name,
                                "node_ids": [gid], "fixtures": [],
                            })
    return sorted(groups, key=lambda g: g["id"])


def _skip_js_non_code(text: str, i: int, prev_sig: str) -> tuple[int, bool]:
    """If text[i] starts a JS comment, string, template literal, or regex literal, return (new_index, True)."""
    n = len(text)
    ch = text[i]
    if ch == "/" and i + 1 < n:
        nxt = text[i + 1]
        if nxt == "/":
            nl = text.find("\n", i + 2)
            return (n if nl == -1 else nl + 1), True
        if nxt == "*":
            end = text.find("*/", i + 2)
            return (n if end == -1 else end + 2), True
        # Heuristic: '/' starts a regex literal unless preceded by an expression-ending token.
        if prev_sig in ("", "(", "[", "{", ",", ";", ":", "=", "!", "?", "&", "|", "^", "~", "+", "-", "*", "%", "<", ">", "\n"):
            j = i + 1
            in_class = False
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]" and in_class:
                    in_class = False
                elif c == "/" and not in_class:
                    j += 1
                    while j < n and text[j].isalpha():
                        j += 1
                    return j, True
                elif c == "\n":
                    break
                j += 1
    if ch in ('"', "'", "`"):
        quote = ch
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == quote:
                return j + 1, True
            j += 1
        return n, True
    return i, False


def _find_matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    prev_sig = ""
    while i < n:
        new_i, skipped = _skip_js_non_code(text, i, prev_sig)
        if skipped:
            i = new_i
            prev_sig = ")"
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
            prev_sig = "{"
        elif ch == "}":
            depth -= 1
            prev_sig = "}"
            if depth == 0:
                return i
        elif not ch.isspace():
            prev_sig = ch
        i += 1
    return -1


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    prev_sig = "("
    while i < n:
        new_i, skipped = _skip_js_non_code(text, i, prev_sig)
        if skipped:
            i = new_i
            prev_sig = ")"
            continue
        ch = text[i]
        if ch == "(":
            depth += 1
            prev_sig = "("
        elif ch == ")":
            depth -= 1
            prev_sig = ")"
            if depth == 0:
                return i
        elif not ch.isspace():
            prev_sig = ch
        i += 1
    return -1


def _scan_js_test_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) character spans of statement-level JS test calls (it/test/specify)."""
    # shortcut: lightweight JS lexer misses dynamically generated test loops; upgrade to tree-sitter if pilot hits edge cases.
    n = len(text)
    i = 0
    prev_sig = ""
    prev_word = ""
    stmt_start = True
    spans: list[tuple[int, int]] = []

    while i < n:
        new_i, skipped = _skip_js_non_code(text, i, prev_sig)
        if skipped:
            i = new_i
            prev_sig = ")"
            prev_word = ""
            continue
        ch = text[i]
        if ch.isspace():
            if ch == "\n" and prev_sig in ("", ";", "{", "}", ")"):
                stmt_start = True
            i += 1
            continue
        if ch.isalpha() or ch in ("_", "$"):
            w_start = i
            while i < n and (text[i].isalnum() or text[i] in ("_", "$")):
                i += 1
            word = text[w_start:i]
            if word == "await" and stmt_start:
                prev_word = "await"
                prev_sig = "w"
                continue
            is_stmt_call = (
                word in JS_TEST_CALLEES
                and stmt_start
                and prev_sig != "."
                and prev_word not in ("function", "const", "let", "var", "return", "export", "class")
            )
            if is_stmt_call:
                # Include leading 'await ' if present on the same statement
                span_start = w_start
                if prev_word == "await":
                    await_idx = text.rfind("await", 0, w_start)
                    if await_idx != -1 and text[await_idx + 5:w_start].strip() == "":
                        span_start = await_idx
                j = i
                while j < n and text[j] in " \t":
                    j += 1
                if j < n and text[j] == ".":
                    m_start = j + 1
                    while m_start < n and text[m_start] in " \t":
                        m_start += 1
                    m_end = m_start
                    while m_end < n and (text[m_end].isalnum() or text[m_end] in ("_", "$")):
                        m_end += 1
                    if text[m_start:m_end] in JS_TEST_MODIFIERS:
                        j = m_end
                        while j < n and text[j] in " \t":
                            j += 1
                if j < n and text[j] == "(":
                    end_paren = _find_matching_paren(text, j)
                    if end_paren != -1:
                        end = end_paren + 1
                        # Consume chained calls like .timeout(5000).retries(2)
                        while True:
                            k = end
                            while k < n and text[k] in " \t":
                                k += 1
                            if k < n and text[k] == ".":
                                k += 1
                                while k < n and (text[k].isalnum() or text[k] in ("_", "$")):
                                    k += 1
                                while k < n and text[k] in " \t":
                                    k += 1
                                if k < n and text[k] == "(":
                                    c_end = _find_matching_paren(text, k)
                                    if c_end != -1:
                                        end = c_end + 1
                                        continue
                            break
                        while end < n and text[end] in " \t;":
                            end += 1
                        spans.append((span_start, end))
                        i = end
                        prev_sig = ";"
                        prev_word = ""
                        stmt_start = True
                        continue
            prev_word = word
            prev_sig = "w"
            stmt_start = False
            continue

        prev_sig = ch
        prev_word = ""
        stmt_start = ch in (";", "{", "}")
        i += 1

    # Drop any nested spans fully contained within an outer span
    outer: list[tuple[int, int]] = []
    for s, e in sorted(spans, key=lambda x: (x[0], -x[1])):
        if outer and outer[-1][0] <= s and e <= outer[-1][1]:
            continue
        outer.append((s, e))
    return outer


def _discover_go_tests(root: Path) -> list[TestGroup]:
    groups: list[TestGroup] = []
    for path in sorted(root.rglob("*_test.go")):
        rel = path.relative_to(root)
        if _is_ignored(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_posix = rel.as_posix()
        seen: set[str] = set()
        for match in GO_TEST_FUNC_RE.finditer(text):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)
            gid = f"{rel_posix}::{name}"
            groups.append({
                "id": gid, "file": rel_posix, "class": None, "function": name,
                "node_ids": [gid], "fixtures": [],
            })
    return sorted(groups, key=lambda g: g["id"])


def _discover_js_tests(root: Path) -> list[TestGroup]:
    groups: list[TestGroup] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if not is_test_file(rel, "JavaScript"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_posix = rel.as_posix()
        spans = _scan_js_test_spans(text)
        if spans:
            for idx in range(len(spans)):
                fn_name = f"js_test_{idx}"
                gid = f"{rel_posix}::{fn_name}"
                groups.append({
                    "id": gid, "file": rel_posix, "class": None, "function": fn_name,
                    "node_ids": [gid], "fixtures": [],
                })
        else:
            gid = f"{rel_posix}::__file__"
            groups.append({
                "id": gid, "file": rel_posix, "class": None, "function": "__file__",
                "node_ids": [gid], "fixtures": [],
            })
    return sorted(groups, key=lambda g: g["id"])


def discover_patcheval_tests(root: Path, language: str) -> list[TestGroup]:
    lang = (language or "").strip().lower()
    if lang == "python":
        return _discover_python_tests(root)
    if lang == "go":
        return _discover_go_tests(root)
    if lang in ("javascript", "js", "typescript", "ts"):
        return _discover_js_tests(root)
    return []


def _prune_go_unused_imports_heuristic(text: str) -> str:
    """Remove simple unused imports from a filtered Go test file."""
    # shortcut: identifier regex may miss aliased/dot/shadowed Go imports; run go/parser in-container if pilot hits compile errors.
    import_block_re = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
    single_import_re = re.compile(r'(?m)^import\s+(?:([A-Za-z_][A-Za-z0-9_]*)\s+)?"([^"]+)"\s*$')

    def _pkg_ident(alias: str | None, pkg_path: str) -> str | None:
        if alias in ("_", "."):
            return None
        if alias:
            return alias
        tail = pkg_path.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"v\d+", tail) and "/" in pkg_path:
            tail = pkg_path.rstrip("/").rsplit("/", 2)[-2]
        tail = re.sub(r"\.v\d+$", "", tail)
        ident = tail.split("-")[-1]
        return ident if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ident) else None

    body = import_block_re.sub("", text)
    body = single_import_re.sub("", body)

    def _filter_block(match: re.Match) -> str:
        lines = match.group(1).splitlines()
        kept_lines = []
        spec_re = re.compile(r'^\s*(?:([A-Za-z_][A-Za-z0-9_.]*)\s+)?"([^"]+)"(?:\s*//.*)?$')
        for line in lines:
            m = spec_re.match(line)
            if not m:
                kept_lines.append(line)
                continue
            ident = _pkg_ident(m.group(1), m.group(2))
            if ident is None or re.search(rf"\b{re.escape(ident)}\b", body):
                kept_lines.append(line)
        if not any('"' in l for l in kept_lines):
            return ""
        return "import (\n" + "\n".join(kept_lines) + "\n)"

    updated = import_block_re.sub(_filter_block, text)

    def _filter_single(match: re.Match) -> str:
        ident = _pkg_ident(match.group(1), match.group(2))
        if ident is None or re.search(rf"\b{re.escape(ident)}\b", body):
            return match.group(0)
        return ""

    return single_import_re.sub(_filter_single, updated)


def _unlink_all_test_files(root: Path, language: str) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and is_test_file(path.relative_to(root), language):
            path.unlink()


def _filter_go_snapshot(root: Path, groups: Sequence[TestGroup], kept_ids: set[str]) -> None:
    if not kept_ids:
        _unlink_all_test_files(root, "Go")
        return
    by_file: dict[str, list[TestGroup]] = {}
    for g in groups:
        by_file.setdefault(g["file"], []).append(g)

    for rel, file_groups in by_file.items():
        path = root / rel
        if not path.is_file():
            continue
        removed = {g["function"] for g in file_groups if g["id"] not in kept_ids}
        if not removed:
            continue
        if len(removed) == len(file_groups):
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        spans: list[tuple[int, int]] = []
        for match in GO_TEST_FUNC_RE.finditer(text):
            if match.group(1) not in removed:
                continue
            brace = text.find("{", match.end())
            if brace == -1:
                continue
            end_brace = _find_matching_brace(text, brace)
            if end_brace == -1:
                continue
            end = end_brace + 1
            while end < len(text) and text[end] in " \t\r\n":
                end += 1
            spans.append((match.start(), end))
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + text[end:]
        text = _prune_go_unused_imports_heuristic(text)
        path.write_text(text, encoding="utf-8", newline="\n")


def _filter_python_snapshot(root: Path, groups: Sequence[TestGroup], kept_ids: set[str]) -> None:
    if not kept_ids:
        _unlink_all_test_files(root, "Python")
        return
    by_file: dict[str, list[TestGroup]] = {}
    for g in groups:
        by_file.setdefault(g["file"], []).append(g)

    for rel, file_groups in by_file.items():
        path = root / rel
        if not path.is_file():
            continue
        removed_groups = [g for g in file_groups if g["id"] not in kept_ids]
        if not removed_groups:
            continue
        if len(removed_groups) == len(file_groups):
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines(keepends=True)
        cuts: list[tuple[int, int]] = []
        removed_by_class: dict[str, set[str]] = {}
        for g in removed_groups:
            parent = tree
            if g.get("class"):
                matches = [x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == g["class"]]
                if len(matches) != 1:
                    continue
                parent = matches[0]
            nodes = [
                x for x in parent.body
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == g["function"]
            ]
            if len(nodes) != 1:
                continue
            node = nodes[0]
            start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            cuts.append((start, node.end_lineno))
            if g.get("class"):
                removed_by_class.setdefault(g["class"], set()).add(node.name)

        class_ranges: list[tuple[int, int]] = []
        for cls_name, rem_names in removed_by_class.items():
            cls = next((x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == cls_name), None)
            if cls is None:
                continue
            remaining = [
                x for x in cls.body
                if not isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) or x.name not in rem_names
            ]
            if not remaining:
                start = min([cls.lineno] + [d.lineno for d in cls.decorator_list]) - 1
                class_ranges.append((start, cls.end_lineno))
                cuts.append((start, cls.end_lineno))

        cuts = [
            cut for cut in cuts
            if cut in class_ranges or not any(l <= cut[0] and cut[1] <= r for l, r in class_ranges)
        ]
        for start, end in sorted(set(cuts), reverse=True):
            lines[start:end] = []
        result = "".join(lines)
        try:
            ast.parse(result)
        except SyntaxError:
            continue
        path.write_text(result, encoding="utf-8", newline="\n")


def _filter_js_snapshot(root: Path, groups: Sequence[TestGroup], kept_ids: set[str]) -> None:
    if not kept_ids:
        _unlink_all_test_files(root, "JavaScript")
        return
    by_file: dict[str, list[TestGroup]] = {}
    for g in groups:
        by_file.setdefault(g["file"], []).append(g)

    for rel, file_groups in by_file.items():
        path = root / rel
        if not path.is_file():
            continue
        removed_funcs = {g["function"] for g in file_groups if g["id"] not in kept_ids}
        if not removed_funcs:
            continue
        if len(removed_funcs) == len(file_groups) or "__file__" in removed_funcs:
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        all_spans = _scan_js_test_spans(text)
        to_remove = [
            span for idx, span in enumerate(all_spans)
            if f"js_test_{idx}" in removed_funcs
        ]
        for start, end in sorted(to_remove, reverse=True):
            text = text[:start] + text[end:]
        path.write_text(text, encoding="utf-8", newline="\n")


def filter_patcheval_snapshot(root: Path, manifest: dict, language: str) -> None:
    groups = manifest.get("groups", [])
    kept_ids = set(manifest.get("kept_groups", []))
    lang = (language or "").strip().lower()
    if lang == "python":
        _filter_python_snapshot(root, groups, kept_ids)
    elif lang == "go":
        _filter_go_snapshot(root, groups, kept_ids)
    elif lang in ("javascript", "js", "typescript", "ts"):
        _filter_js_snapshot(root, groups, kept_ids)
