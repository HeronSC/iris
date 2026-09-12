# File: tools/house_rules.py

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize

from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BOM = "﻿"

SKIP_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "node_modules",
    "runtime",
    "data",
}


def is_test_file(path: Path) -> bool:
    path_str = str(path).replace("\\", "/")
    if "/tests/" in path_str or path_str.startswith("tests/"):
        return True
    return path.name.startswith("test_")


def is_tools_file(path: Path) -> bool:
    path_str = str(path).replace("\\", "/")
    return "/tools/" in path_str or path_str.startswith("tools/")


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def rule_header_first_line(txt: str, *, path: Path | None = None) -> list[str]:
    errs: list[str] = []

    txt = txt.lstrip(BOM)
    lines = txt.splitlines()
    first = lines[0].lstrip() if lines else ""

    if not first.startswith("# File:"):
        errs.append("Header missing at top of file.")
        return errs

    rest = "\n".join(lines[1:])
    only_comments_or_ws = all(
        (ln.strip() == "" or ln.lstrip().startswith("#")) for ln in rest.splitlines()
    )
    if rest.strip() == "" or only_comments_or_ws:
        return errs

    m = re.match(r"(?s)\A#\s*File:[^\r\n]+(?:\r?\n)[ \t]*(?:\r?\n)", txt)
    if not m:
        errs.append(
            "Missing or malformed file header. Expect first line '# File: <path>' "
            "followed by one truly blank line (spaces/tabs allowed)."
        )
    return errs


def rule_no_datetime_utcnow(txt: str) -> list[str]:
    if re.search(r"\bdatetime\.utcnow\s*\(", txt):
        return ["Do not use datetime.utcnow(); use datetime.now(timezone.utc)."]
    return []


def rule_no_print(txt: str, path: Path | None = None) -> list[str]:
    if re.search(r"^\s*print\s*\(", txt, flags=re.MULTILINE):
        return ["Use logger, not print()."]
    return []


def rule_no_triple_quotes(txt: str) -> list[str]:
    errs: list[str] = []
    try:
        tree = ast.parse(txt)
    except (OSError, ValueError, RuntimeError, TypeError):
        return errs
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(getattr(tree.body[0], "value", None), ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        errs.append("Do not use triple-quoted strings (no docstrings).")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                errs.append("Do not use triple-quoted strings (no docstrings).")
    return errs


def rule_no_comments_anywhere(txt: str, path: Path | None = None) -> list[str]:
    errs: list[str] = []
    if not path:
        return errs

    if path.suffix == ".py":
        allowed_prefixes = ("# File:", "#!", "# @", "# global", "# eslint")
        try:
            tokens = tokenize.generate_tokens(io.StringIO(txt).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    comment = token.string.lstrip()
                    if any(comment.startswith(prefix) for prefix in allowed_prefixes):
                        continue
                    errs.append(f"No Python comments allowed (line {token.start[0]}).")
        except (tokenize.TokenError, IndentationError):
            pass
    elif path.suffix == ".js":
        allowed_prefixes = ("// @", "/* global", "/* eslint", "// File:")
        for i, line in enumerate(txt.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            if i == 1 and stripped.startswith("//"):
                content = stripped[2:].lstrip()
                if content.startswith("#"):
                    content = content.lstrip("#").lstrip()
                if content.startswith("File:"):
                    continue
            if any(stripped.startswith(p) for p in allowed_prefixes):
                continue
            if stripped.startswith("//") or stripped.startswith("/*"):
                errs.append(f"No JavaScript comments allowed (line {i}).")
    return errs


def rule_no_legacy_fallbacks(txt: str) -> list[str]:
    if "_legacy" in txt:
        return ["No legacy fallback/shims allowed; use single entrypoints only."]
    return []


def rule_imports_at_top(txt: str) -> list[str]:
    errs: list[str] = []
    txt = txt.lstrip(BOM)
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return errs

    imports: list[int] = []
    first_non_import_lineno = float("inf")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign)):
            if node.lineno < first_non_import_lineno:
                first_non_import_lineno = node.lineno

    lines = txt.split("\n")
    for lineno in imports:
        if lineno > first_non_import_lineno:
            prev_line = lines[lineno - 2] if lineno > 1 else ""
            if "#! @allow-local-import" not in prev_line:
                errs.append(
                    f"All imports must be at the top of the file. (line {lineno})."
                )
    return errs


def rule_no_relative_imports(txt: str) -> list[str]:
    errs: list[str] = []
    txt = txt.lstrip(BOM)
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return errs
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            errs.append(
                "Relative imports not allowed. Use full module path instead of "
                f"relative import. (line {node.lineno})."
            )
    return errs


RULE_FUNCS = [
    rule_header_first_line,
    rule_no_datetime_utcnow,
    rule_no_print,
    rule_no_triple_quotes,
    rule_no_comments_anywhere,
    rule_no_legacy_fallbacks,
    rule_imports_at_top,
    rule_no_relative_imports,
]

PATH_AWARE = {
    rule_header_first_line,
    rule_no_print,
    rule_no_comments_anywhere,
}

RELAXED_FOR_TESTS_AND_TOOLS = {
    rule_no_triple_quotes,
    rule_no_comments_anywhere,
    rule_imports_at_top,
    rule_no_print,
}


def check_file(path: Path) -> list[str]:
    if not path.exists() or path.suffix not in {".py", ".js"}:
        return []
    try:
        if path.resolve() == Path(__file__).resolve():
            return []
    except (OSError, ValueError, RuntimeError, TypeError):
        pass

    txt = read_text(path)
    if path.suffix == ".js":
        return rule_no_comments_anywhere(txt, path=path)

    errs: list[str] = []
    relaxed = is_test_file(path) or is_tools_file(path)

    for fn in RULE_FUNCS:
        if relaxed and fn in RELAXED_FOR_TESTS_AND_TOOLS:
            continue
        if fn in PATH_AWARE:
            errs.extend(fn(txt, path=path))
        else:
            errs.extend(fn(txt))
    return errs


def is_skipped(p: Path) -> bool:
    return any(part in SKIP_PARTS for part in p.parts) or any(
        part.startswith(".") for part in p.parts
    )


def main(argv: list[str]) -> int:
    if not argv:
        root = Path.cwd()
        argv = [str(p) for p in root.rglob("*.py") if not is_skipped(p)]
        if not argv:
            return 0

    failed = False
    for a in argv:
        p = Path(a)
        if is_skipped(p):
            continue
        errs = check_file(p)
        if errs:
            failed = True
            print(f"===== {p.as_posix()} =====")
            for e in errs:
                print(f" - {e}")
            print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
