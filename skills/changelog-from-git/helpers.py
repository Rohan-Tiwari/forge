"""changelog-from-git — group commits by conventional-commits prefix."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Optional

PREFIX_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore)"
    r"(?P<scope>\([^)]+\))?(?P<bang>!)?:\s*(?P<msg>.+)$"
)
GROUPS = {
    "feat": "Features",
    "fix": "Bug fixes",
    "docs": "Documentation",
    "perf": "Performance",
    "refactor": "Refactoring",
    "test": "Tests",
    "build": "Build",
    "ci": "CI",
    "chore": "Chores",
    "style": "Style",
}


def _git(*args: str, repo: Path) -> str:
    r = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


def _last_tag(repo: Path) -> Optional[str]:
    try:
        return _git("describe", "--tags", "--abbrev=0", repo=repo).strip() or None
    except subprocess.CalledProcessError:
        return None


def _commits(since: str, until: str, repo: Path) -> list[dict[str, str]]:
    raw = _git("log", "--format=%H%x00%s%x00%an%x00%as",
               f"{since}..{until}", repo=repo)
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\x00")
        if len(parts) != 4:
            continue
        sha, subject, author, date = parts
        out.append({"sha": sha[:7], "subject": subject,
                    "author": author, "date": date})
    return out


def main(
    since: Optional[str] = None,
    until: str = "HEAD",
    out_path: Optional[str | Path] = None,
    repo: str | Path = ".",
) -> str:
    """Render a markdown changelog. Returns the markdown text."""
    repo_path = Path(repo).expanduser().resolve()
    if since is None:
        since = _last_tag(repo_path) or f"{until}~30"

    commits = _commits(since, until, repo_path)

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for c in commits:
        m = PREFIX_RE.match(c["subject"])
        if m:
            group = GROUPS.get(m.group("type"), "Other")
            c["msg"] = m.group("msg")
            c["scope"] = (m.group("scope") or "").strip("()")
            c["breaking"] = bool(m.group("bang"))
        else:
            group = "Other"
            c["msg"] = c["subject"]
            c["scope"] = ""
            c["breaking"] = False
        grouped[group].append(c)

    lines = [f"# Changelog — {since}..{until}", "",
             f"*{len(commits)} commits*", ""]
    # Stable group order
    order = ["Features", "Bug fixes", "Performance", "Refactoring", "Documentation",
             "Tests", "Build", "CI", "Chores", "Style", "Other"]
    for group in order:
        items = grouped.get(group)
        if not items:
            continue
        lines.append(f"## {group}")
        lines.append("")
        for c in items:
            scope = f" **{c['scope']}**:" if c["scope"] else ""
            bang = " ⚠️" if c["breaking"] else ""
            lines.append(f"- {scope}{bang} {c['msg']} ({c['sha']}, {c['author']})")
        lines.append("")

    text = "\n".join(lines)
    if out_path:
        out = Path(out_path).expanduser()
        from forge.tools import Write
        Write(out, text)
    return text
