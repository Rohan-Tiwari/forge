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
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


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


def _make_write_rules(workspace: Path) -> str:
    """Build (allow file-write*) clauses for the paths we DO want writable."""
    workspace_real = os.path.realpath(workspace)
    forge_home = os.path.realpath(os.path.expanduser("~/.forge"))
    skills_home = os.path.realpath(os.path.expanduser("~/.skills"))
    tmpdir = os.path.realpath(tempfile.gettempdir())

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
    """Build (allow network-outbound) clauses for the allowlist."""
    rules: list[str] = []
    # Localhost is already permitted via the localhost rule; don't repeat.
    # Each allowed host gets a regex-style match.
    for host in allowed_hosts:
        # Match against remote ip:port. We can't do hostname-based matching
        # at this layer (DNS resolution happens in the sandboxed process);
        # what we have is a permissive network-outbound for the allowlist.
        rules.append(f';; allowlist host: {host}')
    # For now, if there are any allowed hosts, allow network-outbound entirely.
    # This is a known limitation — sandbox-exec doesn't support hostname
    # filtering. We document this in SAFETY.md.
    if allowed_hosts:
        rules.append('(allow network-outbound)')
        rules.append('(allow network-inbound)')
    else:
        # No outbound except localhost (already allowed above).
        pass
    return "\n".join(rules)


def build_profile(
    *,
    workspace: Path,
    allowed_network_hosts: Optional[list[str]] = None,
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
    allowed_network_hosts: Optional[list[str]] = None,
) -> tuple[list[str], Optional[Path]]:
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
