"""forge.preview — render a "what's about to happen" preview before running.

Computes a structured preview of a cell so the user can see EXACTLY what's
about to happen before approving it:

  * Intent (what the model says it's doing)
  * Code (the actual Python)
  * Files about to be written + diff for each (if the file exists)
  * Network calls
  * Bash commands
  * Anything the gate flagged

Two preview strategies:

  STATIC — fast, doesn't run anything. Reads writes/network/Bash from
           the gate's AST findings + intent block. Imperfect for dynamic
           targets but always safe to compute. Use Preview.from_gate().

  DRY-RUN — actually runs the cell against an overlay (a tmpdir mirror
           of the workspace), computes real diffs, then discards. Catches
           dynamic-path writes the static analyzer misses. Use
           Preview.from_dry_run(cell, kernel, workspace).

The renderer uses Rich. Designed so the same Preview can be rendered
to a panel (interactive confirm) or to plain text (testing, --plan mode).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path

from forge.gate import GateDecision, IntentBlock


@dataclass
class FileChange:
    """One file that's about to be written, modified, or deleted."""

    path: str
    kind: str               # "create" | "modify" | "delete" | "unknown"
    diff: str | None = None  # unified diff (None for create or unknown)
    bytes_in: int = 0
    bytes_out_estimate: int = 0


@dataclass
class Preview:
    """Structured preview of a cell. Renderable to a Rich panel or to text."""

    intent: IntentBlock
    code: str
    file_changes: list[FileChange] = field(default_factory=list)
    network_calls: list[str] = field(default_factory=list)
    bash_commands: list[str] = field(default_factory=list)
    flagged_reasons: list[str] = field(default_factory=list)
    syntax_error: str | None = None

    @property
    def has_side_effects(self) -> bool:
        """True if anything outside pure compute is going to happen."""
        return bool(
            self.file_changes
            or self.network_calls
            or self.bash_commands
            or self.flagged_reasons
        )

    @property
    def severity_label(self) -> str:
        """Quick visual: green/yellow/red."""
        if self.flagged_reasons or self.syntax_error:
            return "yellow"
        if self.bash_commands or self.file_changes or self.network_calls:
            return "yellow"
        return "green"

    # ---- factory ------------------------------------------------------

    @classmethod
    def from_gate(cls, gate: GateDecision, *, workspace: Path | None = None) -> Preview:
        """Build a static preview from a GateDecision (no execution)."""
        if gate.intent is None or gate.findings is None:
            return cls(
                intent=IntentBlock(intent="(no intent parsed)"),
                code="",
                flagged_reasons=gate.reasons,
                syntax_error=gate.findings.syntax_error if gate.findings else None,
            )

        # Parse the cell to recover the source code (the gate already did
        # this internally; the GateDecision could carry it but doesn't yet).
        code = ""  # filled by caller via Preview.with_code

        file_changes: list[FileChange] = []
        for _func, target in gate.findings.write_calls:
            if not target:
                continue
            change = _build_file_change(target, workspace)
            file_changes.append(change)

        network_calls = sorted({h for (_, h) in gate.findings.net_calls if h})
        bash_commands = list(gate.findings.bash_calls)

        return cls(
            intent=gate.intent,
            code=code,
            file_changes=file_changes,
            network_calls=network_calls,
            bash_commands=bash_commands,
            flagged_reasons=gate.reasons,
            syntax_error=gate.findings.syntax_error,
        )

    def with_code(self, code: str) -> Preview:
        """Return a copy with the source code attached."""
        self.code = code
        return self

    # ---- dry-run ------------------------------------------------------

    @classmethod
    def from_dry_run(
        cls,
        gate: GateDecision,
        *,
        code: str,
        workspace: Path,
        max_workspace_mb: float = 50.0,
        timeout_s: float = 30.0,
    ) -> Preview:
        """Build a preview by ACTUALLY running the cell against an overlay.

        Strategy:
          1. Copy the workspace (minus .git, .forge, __pycache__, node_modules,
             .venv) into a tmpdir.
          2. Spawn a fresh Python subprocess with cwd=overlay, exec the cell.
             Bash + network calls are converted to no-op stubs so we don't
             actually fire them — dry-run is filesystem-only.
          3. After exec, walk the overlay vs the original workspace, find
             changed/added/deleted files, build FileChange objects with
             real unified diffs.
          4. Discard the overlay tmpdir.

        Falls back to from_gate() if:
          - workspace is too big (> max_workspace_mb)
          - cell has syntax error or no code
          - dry-run subprocess crashes
          - copy times out

        The dry-run is deliberately MORE conservative than runtime: we
        replace network/Bash with no-ops, so any real side effects come
        through Write/Edit/open. The user still sees the network/Bash list
        from the gate's AST scan; this just gives them the file diffs too.
        """
        if gate.intent is None or gate.findings is None or not code:
            return cls.from_gate(gate, workspace=workspace).with_code(code or "")
        if not gate.findings.syntax_ok:
            return cls.from_gate(gate, workspace=workspace).with_code(code)

        # Bound: don't dry-run a multi-GB workspace. Static fallback is fine.
        try:
            ws_size_mb = _estimate_size_mb(workspace)
        except OSError:
            return cls.from_gate(gate, workspace=workspace).with_code(code)
        if ws_size_mb > max_workspace_mb:
            preview = cls.from_gate(gate, workspace=workspace).with_code(code)
            preview.flagged_reasons = list(preview.flagged_reasons) + [
                f"workspace too large ({ws_size_mb:.0f} MB) — dry-run skipped"
            ]
            return preview

        with tempfile.TemporaryDirectory(prefix="forge-dryrun-") as overlay_str:
            overlay = Path(overlay_str)
            try:
                _copy_workspace(workspace, overlay)
            except (OSError, shutil.Error):
                return cls.from_gate(gate, workspace=workspace).with_code(code)

            # Run the cell in the overlay
            ok, exec_err = _run_cell_in_overlay(code, overlay, timeout_s=timeout_s)

            # Compute file diffs even if the cell errored partway — what it
            # DID change before crashing is still useful preview info.
            try:
                file_changes = _compute_file_changes(workspace, overlay)
            except OSError:
                file_changes = []

        # Build the Preview from gate (for network + Bash + flagged reasons)
        # then OVERRIDE file_changes with the real ones from the dry-run.
        preview = cls.from_gate(gate, workspace=workspace).with_code(code)
        preview.file_changes = file_changes
        if not ok and exec_err:
            preview.flagged_reasons = list(preview.flagged_reasons) + [
                f"dry-run errored: {exec_err}"
            ]
        return preview

    # ---- rendering ----------------------------------------------------

    def render_text(self, *, max_diff_lines: int = 30) -> str:
        """Plain-text rendering for non-TTY / test contexts."""
        lines: list[str] = []
        lines.append(f"intent: {self.intent.intent}")
        if self.intent.reversible is False:
            lines.append("⚠ reversible: false (this cannot be undone via `forge undo`)")
        if self.flagged_reasons:
            lines.append(f"flagged: {', '.join(self.flagged_reasons)}")
        if self.syntax_error:
            lines.append(f"syntax error: {self.syntax_error}")

        if self.file_changes:
            lines.append("")
            lines.append("Files about to change:")
            for fc in self.file_changes:
                tag = {"create": "+", "modify": "~", "delete": "-",
                       "unknown": "?"}.get(fc.kind, "?")
                lines.append(f"  {tag} {fc.path}  ({fc.kind})")
                if fc.diff:
                    diff_lines = fc.diff.splitlines()
                    if len(diff_lines) > max_diff_lines:
                        diff_lines = diff_lines[:max_diff_lines]
                        diff_lines.append(
                            f"  ... [+{len(fc.diff.splitlines()) - max_diff_lines} more lines]"
                        )
                    lines.extend("    " + line for line in diff_lines)

        if self.network_calls:
            lines.append("")
            lines.append("Network calls:")
            for n in self.network_calls:
                lines.append(f"  → {n}")

        if self.bash_commands:
            lines.append("")
            lines.append("Bash commands:")
            for b in self.bash_commands:
                lines.append(f"  $ {b}")

        if self.code:
            lines.append("")
            lines.append("Code:")
            for line in self.code.splitlines():
                lines.append(f"  {line}")

        return "\n".join(lines)

    def render_rich(self) -> object:  # returns rich.console.RenderableType
        """Rich rendering for interactive TTY confirmation prompts."""
        from rich.console import Group
        from rich.panel import Panel
        from rich.syntax import Syntax
        from rich.text import Text

        body_parts: list[object] = []

        # Header
        header = Text()
        header.append("intent: ", style="bold")
        header.append(self.intent.intent + "\n")
        if self.intent.reversible is False:
            header.append("⚠ reversible: false ", style="bold yellow")
            header.append("(this cannot be undone via `forge undo`)\n", style="dim")
        if self.flagged_reasons:
            header.append("flagged: ", style="bold red")
            header.append(", ".join(self.flagged_reasons) + "\n")
        if self.syntax_error:
            header.append("syntax error: ", style="bold red")
            header.append(self.syntax_error + "\n")
        body_parts.append(header)

        # Code
        if self.code:
            body_parts.append(Text())
            body_parts.append(Panel(
                Syntax(self.code, "python", theme="monokai", line_numbers=False),
                title="code", border_style="dim", padding=(0, 1),
            ))

        # File changes
        if self.file_changes:
            change_lines = Text()
            for fc in self.file_changes:
                tag = {"create": "+", "modify": "~", "delete": "-",
                       "unknown": "?"}.get(fc.kind, "?")
                style = {"create": "green", "modify": "yellow",
                         "delete": "red", "unknown": "dim"}.get(fc.kind, "dim")
                change_lines.append(f"  {tag} ", style=style)
                change_lines.append(f"{fc.path}", style=style)
                change_lines.append(f"  ({fc.kind})\n", style="dim")
                if fc.diff:
                    diff_lines = fc.diff.splitlines()
                    if len(diff_lines) > 30:
                        diff_lines = diff_lines[:30] + [f"... [+{len(fc.diff.splitlines()) - 30} more lines]"]
                    diff_text = "\n".join(diff_lines)
                    change_lines.append(Syntax(diff_text, "diff", theme="monokai").code + "\n")  # type: ignore[arg-type]
            body_parts.append(Panel(
                change_lines, title="files about to change",
                border_style="yellow", padding=(0, 1),
            ))

        # Network
        if self.network_calls:
            net_text = Text()
            for n in self.network_calls:
                net_text.append(f"  → {n}\n", style="cyan")
            body_parts.append(Panel(
                net_text, title="network calls",
                border_style="cyan", padding=(0, 1),
            ))

        # Bash
        if self.bash_commands:
            bash_text = Text()
            for b in self.bash_commands:
                bash_text.append(f"  $ {b}\n", style="bright_white")
            body_parts.append(Panel(
                bash_text, title="bash commands",
                border_style="magenta", padding=(0, 1),
            ))

        return Group(*body_parts)


# =============================================================================
# Internals
# =============================================================================


def _build_file_change(target: str, workspace: Path | None) -> FileChange:
    """Compute a FileChange for one write target."""
    if not target or any(c in target for c in "*?["):
        # Glob declaration — unknown which files
        return FileChange(path=target, kind="unknown")

    abs_path = Path(target).expanduser()
    if not abs_path.is_absolute() and workspace is not None:
        abs_path = (workspace / abs_path).resolve()

    if not abs_path.exists():
        return FileChange(path=target, kind="create")

    # Modify — try to read so we can show context. We don't yet have the
    # *new* content (that happens in dry-run mode v0.2), so the diff is
    # empty for now; we just signal "this file is being modified".
    try:
        bytes_in = abs_path.stat().st_size
    except OSError:
        bytes_in = 0
    return FileChange(path=target, kind="modify", bytes_in=bytes_in)


def diff_files(before: str, after: str, *, path: str = "file") -> str:
    """Helper: build a unified diff between two strings."""
    lines = list(unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=3,
    ))
    return "".join(lines)


# =============================================================================
# Dry-run helpers
# =============================================================================


# Dirs we never copy into the overlay (too big, irrelevant, or forge-internal).
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".forge", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "env", "node_modules", "dist", "build",
    ".DS_Store",
})


def _estimate_size_mb(workspace: Path) -> float:
    """Approximate size of files we'd copy. Walks the tree without reading."""
    total_bytes = 0
    for root, dirs, files in os.walk(workspace):
        # Prune skip-dirs in place so we don't even descend
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            try:
                total_bytes += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total_bytes / (1024 * 1024)


def _copy_workspace(src: Path, dst: Path) -> None:
    """Copy src → dst, skipping forge runtime / build artefacts."""
    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in _SKIP_DIRS]
    # dst is the tmpdir itself, which already exists; copy CONTENTS into it.
    for entry in src.iterdir():
        if entry.name in _SKIP_DIRS:
            continue
        target = dst / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=ignore, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


# Driver that runs in a fresh subprocess, isolated from any forge state.
# Replaces Bash/subprocess/network with no-ops so dry-run is filesystem-only.
#
# SECURITY: also wraps Write/Edit/open to ensure all writes happen INSIDE
# the overlay tmpdir. Without this, a cell calling Write("/abs/path") OR
# Write("~/sensitive") OR Write("../escape") would touch the real
# filesystem. See v0.2.1 audit finding #4.
_DRY_RUN_DRIVER = r'''
import sys, json, traceback, contextlib, io, os
from pathlib import Path

# Read the cell code from FORGE_DRY_RUN_CODE env var.
code = os.environ.get("FORGE_DRY_RUN_CODE", "")
OVERLAY = os.path.realpath(os.getcwd())

def _path_inside_overlay(target):
    """Return absolute resolved path IF inside overlay, else raise."""
    if hasattr(target, "__fspath__"):
        target = os.fspath(target)
    target = os.path.expanduser(str(target))
    abs_path = os.path.realpath(target) if os.path.isabs(target) else os.path.realpath(
        os.path.join(OVERLAY, target)
    )
    overlay_real = OVERLAY
    if abs_path != overlay_real and not abs_path.startswith(overlay_real + os.sep):
        raise PermissionError(
            f"dry-run write escape blocked: {target!r} resolves to "
            f"{abs_path!r}, outside overlay {overlay_real!r}"
        )
    return abs_path

# Stub side-effecting functions so dry-run doesn't fire real network/shell.
class _StubBashResult:
    def __init__(self):
        self.stdout = ""
        self.stderr = "(dry-run: Bash skipped)"
        self.returncode = 0

def Bash(*a, **kw): return _StubBashResult()
def search(*a, **kw): return []
def see(*a, **kw): return "(dry-run: see() skipped)"
def find_skill(*a, **kw): return []
def run_skill(*a, **kw): return None
def call_mcp(*a, **kw): return None

# Real Read/Write/Edit happen against the overlay cwd — that's the whole
# point. We import forge.tools' implementations but WRAP them so any path
# that escapes the overlay raises BEFORE the underlying tool sees it.
sys.path.insert(0, os.environ.get("FORGE_SRC_PATH", ""))
try:
    from forge.tools import Read as _real_Read, Write as _real_Write, Edit as _real_Edit

    def Read(path, **kw):
        _path_inside_overlay(path)
        return _real_Read(path, **kw)

    def Write(path, content):
        _path_inside_overlay(path)
        return _real_Write(path, content)

    def Edit(path, old, new, **kw):
        _path_inside_overlay(path)
        return _real_Edit(path, old, new, **kw)
except Exception:
    # SECURITY: fail closed. If forge.tools can't import, we DO NOT
    # silently fall back to a permissive stdlib Write — that was the
    # previous behavior and it defeated the safety story. Surface the
    # import error so the dry-run is honestly broken rather than a
    # false-positive "clean" preview.
    raise

# Block real network at the SDK level — the dry-run is FS-only. Also
# block socket directly so libs that bypass urllib still get caught.
class _NetBlocked(Exception): pass
def _no_network(*a, **kw): raise _NetBlocked("dry-run: network skipped")
try:
    import urllib.request
    urllib.request.urlopen = _no_network
except ImportError: pass
try:
    import socket
    socket.socket = _no_network  # type: ignore[assignment]
    socket.create_connection = _no_network  # type: ignore[assignment]
except ImportError: pass

GLOBALS = {
    "__name__": "__forge_dryrun__",
    "Read": Read, "Write": Write, "Edit": Edit, "Bash": Bash,
    "search": search, "see": see,
    "find_skill": find_skill, "run_skill": run_skill, "call_mcp": call_mcp,
}

ok = True
err = ""
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        exec(compile(code, "<dry-run>", "exec"), GLOBALS)
except _NetBlocked as e:
    err = f"network blocked (expected for dry-run): {e}"
except PermissionError as e:
    ok = False
    err = f"PermissionError: {e}"
except BaseException as e:
    ok = False
    err = f"{type(e).__name__}: {e}"

print(json.dumps({"ok": ok, "err": err}))
'''


def _run_cell_in_overlay(
    code: str, overlay: Path, *, timeout_s: float
) -> tuple[bool, str]:
    """Run `code` in a fresh subprocess with cwd=overlay. Returns (ok, error)."""
    import sys as _sys

    # SECURITY: minimal env for the dry-run subprocess too. Same reasoning
    # as kernel.py: agent-emitted code in the dry-run can still call
    # os.environ.get('...') and exfiltrate via the FS overlay (which the
    # user then sees in the preview).
    from forge._subprocess_env import build_minimal_env
    forge_src = str(Path(__file__).resolve().parent.parent)
    env = build_minimal_env(extra={
        "FORGE_DRY_RUN_CODE": code,
        "FORGE_SRC_PATH": forge_src,
        "PYTHONUNBUFFERED": "1",
    })

    try:
        proc = subprocess.run(  # noqa: S603
            [_sys.executable, "-c", _DRY_RUN_DRIVER],
            cwd=str(overlay),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"dry-run timed out after {timeout_s}s"
    except OSError as e:
        return False, f"dry-run subprocess failed to launch: {e}"

    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "non-zero exit")[:200]

    # Last line of stdout should be our JSON result.
    last_line = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        import json
        result = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return False, f"dry-run produced unparseable output: {proc.stdout[:200]}"

    return bool(result.get("ok")), str(result.get("err") or "")


def _compute_file_changes(original: Path, overlay: Path) -> list[FileChange]:
    """Walk both trees, build FileChange objects for everything that differs."""
    changes: list[FileChange] = []
    seen: set[Path] = set()

    # Files in overlay (added or modified)
    for root, dirs, files in os.walk(overlay):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            overlay_path = Path(root) / f
            rel = overlay_path.relative_to(overlay)
            seen.add(rel)
            original_path = original / rel
            try:
                overlay_bytes = overlay_path.read_bytes()
            except OSError:
                continue
            if not original_path.exists():
                # Created
                changes.append(FileChange(
                    path=str(rel),
                    kind="create",
                    bytes_in=0,
                    bytes_out_estimate=len(overlay_bytes),
                ))
                continue
            try:
                original_bytes = original_path.read_bytes()
            except OSError:
                continue
            if overlay_bytes == original_bytes:
                continue  # unchanged
            # Modified — try to render a text diff if both decode
            diff = ""
            try:
                before = original_bytes.decode("utf-8")
                after = overlay_bytes.decode("utf-8")
                diff = diff_files(before, after, path=str(rel))
            except UnicodeDecodeError:
                diff = (f"(binary file changed: {len(original_bytes)} → "
                        f"{len(overlay_bytes)} bytes)")
            changes.append(FileChange(
                path=str(rel),
                kind="modify",
                diff=diff,
                bytes_in=len(original_bytes),
                bytes_out_estimate=len(overlay_bytes),
            ))

    # Files in original but not in overlay (deleted by the cell)
    for root, dirs, files in os.walk(original):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            orig_path = Path(root) / f
            rel = orig_path.relative_to(original)
            if rel in seen:
                continue
            changes.append(FileChange(
                path=str(rel),
                kind="delete",
            ))

    return changes
