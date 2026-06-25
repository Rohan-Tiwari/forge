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


def ensure_dirs(workspace: Path) -> None:
    """Create the per-workspace state dirs if they don't exist."""
    workspace_dir(workspace).mkdir(parents=True, exist_ok=True)
    shadow_dir(workspace).mkdir(parents=True, exist_ok=True)
    FORGE_HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_HOME.mkdir(parents=True, exist_ok=True)


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
