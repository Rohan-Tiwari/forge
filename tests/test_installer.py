"""Tests for forge.installer — skill install pipeline.

Covers spec parsing, AST scanner, manifest persistence, and full
install flow against a real tiny local git repo (built on the fly).
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from forge.installer import (
    FloatingRefError,
    InstallError,
    ManifestEntry,
    SkillSpec,
    diff_installed,
    execute_install,
    is_floating_ref,
    load_manifest,
    parse_spec,
    prepare_install,
    save_manifest,
    scan_directory,
    scan_python_file,
)

# =============================================================================
# Spec parsing
# =============================================================================


class TestParseSpec:
    def test_github_shorthand_with_sha(self):
        s = parse_spec("alice/skills@a3f9c2c")
        assert s.url == "https://github.com/alice/skills.git"
        assert s.ref == "a3f9c2c"
        assert s.source == "github.com/alice/skills"
        assert s.name == "skills"
        assert s.subdir is None

    def test_github_shorthand_with_floating_ref(self):
        s = parse_spec("alice/skills@main")
        assert s.ref == "main"

    def test_full_https_url(self):
        s = parse_spec("https://github.com/alice/skills.git@v1.2.3")
        assert s.url == "https://github.com/alice/skills.git"
        assert s.ref == "v1.2.3"
        assert s.source == "github.com/alice/skills"

    def test_with_subdir(self):
        s = parse_spec("alice/skills@a3f9c2:pdf-extract")
        assert s.ref == "a3f9c2"
        assert s.subdir == "pdf-extract"

    def test_missing_ref_raises(self):
        with pytest.raises(InstallError, match="missing @ref"):
            parse_spec("alice/skills")

    def test_empty_ref_raises(self):
        with pytest.raises(InstallError, match="empty ref"):
            parse_spec("alice/skills@")

    def test_unrecognized_url_raises(self):
        with pytest.raises(InstallError, match="unrecognized URL"):
            parse_spec("not-a-url@abc")

    def test_ssh_url(self):
        s = parse_spec("git@github.com:alice/skills.git@a3f9c2")
        assert s.source == "github.com/alice/skills"
        assert s.name == "skills"


class TestIsFloatingRef:
    @pytest.mark.parametrize("ref", ["main", "master", "HEAD", "develop", "trunk", "latest"])
    def test_known_floating(self, ref):
        assert is_floating_ref(ref)

    @pytest.mark.parametrize("ref", [
        "a3f9c2c",       # short sha
        "abcdef0123456789abcdef0123456789abcdef01",  # full sha
        "v1.2.3",        # version tag
        "2024-01-01",    # date tag
    ])
    def test_pinned_refs(self, ref):
        assert not is_floating_ref(ref)

    def test_origin_branch_is_floating(self):
        assert is_floating_ref("origin/main")


# =============================================================================
# AST scanner
# =============================================================================


class TestScanner:
    def test_clean_file_no_findings(self, tmp_path):
        p = tmp_path / "clean.py"
        p.write_text("def add(a, b):\n    return a + b\n")
        assert scan_python_file(p) == []

    def test_eval_flagged_critical(self, tmp_path):
        p = tmp_path / "evil.py"
        p.write_text("def run(s):\n    return eval(s)\n")
        findings = scan_python_file(p)
        assert any(f.severity == "critical" and f.code == "call.eval" for f in findings)

    def test_exec_flagged_critical(self, tmp_path):
        p = tmp_path / "evil.py"
        p.write_text("exec('x=1')\n")
        findings = scan_python_file(p)
        assert any(f.code == "call.exec" for f in findings)

    def test_subprocess_run_flagged(self, tmp_path):
        p = tmp_path / "shell.py"
        p.write_text(
            "import subprocess\n"
            "subprocess.run('ls', shell=True)\n"
        )
        findings = scan_python_file(p)
        assert any("subprocess.run" in f.code for f in findings)

    def test_os_system_flagged(self, tmp_path):
        p = tmp_path / "shell.py"
        p.write_text("import os\nos.system('ls')\n")
        findings = scan_python_file(p)
        assert any("os.system" in f.code for f in findings)

    def test_getattr_builtins_bypass_flagged(self, tmp_path):
        """The classic eval-bypass: getattr(__builtins__, 'eval')."""
        p = tmp_path / "sneaky.py"
        p.write_text(
            "f = getattr(__builtins__, 'ev' + 'al')\n"
            "f('1+1')\n"
        )
        findings = scan_python_file(p)
        assert any(f.code == "getattr.builtins" for f in findings)

    def test_ctypes_import_flagged_warn(self, tmp_path):
        p = tmp_path / "native.py"
        p.write_text("import ctypes\n")
        findings = scan_python_file(p)
        assert any(f.severity == "warn" and "ctypes" in f.code for f in findings)

    def test_syntax_error_reported(self, tmp_path):
        p = tmp_path / "broken.py"
        p.write_text("def foo(:\n    pass\n")
        findings = scan_python_file(p)
        assert findings
        assert findings[0].code == "syntax"
        assert findings[0].severity == "critical"

    def test_unreadable_file(self, tmp_path):
        # Write binary bytes that don't decode as UTF-8
        p = tmp_path / "binary.py"
        p.write_bytes(b"\xff\xfe\x00not valid utf-8")
        findings = scan_python_file(p)
        # We should report SOMETHING (either syntax or unreadable),
        # not silently pretend it's clean.
        assert findings

    def test_scan_directory_walks_tree(self, tmp_path):
        (tmp_path / "ok.py").write_text("x = 1\n")
        (tmp_path / "bad.py").write_text("eval('1')\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "also_bad.py").write_text("exec('1')\n")
        # __pycache__ should be skipped
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "ignore.py").write_text("eval('ignored')\n")

        findings = scan_directory(tmp_path)
        files_with_findings = {f.file for f in findings}
        assert any("bad.py" in f for f in files_with_findings)
        assert any("also_bad.py" in f for f in files_with_findings)
        assert not any("__pycache__" in f for f in files_with_findings)


# =============================================================================
# Manifest persistence
# =============================================================================


class TestManifest:
    def test_load_missing_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr("forge.installer._MANIFEST_PATH", tmp_path / "missing.toml")
        assert load_manifest() == []

    def test_save_and_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr("forge.installer._MANIFEST_PATH", tmp_path / "manifest.toml")
        original = [
            ManifestEntry(
                name="alice-skills",
                source="github.com/alice/skills",
                ref="v1.0",
                sha="abc1234567890",
                install_path="/some/path",
                installed_at="2026-01-01T00:00:00Z",
                skill_count=3,
                findings_summary={"warn": 1},
            ),
        ]
        save_manifest(original)
        loaded = load_manifest()
        assert len(loaded) == 1
        assert loaded[0].name == "alice-skills"
        assert loaded[0].sha == "abc1234567890"
        assert loaded[0].skill_count == 3
        assert loaded[0].findings_summary == {"warn": 1}

    def test_load_corrupt_toml_returns_empty(self, monkeypatch, tmp_path):
        path = tmp_path / "manifest.toml"
        path.write_text("not = valid [toml")
        monkeypatch.setattr("forge.installer._MANIFEST_PATH", path)
        assert load_manifest() == []


# =============================================================================
# End-to-end: build a real local git repo, install from it, verify
# =============================================================================


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout


@pytest.fixture
def fake_skill_repo(tmp_path):
    """Build a real git repo on disk containing one skill, return (path, sha)."""
    repo = tmp_path / "fake-skills"
    repo.mkdir()

    # A clean skill
    skill = repo / "calc"
    skill.mkdir()
    (skill / "SKILL.md").write_text(textwrap.dedent("""
        ---
        name: calc
        description: A clean test skill.
        ---
        # calc skill
        Adds numbers.
    """).lstrip())
    (skill / "helpers.py").write_text(
        "def main(a, b):\n    return a + b\n"
    )

    # Configure git locally for the test (don't touch global config)
    _git("init", "--quiet", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@forge.local", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "initial", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).strip()

    return repo, sha


@pytest.fixture
def installer_dirs(monkeypatch, tmp_path):
    """Redirect SKILLS_HOME to a tmpdir so tests don't write into ~/.skills."""
    skills_home = tmp_path / "skills_home"
    skills_home.mkdir()
    monkeypatch.setattr("forge.installer.SKILLS_HOME", skills_home)
    monkeypatch.setattr("forge.installer._INSTALLED_ROOT",
                        skills_home / "installed")
    monkeypatch.setattr("forge.installer._MANIFEST_PATH",
                        skills_home / "manifest.toml")
    return skills_home


class TestInstallFlow:
    def test_install_pinned_sha_succeeds(self, fake_skill_repo, installer_dirs, tmp_path):
        repo, sha = fake_skill_repo
        spec = SkillSpec(
            url=f"file://{repo}",
            ref=sha,
            source="local/fake",
            name="fake",
        )
        plan = prepare_install(spec, work_root=tmp_path / "work")
        assert plan.resolved_sha == sha
        assert "calc" in plan.skills_found
        assert plan.critical_findings == []

        entry = execute_install(plan)
        assert entry.sha == sha
        assert Path(entry.install_path).exists()
        assert (Path(entry.install_path) / "calc" / "SKILL.md").exists()

    def test_floating_ref_rejected_without_pin(self, fake_skill_repo, installer_dirs):
        repo, _ = fake_skill_repo
        spec = SkillSpec(url=f"file://{repo}", ref="main",
                         source="local/fake", name="fake")
        with pytest.raises(FloatingRefError):
            prepare_install(spec)

    def test_floating_ref_accepted_with_pin(self, fake_skill_repo, installer_dirs, tmp_path):
        repo, _ = fake_skill_repo
        spec = SkillSpec(url=f"file://{repo}", ref="main",
                         source="local/fake", name="fake")
        plan = prepare_install(spec, allow_floating=True,
                               work_root=tmp_path / "work")
        assert plan.resolved_sha  # got resolved to an actual sha

    def test_install_with_dangerous_skill_surfaces_findings(
        self, tmp_path, installer_dirs,
    ):
        # Build a repo with a skill containing eval()
        repo = tmp_path / "evil-skills"
        repo.mkdir()
        skill = repo / "evil"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: evil\ndescription: bad.\n---\n# evil"
        )
        (skill / "helpers.py").write_text(
            "def main(s):\n    return eval(s)  # noqa\n"
        )
        _git("init", "--quiet", "-b", "main", cwd=repo)
        _git("config", "user.email", "t@t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git("config", "commit.gpgsign", "false", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "--quiet", "-m", "x", cwd=repo)
        sha = _git("rev-parse", "HEAD", cwd=repo).strip()

        spec = SkillSpec(url=f"file://{repo}", ref=sha,
                         source="local/evil", name="evil")
        plan = prepare_install(spec, work_root=tmp_path / "work")

        # Plan should report the critical finding — installation isn't blocked
        # at this layer (the CLI shows it to the user); the scanner's job is
        # to surface it.
        assert any(f.code == "call.eval" for f in plan.critical_findings)

    def test_invalid_ref_raises_install_error(self, fake_skill_repo, installer_dirs, tmp_path):
        repo, _ = fake_skill_repo
        spec = SkillSpec(url=f"file://{repo}", ref="ffffffffffff",
                         source="local/fake", name="fake")
        with pytest.raises(InstallError):
            prepare_install(spec, work_root=tmp_path / "work")


class TestDiff:
    def test_diff_not_installed(self, installer_dirs):
        msg = diff_installed("not-there")
        assert "not installed" in msg

    def test_diff_installed_shows_sha(self, monkeypatch, installer_dirs):
        # Manually populate the manifest
        save_manifest([
            ManifestEntry(
                name="x",
                source="github.com/a/x",
                ref="v1",
                sha="abc1234567890",
                install_path=str(installer_dirs / "fake"),
                installed_at="2026-01-01T00:00:00Z",
                skill_count=1,
            ),
        ])
        # The install_path doesn't have to exist for diff; the message
        # surfaces "missing" if it doesn't, but covers the metadata fields.
        (installer_dirs / "fake").mkdir()
        msg = diff_installed("x")
        assert "abc123456789" in msg
        assert "github.com/a/x" in msg
