# Forge

[![CI](https://github.com/Rohan-Tiwari/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/Rohan-Tiwari/forge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Rohan-Tiwari/forge?label=release)](https://github.com/Rohan-Tiwari/forge/releases/latest)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-421%20passing-brightgreen)](tests/)

> A code-first local agent. Emits Python into a persistent kernel instead of JSON tool calls, runs entirely on your machine against `gpt-oss:20b` via Ollama, and treats your filesystem as a first-class object — with sandbox-exec, dry-run previews, git-based undo, and a real safety story.

**Status: v0.2.4** · 421 tests passing on macOS + Linux × Python 3.11/3.12/3.13.

---

## Table of contents

1. [What Forge is](#what-forge-is)
2. [What it can do](#what-it-can-do)
3. [Install](#install)
4. [First run](#first-run)
5. [Commands](#commands)
6. [Architecture](#architecture)
7. [Safety model](#safety-model)
8. [Configuration](#configuration)
9. [Writing skills](#writing-skills)
10. [Daemon mode](#daemon-mode)
11. [Development & testing](#development--testing)
12. [Releases](#releases)
13. [Roadmap & status](#roadmap--status)
14. [Contributing](#contributing)
15. [License](#license)

---

## What Forge is

Forge is an **agent loop** that runs on your laptop. You give it a task in English; it writes Python code into a persistent IPython kernel, observes the output, and decides whether to write another cell or reply in prose. No JSON tool calls, no cloud round-trips for the core loop, no token cost for local-only work.

The design follows the **[CodeAct paradigm](https://arxiv.org/abs/2402.01030)**: actions are expressed as Python, not as serialized function calls. That means loops, conditionals, intermediate variables, and library composition are native — there is no serialization tax between every step.

Three properties make Forge different from a chat wrapper around a local model:

1. **Code-first action.** The agent emits markdown containing one `intent` block and one `py` block per cell. The harness parses, sanity-checks, optionally previews, and runs the code in a persistent kernel.
2. **Defense in depth.** An in-process protected-paths denylist plus a macOS `sandbox-exec` profile. A bypass of layer 1 (raw `os.open`, `ctypes`, or a compromised skill) still hits layer 2.
3. **Honest scope.** Forge tells you exactly what it does and does not defend against — see [docs/SAFETY.md](docs/SAFETY.md). It is **not** a hardened agent for running adversarial code; it is a trust-mode agent for tasks you would have done yourself.

---

## What it can do

| Capability | Detail |
|---|---|
| 🧠 **Local-first reasoning** | Drives `gpt-oss:20b` via Ollama by default. Zero per-call cost. |
| 🪜 **Multi-provider escalation** | Optional chain to Claude / GPT-5 when local model fails twice on format or intent. Detected from env vars; no config needed. |
| 👁️ **Vision** | `see(image)` reads any image via local Qwen2.5-VL. |
| 🌊 **Streaming chat** | Token-by-token rendering, multi-line input, slash commands, persistent history. |
| 🪞 **Real dry-run previews** | The next cell executes in an overlay filesystem; you see the real diff before approving the real run. |
| 🪪 **macOS sandbox** | `sandbox-exec` profile restricts writes to the workspace + `~/.forge` + tmp; reads are open, network is denied by default. |
| 🔌 **MCP integration** | `call_mcp(server, tool, **args)` talks to any stdio MCP server (filesystem, GitHub, etc). |
| 📦 **Skill installer** | `forge skill install <repo>@<sha>` clones at a pinned content-addressed SHA, AST-scans every `.py`, shows findings, prompts for trust. |
| 🔎 **Skill discovery** | `forge skill search` queries GitHub `topic:forge-skill`. |
| 📝 **Plan mode** | `forge plan TASK` returns a structured markdown plan with risk levels, files, network calls, open questions — no execution. |
| 📊 **Activity rollup** | `forge stats` summarizes calls, tokens, cost, latency, gate decisions per window. |
| ⏰ **Daemon mode** | `forge daemon` watches folders + runs cron schedules. |
| 🔁 **Git-based undo** | Every cell is auto-committed to a shadow git on top of a workspace branch. `forge undo` reverts in one command. |

---

## Install

### Prerequisites

| | Required | Notes |
|---|---|---|
| **macOS 14+ on Apple Silicon** | Strongly recommended | `sandbox-exec` is macOS-only. Linux works but loses layer-2 isolation; Windows untested. |
| **Python** | 3.11+ | Tested on 3.11, 3.12, 3.13 in CI. |
| **Ollama** | 0.30+ | https://ollama.com/download |
| **`gpt-oss:20b`** | ~14 GB | `ollama pull gpt-oss:20b` |
| **`qwen2.5vl:7b`** | Optional, ~6 GB | Needed only for the `see()` vision helper. |
| **`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`** | Optional | Enables escalation chain. |

```bash
brew install ollama
brew services start ollama
ollama pull gpt-oss:20b
ollama pull qwen2.5vl:7b   # optional
```

### Install Forge

**From a GitHub Release** (recommended — versioned wheel):

```bash
pip install https://github.com/Rohan-Tiwari/forge/releases/download/v0.2.4/forge_agent-0.2.4-py3-none-any.whl
```

**Or pin to a tag** without using release assets:

```bash
pip install git+https://github.com/Rohan-Tiwari/forge.git@v0.2.4
```

**From source** (for development):

```bash
git clone https://github.com/Rohan-Tiwari/forge.git
cd forge
pip install -e ".[dev]"
```

### Verify

```bash
forge doctor
```

Expects green ticks for: Ollama reachable, `gpt-oss:20b` present, `~/.forge/` writable, kernel can spawn. Investigates anything red.

---

## First run

```bash
# Plan first — see what would happen, no execution
forge plan "Count the markdown files in this repo and tell me total size"

# Then actually run it
forge run "Count the markdown files in this repo and tell me total size"

# Interactive REPL with streaming + multi-line input
forge chat

# Review what the agent did
forge log -n 20
forge stats --days 7
```

Sample one-shot output:

```
session 1782458986-35687 · workspace /Users/you/myproject
driver: gpt-oss:20b · skills: 3 · mode: interactive/preview=cells

ran 1 cells, denied 0, escalations 0, cost $0.0000
╭─────────────────────────────────── reply ────────────────────────────────────╮
│ This project has 47 Python files totaling 8,231 lines of code.               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

In the chat REPL:

- **Enter** inserts a newline; **Esc-Enter** submits (multi-line by default).
- **Ctrl-R** searches history; persisted at `~/.forge/chat-history`.
- **`/`** opens a slash-command menu — `/undo`, `/cost`, `/reset`, `/preview`, `/escalate`, `/skills`, `/help`, `/exit`.

---

## Commands

### Agent commands

| Command | What it does |
|---|---|
| `forge run TASK` | One-shot run. Exits when the agent replies in prose or hits the cell cap. |
| `forge chat` | Interactive REPL — streaming, multi-line, slash commands, history. |
| `forge plan TASK` | Returns a markdown plan (goal, steps with risk levels, files, network, open questions). No execution. |
| `forge doctor` | Verify Ollama, model, paths, kernel spawn. |

### Inspection & history

| Command | What it does |
|---|---|
| `forge stats [--days N]` | Activity rollup: calls, tokens, cost, latency, gate decisions. |
| `forge cost` | Lifetime cost rolled up for the current workspace. |
| `forge log [-n N]` | Tail the audit JSONL (`./.forge/audit.jsonl`). |
| `forge show SHA` | Show the diff for a shadow-git commit. |
| `forge undo` | Revert the last cell's filesystem changes. |

### Skill management

| Command | What it does |
|---|---|
| `forge skill list` | Installed skills (built-in + `~/.skills/` + workspace). |
| `forge skill search QUERY` | Search GitHub `topic:forge-skill`. |
| `forge skill install SPEC` | Install from a git ref. Refuses floating refs without `--pin`. AST-scans before trust prompt. |
| `forge skill update NAME` | Re-install at latest upstream sha. |
| `forge skill diff NAME` | Show what would change at upstream HEAD. |
| `forge skill show NAME` | Render `SKILL.md` + scan findings. |
| `forge skill permit PAT` | Save an always-allow permission rule (file glob or `Bash(prog:*)`). |

### Long-running

| Command | What it does |
|---|---|
| `forge daemon` | File watchers + cron schedules (foreground). |
| `forge daemon --background` | Detach; logs to `~/.forge/daemon.log`. |
| `forge daemon --status` | Show PID + state. |
| `forge daemon --stop` | SIGTERM the daemon. |

---

## Architecture

```
┌──────────────────────────── CLI (Typer) ──────────────────────────────┐
│  run | chat | plan | stats | log | undo | show | doctor | skill | daemon│
└────────────────────────────────┬──────────────────────────────────────┘
                                 │
                        ┌────────▼──────────┐
                        │     Session       │   one turn = perceive · plan · execute · observe
                        │  (session.py)     │   handles retries, escalation, recovery
                        └─┬──────┬─────────┬┘
                          │      │         │
        ┌─────────────────▼┐    ┌▼─────────▼┐    ┌────────────────┐
        │  ModelRouter      │    │   Kernel    │   │     Gate        │
        │  (router.py)      │    │ (kernel.py) │   │   (gate.py)     │
        │  • Ollama         │    │ • Python -u│   │ • intent parser │
        │  • Anthropic      │    │ • -c worker│   │ • AST safety    │
        │  • OpenAI         │    │ • sandbox  │   │   lint          │
        │  • escalation     │    │   wrap     │   │ • declared vs   │
        │  • cost ceiling   │    │ • health   │   │   actual diff   │
        └───────────────────┘    └─┬──────────┘   └─────────────────┘
                                   │
                       ┌───────────▼──────────────┐
                       │   Tool core (tools.py)    │
                       │  Read · Write · Edit ·    │
                       │  Bash · search · see ·    │
                       │  call_mcp · find_skill ·  │
                       │  run_skill                │
                       │  (protected-path enforced)│
                       └─┬───────────┬───────────┬─┘
                         │           │           │
            ┌────────────▼┐  ┌───────▼─────┐  ┌─▼────────────┐
            │ ShadowGit    │  │  AuditLog   │  │ SkillRegistry│
            │ (shadow.py)  │  │ (audit.py)  │  │ (skills.py)  │
            │ per-cell     │  │ append-only │  │ + Installer  │
            │ commit / undo│  │ JSONL       │  │ + AST scan   │
            └──────────────┘  └─────────────┘  └──────────────┘
```

### Modules (21)

| File | Role |
|---|---|
| `cli.py` | Typer command surface; each public subcommand lives here. |
| `session.py` | The agent loop. Drives the model, runs cells, handles retries, escalation, recovery. |
| `router.py` | Multi-provider selection, escalation policy, cost accounting. |
| `providers.py` | Ollama (native `/api/chat`) + Anthropic + OpenAI implementations. |
| `kernel.py` | Persistent Python subprocess; sandbox wrapping; health tracking; nonce-framed protocol. |
| `sandbox.py` | macOS `sandbox-exec` profile generation. Reads open; writes scoped to workspace + state dirs. |
| `gate.py` | Parses the `intent` block, AST-lints the cell, compares declared vs actual writes/network. |
| `tools.py` | `Read`/`Write`/`Edit`/`Bash`/`search`/`see`/`call_mcp`/skill helpers + protected-path enforcement. |
| `preview.py` | Static + overlay-based dry-run previews. Returns real diffs without touching the workspace. |
| `permissions.py` | Session-scoped allow rules + persistent `~/.forge/permissions.toml`. |
| `shadow.py` | Per-cell git auto-commits to a shadow branch; `forge undo` reverts. |
| `audit.py` | Append-only JSONL audit log. |
| `skills.py` | SKILL.md folder registry; built-in + `~/.skills/` + workspace discovery. |
| `installer.py` | `forge skill install` — git clone at pinned SHA, AST scanner, trust prompt with cooldown. |
| `mcp.py` | MCP stdio client with 1MB readline cap + RecursionError handling. |
| `daemon.py` | File watchers + cron scheduler. |
| `repl.py` | `prompt_toolkit` chat REPL with slash menu + history. |
| `config.py` | Defaults, paths, protected lists, pricing, options. |
| `errors.py` | Error taxonomy + boundary wrapping. |
| `log.py` | Structured logging. |
| `_subprocess_env.py` | `build_minimal_env` — strips provider keys before any subprocess invocation. |
| `system_prompt.md` | Driver system prompt (stopping criterion, format rules, examples). |

### Turn lifecycle

```
1. User input
     ↓
2. Router picks provider for role (driver | planner | …)
     ↓
3. Provider call → Completion (content, tokens, finish_reason)
     ↓
4. Recovery checks:
     • finish_reason="tool_call_parse_recovered" → retry with stricter format hint
     • empty content                              → retry once with reminder
     • finish_reason="length"                     → log + treat as parse problem
     ↓
5. Gate parses intent + AST-lints code
     • prose_only?  → reply done
     • parse_problems? → retry with format reminder
     • undeclared writes/network/dynamic_code? → confirm prompt
     ↓
6. Preview engine (optional) → overlay diff
     ↓
7. Kernel.execute(code, timeout) → Observation(stdout, stderr, ok)
     ↓
8. ShadowGit.commit_post() + AuditLog.write("cell.exec", …)
     ↓
9. Synthetic Observation feedback (with stop-hint if cell ran cleanly)
     ↓
   Loop to step 3 until prose reply or cell cap reached
```

### Recovery & resilience

The session loop handles a handful of failure modes that don't terminate the turn:

| Mode | Trigger | Action |
|---|---|---|
| `tool_call_parse_recovered` | Ollama's harmony parser crashes on plain Python output; we extract the model's raw text from the 500 error body. | Retry with a stricter format reminder. Never displayed as the final reply. |
| Empty completion | Model returned no content. | One retry with format reminder, then fail honestly. |
| Format failure | Gate parsed no cell or invalid YAML in `intent` block. | Up to 2 retries; bumps the parse-fail counter that escalates on the third. |
| Intent mismatch | Declared writes/network ⊂ actual. | Confirm prompt in interactive mode; deny in auto mode. |
| Kernel wedged | Too many consecutive errors. | Surface a clear error; user can `/reset` or restart. |
| Loop on null result | Model under-trusts `0`/`[]`/`False`. | System prompt + observation hint push it toward terminating in prose. |

---

## Safety model

Forge is a **trust-mode agent**: assume the model is mostly cooperative, defend against accidents and misconfigurations, not against a determined attacker with code execution.

### Defense layers

| Layer | Always on | What it stops |
|---|---|---|
| Protected-paths denylist (in-process) | ✅ all platforms | Any tool the agent invokes (Read/Write/open/shutil/subprocess) refusing direct access to `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.zshrc`, `~/.bashrc`, gitconfig, netrc, etc. Fail-closed on unparseable paths. |
| macOS `sandbox-exec` profile | ✅ on macOS | OS-level boundary. Writes scoped to workspace + `~/.forge` + `~/.skills` + tmp. Reads are open (gpt-oss reasoning needs to read your files). Network: localhost only. Bypasses of layer 1 (`os.open`, `ctypes`) still hit this. |
| Skill installer AST scan | ✅ | `eval`/`exec`/`getattr_builtins`/`subprocess`/`ctypes` flagged at install time with a 5-second trust cooldown. |
| Git-based undo | ✅ | Every cell auto-committed; `forge undo` reverts in one command. |
| Dry-run preview | Optional | Real diff against an overlay before approval. Workspace untouched on reject. |
| Subprocess env minimization | ✅ | Provider API keys stripped from every subprocess invocation (`build_minimal_env`). |
| Cost ceiling | ✅ | Per-session `cost_ceiling_usd`; further provider calls raise `CostCeilingExceeded`. |

### What we DO NOT protect against

Read [docs/SAFETY.md](docs/SAFETY.md) for the full threat model. The short version:

- **A model that wants to exfiltrate.** Reads are open; a determined model can include your file contents in its next provider call.
- **Hostile skills.** AST scanner is a smell test, not a sandbox. Untrusted skills should be run with sandbox-exec.
- **Side channels.** Sub-100-byte covert channels via timing, error messages, log content. Out of scope.
- **Compromise of the kernel subprocess.** If something escapes the kernel sandbox, you have a bigger problem than Forge.

### Reporting vulnerabilities

See [SECURITY.md](SECURITY.md). TL;DR: open a private security advisory on GitHub.

---

## Configuration

### Environment variables

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Adds Anthropic to the escalation chain (`claude-sonnet-4-6` by default). |
| `OPENAI_API_KEY` | Adds OpenAI to the escalation chain (`gpt-5` by default). |
| `FORGE_OLLAMA_URL` | Override Ollama base URL. Default `http://localhost:11434/v1`. |
| `FORGE_USE_V1_OLLAMA=1` | Use legacy `/v1/chat/completions` instead of native `/api/chat`. Escape hatch. |
| `FORGE_KEEP_ALIVE` | Ollama `keep_alive` value. Default `24h`. |
| `FORGE_HOME` | Override the state dir. Default `~/.forge`. |

### `~/.forge/pricing.toml`

```toml
[pricing."claude-sonnet-4-6"]
input = 1.5     # $/1M tokens, your enterprise rate
output = 7.5

[pricing."my-self-hosted-llm"]
input = 0.0
output = 0.0
```

### `~/.forge/protected_paths.yaml`

Extend the denylist with paths specific to your machine. Built-in defaults cover `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, gitconfig, netrc, shell rc files, and their `.bak`/`.old`/`.save`/`.swp` variants.

### `~/.forge/permissions.toml`

Persistent always-allow rules. Set interactively via `/permit` in chat, or directly:

```toml
[[allow]]
pattern = "Bash(pytest:*)"

[[allow]]
pattern = "Write(./out/**)"
```

---

## Writing skills

A skill is a folder. Drop it in `~/.skills/`, in `./skills/` of your workspace, or install it from a git repo.

```
my-skills/pdf-extract/
├── SKILL.md            # required: frontmatter + procedural body
└── helpers.py          # optional: main(**kwargs) entry point
```

`SKILL.md`:

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

Procedural knowledge here. The agent reads this when it picks the skill;
keep it focused and concrete.
```

Install from a git repo at a pinned SHA:

```bash
forge skill install alice/forge-skills@a3f9c2c
```

The installer refuses floating refs (`main`, `HEAD`) without `--pin`, AST-scans every `.py` for `eval`/`exec`/`subprocess`/`ctypes`/`getattr_builtins`, shows findings, and enforces a 5-second cooldown before the trust prompt.

Three skills ship in the repo as reference implementations under `skills/`:

- `project-stats` — language breakdown + LOC summary
- `tidy-imports` — ruff-driven import sweep
- `changelog-from-git` — grouped commit log between two refs

---

## Daemon mode

Long-lived process that triggers agent runs on file events or cron schedules. Configure in `~/.forge/daemon.toml`:

```toml
[watchers.downloads-triage]
path = "~/Downloads"
pattern = "*.pdf"
event = "created"
task = "Triage the PDF at {path} and write a summary next to it."
cooldown_s = 5

[schedules.daily-standup]
cron = "0 9 * * 1-5"           # 9am weekdays
task = "Run the daily-standup skill and write to ./standup.md"
workspace = "~/work"
```

```bash
forge daemon                    # foreground, logs to console
forge daemon --background       # detach, logs to ~/.forge/daemon.log
forge daemon --status           # show pid + state
forge daemon --stop             # SIGTERM
```

A sample config lives at [`examples/sample-config.toml`](examples/sample-config.toml). Two end-to-end recipes:

- [`examples/triage-inbox/`](examples/triage-inbox/) — file watcher that summarizes PDFs as they land
- [`examples/daily-standup/`](examples/daily-standup/) — cron-driven git log + open-PR summary
- [`examples/codebase-tour/`](examples/codebase-tour/) — chat-mode walkthrough using `project-stats` + `see()`

---

## Development & testing

```bash
git clone https://github.com/Rohan-Tiwari/forge.git
cd forge
pip install -e ".[dev]"
```

### Run the test suite

```bash
pytest                    # 421 tests, ~11s on M1
pytest -x -q              # fail-fast, quiet
pytest tests/test_session.py     # one file
pytest -k "harmony"       # one topic
```

The suite covers the safety-critical paths: the gate (intent + AST lint), the kernel protocol, the router with fake providers (no real API calls), the sandbox profile generator, the skill installer + AST scanner, the protected-paths layer, the daemon scheduler, and the recovery loops. Tests use `tmp_path` and fake providers throughout — no network, no real model.

### Linting & types

```bash
ruff check src tests
ruff format --check src tests
mypy src                  # mypy --strict is roadmap; some src/ modules still WIP
```

Pre-commit hooks are configured in `.pre-commit-config.yaml`:

```bash
pre-commit install
pre-commit run --all-files
```

### CI

`.github/workflows/ci.yml` runs the full suite on every push + PR across:

- **OS:** `macos-latest` + `ubuntu-latest`
- **Python:** 3.11, 3.12, 3.13

A separate `release.yml` triggers on `v*.*.*` tags, builds the wheel + sdist, validates with `twine check`, and attaches the artifacts to a GitHub Release with release notes pulled from `CHANGELOG.md`.

### Project layout

```
forge/
├── src/forge/            # 21 modules; see Architecture above
├── tests/                # 421 tests, pytest, fake providers + tmp_path
├── docs/                 # MkDocs site source
│   ├── index.md
│   ├── quickstart.md
│   └── SAFETY.md
├── examples/             # end-to-end recipes
│   ├── triage-inbox/
│   ├── daily-standup/
│   └── codebase-tour/
├── skills/               # reference skills shipped with the repo
│   ├── project-stats/
│   ├── tidy-imports/
│   └── changelog-from-git/
├── scripts/release.sh    # cut a new version
├── .github/workflows/    # ci.yml + release.yml
├── pyproject.toml        # hatchling, deps, ruff, mypy
├── mkdocs.yml
├── CHANGELOG.md          # Keep a Changelog
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE               # Apache-2.0
```

### Releasing a new version

Use `scripts/release.sh`, or manually:

```bash
# 1. bump version in pyproject.toml + src/forge/__init__.py
# 2. add a section to CHANGELOG.md
# 3. commit, then:
git tag -a v0.2.5 -m "v0.2.5 — short description"
git push origin main v0.2.5
```

The `release` workflow on GitHub Actions builds the wheel + sdist, runs `twine check`, extracts the new CHANGELOG section, and creates the GitHub Release with both artifacts attached.

---

## Releases

Wheel + sdist are attached to each GitHub Release.

- **Latest:** [v0.2.4](https://github.com/Rohan-Tiwari/forge/releases/latest)
- **All releases:** [releases](https://github.com/Rohan-Tiwari/forge/releases)
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)

Install any version:

```bash
pip install https://github.com/Rohan-Tiwari/forge/releases/download/v0.2.4/forge_agent-0.2.4-py3-none-any.whl
```

---

## Roadmap & status

**v0.2.4 — current.** Production-grade for personal use. Stable Ollama wire protocol (native `/api/chat` with parse-error recovery), session-layer guards against null-result loops, 421 tests across safety-critical paths, CI across macOS + Linux × Python 3.11–3.13, GitHub Releases pipeline.

**On deck:**

- [ ] `mypy --strict` across `src/` (currently partial)
- [ ] Coverage report + gap-fill pass
- [ ] Linux sandboxing story (currently graceful fallback to layer 1 only)
- [ ] Per-skill sandbox-exec profiles for untrusted skills

See [CHANGELOG.md](CHANGELOG.md) for the full version history. Issues and feature requests welcome on [GitHub Issues](https://github.com/Rohan-Tiwari/forge/issues).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version:

1. Open an issue first for non-trivial changes.
2. Pre-commit hooks must pass: `pre-commit install && pre-commit run --all-files`.
3. Tests must pass: `pytest`. Add tests for new code paths.
4. Update `CHANGELOG.md` under `## [Unreleased]` for user-facing changes.
5. PRs target `main`.

Areas where outside help is especially welcome:

- Linux sandboxing (bubblewrap / nsjail / seccomp-bpf profiles)
- Per-skill sandbox profiles
- More reference skills (open a PR to add one under `skills/` or publish your own with `topic:forge-skill`)
- Coverage of edge cases in the gate's AST analyzer

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
