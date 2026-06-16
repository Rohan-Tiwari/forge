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
           targets but always safe to compute.

  DRY-RUN — runs the cell against an overlay sandbox: a tmpdir mirroring
           the workspace where Write/Edit go, then diffs the overlay
           against the workspace. Captures REAL writes including dynamic
           paths the static analyzer missed. Skips network and Bash
           (those are inherently side-effect-ful).

For v0.1 we ship STATIC (fast, predictable, no surprises). DRY-RUN is
designed to bolt on without changing the renderer or the confirmation
flow — we add a `Preview.from_dry_run(cell, kernel)` constructor in v0.2.

The renderer uses Rich. Designed so the same Preview can be rendered
to a panel (interactive confirm) or to plain text (testing, --plan mode).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Optional

from forge.gate import GateDecision, IntentBlock


@dataclass
class FileChange:
    """One file that's about to be written."""

    path: str
    kind: str               # "create" | "modify" | "unknown"
    diff: Optional[str] = None  # unified diff (None for create or unknown)
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
    syntax_error: Optional[str] = None

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
    def from_gate(cls, gate: GateDecision, *, workspace: Path | None = None) -> "Preview":
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
        for func, target in gate.findings.write_calls:
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

    def with_code(self, code: str) -> "Preview":
        """Return a copy with the source code attached."""
        self.code = code
        return self

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
                tag = {"create": "+", "modify": "~", "unknown": "?"}.get(fc.kind, "?")
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
                tag = {"create": "+", "modify": "~", "unknown": "?"}.get(fc.kind, "?")
                style = {"create": "green", "modify": "yellow", "unknown": "dim"}.get(fc.kind, "dim")
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
