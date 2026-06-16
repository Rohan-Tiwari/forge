"""Tests for forge.skills — SKILL.md folder discovery + parsing."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from forge.skills import SkillRegistry, load_skill, parse_skill_md


def _make_skill(root: Path, name: str, body: str = "Body content here.", **fm_extra) -> Path:
    """Create a minimal skill folder."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    fm = {"name": name, "description": f"A skill named {name} for testing."}
    fm.update(fm_extra)
    import yaml as _yaml
    skill_md = "---\n" + _yaml.safe_dump(fm) + "---\n" + body
    (folder / "SKILL.md").write_text(skill_md)
    return folder


def test_parse_minimal_skill_md():
    text = textwrap.dedent("""\
        ---
        name: test
        description: a test skill
        ---
        # body
        """)
    fm, body = parse_skill_md(text)
    assert fm.name == "test"
    assert fm.description == "a test skill"
    assert "# body" in body


def test_parse_rejects_no_frontmatter():
    with pytest.raises(ValueError):
        parse_skill_md("# just markdown\n")


def test_parse_rejects_missing_name():
    text = "---\ndescription: x\n---\nbody"
    with pytest.raises(ValueError):
        parse_skill_md(text)


def test_load_skill(tmp_path):
    folder = _make_skill(tmp_path, "demo")
    s = load_skill(folder)
    assert s.name == "demo"
    assert s.description.startswith("A skill")
    assert s.helpers_path is None


def test_helpers_path_detected(tmp_path):
    folder = _make_skill(tmp_path, "with-helpers")
    (folder / "helpers.py").write_text("def main():\n    return 'ok'\n")
    s = load_skill(folder)
    assert s.helpers_path is not None
    assert s.helpers_path.name == "helpers.py"


def test_registry_scans_subdirs(tmp_path):
    _make_skill(tmp_path, "alpha")
    _make_skill(tmp_path, "beta")
    reg = SkillRegistry.scan(roots=[tmp_path])
    names = sorted(s.name for s in reg.skills)
    assert "alpha" in names
    assert "beta" in names


def test_registry_dedups_by_name(tmp_path):
    _make_skill(tmp_path / "first", "shared")
    _make_skill(tmp_path / "second", "shared")
    reg = SkillRegistry.scan(roots=[tmp_path])
    names = [s.name for s in reg.skills]
    assert names.count("shared") == 1


def test_find_returns_relevant_skills(tmp_path):
    _make_skill(tmp_path, "pdf-extract", body="extract PDF content")
    _make_skill(tmp_path, "git-tidy", body="clean up git branches")
    reg = SkillRegistry.scan(roots=[tmp_path])
    results = reg.find("PDF document")
    assert results
    assert results[0]["name"] == "pdf-extract"


def test_find_returns_empty_for_no_match(tmp_path):
    _make_skill(tmp_path, "alpha")
    reg = SkillRegistry.scan(roots=[tmp_path])
    assert reg.find("totally unrelated query") == []


def test_render_for_system_prompt(tmp_path):
    _make_skill(tmp_path, "alpha")
    _make_skill(tmp_path, "beta")
    reg = SkillRegistry.scan(roots=[tmp_path])
    rendered = reg.render_for_system_prompt()
    assert "alpha" in rendered
    assert "beta" in rendered
    assert "find_skill" in rendered  # the discovery hint
