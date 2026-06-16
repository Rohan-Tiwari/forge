# Forge

> A code-first local agent with skills, multi-provider routing, and trust-mode safety rails.
> Built on the Day-0-validated architecture in `notes/agent-arch/`.

**Status:** v0.1 vertical slice — the agent loop runs end-to-end on a real
gpt-oss:20b backend, with intent-block parsing, AST safety lint, protected-path
enforcement, git-based undo, and an Anthropic-style skill registry. Ready for
dogfood; not for production deployment of untrusted skills.

## What it is

An agent that takes action by **emitting Python code into a persistent
interpreter**, not by emitting JSON tool calls. It loads **skills** (folders
with `SKILL.md` frontmatter) on demand, routes calls to whichever model fits
the task, and provides **git-based undo** for every filesystem change.

Designed for the case the workflow research found: tasks where you want an
agent loop that runs locally on your machine, costs nothing per call, sees
your private files, and can be safely interrupted with `forge undo`.

## What it isn't

- A sandbox. Scripts run on the host. Safety is via undo + protected paths +
  diff preview, **not containment**. See [the safety story](#safety-story).
- A replacement for Claude Code or Cursor. Forge is for repeated workflows you
  encode as skills, not for general dev pairing.
- Production-grade. v0.1 is a vertical slice. The MVP roadmap below is honest.

## Quick start

### 1 — Prerequisites

- macOS 14+ on Apple Silicon (other platforms coming; not yet tested)
- Python 3.11+
- Ollama 0.30+ with `gpt-oss:20b` pulled
- ~14 GB disk for the model, ~2 GB free RAM during inference

```bash
# Install Ollama and pull the model
brew install ollama
brew services start ollama
ollama pull gpt-oss:20b
```

### 2 — Install Forge

```bash
pip install -e .
```

### 3 — Verify

```bash
forge doctor
```

Should print green ticks for ollama reachable, model present, dirs created.

### 4 — Run something

One-shot:
```bash
forge run "How many Python files in this project, and what's the total LOC?"
```

Interactive REPL:
```bash
forge chat
```

Inside chat: type your task, watch the agent emit cells, hit `y` to confirm
flagged ones, type `/undo` to revert, `/cost` for spend, `/exit` when done.

## Architecture (one screen)

```
┌─────────────────────────── CLI ───────────────────────────┐
│  forge run | forge chat | forge log | forge undo | ...    │
└──────────────────────────┬────────────────────────────────┘
                           │
                  ┌────────▼─────────┐
                  │     Session      │ ◄── orchestrates the agent loop
                  └─┬──────────────┬─┘
                    │              │
        ┌───────────▼──┐      ┌────▼──────────┐
        │  ModelRouter  │      │    Kernel     │
        │  Ollama+LLM   │      │  python -c    │
        └───────┬───────┘      │  subprocess   │
                │              │  + globals    │
                │              └────┬──────────┘
                │                   │
        ┌───────▼─────┐    ┌────────▼────────┐
        │   Gate      │    │  Tool core      │
        │ intent +    │    │  Read/Write/    │
        │ AST lint    │    │  Edit/Bash/     │
        └─────────────┘    │  search/...     │
                           │  + protected-   │
                           │  path + action  │
                           │  enforcement    │
                           └────┬────────────┘
                                │
                ┌───────────────┼────────────────┐
                │               │                │
        ┌───────▼─────┐  ┌──────▼─────┐  ┌──────▼──────┐
        │ ShadowGit   │  │ AuditLog   │  │ SkillRegistry│
        │ undo per    │  │ JSONL of   │  │ SKILL.md     │
        │ cell        │  │ everything │  │ folders      │
        └─────────────┘  └────────────┘  └─────────────┘
```

The [full architecture document](../notes/agent-arch/01-architecture.md)
goes deeper, including the Day-0 lessons baked in.

## Writing a skill

A skill is a folder with `SKILL.md` and optional helpers/references.

```
my-skills/
└── pdf-extract/
    ├── SKILL.md
    └── helpers.py
```

```yaml
---
name: pdf-extract
description: Extract structured text and tables from a PDF at a given path.
when_to_use: User provides a .pdf path or asks to read a PDF
allowed_tools:
  - Read
  - Bash(pdftotext:*)
  - Write(./out/**)
license: Apache-2.0
---

# pdf-extract

Procedural knowledge for extracting from PDFs ...
```

```python
# helpers.py — exposes a `main(**kwargs)` entry point that run_skill calls.
def main(path: str, *, out: str = None) -> str:
    # ... your code ...
    return "extracted markdown"
```

Drop this folder into `~/.skills/` or `./skills/` and `forge` will find it
automatically.

## Safety story

This is a trust-mode agent. **Code runs on your machine without containment.**
The defenses in place:

| Threat | Defense | Real grade |
|---|---|---|
| Buggy code nukes wrong files | Diff preview + git auto-commit + protected paths | **Strong** |
| Model writes to ~/.ssh | Hardcoded protected-paths denylist | **Strong** |
| Want to undo what just ran | Shadow git per-cell commits, `forge undo` | **Strong** |
| Skill from web is malicious-by-typosquat | Content-addressed install (v0.2), AST scan, manual review | **Medium** |
| Determined attacker who has read this design | Nothing | **None** |

See [`docs/SAFETY.md`](docs/SAFETY.md) for the full threat model.

## Roadmap

**v0.1** (this) — agent loop end-to-end, intent + AST gate, protected paths,
shadow git, skill registry, model router on Ollama, Typer CLI, unit tests.

**v0.2** —
- Vision sub-skill (Qwen2.5-VL via Ollama)
- Multi-provider router (Anthropic + OpenAI as escalation chain)
- `forge skill install <git-url>@<sha>` with AST scan
- Streaming model output to TTY
- MCP server integration via `call_mcp(server, tool, **args)`

**v0.3** —
- `sandbox-exec` profile per-skill (the real safety hardening)
- Background tasks with `run_in_background` / `poll` / `cancel`
- Plan mode + classifier for auto-mode safety review
- TUI (Rich live) for interactive mode

**v1.0** —
- Skill marketplace UX
- Multi-session, multi-workspace
- Linux + Windows support
- Real LiteLLM router with cost dashboards

## Development

```bash
git clone <repo>
cd forge
pip install -e ".[dev]"
pytest                    # run tests
ruff check src tests      # lint
mypy src                  # type-check
```

Layout:

```
src/forge/
├── __init__.py
├── cli.py            — Typer commands
├── session.py        — agent loop
├── router.py         — model selection + escalation + cost
├── kernel.py         — Python subprocess executor
├── gate.py           — intent parser + AST lint + GateDecision
├── tools.py          — pre-imported tool core + builtins guards
├── shadow.py         — git-based undo
├── audit.py          — JSONL append-only log
├── skills.py         — SKILL.md folder registry
├── config.py         — paths, defaults, protected lists
└── system_prompt.md  — driver system prompt (Day 0 fixes baked in)
tests/                — pytest suites
skills/               — first-party skills
docs/                 — extended docs
```

## License

Apache-2.0
