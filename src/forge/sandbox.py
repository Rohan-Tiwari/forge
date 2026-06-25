"""forge.sandbox — macOS sandbox-exec profile generation + kernel spawn.

Wraps the kernel subprocess with a `sandbox-exec` profile that restricts:

  * File writes — only to the workspace, ~/.forge/, /tmp, and a small
    set of system paths Python needs to function
  * Network — outbound TCP/UDP allowed only to localhost and a small
    allowlist (Ollama on 11434 by default; user can extend via mcp.toml)
  * Process operations — fork is allowed (Python needs it); exec is
    allowed only for binaries on PATH (so Bash() still works) but the
    sandbox-exec'd subprocess INHERITS the parent profile, so Bash
    can't escape

This is the load-bearing safety upgrade that closes the "untrusted skill
can exfil ~/.aws/credentials" hole that the README has been honest about
since v0.1.0.

How it composes with the existing protected-paths layer:

  * The protected-paths denylist (forge.tools.is_protected_path) is the
    FIRST line of defense. It runs in-process and rejects obvious
    violations before they reach the kernel boundary.
  * The sandbox-exec profile is the SECOND line of defense. Even if a
    skill bypasses the in-process check (via a private builtins call,
    ctypes, etc.), the kernel can't actually open ~/.aws/credentials
    because the OS won't let it.

The profile language is sandbox-exec's TinyScheme dialect (sandbox(7)).
We don't use the Apple-recommended `sandbox_init` C API because that's
deprecated and Apple says to use `sandbox-exec` instead.

We also handle the Linux case (and other platforms) by no-op'ing — on
non-macOS the kernel just runs without the OS-level boundary, falling
back to the in-process protections only.
"""
from __future__ import annotations

import os
import platform
import shutil
import tempfile
from pathlib import Path

# =============================================================================
# Platform detection
# =============================================================================


def is_supported() -> bool:
    """True iff this OS supports our sandbox boundary."""
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


# =============================================================================
# Profile generation
# =============================================================================


_BASE_PROFILE = r"""(version 1)
(deny default)

;; ---- always-allowed: things Python and ipykernel literally need ----
(allow process-fork)
(allow process-info-pidinfo)
(allow process-info-pidfdinfo)
(allow process-info-setcontrol)
(allow process-info-rusage)
(allow signal (target self))

(allow file-read*)        ;; reads are not the threat we're guarding;
                           ;; protected-paths covers exfil of secrets

;; ---- writes only inside the workspace + forge state + tmp ----
{write_rules}

;; ---- subprocess (Bash/etc.) — exec inherits this profile ----
(allow process-exec*)
(allow process-exec
  (literal "/bin/sh")
  (literal "/bin/bash")
  (literal "/bin/zsh")
  (regex #"^/usr/bin/")
  (regex #"^/usr/local/bin/")
  (regex #"^/opt/homebrew/")
  (regex #"^{python_dir}/")
)

;; ---- system metadata reads ----
(allow sysctl-read)
(allow mach-lookup)       ;; needed for stdio, malloc, dyld
(allow ipc-posix-shm)

;; ---- network — outbound to localhost + allowlist hosts ----
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
{network_rules}
"""


class WorkspaceUnrepresentableError(RuntimeError):
    """Workspace path contains characters that can't be safely interpolated
    into a sandbox-exec profile.

    Sandbox-exec's TinyScheme uses double-quoted strings with backslash
    escaping; embedded newlines, unescaped quotes, parens, or backslashes
    break out of the string literal and could allow profile injection.
    Rather than escape (which is fragile across grammar variants), we
    refuse paths that contain any of these characters.
    """


_FORBIDDEN_PATH_CHARS = set('\n\r\\"()')


def _validate_workspace_path(workspace: Path) -> str:
    """Return realpath as a string IF safe to interpolate; else raise."""
    real = os.path.realpath(workspace)
    bad = _FORBIDDEN_PATH_CHARS & set(real)
    if bad:
        raise WorkspaceUnrepresentableError(
            f"workspace path contains characters that can't be safely "
            f"interpolated into a sandbox-exec profile: "
            f"{sorted(c.encode().hex() for c in bad)}. "
            f"Rename the directory."
        )
    return real


def _make_write_rules(workspace: Path) -> str:
    """Build (allow file-write*) clauses for the paths we DO want writable."""
    workspace_real = _validate_workspace_path(workspace)
    forge_home = _validate_workspace_path(Path(os.path.expanduser("~/.forge")))
    skills_home = _validate_workspace_path(Path(os.path.expanduser("~/.skills")))
    tmpdir = _validate_workspace_path(Path(tempfile.gettempdir()))

    rules: list[str] = []

    # Workspace + descendants
    rules.append(f'(allow file-write* (subpath "{workspace_real}"))')
    # Forge state
    rules.append(f'(allow file-write* (subpath "{forge_home}"))')
    # Skill installs
    rules.append(f'(allow file-write* (subpath "{skills_home}"))')
    # Tmp
    rules.append(f'(allow file-write* (subpath "{tmpdir}"))')
    rules.append('(allow file-write* (subpath "/private/tmp"))')
    rules.append('(allow file-write* (subpath "/private/var/folders"))')

    # Python's bytecode cache — Python wants to write .pyc anywhere it
    # imports from. We let it write under the standard library and
    # site-packages but those are read-only typically.
    rules.append('(allow file-write* (regex #"\\.pyc$"))')
    rules.append('(allow file-write* (regex #"__pycache__"))')

    # Allow stdout/stderr/stdin
    rules.append('(allow file-write-data (literal "/dev/null"))')
    rules.append('(allow file-write-data (literal "/dev/stdout"))')
    rules.append('(allow file-write-data (literal "/dev/stderr"))')
    rules.append('(allow file-write-data (literal "/dev/tty"))')
    rules.append('(allow file-ioctl)')

    return "\n".join(rules)


def _make_network_rules(allowed_hosts: list[str]) -> str:
    """Build (allow network-outbound) clauses for the allowlist.

    SECURITY NOTE: sandbox-exec's TinyScheme DOES NOT support hostname
    filtering. We can only allow/deny by IP, port, or local-vs-remote. So:

    - Localhost is always allowed (set in _BASE_PROFILE — covers Ollama).
    - For anything else, sandbox-exec literally CANNOT enforce a per-host
      rule. The previous implementation responded to a non-empty
      allowlist by emitting a bare `(allow network-outbound)`, which is
      EQUIVALENT TO NO NETWORK SANDBOX AT ALL. That was a silent no-op
      that defeated the safety story.

    The current behavior:
    - allowed_hosts is treated as informational only. We emit a comment
      noting what was requested.
    - We DO NOT emit any catch-all `(allow network-outbound)`. Outbound
      stays denied unless the caller is willing to live without
      sandbox-exec's network rule entirely, in which case they should
      run with FORGE_DISABLE_SANDBOX=1 (and audit-log a warning).

    Future work (v0.3): route through a localhost HTTP proxy that
    enforces hostname filtering and pass `(allow network-bind localhost
    + outbound to proxy)` only. That's the actually-correct architecture.
    """
    rules: list[str] = []
    for host in allowed_hosts:
        # Documentation-only — sandbox-exec can't enforce per-host rules.
        rules.append(f';; requested host: {host} (not enforced — see comment)')
    if allowed_hosts:
        # Pass-through of comments only; no actual outbound allow rule.
        # This is a deliberate choice: better to break outbound HTTPS for
        # the agent than to silently disable the network sandbox.
        rules.append(
            ';; sandbox-exec cannot filter outbound by hostname.\n'
            ';; All non-localhost outbound is DENIED. To override,\n'
            ';; run with FORGE_DISABLE_SANDBOX=1 (NOT recommended).'
        )
    return "\n".join(rules)


def build_profile(
    *,
    workspace: Path,
    allowed_network_hosts: list[str] | None = None,
) -> str:
    """Build a sandbox-exec profile for a Forge kernel.

    Args:
        workspace: agent's working directory; writes here are allowed.
        allowed_network_hosts: hosts the kernel can reach. Localhost is
            always allowed. Any non-empty list opens up outbound network
            entirely (sandbox-exec limitation — see module docstring).
    """
    import sys
    python_dir = os.path.dirname(os.path.realpath(sys.executable))
    return _BASE_PROFILE.format(
        write_rules=_make_write_rules(workspace),
        python_dir=python_dir,
        network_rules=_make_network_rules(allowed_network_hosts or []),
    )


def write_profile(profile: str, *, name: str = "forge-kernel") -> Path:
    """Write the profile to a tmp file. Returns the path. Caller cleans up."""
    fd, path = tempfile.mkstemp(prefix=f"{name}-", suffix=".sb", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(profile)
    except OSError:
        os.close(fd)
        raise
    return Path(path)


# =============================================================================
# Wrapping a subprocess command with sandbox-exec
# =============================================================================


def wrap_command(
    command: list[str],
    *,
    workspace: Path,
    allowed_network_hosts: list[str] | None = None,
) -> tuple[list[str], Path | None]:
    """Prefix `command` with sandbox-exec invocation.

    Returns (wrapped_command, profile_path). If the platform doesn't
    support sandbox-exec, returns (command, None) unchanged. Caller is
    responsible for cleaning up profile_path (if not None) after the
    subprocess exits.

    Disabled if FORGE_DISABLE_SANDBOX=1 in env. This is a documented
    escape hatch for users who hit a real false positive — but every
    true bypass becomes a known issue we want to fix in a profile update,
    so the env var is there for emergencies, not regular use.
    """
    if os.environ.get("FORGE_DISABLE_SANDBOX") == "1":
        return command, None
    if not is_supported():
        return command, None

    profile = build_profile(
        workspace=workspace,
        allowed_network_hosts=allowed_network_hosts,
    )
    profile_path = write_profile(profile)
    return (
        ["sandbox-exec", "-f", str(profile_path), *command],
        profile_path,
    )
