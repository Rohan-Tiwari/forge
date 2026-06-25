"""forge._subprocess_env — minimal env builder for spawned subprocesses.

Multiple critical security findings from the v0.2.1 audit converge here:
the kernel worker, MCP servers, and dry-run subprocess all inherit
`os.environ.copy()` — including provider API keys (ANTHROPIC_API_KEY,
OPENAI_API_KEY, GITHUB_TOKEN). A malicious cell or compromised MCP server
just reads from os.environ and exfiltrates them.

Fix: every subprocess spawned by forge gets a MINIMAL environment built
from an allowlist, plus only the keys the caller explicitly asked for.

Usage:

    from forge._subprocess_env import build_minimal_env
    env = build_minimal_env(pass_through={"GIT_AUTHOR_NAME"})
    subprocess.Popen(cmd, env=env, ...)

Default allowlist covers shell/Python basics. We deliberately do NOT pass
through any FORGE_*, ANTHROPIC_*, OPENAI_*, GITHUB_*, AWS_*, GCP_*
variables.
"""
from __future__ import annotations

import os
from typing import Iterable


# The minimum set every subprocess needs to function:
#   PATH        — exec to find binaries
#   HOME        — many tools default config locations
#   USER        — same
#   LANG / LC_* — locale (formatting, encoding)
#   TZ          — datetime formatting
#   TERM        — terminal capabilities
#   SHELL       — some tools spawn $SHELL
#   PWD         — some tools read this for cwd display
#   TMPDIR      — tempfile, shutil
#
# We intentionally exclude:
#   - All FORGE_* (subprocess shouldn't think it's running as forge)
#   - All *_API_KEY, *_TOKEN, *_SECRET (provider creds)
#   - All AWS_*, GCP_*, AZURE_* (cloud creds)
#   - GITHUB_*, GH_TOKEN (covers gh CLI auth)
#   - PYTHONPATH (could load attacker-controlled modules)
#   - SSH_AUTH_SOCK (ssh-agent socket — agent forwarding risk)
_DEFAULT_ALLOWLIST: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_COLLATE",
    "TZ",
    "TERM",
    "SHELL",
    "PWD",
    "TMPDIR",
    "TEMP",
    "TMP",
    "DISPLAY",      # for GUI subprocs (rare, but harmless)
    # Locale-related extras some tools want
    "LC_TIME",
    "LC_MONETARY",
})


# Prefixes / patterns that signal "secret" — never pass through, even if
# someone explicitly adds the key to pass_through (defense in depth: a
# typo'd pass_through with these patterns is almost always a mistake).
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "API_KEY", "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)


def is_likely_secret(name: str) -> bool:
    """Heuristic: is this env var name probably a secret?

    Used to refuse pass-through even when explicitly requested.
    Conservative — false positives are fine (user can rename their var).
    """
    upper = name.upper()
    return any(pat in upper for pat in _FORBIDDEN_PATTERNS)


def build_minimal_env(
    *,
    pass_through: Iterable[str] = (),
    extra: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal env dict for a child process.

    Args:
        pass_through: names of env vars to copy from the parent if present.
            Refused if the name matches a forbidden pattern (TOKEN/SECRET/etc).
        extra: explicit key=value pairs to add. NOT subject to the forbidden-
            pattern check, since the caller is supplying values directly (not
            forwarding from os.environ).
        base_env: source environment dict (defaults to os.environ).

    Returns:
        A fresh dict with only allowlisted + explicitly-requested entries.
    """
    src = base_env if base_env is not None else os.environ
    env: dict[str, str] = {}

    # Allowlist defaults
    for name in _DEFAULT_ALLOWLIST:
        if name in src:
            env[name] = src[name]

    # Caller-requested pass-through (with forbidden-pattern guard)
    for name in pass_through:
        if name in _DEFAULT_ALLOWLIST:
            continue
        if is_likely_secret(name):
            # Caller asked but the name looks like a secret. Skip silently;
            # logging a warning here would itself leak the name into logs.
            # The caller should know if their pass_through entry was honored
            # by checking the returned dict.
            continue
        if name in src:
            env[name] = src[name]

    # Explicit extras (caller chose value directly)
    if extra:
        env.update(extra)

    return env


def scrub_secrets(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of `env` with any likely-secret keys removed.

    For when we DO need to inherit a parent env wholesale (rare; prefer
    build_minimal_env). Use case: third-party SDKs that read from
    os.environ at import time but we control the child completely.
    """
    return {k: v for k, v in env.items() if not is_likely_secret(k)}
