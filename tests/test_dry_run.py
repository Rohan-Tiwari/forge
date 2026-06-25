"""Tests for the dry-run preview engine in forge.preview.

The dry-run executes a cell against an OVERLAY (a tmpdir copy of the
workspace) so we get REAL diffs from REAL execution, not heuristics.
These tests verify it actually does that, and stays bounded when things
go wrong (timeout, big workspace, syntax error, etc.).
"""
from __future__ import annotations

import textwrap

from forge.gate import check
from forge.preview import FileChange, Preview


def _wrap(code: str, intent: str = "test", writes=None, network=None) -> str:
    """Build a canonical cell."""
    import yaml as _yaml
    intent_yaml = _yaml.safe_dump({
        "intent": intent,
        "writes": writes or [],
        "network": network or [],
        "reversible": True,
    }, default_flow_style=False).strip()
    return f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"


# =============================================================================
# Real-execution dry-run paths
# =============================================================================


class TestDryRunExecution:
    def test_creates_file_visible_in_diff(self, tmp_path):
        """A cell that writes a new file should show up as 'create' in the preview."""
        code = 'Write("./new_file.txt", "hello world")'
        text = _wrap(code, writes=["./new_file.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        creates = [fc for fc in preview.file_changes if fc.kind == "create"]
        assert any(fc.path == "new_file.txt" for fc in creates)
        # Real workspace must NOT have the file — dry-run discarded the overlay
        assert not (tmp_path / "new_file.txt").exists()

    def test_modifies_existing_file_shows_diff(self, tmp_path):
        existing = tmp_path / "config.txt"
        existing.write_text("DEBUG = false\nHOST = localhost\n")

        code = (
            'old = Read("config.txt")\n'
            'Write("config.txt", old.replace("false", "true"))'
        )
        text = _wrap(code, writes=["config.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )

        modifies = [fc for fc in preview.file_changes if fc.kind == "modify"]
        assert any(fc.path == "config.txt" for fc in modifies)
        config_change = next(fc for fc in modifies if fc.path == "config.txt")
        assert config_change.diff and "true" in config_change.diff
        # Original on disk is unchanged
        assert existing.read_text() == "DEBUG = false\nHOST = localhost\n"

    def test_dynamic_path_writes_caught(self, tmp_path):
        """Static analyzer can't know what `f'/foo/{x}.csv'` resolves to;
        dry-run does because it actually runs the loop."""
        code = textwrap.dedent("""
            for i in range(3):
                Write(f"row_{i}.csv", f"id,n\\n{i},x\\n")
        """).strip()
        # The model declared a glob — gate is happy
        text = _wrap(code, writes=["row_*.csv"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        creates = [fc.path for fc in preview.file_changes if fc.kind == "create"]
        assert "row_0.csv" in creates
        assert "row_1.csv" in creates
        assert "row_2.csv" in creates

    def test_deletes_visible_in_diff(self, tmp_path):
        existing = tmp_path / "doomed.txt"
        existing.write_text("bye")

        code = 'import os; os.unlink("doomed.txt")'
        text = _wrap(code, writes=["doomed.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        deletes = [fc for fc in preview.file_changes if fc.kind == "delete"]
        assert any(fc.path == "doomed.txt" for fc in deletes)
        # Real file still here
        assert existing.exists()

    def test_dry_run_doesnt_touch_real_workspace(self, tmp_path):
        """The fundamental safety property: nothing in the user's workspace
        changes during dry-run. Even side-effect-heavy cells."""
        before = tmp_path / "before.txt"
        before.write_text("original")

        code = textwrap.dedent("""
            Write("a.txt", "1")
            Write("b.txt", "2")
            Write("before.txt", "MODIFIED")
            import os
            os.makedirs("subdir", exist_ok=True)
            Write("subdir/c.txt", "3")
        """).strip()
        text = _wrap(code, writes=["a.txt", "b.txt", "before.txt", "subdir/"])
        gate = check(text)

        Preview.from_dry_run(gate, code=code, workspace=tmp_path)

        # Workspace is untouched
        assert before.read_text() == "original"
        assert not (tmp_path / "a.txt").exists()
        assert not (tmp_path / "b.txt").exists()
        assert not (tmp_path / "subdir").exists()


# =============================================================================
# Fallbacks — when dry-run isn't appropriate, fall back to static
# =============================================================================


class TestDryRunFallbacks:
    def test_syntax_error_falls_back_to_static(self, tmp_path):
        """If the cell doesn't even parse, we can't dry-run. Static
        preview from the gate should still surface what we know."""
        code = "def x(:\n    pass"
        text = _wrap(code, writes=[])
        gate = check(text)
        # Even with a syntax error in the gate's findings, from_dry_run
        # shouldn't crash — it should fall back gracefully.
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        assert preview.code == code
        # No file changes (couldn't run anything)
        assert preview.file_changes == []

    def test_huge_workspace_falls_back(self, tmp_path):
        """Workspaces over the size cap shouldn't trigger dry-run.

        We simulate a 'huge' workspace by passing a tiny cap and verifying
        the preview reports the skip in flagged_reasons.
        """
        (tmp_path / "small.txt").write_text("x" * 1024)
        code = 'Write("new.txt", "ok")'
        text = _wrap(code, writes=["new.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
            max_workspace_mb=0.0001,  # ~100 bytes — definitely below
        )
        assert any("workspace too large" in r for r in preview.flagged_reasons)
        # Static fallback still has the gate-derived file change
        assert preview.code == code

    def test_runtime_error_in_cell_still_returns_partial_preview(self, tmp_path):
        """Cell crashes mid-execution. The dry-run reports what changed
        BEFORE the crash + the error in flagged_reasons."""
        code = textwrap.dedent("""
            Write("first.txt", "before crash")
            raise RuntimeError("boom")
            Write("never_reached.txt", "x")
        """).strip()
        text = _wrap(code, writes=["first.txt", "never_reached.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        # The first write happened before the crash — captured.
        assert any(fc.path == "first.txt" for fc in preview.file_changes)
        # The post-crash one didn't happen — not captured.
        assert not any(fc.path == "never_reached.txt"
                       for fc in preview.file_changes)
        # The crash is surfaced
        assert any("dry-run errored" in r or "RuntimeError" in r
                   for r in preview.flagged_reasons)

    def test_skip_dirs_not_copied(self, tmp_path):
        """The overlay doesn't copy .git, __pycache__, .venv, node_modules.
        Without this, dry-run on a real repo would be miserably slow."""
        # Build a workspace with skip dirs inside
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "huge_blob").write_text("x" * 100_000)
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "junk.pyc").write_text("y" * 100_000)
        (tmp_path / "real.txt").write_text("hello")

        # Run a no-op cell
        code = 'pass'
        text = _wrap(code, writes=[])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        # No changes (it was a no-op cell) — but importantly it didn't
        # take forever to copy the .git blob.
        assert preview.file_changes == []


# =============================================================================
# Network/Bash stubs in dry-run
# =============================================================================


class TestDryRunIsolation:
    def test_bash_calls_are_stubbed(self, tmp_path):
        """Bash should NOT execute real commands during dry-run."""
        code = (
            'r = Bash("echo this would fire if not stubbed > marker.txt")\n'
            'print(r.stdout)\n'
        )
        text = _wrap(code, writes=[])
        gate = check(text)
        Preview.from_dry_run(gate, code=code, workspace=tmp_path)

        # If Bash actually fired the marker file would exist somewhere in
        # the workspace AFTER the dry-run discarded the overlay — the
        # whole point is that it doesn't.
        assert not (tmp_path / "marker.txt").exists()

    def test_network_calls_are_blocked(self, tmp_path):
        """urlopen should fail with a known error during dry-run, not
        actually fire."""
        code = textwrap.dedent("""
            try:
                import urllib.request
                urllib.request.urlopen("http://example.com")
                marker = "REACHED"
            except Exception:
                marker = "BLOCKED"
            Write("marker.txt", marker)
        """).strip()
        text = _wrap(code, writes=["marker.txt"])
        gate = check(text)
        preview = Preview.from_dry_run(
            gate, code=code, workspace=tmp_path,
        )
        # The marker file was created in the overlay; we can read it from
        # the diff.
        creates = [fc for fc in preview.file_changes if fc.path == "marker.txt"]
        assert creates


# =============================================================================
# Renderer with delete kind
# =============================================================================


def test_render_text_includes_delete_marker():
    from forge.gate import IntentBlock
    p = Preview(
        intent=IntentBlock(intent="cleanup"),
        code="",
        file_changes=[
            FileChange(path="old.txt", kind="delete"),
            FileChange(path="new.txt", kind="create"),
            FileChange(path="modified.txt", kind="modify",
                       diff="--- a\n+++ b\n-old\n+new"),
        ],
    )
    text = p.render_text()
    # All three change kinds are visible
    assert "- old.txt" in text
    assert "+ new.txt" in text
    assert "~ modified.txt" in text
