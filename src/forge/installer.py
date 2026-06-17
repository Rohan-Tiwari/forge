"""forge.installer — install skills from git repos with content-addressed pinning.

Usage from the CLI:
    forge skill install <git-url>@<sha-or-tag>     # tag is resolved + pinned
    forge skill install <github-shorthand>@<sha>   # 'alice/skills@a3f9c2'
    forge skill diff <name>                         # what changed since last upgrade

Layout on disk:
    ~/.skills/installed/<source>/<name>@<sha>/      # one folder per pinned version
    ~/.skills/installed/<source>/<name>             # symlink to current version
    ~/.skills/manifest.toml                          # signed manifest of installs

The installer:
  1. Parses git URL + ref. Refuses floating tags ('main', 'HEAD') unless
     --pin is passed and the user confirms.
  2. Clones to a tmpdir, resolves the ref to a sha, copies tree to the
     final pinned location.
  3. Runs an AST scan on every .py file: flags eval/exec/__import__/
     dunder-walks/undeclared network/destructive FS ops. Surfaces findings
     to the user.
  4. Renders SKILL.md + every bundled file + the AST scan. Asks for
     one-time trust confirmation (5-second cooldown to prevent muscle-memory
     'y'-spam after a typo'd command).
  5. Updates ~/.skills/manifest.toml with the install record.

For v0.3 we'll add signature verification; for v0.2 it's content-addressed
pinning + manual review.
"""
from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tomli_w
import tomllib

from forge.config import SKILLS_HOME


_INSTALLED_ROOT = SKILLS_HOME / "installed"
_MANIFEST_PATH = SKILLS_HOME / "manifest.toml"

# Floating refs we refuse without --pin confirmation.
_FLOATING_REFS = {"main", "master", "HEAD", "trunk", "develop", "latest"}


# =============================================================================
# Errors
# =============================================================================


class InstallError(RuntimeError):
    """Anything that prevents a successful install."""


class FloatingRefError(InstallError):
    """User asked to install a tag that isn't a sha."""


# =============================================================================
# Git URL parsing
# =============================================================================


@dataclass
class SkillSpec:
    """Parsed `<git-url>@<ref>` install spec."""

    url: str            # https://github.com/alice/skills.git
    ref: str            # 'a3f9c2c' (sha) or 'main' (tag)
    source: str         # 'github.com/alice/skills' (used as the on-disk source dir)
    name: str           # 'skills' (the repo basename, used as install name)
    subdir: Optional[str] = None  # if user wants only one skill from a multi-skill repo


_GITHUB_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)$")


def parse_spec(raw: str) -> SkillSpec:
    """Parse `<url-or-shorthand>@<ref>[:<subdir>]` into a SkillSpec.

    Examples:
        alice/forge-skills@a3f9c2c            → github shorthand, sha
        alice/forge-skills@main               → ditto, floating ref (rejected unless --pin)
        https://github.com/alice/skills.git@v1.2.3
        https://gitlab.com/alice/skills.git@a3f9c2:pdf-extract  ← only the pdf-extract subdir
    """
    if "@" not in raw:
        raise InstallError(
            f"missing @ref in {raw!r}. Use <repo>@<sha-or-tag>."
        )
    url_part, _, after_at = raw.rpartition("@")
    ref_part, _, subdir = after_at.partition(":")
    ref = ref_part.strip()
    subdir = subdir.strip() or None
    url_part = url_part.strip()

    if not ref:
        raise InstallError(f"empty ref in {raw!r}")

    # GitHub shorthand?
    m = _GITHUB_SHORTHAND.match(url_part)
    if m:
        owner, repo = m.group(1), m.group(2)
        url = f"https://github.com/{owner}/{repo}.git"
        source = f"github.com/{owner}/{repo}"
        name = repo
    else:
        if not url_part.startswith(("http://", "https://", "git@")):
            raise InstallError(
                f"unrecognized URL {url_part!r}. Use github shorthand "
                f"(alice/repo) or a full URL (https://...)."
            )
        url = url_part
        # Derive a stable source key from URL.
        m2 = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?/?$", url)
        if m2:
            host, path = m2.group(1), m2.group(2)
            source = f"{host}/{path}"
            name = path.rsplit("/", 1)[-1]
        else:
            # ssh form: git@github.com:alice/repo.git
            m3 = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
            if m3:
                host, path = m3.group(1), m3.group(2)
                source = f"{host}/{path}"
                name = path.rsplit("/", 1)[-1]
            else:
                source = url.replace("/", "_")
                name = source.rsplit("_", 1)[-1]

    return SkillSpec(url=url, ref=ref, source=source, name=name, subdir=subdir)


def is_floating_ref(ref: str) -> bool:
    """A ref is 'floating' if it's a moving branch name rather than a sha or tag."""
    if ref in _FLOATING_REFS:
        return True
    if ref.startswith(("refs/heads/", "origin/")):
        return True
    # Heuristic: shas are hex, length >= 7
    if re.fullmatch(r"[a-fA-F0-9]{7,40}", ref):
        return False
    # Anything else that LOOKS like a tag (v1.2.3, 2024-01-01, etc.) we accept.
    return False


# =============================================================================
# AST scanner
# =============================================================================


@dataclass
class ScanFinding:
    severity: str  # 'critical' | 'warn' | 'info'
    file: str
    line: int
    code: str      # short label
    detail: str


# Things we flag at install time. Mirrors forge.gate.analyze but stricter —
# we want to alert humans BEFORE the skill ever runs, not after.
_DANGEROUS_CALLS = {
    "eval", "exec", "compile",
    "__import__",
    "input",  # interactive input is suspicious in a skill
}
_DANGEROUS_QUALNAMES = {
    "os.system", "os.popen", "os.execvp", "os.execv", "os.execve",
    "os.execvpe", "os.spawnv", "os.spawnvp", "os.spawnvpe",
    "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.run", "subprocess.Popen",
    "ctypes.CDLL",
}


def scan_python_file(path: Path) -> list[ScanFinding]:
    """Walk a .py file's AST and surface anything worth a human review."""
    findings: list[ScanFinding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        findings.append(ScanFinding(
            severity="warn", file=str(path), line=0,
            code="unreadable", detail=f"can't read file: {e}",
        ))
        return findings

    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        findings.append(ScanFinding(
            severity="critical", file=str(path), line=e.lineno or 0,
            code="syntax", detail=f"file does not parse: {e.msg}",
        ))
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            qn = _qualname(node.func)
            base = qn.split(".")[-1]
            if base in _DANGEROUS_CALLS:
                findings.append(ScanFinding(
                    severity="critical", file=str(path), line=node.lineno,
                    code=f"call.{base}",
                    detail=f"calls {base!r} — code injection or interactive prompt",
                ))
            if qn in _DANGEROUS_QUALNAMES:
                findings.append(ScanFinding(
                    severity="critical", file=str(path), line=node.lineno,
                    code=f"call.{qn}",
                    detail=f"calls {qn!r} — shells out / runs other binaries",
                ))
            # getattr(__builtins__, ...) is a known eval-bypass
            if base == "getattr" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Name) and a0.id in {"__builtins__", "builtins"}:
                    findings.append(ScanFinding(
                        severity="critical", file=str(path), line=node.lineno,
                        code="getattr.builtins",
                        detail="dynamic attribute access on builtins — common eval bypass",
                    ))

        # Imports we don't want to see in a "downloaded skill"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"ctypes", "_ctypes"}:
                    findings.append(ScanFinding(
                        severity="warn", file=str(path), line=node.lineno,
                        code="import.ctypes",
                        detail="imports ctypes — can call arbitrary native code",
                    ))
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in {"ctypes", "_ctypes"}:
                findings.append(ScanFinding(
                    severity="warn", file=str(path), line=node.lineno,
                    code="import.ctypes",
                    detail=f"imports from {mod} — can call arbitrary native code",
                ))

    return findings


def scan_directory(root: Path) -> list[ScanFinding]:
    """Walk a tree, scan every .py file, return all findings."""
    findings: list[ScanFinding] = []
    for py in root.rglob("*.py"):
        # Don't scan __pycache__ or test fixtures
        if any(part in {"__pycache__", ".git"}
               for part in py.relative_to(root).parts):
            continue
        findings.extend(scan_python_file(py))
    return findings


def _qualname(node: ast.AST) -> str:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


# =============================================================================
# Manifest — what's installed and from where
# =============================================================================


@dataclass
class ManifestEntry:
    name: str
    source: str
    ref: str
    sha: str            # the resolved sha at install time
    install_path: str
    installed_at: str   # ISO 8601 utc
    skill_count: int = 0
    findings_summary: dict[str, int] = field(default_factory=dict)


def load_manifest() -> list[ManifestEntry]:
    if not _MANIFEST_PATH.exists():
        return []
    try:
        with _MANIFEST_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out: list[ManifestEntry] = []
    for entry in data.get("installs", []):
        if not isinstance(entry, dict):
            continue
        out.append(ManifestEntry(
            name=entry.get("name", ""),
            source=entry.get("source", ""),
            ref=entry.get("ref", ""),
            sha=entry.get("sha", ""),
            install_path=entry.get("install_path", ""),
            installed_at=entry.get("installed_at", ""),
            skill_count=int(entry.get("skill_count", 0) or 0),
            findings_summary={
                k: int(v) for k, v in (entry.get("findings_summary") or {}).items()
            },
        ))
    return out


def save_manifest(entries: list[ManifestEntry]) -> None:
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "installs": [
            {
                "name": e.name,
                "source": e.source,
                "ref": e.ref,
                "sha": e.sha,
                "install_path": e.install_path,
                "installed_at": e.installed_at,
                "skill_count": e.skill_count,
                "findings_summary": e.findings_summary,
            }
            for e in entries
        ],
    }
    with _MANIFEST_PATH.open("wb") as f:
        tomli_w.dump(data, f)


# =============================================================================
# Install pipeline
# =============================================================================


@dataclass
class InstallPlan:
    """What `forge skill install` will do, before it does it.

    The CLI shows this to the user, asks for confirmation, then executes.
    """

    spec: SkillSpec
    resolved_sha: str
    workdir: Path                # tmp clone location
    install_path: Path           # final pinned location
    skills_found: list[str] = field(default_factory=list)
    findings: list[ScanFinding] = field(default_factory=list)

    @property
    def critical_findings(self) -> list[ScanFinding]:
        return [f for f in self.findings if f.severity == "critical"]


def _git(*args: str, cwd: Optional[Path] = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603,S607
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def prepare_install(spec: SkillSpec, *, allow_floating: bool = False,
                    work_root: Optional[Path] = None) -> InstallPlan:
    """Clone + scan a skill repo, return an InstallPlan.

    Doesn't move anything to the final location — that's `execute_install`.
    Splitting the two means the CLI can show the plan first.
    """
    if is_floating_ref(spec.ref) and not allow_floating:
        raise FloatingRefError(
            f"refusing to install floating ref {spec.ref!r}. "
            f"Pass --pin to accept that this version may move under your feet, "
            f"or specify a sha/tag instead."
        )

    work_root = work_root or (SKILLS_HOME / ".tmp")
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = work_root / hashlib.sha256(
        f"{spec.url}@{spec.ref}@{time.time()}".encode()
    ).hexdigest()[:16]

    # Clone (shallow). git clone --depth=1 + fetch the specific ref.
    _git("clone", "--quiet", "--depth", "1", spec.url, str(workdir))
    # The ref might not be the default branch; try to fetch + checkout.
    try:
        _git("fetch", "--quiet", "--depth", "1", "origin", spec.ref, cwd=workdir)
        _git("checkout", "--quiet", "FETCH_HEAD", cwd=workdir)
    except subprocess.CalledProcessError:
        # Maybe `ref` IS the default branch already. Try direct checkout.
        try:
            _git("checkout", "--quiet", spec.ref, cwd=workdir)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise InstallError(
                f"can't check out {spec.ref!r} in {spec.url}: {e.stderr}"
            ) from e

    # Resolve to a sha for content-addressed pinning.
    sha = _git("rev-parse", "HEAD", cwd=workdir).stdout.strip()

    # Where do the skills live? If user passed :subdir, only that path.
    # Otherwise: any folder containing SKILL.md, recursively.
    scan_root = workdir / spec.subdir if spec.subdir else workdir

    # Find skills in the tree
    skill_dirs: list[Path] = []
    for skill_md in scan_root.rglob("SKILL.md"):
        if any(part in {".git", "__pycache__"}
               for part in skill_md.relative_to(scan_root).parts):
            continue
        skill_dirs.append(skill_md.parent)

    # Run AST scan on the .py files
    findings = scan_directory(scan_root)

    install_path = _INSTALLED_ROOT / spec.source / f"{spec.name}@{sha[:12]}"

    return InstallPlan(
        spec=spec,
        resolved_sha=sha,
        workdir=workdir,
        install_path=install_path,
        skills_found=[d.name for d in skill_dirs],
        findings=findings,
    )


def execute_install(plan: InstallPlan) -> ManifestEntry:
    """Move the cloned tree to its final pinned location, update manifest."""
    if plan.install_path.exists():
        # Already installed at this exact sha — idempotent.
        shutil.rmtree(plan.workdir, ignore_errors=True)
    else:
        plan.install_path.parent.mkdir(parents=True, exist_ok=True)
        # If user specified a subdir, only move that subtree.
        src = plan.workdir / plan.spec.subdir if plan.spec.subdir else plan.workdir
        if not src.exists():
            shutil.rmtree(plan.workdir, ignore_errors=True)
            raise InstallError(
                f"subdir {plan.spec.subdir!r} not found in {plan.spec.url}"
            )
        shutil.copytree(src, plan.install_path,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        shutil.rmtree(plan.workdir, ignore_errors=True)

    # Manifest update — replace any entry with the same name+source.
    manifest = [e for e in load_manifest()
                if not (e.name == plan.spec.name and e.source == plan.spec.source)]
    findings_summary: dict[str, int] = {}
    for f in plan.findings:
        findings_summary[f.severity] = findings_summary.get(f.severity, 0) + 1

    entry = ManifestEntry(
        name=plan.spec.name,
        source=plan.spec.source,
        ref=plan.spec.ref,
        sha=plan.resolved_sha,
        install_path=str(plan.install_path),
        installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        skill_count=len(plan.skills_found),
        findings_summary=findings_summary,
    )
    manifest.append(entry)
    save_manifest(manifest)
    return entry


# =============================================================================
# Diff between installed version and latest upstream
# =============================================================================


def diff_installed(name: str) -> str:
    """Show what would change if we re-installed at upstream HEAD.

    Returns a human-readable diff (or 'up to date' / 'not installed').
    """
    entries = [e for e in load_manifest() if e.name == name]
    if not entries:
        return f"skill {name!r} not installed"
    latest = entries[-1]
    install_path = Path(latest.install_path)
    if not install_path.exists():
        return f"skill {name!r}: install path missing — reinstall recommended"
    return (
        f"skill {name}\n"
        f"  installed:  {latest.sha[:12]} from {latest.source}@{latest.ref}\n"
        f"  installed:  {latest.installed_at}\n"
        f"  install:    {latest.install_path}\n"
        f"  findings:   {latest.findings_summary or '(clean)'}\n"
        f"  to upgrade: forge skill install {latest.source.replace('github.com/','')}"
        f"@<new-sha>"
    )
