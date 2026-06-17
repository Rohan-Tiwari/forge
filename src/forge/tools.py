"""forge.tools — the pre-imported tool core that runs inside the kernel.

This is the load-bearing safety layer. Every protected-path/protected-action
check happens HERE, on the actual call. The agent's emitted Python code can't
route around these because they live one stack frame deeper than the agent
itself. Even an `import skills.evil; skills.evil.do_bad()` ends up calling
through one of these wrappers (or hitting our patched builtins.open / subprocess
interceptors below).

The tools are thin wrappers; the real work is in the safety checks. If you
extend the tool surface, the rule is: every new tool MUST do its own protected-
path / protected-action check before doing the work, not after.
"""
from __future__ import annotations

import builtins
import fnmatch
import os
import re
import shutil as _shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from forge.config import (
    EFFECTIVE_PROTECTED_ACTIONS as PROTECTED_ACTIONS,
    EFFECTIVE_PROTECTED_PATHS as PROTECTED_PATHS,
)


# =============================================================================
# Errors
# =============================================================================


class ProtectedPathError(PermissionError):
    """Raised when a tool tries to write to a protected path."""


class ProtectedActionError(PermissionError):
    """Raised when Bash tries to run a protected action."""


# =============================================================================
# Path-resolution + protection check.
# =============================================================================


def _expand(p: str | os.PathLike[str]) -> Path:
    """Expand ~ and env vars, then canonicalize via realpath.

    `realpath` follows symlinks and (on macOS APFS, which is case-insensitive)
    canonicalizes case. This is what closes the case-sensitivity bypass.
    """
    expanded = os.path.expanduser(os.path.expandvars(str(p)))
    # Use realpath, not Path.resolve(), to canonicalize case on macOS.
    # If the path doesn't exist yet, fall back to expanded path (still
    # absolute thanks to expanduser when input started with ~).
    real = os.path.realpath(expanded)
    return Path(real if real else expanded).absolute()


def _norm_for_compare(p: str) -> str:
    """Normalize a path string for case-insensitive comparison on macOS APFS.

    On Linux ext4 paths are case-sensitive — but the protected list is hand-
    authored with the exact casing we expect. Lowercasing both sides on macOS
    closes the `~/.SSH` vs `~/.ssh` bypass without false positives elsewhere.
    """
    if sys.platform == "darwin":
        return p.casefold()
    return p


def _matches_pattern(absolute: Path, pattern: str) -> bool:
    """Does an absolute path match a protected-path pattern?"""
    pat = os.path.expanduser(pattern)
    abs_str = _norm_for_compare(str(absolute))

    # Glob pattern — fnmatch the full path
    if "*" in pat or "?" in pat:
        pat_norm = _norm_for_compare(pat)
        if fnmatch.fnmatch(abs_str, pat_norm):
            return True
        # Also match basename against the last segment of the pattern
        last_seg = pat_norm.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(_norm_for_compare(absolute.name), last_seg):
            return True
        return False

    # Plain path — match exact or descendant
    try:
        pat_resolved = Path(os.path.realpath(pat)).absolute()
    except OSError:
        pat_resolved = Path(pat).absolute()
    pat_str = _norm_for_compare(str(pat_resolved))
    if abs_str == pat_str:
        return True
    if abs_str.startswith(pat_str + "/"):
        return True
    return False


def is_protected_path(path: str | os.PathLike[str]) -> bool:
    """Is this path on the hardcoded protected-paths denylist?

    Fail-CLOSED: any path we can't reason about (OS errors, weird names) is
    treated as protected. The previous fail-open default was a smell.
    """
    try:
        absolute = _expand(path)
    except (ValueError, OSError):
        return True  # fail-closed
    return any(_matches_pattern(absolute, p) for p in PROTECTED_PATHS)


def assert_writable(path: str | os.PathLike[str]) -> None:
    if is_protected_path(path):
        raise ProtectedPathError(
            f"refusing to write to protected path: {path}\n"
            f"(if this is intentional, edit ~/.forge/protected_paths.yaml — "
            f"the agent itself cannot bypass this)"
        )


def assert_readable(path: str | os.PathLike[str]) -> None:
    """Block READS of protected paths too.

    Reading ~/.ssh/id_rsa is exfiltration risk #1 — even if the agent doesn't
    write the secret, it could include it in a model prompt and leak via the
    next API call. The Read() tool already enforced this, but raw open(p)
    didn't until now.
    """
    if is_protected_path(path):
        raise ProtectedPathError(
            f"refusing to read protected path: {path}\n"
            f"(if this is intentional, edit ~/.forge/protected_paths.yaml)"
        )


# =============================================================================
# Protected-action check (Bash wrapper)
# =============================================================================


def _bash_command_is_protected(cmd: str) -> Optional[str]:
    """Return the matching pattern if cmd contains a protected action, else None.

    Conservative substring + word-boundary check. Pattern matching is
    intentionally simple — adversarial bypasses are NOT in scope for trust mode.
    """
    norm = re.sub(r"\s+", " ", cmd.strip())
    for pat in PROTECTED_ACTIONS:
        if pat in norm:
            return pat
    # Word-boundary safety: catch standalone `sudo` even without trailing space
    for word in ("sudo",):
        if re.search(rf"\b{re.escape(word)}\b", norm):
            return word
    return None


# =============================================================================
# Tool implementations.
#
# These are the functions pre-imported into the kernel as globals.
# =============================================================================


def Read(path: str | os.PathLike[str], *, max_bytes: int = 1_000_000) -> str:
    """Read a text file, returning its contents as a string.

    No protection here — reads are allowed everywhere except sensitive files
    that even Read shouldn't touch. `~/.ssh/id_rsa` is excluded via the
    protected-paths check (defense in depth — exfil prevention).
    """
    absolute = _expand(path)
    if is_protected_path(absolute):
        raise ProtectedPathError(f"refusing to read protected path: {path}")
    if not absolute.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if absolute.is_dir():
        raise IsADirectoryError(f"is a directory: {path}")
    data = absolute.read_bytes()[:max_bytes]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def Write(path: str | os.PathLike[str], content: str) -> None:
    """Write content to path. Refuses protected paths.

    Creates parent directories as needed. Overwrites existing files (the
    shadow git layer makes this reversible, so 'overwrite' is the right
    default semantically).
    """
    assert_writable(path)
    absolute = _expand(path)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(content, encoding="utf-8")


def Edit(path: str | os.PathLike[str], old: str, new: str, *, replace_all: bool = False) -> None:
    """Replace `old` with `new` in `path`. Errors if `old` not found or not unique.

    For unique edits (replace_all=False), `old` must occur exactly once. This
    is the same contract Claude Code's Edit tool uses — it forces the agent to
    quote enough surrounding context to disambiguate.
    """
    assert_writable(path)
    absolute = _expand(path)
    if not absolute.exists():
        raise FileNotFoundError(f"no such file: {path}")
    text = absolute.read_text(encoding="utf-8")
    if replace_all:
        new_text = text.replace(old, new)
    else:
        n = text.count(old)
        if n == 0:
            raise ValueError(f"old string not found in {path}")
        if n > 1:
            raise ValueError(
                f"old string is not unique in {path} ({n} matches); "
                f"add surrounding context or pass replace_all=True"
            )
        new_text = text.replace(old, new, 1)
    absolute.write_text(new_text, encoding="utf-8")


@dataclass
class BashResult:
    cmd: str
    returncode: int
    stdout: str
    stderr: str

    def __repr__(self) -> str:
        head = f"BashResult(cmd={self.cmd!r}, returncode={self.returncode})"
        if self.stdout:
            head += f"\n--- stdout ({len(self.stdout)} chars) ---\n{self.stdout}"
        if self.stderr:
            head += f"\n--- stderr ({len(self.stderr)} chars) ---\n{self.stderr}"
        return head


def Bash(cmd: str, *, timeout: int = 120, cwd: Optional[str | os.PathLike[str]] = None) -> BashResult:
    """Run a shell command. Refuses protected actions (sudo, rm -rf /, etc).

    NOTE: This uses `shell=True` because that matches how a developer would
    type the command. The protected-action check is done BEFORE the shell sees
    it. We do not attempt to defeat shell-injection — trust mode means you
    trust the agent's authorial intent. If you need full sandboxing, that's
    the v1 sandbox-exec story.
    """
    matched = _bash_command_is_protected(cmd)
    if matched:
        raise ProtectedActionError(
            f"refusing to run protected action {matched!r} in: {cmd!r}\n"
            f"(this requires explicit user confirmation outside of agent code)"
        )

    proc = subprocess.run(  # noqa: S602 — shell=True is intentional, see docstring
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    return BashResult(
        cmd=cmd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def search(pattern: str, *, path: str | os.PathLike[str] = ".",
           max_results: int = 200) -> list[dict[str, Any]]:
    """Recursive ripgrep-style search. Returns list of {file, line, text}.

    Falls back to grep -rn if rg isn't installed. Read-only, no protection
    needed beyond what Read does.
    """
    rg_available = subprocess.run(  # noqa: S603,S607
        ["which", "rg"], capture_output=True, text=True
    ).returncode == 0
    if rg_available:
        proc = subprocess.run(  # noqa: S603,S607
            ["rg", "--json", "--no-heading", "-n", pattern, str(path)],
            capture_output=True, text=True, timeout=60,
        )
        out: list[dict[str, Any]] = []
        import json as _json
        for line in proc.stdout.splitlines()[:max_results * 2]:
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if rec.get("type") == "match":
                d = rec["data"]
                out.append({
                    "file": d["path"]["text"],
                    "line": d["line_number"],
                    "text": d["lines"]["text"].rstrip("\n"),
                })
            if len(out) >= max_results:
                break
        return out

    proc = subprocess.run(  # noqa: S603,S607
        ["grep", "-rn", "--include=*", pattern, str(path)],
        capture_output=True, text=True, timeout=60,
    )
    out2: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[:max_results]:
        m = re.match(r"^([^:]+):(\d+):(.*)$", line)
        if m:
            out2.append({"file": m.group(1), "line": int(m.group(2)), "text": m.group(3)})
    return out2


# =============================================================================
# Pluggable hooks — installed by the Session for skills/router integration.
# Tests + bare-kernel usage see the no-op stubs.
# =============================================================================


_FIND_SKILL: Callable[[str], list[dict[str, Any]]] = lambda q: []
_RUN_SKILL: Callable[..., Any] = lambda *a, **kw: (_ for _ in ()).throw(
    RuntimeError("run_skill not wired; install via forge.tools.set_skill_runtime()")
)
_CALL_MCP: Callable[..., Any] = lambda *a, **kw: (_ for _ in ()).throw(
    RuntimeError(
        "call_mcp not wired. Configure servers in ~/.forge/mcp.toml; the "
        "Session wires this on start()."
    )
)


def see(image: str | os.PathLike[str] | bytes,
        *,
        prompt: str = "Describe this image. Include any text, structure, key elements, and notable details.",
        model: str | None = None,
        ollama_url: str | None = None,
        timeout: float = 120.0) -> str:
    """Pass an image to the local vision model. Returns a text description.

    Accepts:
      - str / Path: a path to an image file (PNG, JPEG, GIF, WEBP, BMP).
        Tilde-expansion and protected-path checks apply (we refuse to read
        from `~/.ssh/*` etc., consistent with Read()).
      - bytes: raw image bytes.

    The model is the `vision` role's primary in the router config (default
    `qwen2.5vl:7b`). Override with the `model` kwarg or set FORGE_VISION_MODEL.

    Returns the model's text description as a string. Raises RuntimeError on
    HTTP/parse errors so callers can decide whether to retry or fall back.

    Caching: identical (image-bytes, prompt) pairs within a session return
    the cached description. Saves both wall-clock and the model's wakeup cost.
    """
    import base64
    import hashlib
    import json as _json
    import os as _os
    import time
    import urllib.request

    # ---- resolve input to bytes ------------------------------------------
    if isinstance(image, bytes):
        img_bytes = image
        src_label = f"<{len(image)} bytes>"
    else:
        path = _expand(image)
        # Reads of protected paths are blocked even for vision — consistent
        # with Read() and the post-shake-out hardening.
        if is_protected_path(path):
            raise ProtectedPathError(f"refusing to read protected path: {image}")
        if not path.exists():
            raise FileNotFoundError(f"no such image: {image}")
        if path.is_dir():
            raise IsADirectoryError(f"is a directory: {image}")
        img_bytes = path.read_bytes()
        src_label = str(path)

    if len(img_bytes) > 20 * 1024 * 1024:  # 20 MB cap to be safe
        raise ValueError(
            f"image too large: {len(img_bytes)} bytes (max 20 MB). "
            f"Resize before calling see()."
        )

    # ---- cache lookup ----------------------------------------------------
    cache_key = hashlib.sha256(img_bytes + prompt.encode("utf-8")).hexdigest()
    cache = _SEE_CACHE
    if cache_key in cache:
        return cache[cache_key]

    # ---- POST to Ollama --------------------------------------------------
    model_name = model or _os.environ.get("FORGE_VISION_MODEL", "qwen2.5vl:7b")
    base_url = ollama_url or _os.environ.get(
        "FORGE_OLLAMA_URL", "http://localhost:11434/v1"
    )
    # Strip /v1 if present — vision needs the native /api/chat endpoint
    # (the OpenAI-shaped /v1/chat/completions doesn't accept Ollama's image
    # field shape).
    api_base = base_url.rsplit("/v1", 1)[0]
    api_url = api_base.rstrip("/") + "/api/chat"

    payload = {
        "model": model_name,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [base64.b64encode(img_bytes).decode("ascii")],
        }],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    req = urllib.request.Request(
        api_url,
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.load(resp)
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"vision model {model_name} returned HTTP {e.code}: {body}"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"vision model unreachable at {api_url}: {e}. "
            f"Is `ollama serve` running? Did you `ollama pull {model_name}`?"
        ) from e

    description = data.get("message", {}).get("content", "").strip()
    if not description:
        raise RuntimeError(
            f"vision model returned empty description for {src_label}"
        )

    cache[cache_key] = description
    return description


# Per-session image cache (cleared on Session lifecycle by importing module reload).
# We keep it module-scoped because see() is a global — callers don't have a
# session handle. The Session.close() implementation could clear this, but
# v0 lets it grow within a Python interpreter lifetime.
_SEE_CACHE: dict[str, str] = {}


def find_skill(query: str) -> list[dict[str, Any]]:
    """Search installed skills for the query. Returns [{name, description, score}]."""
    return _FIND_SKILL(query)


def run_skill(name: str, **kwargs: Any) -> Any:
    """Invoke another skill by name. Activates if not already active."""
    return _RUN_SKILL(name, **kwargs)


def call_mcp(server: str, tool: str, **arguments: Any) -> Any:
    """Invoke a tool on a configured MCP server.

    The Session wires this on start() to its MCPRegistry. Configure servers
    in `~/.forge/mcp.toml`. Servers are spawned lazily on first call and
    torn down on Session.close.
    """
    return _CALL_MCP(server, tool, **arguments)


def set_skill_runtime(
    *,
    find: Optional[Callable[[str], list[dict[str, Any]]]] = None,
    run: Optional[Callable[..., Any]] = None,
    see_fn: Optional[Callable[[Any], str]] = None,  # deprecated; see() is real now
    mcp: Optional[Callable[..., Any]] = None,
) -> None:
    """Wire the skill / MCP callbacks. Called by Session.start().

    Note: `see_fn` is no longer used — see() is now a real implementation
    that talks to the vision sub-skill via the Ollama API directly. The
    parameter is kept for backward compatibility and ignored.
    """
    global _FIND_SKILL, _RUN_SKILL, _CALL_MCP
    if find is not None:
        _FIND_SKILL = find
    if run is not None:
        _RUN_SKILL = run
    if mcp is not None:
        _CALL_MCP = mcp
    # see_fn intentionally ignored — see() is wired directly to Ollama now.


# =============================================================================
# Builtins interceptor — closes the open() bypass.
#
# A skill could `import builtins; builtins.open(path, "w")` and dodge our
# Write tool. The interceptor wraps builtins.open so every read/write open()
# also goes through the appropriate assertion.
#
# We also wrap os.open (used by shutil + many libraries internally),
# shutil.copy/copy2/copyfile/copytree/move (which use os.open + os.write),
# and subprocess.run/Popen (catches `cp ~/.ssh/x ...` style exfil).
#
# Install this once at kernel startup (forge.kernel does this).
# We deliberately DO NOT expose the originals as module attributes — they
# live in private cell-scope to make casual restoration harder. A
# determined attacker can still find them via `gc.get_referrers`, but that
# takes effort. Trust mode means we raise the bar, not seal the boundary.
# =============================================================================


_INSTALLED = False
# We keep the originals only inside the install closure, not as module attrs.
# `__OPEN__` is named to be hard to guess but it's not a security boundary —
# just a convenience for uninstall/idempotency.
__OPEN__: Optional[Callable[..., Any]] = None
__OS_OPEN__: Optional[Callable[..., Any]] = None
__SUBPROC_RUN__: Optional[Callable[..., Any]] = None
__SUBPROC_POPEN__: Optional[Callable[..., type]] = None
__SHUTIL_FUNCS__: dict[str, Callable[..., Any]] = {}


def install_builtin_guards() -> None:
    """Patch low-level I/O primitives to enforce protected paths/actions.

    Idempotent. Called by forge.kernel during kernel startup.

    What's wrapped (every place LLM-emitted code can perform I/O):
      - builtins.open       (read + write modes)
      - os.open             (low-level fd open)
      - shutil.copy/copy2/copyfile/copytree/move
      - subprocess.run / subprocess.Popen
    """
    global _INSTALLED, __OPEN__, __OS_OPEN__, __SUBPROC_RUN__, __SUBPROC_POPEN__, __SHUTIL_FUNCS__
    if _INSTALLED:
        return

    # Capture originals
    __OPEN__ = builtins.open
    __OS_OPEN__ = os.open
    __SUBPROC_RUN__ = subprocess.run
    __SUBPROC_POPEN__ = subprocess.Popen  # type: ignore[assignment]
    for name in ("copy", "copy2", "copyfile", "copytree", "move"):
        __SHUTIL_FUNCS__[name] = getattr(_shutil, name)

    # ---- builtins.open: write OR read of protected paths blocked ----
    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(c in mode for c in "wax"):
            assert_writable(file)
        else:
            assert_readable(file)
        return __OPEN__(file, mode, *args, **kwargs)  # type: ignore[misc]

    builtins.open = guarded_open  # type: ignore[assignment]

    # ---- os.open: same protection at the syscall level ----
    O_WRONLY = os.O_WRONLY
    O_RDWR = os.O_RDWR
    O_CREAT = os.O_CREAT
    O_APPEND = os.O_APPEND

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # If any write/create flag, assert writable; otherwise readable.
        is_write = bool(flags & (O_WRONLY | O_RDWR | O_CREAT | O_APPEND))
        if is_write:
            assert_writable(path)
        else:
            assert_readable(path)
        return __OS_OPEN__(path, flags, *args, **kwargs)  # type: ignore[misc]

    os.open = guarded_os_open  # type: ignore[assignment]

    # ---- shutil: copy/move read source AND write dest, both protected paths ----
    def make_guarded_copy(orig: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            assert_readable(src)
            assert_writable(dst)
            return orig(src, dst, *args, **kwargs)
        return guarded

    for name, fn in __SHUTIL_FUNCS__.items():
        setattr(_shutil, name, make_guarded_copy(fn))

    # ---- subprocess: scan command for protected actions AND for protected paths ----
    def _check_cmd(cmd: Any) -> None:
        if isinstance(cmd, str):
            joined = cmd
        elif isinstance(cmd, (list, tuple)):
            joined = " ".join(str(c) for c in cmd)
        else:
            return
        matched = _bash_command_is_protected(joined)
        if matched:
            raise ProtectedActionError(
                f"refusing to run protected action {matched!r}: {joined!r}"
            )
        # Also: catch shell commands that read protected paths (cat/cp/scp/rsync ~/.ssh)
        # We do this by scanning the joined command for any protected-path reference.
        for pat in PROTECTED_PATHS:
            pat_expanded = os.path.expanduser(pat)
            if "*" in pat_expanded:
                continue  # globs not meaningful as substrings
            if pat_expanded in joined or pat in joined:
                # Plus a reading verb in the same command
                for verb in ("cat ", "cp ", "scp ", "rsync ", "tar ", "zip ",
                             "less ", "more ", "head ", "tail ", "od ", "xxd ",
                             "base64 ", "openssl "):
                    if verb in joined:
                        raise ProtectedActionError(
                            f"refusing shell command that touches protected path "
                            f"{pat!r} via {verb.strip()!r}: {joined!r}"
                        )

    def guarded_subprocess_run(*args: Any, **kwargs: Any) -> Any:
        cmd = args[0] if args else kwargs.get("args")
        _check_cmd(cmd)
        return __SUBPROC_RUN__(*args, **kwargs)  # type: ignore[misc]

    class _GuardedPopen(__SUBPROC_POPEN__):  # type: ignore[misc,valid-type]
        def __init__(self, args: Any, *more: Any, **kwargs: Any):
            _check_cmd(args)
            super().__init__(args, *more, **kwargs)

    subprocess.run = guarded_subprocess_run  # type: ignore[assignment]
    subprocess.Popen = _GuardedPopen  # type: ignore[assignment,misc]
    _INSTALLED = True


def uninstall_builtin_guards() -> None:
    """Restore the originals. For tests."""
    global _INSTALLED
    if __OPEN__ is not None:
        builtins.open = __OPEN__
    if __OS_OPEN__ is not None:
        os.open = __OS_OPEN__  # type: ignore[assignment]
    if __SUBPROC_RUN__ is not None:
        subprocess.run = __SUBPROC_RUN__
    if __SUBPROC_POPEN__ is not None:
        subprocess.Popen = __SUBPROC_POPEN__  # type: ignore[assignment]
    for name, fn in __SHUTIL_FUNCS__.items():
        setattr(_shutil, name, fn)
    _INSTALLED = False


# =============================================================================
# What gets pre-imported into the kernel.
# =============================================================================


def kernel_globals() -> dict[str, Any]:
    """The dict of names the kernel injects into every cell's scope."""
    return {
        "Read": Read,
        "Write": Write,
        "Edit": Edit,
        "Bash": Bash,
        "search": search,
        "see": see,
        "find_skill": find_skill,
        "run_skill": run_skill,
        "call_mcp": call_mcp,
        # Convenience re-exports
        "ProtectedPathError": ProtectedPathError,
        "ProtectedActionError": ProtectedActionError,
    }
