"""Tests for forge.preview — what the user sees before approving."""
from __future__ import annotations

from pathlib import Path

import pytest

from forge.gate import check
from forge.preview import FileChange, Preview


def _gate(text: str):
    return check(text)


def _wrap(code: str, intent: str = "test", writes=None, network=None) -> str:
    import yaml as _yaml
    intent_yaml = _yaml.safe_dump({
        "intent": intent,
        "writes": writes or [],
        "network": network or [],
        "reversible": True,
    }, default_flow_style=False).strip()
    return f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"


def test_preview_from_pure_compute():
    """Cell that does pure compute → no side effects → preview is benign."""
    gate = _gate(_wrap('print(1+1)'))
    p = Preview.from_gate(gate).with_code('print(1+1)')
    assert not p.has_side_effects
    assert p.severity_label == "green"


def test_preview_from_write():
    gate = _gate(_wrap('Write("./out/foo.csv", "x")', writes=["./out/foo.csv"]))
    p = Preview.from_gate(gate).with_code('Write("./out/foo.csv", "x")')
    assert p.has_side_effects
    assert any(fc.path == "./out/foo.csv" for fc in p.file_changes)


def test_preview_from_network():
    gate = _gate(_wrap(
        'import requests; requests.get("https://api.github.com/x")',
        network=["api.github.com"],
    ))
    p = Preview.from_gate(gate)
    assert p.has_side_effects
    assert any("api.github.com" in n for n in p.network_calls)


def test_preview_from_bash():
    gate = _gate(_wrap('Bash("ls -la")'))
    p = Preview.from_gate(gate)
    assert p.has_side_effects
    assert "ls -la" in p.bash_commands


def test_preview_render_text_no_side_effects():
    gate = _gate(_wrap('print(1)'))
    p = Preview.from_gate(gate).with_code('print(1)')
    text = p.render_text()
    assert "intent: test" in text
    assert "Code:" in text


def test_preview_render_text_with_writes():
    gate = _gate(_wrap('Write("./out/x.txt", "hi")', writes=["./out/x.txt"]))
    p = Preview.from_gate(gate).with_code('Write("./out/x.txt", "hi")')
    text = p.render_text()
    assert "Files about to change" in text
    assert "./out/x.txt" in text


def test_preview_render_text_with_irreversible():
    """reversible: false should print a warning."""
    text_block = """```intent
intent: "x"
writes: []
network: []
reversible: false
```
```py
print(1)
```"""
    gate = check(text_block)
    p = Preview.from_gate(gate).with_code('print(1)')
    text = p.render_text()
    assert "reversible: false" in text


def test_preview_file_change_for_existing_file(tmp_path):
    """If the target file exists, kind should be 'modify'."""
    target = tmp_path / "existing.txt"
    target.write_text("before")
    gate = _gate(_wrap(f'Write("{target}", "after")', writes=[str(target)]))
    p = Preview.from_gate(gate, workspace=tmp_path)
    fc = next((fc for fc in p.file_changes if str(target) in fc.path), None)
    assert fc is not None
    assert fc.kind == "modify"


def test_preview_file_change_for_missing_file(tmp_path):
    """If the target doesn't exist, kind should be 'create'."""
    target = tmp_path / "new.txt"
    gate = _gate(_wrap(f'Write("{target}", "x")', writes=[str(target)]))
    p = Preview.from_gate(gate, workspace=tmp_path)
    fc = next((fc for fc in p.file_changes if str(target) in fc.path), None)
    assert fc is not None
    assert fc.kind == "create"


def test_preview_glob_target_is_unknown():
    """Glob-style declarations (./data/*.txt) are 'unknown' until dry-run."""
    gate = _gate(_wrap(
        'from pathlib import Path\nfor f in Path("./data").glob("*.txt"):\n    f.unlink()',
        writes=["./data/*.txt"],
    ))
    p = Preview.from_gate(gate)
    glob_changes = [fc for fc in p.file_changes if "*" in fc.path]
    if glob_changes:
        assert glob_changes[0].kind == "unknown"
