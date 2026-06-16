"""forge.shadow — git-based undo for filesystem mutations.

Every cell auto-commits the workspace (pre + post) into a shadow git repo
living at `<workspace>/.forge/shadow`. The shadow uses git's plumbing
(`git --git-dir=... --work-tree=...`) to track files in the workspace
WITHOUT touching the workspace's own .git directory. This means:

  - The user's real git workflow is unaffected
  - Every cell is a recoverable checkpoint
  - `forge undo` reverts the last cell's filesystem changes
  - `forge log` shows cell-by-cell history with intent blocks as commit msgs

The shadow ignores the same things the workspace's .gitignore would, plus
forge's own .forge/ dir. It's deliberately fast and never blocks the agent —
worst case, a commit fails (disk full, etc.) and we surface the error but
don't deny the cell.

Hardening:
  * `git read-tree --reset -u <sha>` + `git clean -fd` for undo (correctly
    removes files added by the undone commit, which `checkout -- .` does NOT)
  * sha validation before reset_to (was silent on bad sha)
  * LC_ALL=C on subprocess for stable error parsing
  * commit() returns None on disk-full / lock-contention rather than raise
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ShadowCommit:
    sha: str
    message: str


_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


class ShadowGit:
    """Per-workspace shadow git repo. Cheap to create, cheap to commit."""

    def __init__(self, *, workspace: Path):
        self.workspace = workspace.resolve()
        self.git_dir = self.workspace / ".forge" / "shadow"

    # ---- subprocess wrapper ---------------------------------------------

    def _git(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.workspace}",
            *args,
        ]
        return subprocess.run(  # noqa: S603,S607
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            env=_GIT_ENV,
        )

    # ---- lifecycle -------------------------------------------------------

    def init(self) -> None:
        """Initialize the shadow repo if absent. Idempotent."""
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603,S607
            ["git", "init", "--bare", "-q", str(self.git_dir)],
            check=True, capture_output=True, env=_GIT_ENV,
        )
        # Friendly identity. Shadow commits never leave the laptop.
        self._git("config", "user.email", "shadow@forge.local")
        self._git("config", "user.name", "forge-shadow")
        # Honor the workspace's .gitignore plus exclude .forge/
        self._git("config", "core.excludesfile", str(self.workspace / ".forge" / ".gitignore"))
        ignore_path = self.workspace / ".forge" / ".gitignore"
        ignore_path.parent.mkdir(parents=True, exist_ok=True)
        ignore_path.write_text(".forge/\n__pycache__/\n*.pyc\n.DS_Store\n")
        # Initial commit so HEAD always exists.
        self._git("add", "-A", check=False)
        self._git("commit", "--allow-empty", "-m", "shadow:init", check=False)

    # ---- commit / log / undo --------------------------------------------

    def commit(self, message: str, *, allow_empty: bool = True) -> Optional[ShadowCommit]:
        """Stage everything and commit. Returns the commit, or None on benign failure.

        Failures we swallow:
          - "nothing to commit" when allow_empty=False
          - disk full
          - git lock contention (concurrent shadow operations — rare but
            possible on multi-process)

        Caller decides whether to surface the None or just log it.
        """
        try:
            self._git("add", "-A", check=False)
        except (subprocess.CalledProcessError, OSError):
            return None

        args = ["commit"]
        if allow_empty:
            args.append("--allow-empty")
        args.extend(["-m", message])

        try:
            self._git(*args, check=True)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "") + (e.stdout or "")
            # benign: "nothing to commit" — only happens with allow_empty=False
            if "nothing to commit" in stderr:
                return None
            # disk full or lock contention — log via return value, don't raise
            if any(s in stderr for s in
                   ("No space left", "unable to lock", "index.lock")):
                return None
            # Anything else is a real bug — surface it to the caller.
            raise
        sha = self._git("rev-parse", "HEAD").stdout.strip()
        return ShadowCommit(sha=sha, message=message)

    def log(self, n: int = 20) -> list[ShadowCommit]:
        """Most recent shadow commits, newest first."""
        result = self._git("log", f"-n{n}", "--pretty=format:%H%x09%s", check=False)
        out: list[ShadowCommit] = []
        for line in result.stdout.splitlines():
            sha, _, msg = line.partition("\t")
            if sha:
                out.append(ShadowCommit(sha=sha, message=msg))
        return out

    def show(self, sha: str) -> str:
        """Return the diff for one shadow commit."""
        if not self._is_valid_sha(sha):
            return f"(no such commit: {sha})"
        return self._git("show", "--stat", "-p", sha, check=False).stdout

    def undo_last(self) -> Optional[ShadowCommit]:
        """Restore the working tree to the state before the most recent commit.

        Uses `git read-tree --reset -u <prev>` followed by `git clean -fd`,
        which correctly:
          1. Restores tracked files to their state in <prev>
          2. Removes files that were ADDED by the undone commit (which
             `checkout -- .` would leave behind)
          3. Honors the shadow's .gitignore (so we don't blow away anything
             outside the tracked tree).
        """
        log = self.log(2)
        if len(log) < 2:
            return None
        target, undone = log[1], log[0]
        # Restore tracked files
        try:
            self._git("read-tree", "--reset", "-u", target.sha)
        except subprocess.CalledProcessError:
            # Fall back to checkout — better than nothing
            self._git("checkout", target.sha, "--", ".", check=False)
        # Clean files that were added in the undone commit
        self._git("clean", "-fd", check=False)
        # Snapshot the new state so future undos work
        self.commit(f"shadow:undo {undone.sha[:7]}", allow_empty=True)
        return undone

    def reset_to(self, sha: str) -> bool:
        """Hard reset to a specific shadow commit. Returns False on bad sha."""
        if not self._is_valid_sha(sha):
            return False
        try:
            self._git("read-tree", "--reset", "-u", sha)
        except subprocess.CalledProcessError:
            self._git("checkout", sha, "--", ".", check=False)
        self._git("clean", "-fd", check=False)
        self.commit(f"shadow:reset_to {sha[:7]}", allow_empty=True)
        return True

    def _is_valid_sha(self, sha: str) -> bool:
        """Check that `sha` resolves to a real commit in the shadow."""
        try:
            r = self._git("rev-parse", "--verify", f"{sha}^{{commit}}", check=True)
            return bool(r.stdout.strip())
        except subprocess.CalledProcessError:
            return False

