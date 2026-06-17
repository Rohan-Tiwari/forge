# Changelog

All notable changes to Forge are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — Wave 1: vision + streaming + multi-line REPL

### Added

- **Vision sub-skill (`see()`)** — `tools.see(image_or_path)` now talks to a
  local Qwen2.5-VL via Ollama. Accepts file paths, `Path` objects, or raw
  bytes. Per-session result cache (`(image-bytes, prompt)` → description).
  Refuses protected paths, surfaces clear errors on connection failure.
  ~14 unit tests + 1 live integration test.
- **Token-streaming completions** — `router.complete_stream()` yields
  `StreamChunk` deltas as the model generates. `Session.turn(user, on_chunk=...)`
  exposes a callback that receives every delta; `forge chat` uses Rich's
  `Live` region to render tokens live to the TTY, then collapses to the
  final reply panel. Fall-back to buffered mode when `--no-stream` or stdout
  isn't a TTY.
- **prompt_toolkit chat REPL** — multi-line input (Enter for newline,
  Esc-Enter to submit), file-backed history at `~/.forge/chat-history`,
  bracketed paste, slash-command auto-completion (`/exit`, `/undo`, `/cost`,
  `/reset`, `/preview <mode>`, `/skills`, `/help`). History search via
  Ctrl-R works like in bash.
- **CLI flag `--no-stream`** for `forge chat` to disable token streaming.

### Changed

- The vision role now defaults to `qwen2.5vl:7b` (was `qwen2.5-vl:7b` —
  matches Ollama's actual model name).
- `set_skill_runtime()` no longer accepts `see_fn` — `see()` is wired
  directly to Ollama now. The kwarg is kept for backward compat and ignored.

### Tests

- 167 passing, all green. Up from 144 in v0.1.0.
- New: `tests/test_vision.py` (15 tests including 1 live integration),
  `tests/test_streaming.py` (9 tests).

---

## [0.1.0] — initial release

A code-first local agent with skills, multi-provider routing, and trust-mode
safety rails. Built for personal use on macOS Apple Silicon with Ollama
and gpt-oss:20b as the default driver.

### Architecture

- **Code-first action.** Agent emits Python in markdown, harness executes in
  a persistent subprocess kernel. Better composition than JSON tool calls
  ([CodeAct paradigm](https://arxiv.org/abs/2402.01030)).
- **Skills.** Anthropic-style folders with `SKILL.md` (YAML frontmatter +
  body). Two-tier loading: eager metadata (≤5k tokens) + lazy `find_skill()`
  retrieval.
- **Multi-provider router.** Five named roles (`driver` / `planner` /
  `vision` / `classifier` / `summarizer`), each with primary + escalation
  chain. v0.1 ships only the local Ollama path; Anthropic / OpenAI are
  one-line extensions.
- **Trust-mode safety.** Defense via diff preview, git-based undo,
  protected-path/action denylists, and post-write self-checks — NOT
  containment. See [`docs/SAFETY.md`](docs/SAFETY.md) for the honest
  threat model and [`docs/SHAKE-OUT.md`](docs/SHAKE-OUT.md) for the
  pre-launch shake-out report.

### Components

| Module | LOC | What it does |
|---|---:|---|
| `forge.gate` | ~530 | Intent block parser + AST safety lint + GateDecision |
| `forge.tools` | ~470 | `Read` / `Write` / `Edit` / `Bash` / `search` / `see` + protected-paths + protected-actions + builtins/`os.open`/`shutil`/`subprocess` guards |
| `forge.cli` | ~430 | Typer commands: run, chat, log, undo, show, cost, doctor, skill, permission |
| `forge.session` | ~420 | The agent loop + history truncation + post-write checks + retry counters |
| `forge.kernel` | ~390 | Persistent Python subprocess with dedicated result fd, nonce verification, threading-Lock'd execute, SIGTERM→kill timeout |
| `forge.preview` | ~250 | Structured "what's about to happen" with Rich rendering |
| `forge.skills` | ~230 | Anthropic-style folder registry, two-tier loading, find_skill |
| `forge.permissions` | ~210 | Pattern-based allow rules (`Bash(git:*)` / `Write(./out/**)` / `Network(host)`), session + persistent grants |
| `forge.router` | ~220 | Multi-provider router with Day 0 system-prompt fixes baked in |
| `forge.shadow` | ~180 | Git-based undo: `read-tree --reset -u` + `clean -fd`; sha validation |
| `forge.audit` | ~160 | Append-only JSONL with `os.fsync`, `fcntl.flock`, 10MB rotation, ms timestamps |
| `forge.config` | ~225 | Hardcoded protected lists + YAML override loader (additive only) |

**Total: ~3,700 LOC across 11 modules + 144 tests.**

### Safety hardening (post-shake-out)

The pre-launch shake-out workflow surfaced multiple safety holes; all are
now closed. Highlights:

- **Case-insensitive path matching on macOS APFS** — closes the `~/.SSH`
  bypass.
- **Sibling-file glob protection** — `~/.zshrc.bak`, `~/.aws.bak`,
  `~/.bashrc.swp` etc. now protected.
- **Reads of protected paths blocked** — was only writes (Day 0 hole).
- **`os.open`, `shutil.copy/copy2/copyfile/copytree/move` interception** —
  the `builtins.open` guard alone wasn't enough.
- **`subprocess.run`/`subprocess.Popen` scan for protected-path reads** —
  catches `cp ~/.zshrc /tmp/x`, `cat ~/.ssh/id_rsa`, `tar czf - ~/.ssh`.
- **`_ORIGINAL_OPEN` no longer a public attribute** — was a one-line escape.
- **Fail-CLOSED on path-expansion errors** — was fail-open.

### Day 0 system prompt fixes (baked in)

gpt-oss:20b was trained with a built-in `python` tool channel that hijacks
markdown-fence-based instructions. The driver system prompt:

- Frames the role as *"write Python in markdown for an external harness"*
  rather than "execute Python".
- Sets `think=False` on the API call.
- Explicitly forbids tool calls.
- Uses ` ```py ` instead of ` ```python ` as the code fence.

Without these, gpt-oss returns empty content with `finish_reason=tool_calls`
and Ollama returns HTTP 500. With them, Day 0 measured 95% format
compliance, 100% intent honesty, 85% on-task at high effort across 60 calls.

### Preview-and-confirm UX

When a cell has side effects (writes / network / Bash), the user sees a
structured preview before approving:

```
intent: <what the model says it's doing>
flagged: <anything the gate noticed>

code: <the python>
files about to change: <each path with create/modify tag, diffs for modify>
network calls: <hostnames>
bash commands: <each command>
```

User answers `y` (allow once) / `n` (deny — fed back to model as Observation
so it tries a different approach) / `a` (always for this session — adds a
PermissionStore grant for the implied actions). Pre-approved actions skip
the prompt. `forge skill permit` adds persistent grants to
`~/.forge/permissions.toml`.

### CLI

```
forge run "<task>"        — one-shot agent run
forge chat                — interactive REPL with /undo, /cost, /reset, /preview
forge log [-n N] [--full] [--session ID]
forge undo                — revert last cell
forge show <sha>          — diff for a shadow commit
forge cost                — lifetime spend
forge skill list / show / permit
forge doctor              — verify Ollama + model + dirs
forge --version
```

### First-party skills shipped

- `project-stats` — language breakdown + LOC + git info (read-only)
- `changelog-from-git` — group commits by conventional-commits prefix
- `tidy-imports` — pure-Python isort-equivalent across a tree

### Non-goals for v0.1

- **Not a sandbox.** Run untrusted skills at your own risk. v0.2 adds
  `sandbox-exec` profile per-skill.
- **Not a Claude Code replacement.** Forge is for repeated workflows you
  encode as skills; Claude Code is better for general dev pairing.
- **No vision sub-skill yet.** `see()` is wired but no VLM is pulled.
- **No streaming.** Model output is buffered until complete.
- **No MCP integration yet.** `call_mcp()` is a stub.
