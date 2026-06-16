"""tidy-imports — sort and dedupe Python imports, isort-style."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Optional

# Stdlib module names — sys.stdlib_module_names is exhaustive on 3.10+.
STDLIB = set(sys.stdlib_module_names)


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def _classify(module: str, project_modules: set[str]) -> int:
    """Return group index: 0=__future__, 1=stdlib, 2=third-party, 3=local."""
    if module == "__future__":
        return 0
    root = _root_module(module)
    if root in STDLIB:
        return 1
    if module.startswith(".") or root in project_modules:
        return 3
    return 2


def _project_modules(root: Path) -> set[str]:
    """Heuristic: top-level Python packages directly under `root` or `src/`."""
    out: set[str] = set()
    for d in (root, root / "src"):
        if d.is_dir():
            for child in d.iterdir():
                if child.is_dir() and (child / "__init__.py").exists():
                    out.add(child.name)
    return out


def _is_pure_import_node(node: ast.AST) -> bool:
    return isinstance(node, (ast.Import, ast.ImportFrom))


def _module_for_node(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name
    if isinstance(node, ast.ImportFrom):
        prefix = "." * (node.level or 0)
        return prefix + (node.module or "")
    return ""


def _sort_key_for_node(node: ast.AST, project_modules: set[str]) -> tuple:
    mod = _module_for_node(node)
    group = _classify(mod, project_modules)
    # isort-style: by group, then by module name (case-insensitive),
    # then by `from X import ...` over `import X` (1 vs 0)
    is_from = isinstance(node, ast.ImportFrom)
    return (group, mod.lower(), 1 if is_from else 0, mod)


def _format_node(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        names = ", ".join(
            f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names
        )
        return f"import {names}"
    if isinstance(node, ast.ImportFrom):
        prefix = "." * (node.level or 0)
        mod = prefix + (node.module or "")
        names = ", ".join(
            f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names
        )
        return f"from {mod} import {names}"
    return ""


def tidy_file(path: str | Path, *, project_modules: Optional[set[str]] = None,
              dry_run: bool = False) -> Optional[str]:
    """Tidy imports in one file. Returns new text if changed, else None.

    If `dry_run`, returns the new text without writing.
    """
    p = Path(path).expanduser().resolve()
    text = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None  # leave broken files alone

    if project_modules is None:
        # Best-effort guess from the file's parent
        project_modules = _project_modules(p.parents[1] if len(p.parents) > 1 else p.parent)

    # Find the contiguous top-level import block.
    body = tree.body
    if not body:
        return None
    # Skip docstring
    start = 0
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        start = 1

    end = start
    while end < len(body) and _is_pure_import_node(body[end]):
        end += 1

    if end <= start:
        return None  # no import block

    import_nodes = body[start:end]

    # Build the new block: dedup, sort.
    seen: set[str] = set()
    unique: list[ast.AST] = []
    for node in import_nodes:
        rendered = _format_node(node)
        if rendered in seen:
            continue
        seen.add(rendered)
        unique.append(node)

    sorted_nodes = sorted(unique, key=lambda n: _sort_key_for_node(n, project_modules))

    # Group with blank lines.
    grouped: list[list[ast.AST]] = [[], [], [], []]
    for n in sorted_nodes:
        g = _classify(_module_for_node(n), project_modules)
        grouped[g].append(n)

    new_lines: list[str] = []
    for group in grouped:
        if not group:
            continue
        for n in group:
            new_lines.append(_format_node(n))
        new_lines.append("")
    if new_lines and new_lines[-1] == "":
        new_lines.pop()

    new_block = "\n".join(new_lines)

    # Splice it back into the source. Use line-based, since AST col_offset is fragile.
    src_lines = text.splitlines()
    first_line = import_nodes[0].lineno - 1
    last_line = import_nodes[-1].end_lineno or import_nodes[-1].lineno
    new_src = "\n".join(
        src_lines[:first_line] + new_block.split("\n") + src_lines[last_line:]
    )
    if not new_src.endswith("\n"):
        new_src += "\n"

    if new_src == text:
        return None

    if not dry_run:
        from forge.tools import Write
        Write(p, new_src)
    return new_src


def main(path: str | Path = ".", *, dry_run: bool = False) -> dict[str, object]:
    """Walk `path`, tidy every .py file, return summary."""
    root = Path(path).expanduser().resolve()
    project_mods = _project_modules(root)

    if root.is_file():
        files = [root]
    else:
        # Skip excluded dirs
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
                "dist", "build", ".forge"}
        files = [
            f for f in root.rglob("*.py")
            if not any(part in skip for part in f.relative_to(root).parts)
        ]

    changed: list[str] = []
    for f in files:
        result = tidy_file(f, project_modules=project_mods, dry_run=dry_run)
        if result is not None:
            changed.append(str(f.relative_to(root)) if not f == root else f.name)

    return {
        "scanned": len(files),
        "changed": len(changed),
        "files": changed,
        "dry_run": dry_run,
    }
