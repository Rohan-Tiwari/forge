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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from forge.config import SKILLS_HOME

_INSTALLED_ROOT = SKILLS_HOME / "installed"
_MANIFEST_PATH = SKILLS_HOME / "manifest.toml"

# Floating refs we refuse without --pin confirmation.
_FLOATING_REFS = {"main", "master", "HEAD", "trunk", "develop", "latest"}

# Valid git ref name pattern. We additionally refuse leading '-' since
# git interprets that as an option flag, allowing argument injection
# (e.g. '--upload-pack=/path/evil' as a "ref" would execute arbitrary
# binaries during fetch). See v0.2.1 audit finding #2.
_VALID_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _is_safe_ref(ref: str) -> bool:
    """A git ref is 'safe' for argv-positional use IFF:

    1. Matches our restrictive character set ([A-Za-z0-9._/-])
    2. Does NOT start with '-' (would be parsed as an option flag)
    3. Does NOT contain '..' (path traversal in some git ref semantics)
    """
    if not ref:
        return False
    if ref.startswith("-"):
        return False
    if ".." in ref:
        return False
    if not _VALID_REF_RE.match(ref):
        return False
    return True


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
    subdir: str | None = None  # if user wants only one skill from a multi-skill repo


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


def _git(*args: str, cwd: Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603,S607
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def prepare_install(spec: SkillSpec, *, allow_floating: bool = False,
                    work_root: Path | None = None) -> InstallPlan:
    """Clone + scan a skill repo, return an InstallPlan.

    Doesn't move anything to the final location — that's `execute_install`.
    Splitting the two means the CLI can show the plan first.
    """
    # SECURITY: validate the ref BEFORE invoking git. An attacker-controlled
    # ref like '--upload-pack=/path/evil' would be parsed by git as an
    # option flag, allowing arbitrary local binary execution during fetch.
    # See v0.2.1 audit finding #2.
    if not _is_safe_ref(spec.ref):
        raise InstallError(
            f"refusing to use unsafe git ref {spec.ref!r}. "
            f"Refs must match [A-Za-z0-9._/-]+ and may not start with '-' "
            f"or contain '..'."
        )

    if is_floating_ref(spec.ref) and not allow_floating:
        raise FloatingRefError(
            f"refusing to install floating ref {spec.ref!r}. "
            f"Pass --pin to accept that this version may move under your feet, "
            f"or specify a sha/tag instead."
        )

    # SECURITY: validate subdir if present. Resolving to a path outside the
    # cloned workdir would let an attacker reference any file on disk
    # (e.g. subdir='../../etc/passwd').
    if spec.subdir:
        if ".." in Path(spec.subdir).parts:
            raise InstallError(
                f"refusing subdir with '..' component: {spec.subdir!r}"
            )

    work_root = work_root or (SKILLS_HOME / ".tmp")
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = work_root / hashlib.sha256(
        f"{spec.url}@{spec.ref}@{time.time()}".encode()
    ).hexdigest()[:16]

    # Clone (shallow). git clone --depth=1 + fetch the specific ref.
    # SECURITY: use a positional separator '--' so even a future regression
    # can't reinterpret the URL as a flag. And use isolated git config to
    # prevent the user's global config (smudge filters, core.hooksPath)
    # from running attacker-controlled code during the clone.
    isolated_env = _isolated_git_env()
    subprocess.run(  # noqa: S603,S607
        ["git", "clone", "--quiet", "--depth", "1", "--", spec.url, str(workdir)],
        env=isolated_env, capture_output=True, text=True, check=True,
    )
    # The ref might not be the default branch; try to fetch + checkout.
    try:
        # SECURITY: '--' separator forces git to treat spec.ref as a positional
        # argument (refspec), not an option. Already validated above too,
        # but defense-in-depth.
        subprocess.run(  # noqa: S603,S607
            ["git", "fetch", "--quiet", "--depth", "1", "origin", "--", spec.ref],
            cwd=workdir, env=isolated_env, capture_output=True, text=True, check=True,
        )
        subprocess.run(  # noqa: S603,S607
            ["git", "checkout", "--quiet", "FETCH_HEAD"],
            cwd=workdir, env=isolated_env, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        # Maybe `ref` IS the default branch already. Try direct checkout.
        try:
            subprocess.run(  # noqa: S603,S607
                ["git", "checkout", "--quiet", "--", spec.ref],
                cwd=workdir, env=isolated_env, capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise InstallError(
                f"can't check out {spec.ref!r} in {spec.url}: {e.stderr}"
            ) from e

    # Resolve to a sha for content-addressed pinning.
    sha_proc = subprocess.run(  # noqa: S603,S607
        ["git", "rev-parse", "HEAD"],
        cwd=workdir, env=isolated_env, capture_output=True, text=True, check=True,
    )
    sha = sha_proc.stdout.strip()

    # Where do the skills live? If user passed :subdir, only that path.
    # SECURITY: resolve and verify the subdir stays inside the workdir
    # (defense-in-depth — we already rejected '..' but realpath catches
    # symlink-based escapes too).
    if spec.subdir:
        scan_root = (workdir / spec.subdir).resolve()
        workdir_real = workdir.resolve()
        try:
            scan_root.relative_to(workdir_real)
        except ValueError:
            shutil.rmtree(workdir, ignore_errors=True)
            raise InstallError(
                f"subdir {spec.subdir!r} resolves outside the clone "
                f"({scan_root} not under {workdir_real})"
            )
    else:
        scan_root = workdir

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


def _isolated_git_env() -> dict[str, str]:
    """Return an env that neutralizes the user's git config during a clone.

    SECURITY: by default, a git clone runs the user's globally-configured
    smudge filters, hooks, and credential helpers — any of which could
    execute attacker-controlled code from the cloned repo. We point git
    at empty config files to prevent that.
    """
    from forge._subprocess_env import build_minimal_env
    env = build_minimal_env(
        # Git wants to know the user's identity for some operations even
        # without committing. Provide a no-op identity.
        extra={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",  # never prompt for credentials
            "GIT_AUTHOR_NAME": "forge",
            "GIT_AUTHOR_EMAIL": "forge@localhost",
            "GIT_COMMITTER_NAME": "forge",
            "GIT_COMMITTER_EMAIL": "forge@localhost",
        },
    )
    return env


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


# =============================================================================
# Search — query GitHub for repos tagged forge-skill
# =============================================================================


def search_skills(query: str, *, limit: int = 10) -> dict[str, Any]:
    """Search GitHub for repos with the `forge-skill` topic.

    Returns a dict:
        {
            "results": [{name, owner, full_name, description, stars, url, updated}, ...],
            "rate_limited": bool,
            "rate_limit_remaining": int | None,
        }

    No GitHub auth required for unauthenticated 60 req/hr.
    Users behind corporate proxies who hit rate limits can set GITHUB_TOKEN
    to upgrade to 5000/hr.
    """
    import json
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    q = (query.strip() + " topic:forge-skill" if query.strip()
         else "topic:forge-skill")
    url = (
        "https://api.github.com/search/repositories?"
        f"q={urllib.parse.quote_plus(q)}&sort=stars&order=desc&per_page={limit}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "forge-skill-search",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            headers = dict(resp.getheaders())
    except urllib.error.HTTPError as e:
        # 403/429 with rate-limit headers means we're throttled, not "no results"
        try:
            hdrs = dict(e.headers or {})
            remaining_str = hdrs.get("X-RateLimit-Remaining", "")
            remaining = int(remaining_str) if remaining_str.isdigit() else None
            return {
                "results": [],
                "rate_limited": e.code in (403, 429) and remaining == 0,
                "rate_limit_remaining": remaining,
            }
        except (AttributeError, ValueError):
            return {"results": [], "rate_limited": False,
                    "rate_limit_remaining": None}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {"results": [], "rate_limited": False,
                "rate_limit_remaining": None}

    remaining_str = headers.get("X-RateLimit-Remaining", "")
    remaining = int(remaining_str) if remaining_str.isdigit() else None

    results: list[dict[str, str]] = []
    for item in (data.get("items") or [])[:limit]:
        results.append({
            "full_name": str(item.get("full_name", "")),
            "owner": str(item.get("owner", {}).get("login", "")),
            "name": str(item.get("name", "")),
            "description": (str(item.get("description") or "")[:160]),
            "stars": str(item.get("stargazers_count", 0)),
            "url": str(item.get("html_url", "")),
            "updated": str(item.get("updated_at", ""))[:10],
            "default_branch": str(item.get("default_branch") or "main"),
        })
    return {
        "results": results,
        "rate_limited": False,
        "rate_limit_remaining": remaining,
    }


def latest_sha(owner: str, repo: str, ref: str = "HEAD") -> str | None:
    """Look up the latest sha for a ref. Used by `forge skill update` to
    discover whether an upgrade exists without needing a local clone.

    Returns None on any failure — caller uses that as 'don't know,
    user must specify sha'.
    """
    import json
    import os
    import urllib.error
    import urllib.request

    if ref == "HEAD":
        # Use the default branch endpoint
        url = f"https://api.github.com/repos/{owner}/{repo}/branches/HEAD"
        # ↑ HEAD isn't a branch; we'd need to look up default_branch first.
        # Easier: get the repo metadata.
        meta_url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            req = urllib.request.Request(
                meta_url,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "forge-skill-update"},
            )
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                meta = json.load(resp)
            ref = meta.get("default_branch", "main")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
            return None

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "forge-skill-update"},
        )
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return data.get("sha")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
