"""Forge — code-first local agent with skills, multi-provider routing, and
trust-mode safety rails.

Public surface lives in submodules:

    forge.gate       — intent block parsing + AST safety lint
    forge.tools      — pre-imported tool core (Read, Write, Edit, Bash, ...)
    forge.kernel     — persistent IPython kernel supervisor
    forge.shadow     — git-shadow undo layer
    forge.audit      — append-only structured audit log
    forge.skills     — SKILL.md folder registry
    forge.router     — multi-provider model router
    forge.session    — the agent loop (perceive-plan-execute)
    forge.cli        — command-line entry points
"""
from __future__ import annotations

__version__ = "0.2.2"
__all__ = ["__version__"]
