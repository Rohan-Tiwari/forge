# Forge

> A code-first local agent with skills, multi-provider routing, sandbox safety,
> dry-run previews, and a daemon that triggers on file events or schedules.

**Status: v0.2.0** — feature-complete vertical slice with 4 waves shipped. 331 tests, all green.
Battle-tested locally; production deployment of untrusted skills still gated on Wave-5 work.

## What's new in v0.2

| | |
|---|---|
| 👁️ **Vision** | `see(image)` reads any image via local Qwen2.5-VL |
| 🌊 **Streaming** | Token-by-token rendering in the chat REPL |
| ⌨️ **Multi-line REPL** | prompt_toolkit chat with history + slash completion |
| 🪜 **Multi-provider router** | Anthropic + OpenAI as escalation chain (auto-detected via env) |
| 🪞 **Real dry-run preview** | Cells executed against an overlay → real diffs, real workspace untouched |
| 🪪 **macOS sandbox** | `sandbox-exec` profile limits FS writes + network |
| 🔌 **MCP integration** | `call_mcp(server, tool, **args)` talks to any stdio MCP server |
| 📦 **Skill installer** | `forge skill install <repo>@<sha>` with AST scan + content-addressed pinning |
| 🔎 **Skill discovery** | `forge skill search` queries GitHub `topic:forge-skill` |
| 📝 **Plan mode** | `forge plan TASK` returns markdown plan with risk levels, no execution |
| 📊 **forge stats** | Per-window summary: calls, tokens, cost, latency, gate decisions |
| 💸 **Pricing override** | `~/.forge/pricing.toml` for custom rates |
| ⏰ **Daemon mode** | `forge daemon` watches folders + runs cron schedules |

## What it is

An agent that takes action by **emitting Python code into a persistent
interpreter**, not by emitting JSON tool calls. It loads **skills** (folders
with `SKILL.md` frontmatter) on demand, routes calls to whichever model fits
the task, and provides **git-based undo** for every filesystem change.

Designed for tasks where you want an agent loop that runs locally on your
machine, costs nothing per local call, sees your private files, and can be
safely interrupted with `forge undo`.

## Quick start

### 1 — Prerequisites

- macOS 14+ on Apple Silicon (other platforms work but sandbox-exec is macOS-only)
- Python 3.11+
- Ollama 0.30+ with `gpt-oss:20b` pulled (~14 GB) + `qwen2.5vl:7b` (~6 GB, optional for vision)
- (Optional) `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` for the escalation chain

```bash
brew install ollama
brew services start ollama
ollama pull gpt-oss:20b
ollama pull qwen2.5vl:7b   # optional, for the see() vision sub-skill
```

### 2 — Install Forge

```bash
git clone https://github.com/Rohan-Tiwari/forge.git
cd forge
pip install -e .
forge doctor    # green checks all the way down means you're good
```

### 3 — Run something

```bash
# Plan first
forge plan "Count the markdown files in this repo and tell me total size"

# Then run it
forge run "Count the markdown files in this repo and tell me total size"

# Interactive REPL with streaming + multi-line input
forge chat

# View activity rollup
forge stats --days 7
```

## CLI reference

```
forge run TASK            One-shot agent run
forge chat                Interactive REPL with streaming
forge plan TASK           Markdown plan, no execution
forge stats [--days N]    Per-window activity rollup
forge cost                Lifetime cost (this workspace)
forge log [-n N]          Recent audit log entries
forge undo                Revert last cell
forge show SHA            Diff for a shadow commit
forge doctor              Verify Ollama + model + dirs
forge daemon              Long-lived process for watchers + schedules

forge skill list          Installed skills
forge skill search QUERY  Find skills on GitHub (topic:forge-skill)
forge skill install SPEC  Install from git, e.g. alice/skills@a3f9c2c
forge skill update NAME   Re-install at latest upstream sha
forge skill show NAME     Render SKILL.md + scan
forge skill diff NAME     What would change at upstream HEAD
forge skill permit PAT    Save an always-allow permission rule
```

## Architecture (one screen)

```
┌─────────────────────── CLI ────────────────────────────┐
│  run | chat | plan | stats | log | undo | skill | daemon│
└────────────────────────┬───────────────────────────────┘
                         │
                ┌────────▼─────────┐
                │     Session      │   orchestrates one turn
                └─┬──────────────┬─┘
                  │              │
      ┌───────────▼──┐      ┌────▼──────────┐
      │  ModelRouter  │      │    Kernel     │   ◄── sandbox-exec wraps
      │  Anthropic +  │      │  Python -u    │       this on macOS
      │  OpenAI +     │      │  + intercept  │
      │  Ollama       │      │  open/Bash/etc│
      └───────┬───────┘      └────┬──────────┘
              │                   │
      ┌───────▼─────┐    ┌────────▼────────┐    ┌─────────────┐
      │   Gate      │    │  Tool core      │    │   MCP       │
      │ intent +    │    │  Read/Write/    │◄───┤ call_mcp()  │
      │ AST lint    │    │  Edit/Bash/     │    │  servers    │
      └─────────────┘    │  search/see/    │    │  (gh, fs..) │
                         │  call_mcp/...   │    └─────────────┘
                         └────┬────────────┘
                              │
              ┌───────────────┼───────────────────┐
              │               │                   │
      ┌───────▼─────┐  ┌──────▼─────┐  ┌──────────▼──┐
      │ ShadowGit   │  │ AuditLog   │  │ SkillRegistry│
      │ undo per    │  │ JSONL of   │  │  +Installer  │
      │ cell        │  │ everything │  │  +Scanner    │
      └─────────────┘  └────────────┘  └─────────────┘
```

Architecture deep-dive: see [`notes/agent-arch/01-architecture.md`](../notes/agent-arch/01-architecture.md).

## Writing a skill

```
my-skills/pdf-extract/
├── SKILL.md            # required
└── helpers.py          # optional: main(**kwargs) entry point
```

```yaml
---
name: pdf-extract
description: Extract structured text + tables from a PDF.
when_to_use: User provides a .pdf path or asks to read a PDF.
allowed_tools:
  - Read
  - Bash(pdftotext:*)
  - Write(./out/**)
license: Apache-2.0
---

# pdf-extract

Procedural knowledge here…
```

Drop it in `~/.skills/` or `./skills/`. Or install from a git repo:

```bash
forge skill install alice/forge-skills@a3f9c2c
```

The installer refuses floating refs (`main`, `HEAD`) without `--pin`, AST-scans
every `.py`, shows you the findings, and gives you a 5-second cooldown before
the trust prompt.

## Configuring providers

The router auto-detects API keys at startup:

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # adds claude-sonnet-4-6 to escalation chain
export OPENAI_API_KEY=sk-...           # adds gpt-5 to escalation chain
```

Without either, Ollama is the only backend. With one or both set,
escalation triggers — 2× intent-mismatch, 2× format-fail, or `/escalate`
in chat — promote the next call up the chain.

Override per-model pricing in `~/.forge/pricing.toml`:

```toml
[pricing."claude-sonnet-4-6"]
input = 1.5     # $/1M tokens, your enterprise rate
output = 7.5

[pricing."my-self-hosted-llm"]
input = 0.0
output = 0.0
```

## Daemon mode

Long-lived process that triggers agent runs on file events or cron schedules.
Configure in `~/.forge/daemon.toml`:

```toml
[watchers.downloads-triage]
path = "~/Downloads"
pattern = "*.pdf"
event = "created"
task = "Triage the PDF at {path} and write a summary next to it."
cooldown_s = 5

[schedules.daily-standup]
cron = "0 9 * * 1-5"   # 9am weekdays
task = "Run the daily-standup skill and write to ./standup.md"
workspace = "~/work"
```

```bash
forge daemon              # foreground, logs to console
forge daemon --background # detach, logs to ~/.forge/daemon.log
forge daemon --status     # show pid + state
forge daemon --stop       # SIGTERM the daemon
```

## Examples

Working recipes for common workflows in [`examples/`](examples/):

- `examples/triage-inbox/` — file watcher that summarizes PDFs as they land
- `examples/daily-standup/` — cron-driven git log + open-PR summary
- `examples/codebase-tour/` — chat-mode walkthrough using project-stats + see()

## Safety story

This is a trust-mode agent with **two layers of defense in depth**.

| Layer | Always on? | What it stops |
|---|---|---|
| In-process protected-paths denylist | ✅ all platforms | Direct write/read of `~/.ssh`, `~/.aws`, `~/.zshrc`, etc. via any tool the agent might use (open, Write, shutil, subprocess) |
| macOS `sandbox-exec` profile | macOS only | OS-level write/network boundary — even bypasses of layer 1 (raw `os.open`, `ctypes`) hit this |
| Git-based undo | ✅ | Anything in the workspace is reversible via `forge undo` |
| Dry-run preview | optional | See the real diff before approving (workspace untouched) |
| Skill installer AST scan | ✅ | `eval`/`exec`/`subprocess`/`ctypes` flagged at install time |

See [`docs/SAFETY.md`](docs/SAFETY.md) for the honest threat model — including
**what we DON'T protect against** (determined attackers, sub-100-byte covert
channels, etc).

## Development

```bash
pip install -e ".[dev]"
pytest                 # 331 tests, ~9s
ruff check src tests
mypy src
```

Layout:

```
src/forge/
├── cli.py            — Typer commands
├── session.py        — agent loop
├── router.py         — model routing + escalation
├── providers.py      — Ollama / Anthropic / OpenAI implementations
├── kernel.py         — Python subprocess + sandbox wrapping
├── sandbox.py        — sandbox-exec profile generation
├── gate.py           — intent block parser + AST lint
├── tools.py          — Read/Write/Edit/Bash + protected-path enforcement
├── preview.py        — static + dry-run preview engine
├── permissions.py    — session + persistent allow rules
├── shadow.py         — git-based undo
├── audit.py          — append-only JSONL
├── skills.py         — SKILL.md folder registry
├── installer.py      — forge skill install + AST scanner
├── mcp.py            — MCP stdio client
├── daemon.py         — file watchers + cron scheduler
├── repl.py           — prompt_toolkit REPL builder
├── config.py         — defaults, paths, protected lists
└── system_prompt.md  — driver system prompt (Day 0 fixes baked in)
```

## License

Apache-2.0
