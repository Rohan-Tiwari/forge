"""forge.errors — central exception hierarchy.

All Forge-specific exceptions inherit from `ForgeError`. This means:
  - Library users can catch `ForgeError` to handle "anything Forge raises"
    without catching too broadly.
  - The CLI's top-level handler can render a sensible message for any
    ForgeError subclass without a noisy traceback.
  - Subclasses carry semantic information (ProtectedPath vs Auth vs Config)
    so callers can branch on type.

A subset of forge's exceptions predate this module (ProtectedPathError in
tools.py, MCPError in mcp.py, CostCeilingExceeded in router.py, etc.). They
are re-exported from this module so `from forge.errors import X` works as
the canonical import path, AND registered as virtual subclasses of
ForgeError via `ForgeError.register(...)` so `isinstance(err, ForgeError)`
returns True for any of them.

For new code, prefer `from forge.errors import X`.
"""
from __future__ import annotations

from abc import ABCMeta

# =============================================================================
# Root — uses ABCMeta so we can register virtual subclasses.
# =============================================================================


class ForgeError(Exception, metaclass=ABCMeta):
    """Base class for all Forge-specific exceptions.

    User-facing errors should subclass this (or be registered via
    `ForgeError.register(...)` for legacy types) so the CLI can render
    them cleanly. Subclasses should include enough detail in the message
    that a sensible action is obvious from reading the error alone.
    """


# =============================================================================
# Config
# =============================================================================


class ConfigError(ForgeError):
    """Bad or unreadable configuration file."""


class PricingConfigError(ConfigError):
    """~/.forge/pricing.toml is malformed."""


class MCPConfigError(ConfigError):
    """~/.forge/mcp.toml is malformed."""


class DaemonConfigError(ConfigError):
    """~/.forge/daemon.toml is malformed."""


class PermissionConfigError(ConfigError):
    """~/.forge/permissions.toml is malformed."""


# =============================================================================
# Safety — re-export existing types from forge.tools.
# =============================================================================

from forge.tools import (  # noqa: E402
    ProtectedActionError,
    ProtectedPathError,
)


class SafetyError(ForgeError):
    """A safety boundary fired."""


# Register existing exception classes as virtual ForgeError subclasses so
# `isinstance(err, ForgeError)` returns True without touching their
# __bases__ (which can fail for C-level layout reasons).
ForgeError.register(ProtectedPathError)
ForgeError.register(ProtectedActionError)
SafetyError.register(ProtectedPathError)
SafetyError.register(ProtectedActionError)


# =============================================================================
# Kernel
# =============================================================================


class KernelError(ForgeError):
    """Something went wrong in the agent's Python kernel."""


class KernelTimeout(KernelError):
    """Cell ran longer than the timeout."""


class KernelWedged(KernelError):
    """Kernel exceeded consecutive-error threshold; needs reset."""


# =============================================================================
# Gate
# =============================================================================


class GateError(ForgeError):
    """Intent block / AST lint failure."""


# =============================================================================
# Provider
# =============================================================================


class ProviderError(ForgeError):
    """Backend (Anthropic/OpenAI/Ollama) returned an error."""


class ProviderAuthError(ProviderError):
    """Missing or invalid API key."""


class ProviderRateLimitError(ProviderError):
    """Backend rate-limited the request."""


class AnthropicError(ProviderError):
    """Anthropic-specific error from the API SDK."""


class OpenAIError(ProviderError):
    """OpenAI-specific error from the API SDK."""


class OllamaError(ProviderError):
    """Ollama-specific error (connection, model not found, etc.)."""


from forge.router import CostCeilingExceeded  # noqa: E402

ForgeError.register(CostCeilingExceeded)
ProviderError.register(CostCeilingExceeded)


# =============================================================================
# MCP — re-export + register
# =============================================================================

from forge.mcp import (  # noqa: E402
    MCPCallError,
    MCPError,
    MCPServerNotConfigured,
)

ForgeError.register(MCPError)
ForgeError.register(MCPCallError)
ForgeError.register(MCPServerNotConfigured)


# =============================================================================
# Installer — re-export + register
# =============================================================================

from forge.installer import (  # noqa: E402
    FloatingRefError,
    InstallError,
)

ForgeError.register(InstallError)
ForgeError.register(FloatingRefError)


# =============================================================================
# Skill
# =============================================================================


class SkillError(ForgeError):
    """Something went wrong with a skill (loading, activation, missing entry)."""


class SkillNotFound(SkillError):
    """Asked for a skill name not in the registry."""


# =============================================================================
# Convenience: tuple of every ForgeError type for the CLI's error renderer.
# =============================================================================


def all_forge_errors() -> tuple[type, ...]:
    """Tuple of every ForgeError subclass that's user-facing."""
    return (ForgeError,)


__all__ = [
    "ForgeError",
    "ConfigError",
    "PricingConfigError",
    "MCPConfigError",
    "DaemonConfigError",
    "PermissionConfigError",
    "SafetyError",
    "ProtectedPathError",
    "ProtectedActionError",
    "KernelError",
    "KernelTimeout",
    "KernelWedged",
    "GateError",
    "ProviderError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "AnthropicError",
    "OpenAIError",
    "OllamaError",
    "CostCeilingExceeded",
    "MCPError",
    "MCPCallError",
    "MCPServerNotConfigured",
    "InstallError",
    "FloatingRefError",
    "SkillError",
    "SkillNotFound",
    "all_forge_errors",
]
