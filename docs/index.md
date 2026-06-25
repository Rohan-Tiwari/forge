# Forge

A code-first local agent with skills, multi-provider routing, sandbox
safety, dry-run previews, and a daemon that triggers on file events or
schedules.

**Status: v0.2.1** — 412 tests passing across the safety-critical paths.
Production-grade for personal use; deployment of untrusted skills still
gated on the per-skill sandbox-exec profile work documented in
[the safety model](SAFETY.md).

## Why this exists

Most agent frameworks emit JSON tool calls. Forge emits **Python** into a
persistent interpreter — the [CodeAct paradigm](https://arxiv.org/abs/2402.01030)
applied to a local-first agent that respects your time and your
filesystem.

Three properties make Forge different:

1. **Code-first action.** Loops, conditionals, intermediate values — all
   native, no JSON serialization tax between every step.
2. **Two-layer defense in depth.** In-process protected-paths denylist
   plus macOS `sandbox-exec` profile. A bypass of layer 1 (via raw
   `os.open`, `ctypes`, or a compromised skill) still hits layer 2.
3. **Honest about what it isn't.** See [SAFETY.md](SAFETY.md) for the
   actual threat model. Forge tells you which categories of attack it
   does and doesn't defend against.

## What's in this site

- **[Quickstart](quickstart.md)** — install, doctor, first run
- **[Architecture](architecture.md)** — the 18-module map and how they fit
- **[CLI reference](cli.md)** — every command + flag
- **[Writing skills](skills.md)** — SKILL.md format, installer, AST scan
- **[Safety model](SAFETY.md)** — what we defend, what we don't, why
- **API reference** — auto-generated from docstrings
