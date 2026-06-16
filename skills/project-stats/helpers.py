"""project-stats — structural statistics for a code project."""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

# Common code extensions and their language label.
LANG_BY_EXT: dict[str, str] = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".sh": "Shell", ".zsh": "Shell", ".bash": "Shell",
    ".md": "Markdown", ".rst": "ReST",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".sql": "SQL", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".json": "JSON",
}

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                "dist", "build", ".forge", ".pytest_cache", ".mypy_cache"}


def _is_git_repo(path: Path) -> bool:
    return subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).returncode == 0


def _list_files(path: Path) -> list[Path]:
    """List files, honoring git's ignore rules when available."""
    if _is_git_repo(path):
        r = subprocess.run(  # noqa: S603,S607
            ["git", "-C", str(path), "ls-files"],
            capture_output=True, text=True, check=True,
        )
        return [path / line for line in r.stdout.splitlines() if line]
    out: list[Path] = []
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in f.relative_to(path).parts):
            continue
        out.append(f)
    return out


def _line_count(file: Path) -> int:
    try:
        with file.open("rb") as f:
            return sum(1 for _ in f)
    except (OSError, ValueError):
        return 0


def main(path: str | Path = ".") -> dict[str, Any]:
    """Produce structural stats for a code project. Returns a dict."""
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"no such path: {path}")

    files = _list_files(root)
    by_lang: Counter[str] = Counter()
    loc_by_lang: Counter[str] = Counter()
    file_locs: list[tuple[int, Path]] = []

    for f in files:
        ext = f.suffix.lower()
        lang = LANG_BY_EXT.get(ext, "Other" if ext else "Unknown")
        by_lang[lang] += 1
        if lang == "Other" or lang == "Unknown":
            continue
        n = _line_count(f)
        loc_by_lang[lang] += n
        file_locs.append((n, f))

    file_locs.sort(reverse=True)

    git_info: dict[str, Any] = {}
    if _is_git_repo(root):
        first_commit = subprocess.run(  # noqa: S603,S607
            ["git", "-C", str(root), "log", "--reverse", "--format=%h %as %s", "-n1"],
            capture_output=True, text=True,
        ).stdout.strip()
        last_commit = subprocess.run(  # noqa: S603,S607
            ["git", "-C", str(root), "log", "--format=%h %as %s", "-n1"],
            capture_output=True, text=True,
        ).stdout.strip()
        git_info = {
            "first_commit": first_commit,
            "last_commit": last_commit,
        }

    summary = {
        "path": str(root),
        "file_count": len(files),
        "total_loc": int(sum(loc_by_lang.values())),
        "by_language": dict(loc_by_lang.most_common(8)),
        "file_count_by_language": dict(by_lang.most_common(8)),
        "largest_files": [(n, str(f.relative_to(root))) for n, f in file_locs[:5]],
        "git": git_info,
    }
    return summary


def render_markdown(stats: dict[str, Any]) -> str:
    """Render a stats dict (from main()) as a readable markdown summary."""
    lines = [
        f"# Project stats — `{stats['path']}`",
        "",
        f"- **{stats['file_count']:,}** files",
        f"- **{stats['total_loc']:,}** lines of code",
        "",
        "## Languages by LOC",
        "",
    ]
    for lang, n in stats["by_language"].items():
        lines.append(f"- {lang}: {n:,}")
    lines.append("")
    lines.append("## Largest files")
    lines.append("")
    for n, p in stats["largest_files"]:
        lines.append(f"- {p} — {n:,} LOC")
    if stats.get("git"):
        lines.extend(["", "## Git",
                      f"- first commit: {stats['git'].get('first_commit', '?')}",
                      f"- last commit:  {stats['git'].get('last_commit', '?')}"])
    return "\n".join(lines)
