#!/usr/bin/env python3
"""Single-file bundler: concatenates src/{lifecycle,capture,ui,main}.py
into loupe.py at the repo root.

Deterministic / idempotent: run repeatedly on unchanged sources, byte-identical
output. Strips each module's shebang/docstring/imports and consolidates all
`gi.require_version` calls plus a single `from gi.repository import ...` at
the top (any module-conditional try/except import probes are left in place
since they can't be safely hoisted).

Usage: python3 tools/build.py
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUTPUT = ROOT / "loupe.py"

MODULE_ORDER = ["lifecycle", "capture", "ui", "main"]
SIBLING_NAMES = set(MODULE_ORDER)

ENTRYPOINT_MODULE = "main"  # its docstring becomes loupe.py's; its __main__
                             # guard is the only one kept (see _is_main_guard)
DOCSTRING_SOURCE_MODULE = ENTRYPOINT_MODULE


def _is_gi_require_version(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "require_version"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "gi"
    )


def _is_plain_import_gi(node: ast.stmt) -> bool:
    return isinstance(node, ast.Import) and any(a.name == "gi" for a in node.names)


def _is_sibling_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return all(alias.name.split(".")[0] in SIBLING_NAMES for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module is not None and node.module.split(".")[0] in SIBLING_NAMES
    return False


def _is_main_guard(node: ast.stmt) -> bool:
    """`if __name__ == "__main__": ...` — each src module may have its own
    (CLI test entry, manual harness); only main.py's should survive bundling,
    as the single trailing entrypoint. Bundling everyone's would execute them
    all in file order when loupe.py runs as __main__ (a real bug caught by
    the --smoke run: ui.py's harness ran instead of the wired app)."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _module_docstring_node(tree: ast.Module) -> ast.stmt | None:
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        return tree.body[0]
    return None


class ProcessedModule:
    def __init__(self, name: str):
        self.name = name
        self.text = (SRC / f"{name}.py").read_text()
        self.lines = self.text.splitlines(keepends=True)
        self.tree = ast.parse(self.text, filename=f"{name}.py")

        self.gi_requires: set[tuple[str, str]] = set()
        self.gi_repo_names: set[str] = set()
        self.plain_imports: set[str] = set()
        self.drop_ranges: list[tuple[int, int]] = []
        self.docstring_text: str | None = None

        self._classify()

    def _drop(self, node: ast.stmt) -> None:
        self.drop_ranges.append((node.lineno, node.end_lineno))

    def _classify(self) -> None:
        doc_node = _module_docstring_node(self.tree)
        if doc_node is not None:
            self.docstring_text = ast.get_source_segment(self.text, doc_node)
            self._drop(doc_node)

        for node in self.tree.body:
            if node is doc_node:
                continue
            if self.name != ENTRYPOINT_MODULE and _is_main_guard(node):
                self._drop(node)
                continue
            if _is_plain_import_gi(node) or _is_gi_require_version(node):
                self._drop(node)
                if _is_gi_require_version(node):
                    ns, ver = node.value.args[0].value, node.value.args[1].value
                    self.gi_requires.add((ns, ver))
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "gi.repository":
                self._drop(node)
                self.gi_repo_names.update(a.name for a in node.names)
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if _is_sibling_import(node):
                    self._drop(node)
                    continue
                self._drop(node)
                self.plain_imports.add(ast.get_source_segment(self.text, node))
                continue
            # everything else (functions, classes, conditional try/except
            # import probes, module-level statements) stays in the body.

    def body_text(self) -> str:
        dropped = set()
        for start, end in self.drop_ranges:
            dropped.update(range(start, end + 1))

        kept = [
            line for i, line in enumerate(self.lines, start=1) if i not in dropped
        ]
        text = "".join(kept)

        # tidy up: strip leading/trailing blank lines, collapse 3+ blank
        # lines left behind by stripped imports down to at most 1 blank line.
        text = text.strip("\n")
        lines = text.split("\n")
        cleaned: list[str] = []
        blank_run = 0
        for line in lines:
            if line.strip() == "":
                blank_run += 1
                if blank_run > 2:
                    continue
            else:
                blank_run = 0
            cleaned.append(line)
        return "\n".join(cleaned).strip("\n") + "\n"


def build() -> str:
    modules = [ProcessedModule(name) for name in MODULE_ORDER]

    future_imports: set[str] = set()
    plain_imports: set[str] = set()
    gi_requires: set[tuple[str, str]] = set()
    gi_repo_names: set[str] = set()

    for m in modules:
        for imp in m.plain_imports:
            if imp.startswith("from __future__"):
                future_imports.add(imp)
            else:
                plain_imports.add(imp)
        gi_requires |= m.gi_requires
        gi_repo_names |= m.gi_repo_names

    docstring = next(
        m.docstring_text for m in modules if m.name == DOCSTRING_SOURCE_MODULE
    )

    parts = ["#!/usr/bin/env python3\n", docstring, "\n"]

    if future_imports:
        for imp in sorted(future_imports):
            parts.append(imp + "\n")
        parts.append("\n")

    for imp in sorted(plain_imports):
        parts.append(imp + "\n")
    parts.append("\n")

    parts.append("import gi\n\n")
    for ns, ver in sorted(gi_requires):
        parts.append(f'gi.require_version("{ns}", "{ver}")\n')
    parts.append("\n")

    if gi_repo_names:
        parts.append(f"from gi.repository import {', '.join(sorted(gi_repo_names))}\n")
    parts.append("\n")

    for m in modules:
        parts.append(f"# ==== src/{m.name}.py ====\n")
        parts.append(m.body_text())
        parts.append("\n\n")

    output = "".join(parts)
    output = output.rstrip("\n") + "\n"
    return output


def main() -> None:
    output = build()
    OUTPUT.write_text(output)
    OUTPUT.chmod(0o755)
    print(f"wrote {OUTPUT} ({len(output)} bytes)")


if __name__ == "__main__":
    main()
