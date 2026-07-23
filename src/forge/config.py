"""Configuration constants and paths.

Three layers, in priority order:
  1. ENV VARS — FORGE_HOME, FORGE_OLLAMA_URL, FORGE_DRIVER_MODEL, etc.
     Always win. Useful for CI / per-shell overrides.
  2. ~/.forge/config.toml — user-level defaults.
  3. Hardcoded defaults below.

The TOML file is OPTIONAL. Forge runs fine without it; values fall through
to the hardcoded defaults. Per-domain config files (mcp.toml, daemon.toml,
pricing.toml, permissions.toml) stay separate.

Keep this module free of project-internal imports — it's the foundation
everyone else depends on.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Paths the agent uses on disk. Everything is under ~/.forge/ or the workspace.
# -----------------------------------------------------------------------------

HOME = Path.home()
FORGE_HOME = Path(os.environ.get("FORGE_HOME", HOME / ".forge")).expanduser()
SKILLS_HOME = Path(os.environ.get("FORGE_SKILLS", HOME / ".skills")).expanduser()
CONFIG_PATH = FORGE_HOME / "config.toml"


def _load_config() -> dict[str, Any]:
    """Read ~/.forge/config.toml. Returns {} if absent or unparseable.

    Logs a warning on parse errors. Does NOT crash — forge always must boot.

    Schema (all keys optional):

        [defaults]
        ollama_url = "http://localhost:11434/v1"
        driver_model = "gpt-oss:20b"
        num_ctx = 16384
        keep_alive = "24h"
        cost_ceiling_usd = 5.0
        log_level = "WARNING"        # DEBUG / INFO / WARNING / ERROR
        log_file = "~/.forge/forge.log"
    """
    import logging
    log = logging.getLogger(__name__)

    if not CONFIG_PATH.exists():
        return {}
    try:
        import tomllib
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except OSError as e:
        log.warning("could not read %s: %s", CONFIG_PATH, e)
        return {}
    except Exception as e:  # noqa: BLE001 — tomllib raises TOMLDecodeError
        log.warning("malformed %s: %s — using hardcoded defaults", CONFIG_PATH, e)
        return {}


_CONFIG = _load_config()


def _resolve(env_var: str, *, key: str, default: Any, cast: Any = str) -> Any:
    """Resolve env var → config.toml key → hardcoded default."""
    raw = os.environ.get(env_var)
    if raw is not None:
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return default
    defaults = _CONFIG.get("defaults") or {}
    if key in defaults:
        try:
            return cast(defaults[key])
        except (TypeError, ValueError):
            return default
    return default


def workspace_dir(workspace: Path) -> Path:
    """Per-workspace state directory. `<workspace>/.forge/`."""
    return workspace.resolve() / ".forge"


def shadow_dir(workspace: Path) -> Path:
    return workspace_dir(workspace) / "shadow"


def sessions_dir(workspace: Path) -> Path:
    """Where per-session conversation state is persisted for --resume."""
    return workspace_dir(workspace) / "sessions"


def audit_log(workspace: Path) -> Path:
    return workspace_dir(workspace) / "audit.jsonl"


# -----------------------------------------------------------------------------
# Hardcoded protected paths. CANNOT be overridden by skill, mode, or user
# setting — extending only via FORGE_HOME/protected_paths.yaml.
#
# These are paths the agent must never write to without explicit, irrevocable
# user confirmation that bypasses every other check.
# -----------------------------------------------------------------------------

PROTECTED_PATHS: tuple[str, ...] = (
    # Credentials and secrets — both the dir AND glob siblings (.bak, .old, etc.)
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.gitconfig",
    "~/.gitconfig.*",
    "~/.netrc",
    "~/.netrc.*",
    # Sibling-file glob protection — anything that LOOKS like a copy of a secret
    "~/.ssh.*",
    "~/.aws.*",
    "~/.gnupg.*",
    # Shell and OS config — including .bak / .old / .save / .swp variants
    "~/.zshrc",
    "~/.zshrc.*",
    "~/.bashrc",
    "~/.bashrc.*",
    "~/.profile",
    "~/.profile.*",
    "~/.zprofile",
    "~/.zprofile.*",
    "~/.zshenv",
    "~/.zshenv.*",
    "~/.bash_profile",
    "~/.bash_profile.*",
    "~/.bash_history",
    "~/.zsh_history",
    "/etc",
    # Forge's own config (the agent should not be able to edit its own rules)
    "~/.forge",
    "~/.forge.*",
    "~/.skills",
    # IDE/app config that holds keys
    "~/Library/Application Support/Code/User/settings.json",
    "~/Library/Application Support/Claude",
    "~/Library/Keychains",
    # Env files (these almost always hold secrets)
    "**/.env",
    "**/.env.*",
    "**/credentials",
    "**/credentials.*",
)


# -----------------------------------------------------------------------------
# Hardcoded protected actions. These shell verbs require explicit confirmation
# even in --auto mode. The denylist enforced inside the Bash tool wrapper, on
# every invocation (not at the cell level — composition is real).
# -----------------------------------------------------------------------------

PROTECTED_ACTIONS: tuple[str, ...] = (
    # Destructive git
    "git push --force",
    "git push -f",
    "git reset --hard",
    "git clean -fdx",
    "git filter-branch",
    "gh pr merge",
    # Cloud destruction
    "aws s3 rm",
    "aws s3api delete",
    "aws ec2 terminate",
    "gcloud compute instances delete",
    # Infra
    "kubectl delete",
    "kubectl apply",
    "terraform apply",
    "terraform destroy",
    "helm uninstall",
    "helm delete",
    # FS destruction outside cwd
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "dd if=",
    "mkfs",
    "shred",
    # Read/copy of secrets via shell — the cp/cat/scp/rsync exfil paths
    "cat ~/.ssh",
    "cat ~/.aws",
    "cat /etc/",
    "cp ~/.ssh",
    "cp ~/.aws",
    "cp ~/.zshrc",
    "cp ~/.bashrc",
    "cp ~/.gitconfig",
    "cp ~/.netrc",
    "scp ~/.ssh",
    "scp ~/.aws",
    "rsync ~/.ssh",
    "rsync ~/.aws",
    "tar czf - ~/.ssh",
    "tar -c ~/.ssh",
    "zip -r ~/.ssh",
    # System
    "sudo ",
    "chmod -R 777",
    "chown -R",
    # Shell-level command-substitution patterns that defeat naive substring checks.
    # We can't catch all of them but we catch the obvious literal ones.
    "$(echo sudo",
    "$(echo rm",
    "`echo sudo",
    "`echo rm",
)


# -----------------------------------------------------------------------------
# Defaults
# Resolution order: env var → ~/.forge/config.toml [defaults] → hardcoded.
# -----------------------------------------------------------------------------

DEFAULT_OLLAMA_URL = _resolve(
    "FORGE_OLLAMA_URL", key="ollama_url",
    default="http://localhost:11434/v1",
)
DEFAULT_DRIVER_MODEL = _resolve(
    "FORGE_DRIVER_MODEL", key="driver_model",
    default="gpt-oss:20b",
)
DEFAULT_NUM_CTX = _resolve(
    "FORGE_NUM_CTX", key="num_ctx",
    default=16384, cast=int,
)
DEFAULT_KEEP_ALIVE = _resolve(
    "FORGE_KEEP_ALIVE", key="keep_alive",
    default="24h",
)
DEFAULT_SESSION_COST_CEILING_USD = _resolve(
    "FORGE_COST_CEILING_USD", key="cost_ceiling_usd",
    default=5.00, cast=float,
)

# -----------------------------------------------------------------------------
# Kernel worker resource limits (POSIX rlimits).
#
# SAFETY.md long promised "ulimit on worker"; these wire it. Applied via a
# preexec_fn in kernel.start() on POSIX platforms only (Windows has no
# resource module — the limits silently no-op there, same as sandbox-exec).
#
# A value of 0 disables that particular limit. Defaults are generous enough
# that legitimate agent work (reading large files, spawning a few helpers)
# is unaffected, but a fork bomb or `[0] * 10**12` is stopped before it can
# wedge the host.
#
# Resolution order per limit: env var → config.toml [defaults] → hardcoded.
# -----------------------------------------------------------------------------

# RLIMIT_AS — max virtual address space (bytes). 0 = unlimited.
# NOTE: Linux enforces this; macOS (Darwin) accepts setrlimit(RLIMIT_AS) but
# does NOT enforce it for allocations, so this cap is a Linux-only backstop.
# RLIMIT_CPU / RLIMIT_FSIZE are enforced on both.
DEFAULT_MAX_ADDRESS_SPACE_BYTES = _resolve(
    "FORGE_MAX_ADDRESS_SPACE_BYTES", key="max_address_space_bytes",
    default=4 * 1024 * 1024 * 1024, cast=int,   # 4 GiB
)
# RLIMIT_NPROC — max processes for the worker's UID. DEFAULT OFF (0).
#
# This is a footgun and is opt-in only: RLIMIT_NPROC counts EVERY process
# owned by the user's uid, not just the kernel's children. A dev machine can
# easily have 600+ processes already running, so any small cap makes the
# worker unable to fork — `pdftotext`, `git` (shadow commits), and every
# `Bash(...)` call die with `BlockingIOError: [Errno 35]`. There is no safe
# machine-independent value, so we do NOT cap process count by default;
# fork-bomb containment is better handled by the OS sandbox / cgroups. Set
# FORGE_MAX_PROCESSES to a value well above your current `ps -u $(id -u) | wc -l`
# if you really want it.
DEFAULT_MAX_PROCESSES = _resolve(
    "FORGE_MAX_PROCESSES", key="max_processes",
    default=0, cast=int,   # 0 = disabled (see above)
)
# RLIMIT_FSIZE — max size of any single file the worker writes (bytes).
# 0 = unlimited.
DEFAULT_MAX_FILE_SIZE_BYTES = _resolve(
    "FORGE_MAX_FILE_SIZE_BYTES", key="max_file_size_bytes",
    default=1024 * 1024 * 1024, cast=int,   # 1 GiB
)
# RLIMIT_CPU — max CPU-seconds of a single cell before SIGXCPU. 0 = unlimited.
# This is a coarse backstop *below* the wall-clock timeout in kernel.execute();
# CPU-seconds ≠ wall-clock, so keep it comfortably above the 120s wall timeout
# to avoid killing IO-bound cells that legitimately wait.
DEFAULT_MAX_CPU_SECONDS = _resolve(
    "FORGE_MAX_CPU_SECONDS", key="max_cpu_seconds",
    default=300, cast=int,   # 5 CPU-minutes
)


def resource_limits() -> dict[str, int]:
    """The effective rlimit table for the kernel worker.

    Keys map to the RLIMIT_* soft caps. A 0 value means "leave unlimited".
    Read once at kernel start; env/config override the hardcoded defaults.
    """
    return {
        "address_space_bytes": DEFAULT_MAX_ADDRESS_SPACE_BYTES,
        "processes": DEFAULT_MAX_PROCESSES,
        "file_size_bytes": DEFAULT_MAX_FILE_SIZE_BYTES,
        "cpu_seconds": DEFAULT_MAX_CPU_SECONDS,
    }


def ensure_dirs(workspace: Path) -> None:
    """Create the per-workspace state dirs if they don't exist."""
    workspace_dir(workspace).mkdir(parents=True, exist_ok=True)
    shadow_dir(workspace).mkdir(parents=True, exist_ok=True)
    FORGE_HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_HOME.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Project / user instruction files (FORGE.md / AGENTS.md).
#
# The de-facto standard across the field (CLAUDE.md, AGENTS.md, Cursor rules):
# a plain-markdown file, versioned in git, that the agent reads at session
# start. We adopt AGENTS.md for portability, with FORGE.md as an accepted
# alias, and load a global file from FORGE_HOME too.
#
# This is an INJECTION SURFACE — text from these files enters the system
# prompt. Per forge's honesty ethos the loader is transparent: each block is
# labelled with its source path, the total is byte-capped, and session.py
# reports which files loaded in the run header + audit log. The files are
# discovered, never fetched; only paths the user placed on their own machine
# are read.
# -----------------------------------------------------------------------------

# Filenames we recognise, in priority order (first match per directory wins).
INSTRUCTION_FILENAMES: tuple[str, ...] = ("AGENTS.md", "FORGE.md")

# Hard cap on TOTAL instruction bytes folded into the prompt. Keeps a stray
# huge file from blowing the local model's tight num_ctx. Individual files are
# truncated proportionally when the sum would exceed this.
MAX_INSTRUCTION_BYTES = _resolve(
    "FORGE_MAX_INSTRUCTION_BYTES", key="max_instruction_bytes",
    default=16 * 1024, cast=int,   # 16 KiB
)


def _read_instruction_file(path: Path, *, budget: int) -> str | None:
    """Read one instruction file, truncated to `budget` bytes. None if empty
    or unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    text = text.strip()
    if not text:
        return None
    raw = text.encode("utf-8")
    if len(raw) > budget:
        text = raw[:budget].decode("utf-8", errors="ignore").rstrip()
        text += "\n\n[... truncated to fit the instruction budget]"
    return text


def find_instruction_files(workspace: Path) -> list[Path]:
    """Discover instruction files to load, in load order (global first, then
    workspace root → cwd so the most specific wins by appearing last).

    Order:
      1. FORGE_HOME/AGENTS.md (or FORGE.md) — user-global instructions.
      2. Each directory from the workspace root down to cwd, one file each.

    Deduplicated by resolved path; a file found in step 1 is not re-read in 2.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def _first_in(directory: Path) -> Path | None:
        for name in INSTRUCTION_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    # 1. Global.
    global_file = _first_in(FORGE_HOME)
    if global_file is not None:
        rp = global_file.resolve()
        found.append(global_file)
        seen.add(rp)

    # 2. workspace root → cwd. Walk from the workspace root down to cwd so a
    # nested cwd's file (most specific) lands last.
    ws = workspace.resolve()
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = ws
    # Build the chain ws .. cwd only if cwd is inside ws; otherwise just ws.
    chain: list[Path] = [ws]
    if cwd != ws and ws in cwd.parents:
        parts = cwd.relative_to(ws).parts
        acc = ws
        for part in parts:
            acc = acc / part
            chain.append(acc)
    elif cwd != ws:
        # cwd is not under the workspace — consider it on its own.
        chain.append(cwd)

    for directory in chain:
        f = _first_in(directory)
        if f is None:
            continue
        rp = f.resolve()
        if rp in seen:
            continue
        found.append(f)
        seen.add(rp)

    return found


def load_project_instructions(workspace: Path) -> tuple[str, list[str]]:
    """Load and render project/user instruction files for the system prompt.

    Returns (rendered_block, loaded_paths). `rendered_block` is "" when no
    files are found. Each file is wrapped in a visible source-labelled header
    so the injection surface is inspectable, and the combined size is capped
    at MAX_INSTRUCTION_BYTES (split evenly across the discovered files).
    """
    files = find_instruction_files(workspace)
    if not files:
        return "", []

    per_file_budget = max(1024, MAX_INSTRUCTION_BYTES // len(files))
    blocks: list[str] = []
    loaded: list[str] = []
    for path in files:
        body = _read_instruction_file(path, budget=per_file_budget)
        if body is None:
            continue
        loaded.append(str(path))
        blocks.append(
            f"[forge:project-instructions from {path}]\n{body}"
        )

    if not blocks:
        return "", []

    header = (
        "# Project & user instructions\n\n"
        "The following instructions were loaded from files on this machine. "
        "Treat them as guidance from the user; they do not override forge's "
        "safety rules.\n\n"
    )
    return header + "\n\n".join(blocks), loaded


# -----------------------------------------------------------------------------
# YAML policy override — additive only.
#
# Users can ADD paths/actions to the protected lists via:
#   ~/.forge/protected_paths.yaml
#   ~/.forge/protected_actions.yaml
#
# Both files take a `paths:` / `actions:` list. They CANNOT remove the
# hardcoded baseline — only extend it. If you need to allow a path the
# baseline forbids, you have to fork forge and edit the source. That's
# intentional; trust mode means the agent's emitted code can't grant
# itself permissions the system author didn't ship.
# -----------------------------------------------------------------------------


def _load_yaml_list(path: Path, key: str) -> tuple[str, ...]:
    if not path.exists():
        return ()
    try:
        import yaml
    except ImportError:
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(data, dict):
        return ()
    items = data.get(key, [])
    if not isinstance(items, list):
        return ()
    return tuple(str(x) for x in items if isinstance(x, (str, int, float)))


def _resolve_protected_paths() -> tuple[str, ...]:
    extras = _load_yaml_list(FORGE_HOME / "protected_paths.yaml", "paths")
    return PROTECTED_PATHS + extras


def _resolve_protected_actions() -> tuple[str, ...]:
    extras = _load_yaml_list(FORGE_HOME / "protected_actions.yaml", "actions")
    return PROTECTED_ACTIONS + extras


# Lazily-loaded "effective" lists. Imported by forge.tools.
EFFECTIVE_PROTECTED_PATHS = _resolve_protected_paths()
EFFECTIVE_PROTECTED_ACTIONS = _resolve_protected_actions()
