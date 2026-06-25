# Quickstart

## Prerequisites

- **macOS 14+** on Apple Silicon (Linux works but `sandbox-exec` is
  macOS-only; on Linux you fall back to the in-process protected-paths
  layer alone)
- **Python 3.11+**
- **Ollama 0.30+** with `gpt-oss:20b` pulled (~14 GB)
- **Optional:** `qwen2.5vl:7b` (~6 GB) for the `see()` vision sub-skill
- **Optional:** `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` for escalation

## Install Ollama + models

```bash
brew install ollama
brew services start ollama
ollama pull gpt-oss:20b
ollama pull qwen2.5vl:7b   # optional, for vision
```

## Install Forge

From PyPI (once published):

```bash
pip install forge-agent
```

From source:

```bash
git clone https://github.com/Rohan-Tiwari/forge.git
cd forge
pip install -e .
```

Verify:

```bash
forge doctor
```

Should print green ticks for ollama reachable, model present, dirs
created.

## First run

```bash
forge run "How many Python files in this project, and what's the total LOC?"
```

Output:

```
session 1781734949-49465 · workspace /Users/you/myproject
driver: gpt-oss:20b · skills: 0 · mode: auto/preview=cells

ran 1 cells, denied 0, escalations 0, cost $0.0000
╭─────────────────────────────────── reply ────────────────────────────────────╮
│ This project has 47 Python files totaling 8,231 lines of code.               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## Interactive chat

```bash
forge chat
```

In the REPL:

- **Multi-line input** — Enter inserts a newline; Esc-Enter submits
- **History** — `Ctrl-R` searches like in bash, persisted at `~/.forge/chat-history`
- **Slash commands** — type `/` for an autocomplete menu
    - `/undo` revert the last cell's filesystem changes
    - `/cost` show session spend
    - `/reset` clear the kernel's global namespace
    - `/preview always|cells|never` change preview mode
    - `/escalate` next call uses the next model in the chain
    - `/skills` list installed skills
    - `/help` full list
    - `/exit`

## Plan first, run later

For high-stakes tasks, get a structured plan before executing:

```bash
forge plan "Refactor every test file to use parametrize fixtures"
```

Returns a markdown plan with goal, steps with risk levels, files touched,
network calls, and open questions. No cells execute. Once you've reviewed:

```bash
forge run "Refactor every test file to use parametrize fixtures"
```

## What's next

- [Architecture](architecture.md) — what each module does
- [Writing skills](skills.md) — package reusable workflows
- [Safety model](SAFETY.md) — what we defend against (and what we don't)
