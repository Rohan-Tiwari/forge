"""Tests for forge.gate — intent parser + AST lint + GateDecision."""
from __future__ import annotations

from forge.gate import (
    GateAction,
    analyze,
    check,
    parse_cell,
)

# =============================================================================
# parse_cell
# =============================================================================


def test_parses_canonical_cell():
    text = """```intent
intent: "Count files"
writes: []
network: []
reversible: true
```

```py
print("hi")
```
"""
    cell = parse_cell(text)
    assert cell.parse_problems == []
    assert cell.intent_first
    assert cell.intent is not None
    assert cell.intent.intent == "Count files"
    assert cell.code == 'print("hi")'


def test_parse_accepts_python_fence_too():
    text = """```intent
intent: "x"
writes: []
network: []
```

```python
print(1)
```
"""
    cell = parse_cell(text)
    assert cell.parse_problems == []
    assert cell.code == "print(1)"


def test_prose_only_is_not_a_problem():
    cell = parse_cell("Just answering: the answer is 42.")
    assert cell.parse_problems == []
    assert cell.code is None
    assert cell.intent is None


def test_missing_intent_fence():
    text = "```py\nprint(1)\n```"
    cell = parse_cell(text)
    assert "no_intent_fence" in cell.parse_problems


def test_intent_after_python_is_a_problem():
    text = """```py
print(1)
```

```intent
intent: "x"
writes: []
network: []
```
"""
    cell = parse_cell(text)
    assert "intent_after_python" in cell.parse_problems


def test_bad_intent_yaml():
    text = """```intent
intent: "broken
writes []
```

```py
print(1)
```
"""
    cell = parse_cell(text)
    assert any(p.startswith("intent_yaml_unparseable") for p in cell.parse_problems)


def test_intent_block_validates_via_pydantic():
    # Missing required `intent` field
    text = """```intent
writes: ["a.txt"]
network: []
```

```py
print(1)
```
"""
    cell = parse_cell(text)
    assert any(p.startswith("intent_schema_error") for p in cell.parse_problems)


# =============================================================================
# AST analysis
# =============================================================================


def test_ast_finds_simple_write():
    f = analyze('Write("foo.txt", "hi")')
    assert f.syntax_ok
    assert ("Write", "foo.txt") in f.write_calls


def test_ast_finds_open_write_mode():
    f = analyze('open("out.txt", "w").write("x")')
    assert f.syntax_ok
    assert ("open", "out.txt") in f.write_calls


def test_ast_ignores_open_read_mode():
    f = analyze('open("in.txt").read()')
    assert f.syntax_ok
    assert f.write_calls == []


def test_ast_resolves_variable_for_open():
    """The 'csv_path' regression from Day 0 — must NOT report variable name."""
    f = analyze('csv_path = "./out/loc.csv"\nopen(csv_path, "w").write("x")')
    assert f.syntax_ok
    targets = [t for (_, t) in f.write_calls]
    assert "./out/loc.csv" in targets
    assert "csv_path" not in targets


def test_ast_finds_path_rename():
    f = analyze('from pathlib import Path\nPath("a.txt").rename("b.txt")')
    assert f.syntax_ok
    assert any(t == "b.txt" for (_, t) in f.write_calls)


def test_ast_finds_requests_get():
    f = analyze('import requests\nrequests.get("https://api.github.com/repos/x/y")')
    assert f.syntax_ok
    hosts = [h for (_, h) in f.net_calls]
    assert "https://api.github.com/repos/x/y" in hosts


def test_ast_flags_eval():
    f = analyze('eval("1+1")')
    assert "eval" in f.dynamic_code


def test_ast_flags_exec():
    f = analyze('exec("x=1")')
    assert "exec" in f.dynamic_code


def test_ast_flags_dunder_import():
    f = analyze('m = __import__("os")')
    assert "__import__" in f.dynamic_code


def test_ast_handles_syntax_error():
    f = analyze("def x(:\n  pass")
    assert not f.syntax_ok
    assert f.syntax_error is not None


def test_ast_finds_bash_curl():
    f = analyze('Bash("curl https://example.com/x")')
    assert any("example.com" in h for (_, h) in f.net_calls)


def test_ast_finds_skill_imports():
    f = analyze("from skills.pdf_extract import extract")
    assert "pdf_extract" in f.used_skills


# =============================================================================
# Full gate decisions
# =============================================================================


def _wrap(code: str, intent: str = "test", writes=None, network=None) -> str:
    """Build a canonical cell with the given declarations."""
    import yaml as _yaml
    intent_yaml = _yaml.safe_dump({
        "intent": intent,
        "writes": writes or [],
        "network": network or [],
        "reversible": True,
    }, default_flow_style=False).strip()
    return f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"


def test_gate_allows_honest_cell():
    text = _wrap('print(1+1)', writes=[], network=[])
    d = check(text)
    assert d.action == GateAction.ALLOW
    assert not d.reasons


def test_gate_denies_unparseable():
    text = "```py\nprint(1)\n```"  # no intent fence
    d = check(text)
    assert d.action == GateAction.DENY


def test_gate_confirms_undeclared_write():
    text = _wrap('Write("foo.txt", "x")', writes=[])  # lying!
    d = check(text)
    assert d.action == GateAction.CONFIRM
    assert any("undeclared_writes" in r for r in d.reasons)


def test_gate_confirms_undeclared_network():
    text = _wrap('import requests; requests.get("https://evil.com")', network=[])
    d = check(text)
    assert d.action == GateAction.CONFIRM
    assert any("undeclared_network" in r for r in d.reasons)


def test_gate_allows_declared_write():
    text = _wrap('Write("foo.txt", "x")', writes=["foo.txt"])
    d = check(text)
    assert d.action == GateAction.ALLOW


def test_gate_allows_glob_declaration():
    """Day 0 finding: `./data/*.txt` covers Path('data/x.txt').rename(...)."""
    text = _wrap(
        'from pathlib import Path\nPath("data/x.txt").rename("data/x.md")',
        writes=["./data/*.txt", "./data/*.md"],
    )
    d = check(text)
    assert d.action == GateAction.ALLOW


def test_gate_allows_overdeclared():
    """Overdeclaring (declaring more than the AST sees) is conservative — allow."""
    text = _wrap(
        'print("nothing happens")',
        writes=["./out/will-not-actually-write.txt"],
    )
    d = check(text)
    assert d.action == GateAction.ALLOW


def test_gate_confirms_eval():
    text = _wrap('eval("1+1")')
    d = check(text)
    assert d.action == GateAction.CONFIRM
    assert any("dynamic_code" in r for r in d.reasons)


def test_gate_returns_prose_only_for_no_fences():
    d = check("Just a prose reply, no code.")
    assert d.action == GateAction.ALLOW
    assert "prose_only" in d.reasons


def test_gate_handles_syntax_error_in_cell():
    text = _wrap("def x(:\n  pass")
    d = check(text)
    assert d.action == GateAction.DENY
    assert any("syntax_error" in r for r in d.reasons)


# =============================================================================
# Extended gate detections (post-shake-out)
# =============================================================================


def test_ast_folds_binop_string_concat():
    """`"rm" + " -rf " + "/"` resolves to "rm -rf /" — was a Day-0 blind spot."""
    f = analyze('Bash("rm" + " -rf " + "/")')
    assert f.bash_calls == ["rm -rf /"]


def test_ast_captures_subprocess_run_string():
    f = analyze('import subprocess; subprocess.run("echo hi", shell=True)')
    assert any("echo hi" in c for c in f.bash_calls)


def test_ast_captures_subprocess_run_list():
    f = analyze('import subprocess; subprocess.run(["cp", "/etc/passwd", "/tmp/x"])')
    assert any("cp /etc/passwd /tmp/x" in c for c in f.bash_calls)


def test_ast_captures_os_system():
    f = analyze('import os; os.system("sudo whoami")')
    assert any("sudo whoami" in c for c in f.bash_calls)


def test_ast_captures_os_unlink():
    f = analyze('import os; os.unlink("/tmp/x.txt")')
    assert ("os.unlink", "/tmp/x.txt") in f.write_calls


def test_ast_captures_path_unlink():
    f = analyze('from pathlib import Path; Path("/tmp/x.txt").unlink()')
    assert any(t == "/tmp/x.txt" for (_, t) in f.write_calls)


def test_ast_captures_shutil_rmtree():
    f = analyze('import shutil; shutil.rmtree("/tmp/junk")')
    assert ("shutil.rmtree", "/tmp/junk") in f.write_calls


def test_ast_resolves_transitive_aliases():
    """a = "x"; b = a; c = b — resolution should chain through to "x"."""
    f = analyze('a = "/tmp/x.txt"\nb = a\nc = b\nopen(c, "w")')
    assert ("open", "/tmp/x.txt") in f.write_calls


def test_gate_drops_bare_name_lenience():
    """An unresolved Name written-to is now an undeclared write, not lenient pass."""
    text = _wrap('open(some_var_we_dont_know, "w")', writes=[])
    d = check(text)
    # We don't trust "some_var_we_dont_know" as if it matched anything.
    assert d.action == GateAction.CONFIRM


def test_gate_flags_undeclared_unlink():
    """Deletion is a write — must be declared."""
    text = _wrap('import os; os.unlink("/tmp/x")', writes=[])
    d = check(text)
    assert d.action == GateAction.CONFIRM
    assert any("undeclared_writes" in r for r in d.reasons)


def test_gate_allows_honest_unlink():
    text = _wrap('import os; os.unlink("/tmp/x")', writes=["/tmp/x"])
    d = check(text)
    assert d.action == GateAction.ALLOW
