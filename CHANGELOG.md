# Changelog

All notable changes to Forge are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.2] — 2026-06-25 — Ollama harmony tool-call wire fix

### Fixed

- **Ollama harmony tool-call HTTP 500 regression** (issue surfaced in real
  sessions running `gpt-oss:20b` for filesystem queries). Ollama's
  OpenAI-compatible `/v1/chat/completions` endpoint runs the model's output
  through its **harmony** parser, which intermittently misclassifies plain
  Python code blocks (from our intent+py output) as `python` tool calls,
  then crashes JSON-parsing the source as tool arguments — surfacing as:
  > `error parsing tool call: raw='from pathlib import Path...', err=invalid character 'r' in literal false`

  This was a **wire-protocol** bug masquerading as a prompt-engineering
  bug — no amount of "do NOT call tools" framing in the system prompt
  prevents the server from running its parser. Fixed in two layers:

  1. **Native endpoint by default.** `OllamaProvider` now talks to
     `/api/chat` directly (via stdlib `urllib`) instead of going through
     the OpenAI SDK's `/v1/chat/completions`. `think=False` rides on the
     wire body, not in an `extra_body` shim. Streaming uses NDJSON from
     the same endpoint.
  2. **Parse-error recovery.** When Ollama's harmony parser does fire
     and returns HTTP 500, we extract the model's raw output from the
     `raw=...` field of the error body via regex and return it as a
     `Completion` with `finish_reason="tool_call_parse_recovered"`. The
     turn keeps moving instead of failing.

  The legacy `/v1/chat/completions` path remains available behind
  `FORGE_USE_V1_OLLAMA=1` as an escape hatch.

### Tests

- 6 new tests in `tests/test_router_providers.py` covering: raw-output
  extraction from a parse-error body, parse-error → recovered Completion,
  unrelated 4xx errors still raising, unreachable Ollama giving a friendly
  error, and the happy path verifying `/api/chat` is hit with `think=False`
  on the wire. Total: 418 tests passing (412 → 418).

## [0.2.1] — 2026-06-25 — production-grade hardening

A polish release focused on **closing every known issue before broader
distribution**. 412 tests passing (+ 69 from v0.2.0). 5 critical security
findings from an internal audit closed; 7 deferred dogfood findings closed.

### Security (5 critical findings from `docs/V021-AUDIT.json`)

- **Sandbox network rule was a silent no-op.** `_make_network_rules` emitted
  a bare `(allow network-outbound)` whenever the allowlist was non-empty,
  and the default kernel config passed `["localhost", "127.0.0.1"]` — so
  the sandbox profile literally allowed arbitrary outbound traffic. Fixed:
  emit informational comments only; sandbox-exec cannot filter by hostname,
  so non-localhost outbound is now genuinely denied. (commit `9cefc7b`)
- **Git argument injection in installer.** A crafted ref like
  `--upload-pack=/path/evil` was passed positionally to `git fetch` and
  `git checkout`, allowing arbitrary local binary execution during clone.
  Fixed: `re.fullmatch(r"[A-Za-z0-9._/-]+")` validation, reject leading
  `-`, reject `..`, use `--` positional separator. Also clone now uses
  isolated git config so smudge filters / hooks / credential helpers
  can't run.
- **MCP unbounded readline + RecursionError.** A hostile MCP server could
  OOM the parent via unbounded `stdout.readline()`; deeply nested JSON
  raised `RecursionError` which escaped the loop. Fixed: 1MB line cap +
  catch `RecursionError`.
- **Dry-run absolute-path escape.** The dry-run subprocess set `cwd=overlay`
  but had no FS jail. Cells writing to absolute paths or with `..` reached
  the real filesystem and were INVISIBLE to the preview. Fixed: wrap
  Write/Edit/open with `_path_inside_overlay` guard; fail closed on
  forge.tools import error (no more permissive stdlib fallback).
- **Provider secrets inherited into every subprocess.** Kernel worker, MCP
  servers, AND dry-run subprocess all used `os.environ.copy()`, exposing
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GITHUB_TOKEN` to any spawned
  process. Fixed: new `forge._subprocess_env.build_minimal_env()` builds
  child env from a strict allowlist (PATH, HOME, USER, LANG, etc.) plus
  caller-supplied extras. Wired into all three spawn sites.

### Deferred dogfood findings from W4-DOGFOOD.md (commit `1895702`)

- `#4` Plan-mode help text clarified re: `.forge/` workspace bootstrap
- `#5` Planner prompt rewritten with "minimal one-shot solutions" principle
- `#6` `forge run --show-stdout` flag adds last cell's stdout to reply panel
- `#9` `forge skill search` disambiguates "no matches" vs "rate-limited"
- `#10` `_load_pricing_override` now logs warnings via `logging.warning`
- `#11` Pricing.toml hot-reload — `price()` stats the file on every call
- `#12` `_PRICING` merged dict has inspection-only comment

### Infrastructure

- **`forge.log` module** — structured logging via stdlib `logging`. `FORGE_LOG_LEVEL`
  env var + `FORGE_LOG_FILE` for rotating-file output. Idempotent
  `setup_logging()`. Wired into CLI startup.
- **`forge.errors` module** — unified `ForgeError` exception hierarchy.
  Legacy exceptions registered as virtual subclasses via `ABCMeta.register`
  so `isinstance(err, ForgeError)` works without touching their `__bases__`.
- **`~/.forge/config.toml`** — optional user-level defaults (`ollama_url`,
  `driver_model`, `num_ctx`, `keep_alive`, `cost_ceiling_usd`). Layered
  under env vars, layered over hardcoded defaults. See
  `examples/sample-config.toml`.
- **Ruff clean across `src/` + `tests/`.** 220+ auto-fixed; remaining
  ignores documented in `pyproject.toml` with rationale.
- **CI workflow** (`.github/workflows/ci.yml`): pytest + ruff on
  macos-latest + ubuntu-latest across Python 3.11/3.12/3.13. Build check
  verifies wheel + sdist generate correctly.
- **Publish workflow** (`.github/workflows/publish.yml`): tag-triggered
  PyPI publishing via Trusted Publishing. `-rc` tags → Test PyPI; stable
  tags → real PyPI.
- **Pre-commit hooks** (`.pre-commit-config.yaml`): ruff format + ruff
  check + trailing-whitespace + end-of-file-fixer + check-toml + a custom
  leak-scan hook that blocks commits containing GitHub PATs / Anthropic /
  OpenAI keys.
- **Release script** (`scripts/release.sh`): one-command version bump
  + CHANGELOG section + tag + push for patch/minor/major/rc.
- **PyPI metadata complete.** `forge_agent-0.2.1-py3-none-any.whl` builds
  and passes `twine check`. `py.typed` marker for downstream type-checkers.
  Full `[tool.hatch.build.targets.sdist]` include list for reproducible
  source distributions.
- **CONTRIBUTING.md + SECURITY.md** added.
- **MkDocs site** (`mkdocs.yml`, `docs/index.md`, `docs/quickstart.md`):
  Material theme, mkdocstrings auto-API, ready to deploy to GitHub Pages.

### Tests

**412 passing** (up from 343). New test suites:

- `test_log_and_errors.py` (23 tests) — logger config + hierarchy
- `test_config.py` (9 tests) — env / config.toml / defaults layering
- `test_security_fixes.py` (37 tests) — every critical finding has a
  regression test (the git ref exploit vector is parametrized with the
  exact `--upload-pack=/path/evil` payload)

### Deps

- `+build>=1.0` (dev — sdist/wheel build verification)
- `+twine>=5.0` (dev — PyPI metadata validation)
- `+pytest-timeout>=2.4` (dev — already used, now declared)
- `+mkdocs>=1.6`, `+mkdocs-material>=9.5`, `+mkdocstrings[python]>=0.27`
  (docs — optional extra, `pip install forge-agent[docs]`)

### Internal documentation

- `docs/V021-AUDIT.json` — full structured audit output (input to v0.2.1 work)
- `docs/W4-DOGFOOD.md` — the dogfood report from the prior release

---

## [0.2.0] — 2026-06-24 — Waves 1-4

Major release. 5 waves of features, **331 tests passing**, ~9,000 LOC of
source. Honest scope: feature-complete on the architecture from the original
plan; production deployment of untrusted skills still gated on Wave-5 work
(see SAFETY.md).

### Highlights

- **Vision** — `see(image)` reads images via local Qwen2.5-VL
- **Streaming** — token-by-token rendering in chat
- **Multi-line REPL** — prompt_toolkit with history + slash completion
- **Multi-provider** — Anthropic + OpenAI as auto-detected escalation chain
- **Dry-run preview** — cells execute against an overlay, real diffs surface
- **macOS sandbox** — `sandbox-exec` profile bounds the kernel
- **MCP** — `call_mcp(server, tool, **args)` talks to stdio MCP servers
- **Skill installer** — git-pinned + AST-scanned skill install
- **Skill discovery** — `forge skill search` queries GitHub
- **Plan mode** — `forge plan TASK` returns markdown plan, no execution
- **forge stats** — per-window activity rollup
- **Pricing override** — `~/.forge/pricing.toml`
- **Daemon mode** — file watchers + cron schedules

### Wave 4 (this release): polish + power features

- **W4a — Daemon mode** (commit `fcc4150`): `forge daemon` with file watchers
  + cron schedules + double-fork backgrounding. Custom 5-field cron parser.
  Per-watcher debouncing. 29 new tests.
- **W4b — Pricing.toml override** (commit `762772a`): users customize
  per-model rates without editing source. Override merged over baseline.
- **W4c — Plan mode** (commit `762772a`): markdown plan with goal / steps /
  risk levels / files / network / open questions. No execution.
- **W4d — Skill discovery** (commit `762772a`): `forge skill search` queries
  GitHub `topic:forge-skill`. `forge skill update` re-installs at upstream
  HEAD.
- **W4e — forge stats** (commit `762772a`): per-window summary with sessions,
  calls, tokens, cost, latency p50/p95, gate decisions, top models.
- **W4g — Polish** (this commit): version bump to 0.2.0, comprehensive README
  rewrite, 3 working `examples/` recipes (triage-inbox, daily-standup,
  codebase-tour), CHANGELOG.

### Tests

**331 passing** (+302 from v0.1.0). New test suites:
- `test_vision.py` (15) — see() against mocked + real Qwen
- `test_streaming.py` (9) — streaming + REPL session construction
- `test_router_providers.py` (24) — provider routing, escalation, pricing
- `test_mcp.py` (20) — MCP client against in-process fake server
- `test_installer.py` (39) — skill install end-to-end from real local git
- `test_dry_run.py` (12) — overlay execution safety invariant
- `test_sandbox.py` (24) — sandbox-exec profile + real boundary firing
- `test_pricing_and_plan.py` (16) — pricing override + plan mode
- `test_daemon.py` (29) — cron parser + watcher debouncer + PID helpers

### Deps added in v0.2

- `anthropic>=0.40` — Anthropic SDK for the escalation chain
- `prompt_toolkit>=3.0` — multi-line REPL
- `watchdog>=4.0` — daemon mode file watchers
- (already there from v0.1) `tomli-w>=1.0`

---

## [Unreleased] — Waves 2 + 3: real escalation, MCP, skill installer, dry-run, sandbox

### Wave 2a — Multi-provider router with real escalation (commit 666fd35)

- **`forge.providers`** module — Provider Protocol with three implementations:
  - `OllamaProvider` (default catch-all, talks to `localhost:11434/v1`)
  - `AnthropicProvider` — `claude-*` models via the official anthropic SDK; reads `ANTHROPIC_API_KEY`
  - `OpenAIProvider` — `gpt-*` / `o3-*` / `o4-*` via openai SDK; reads `OPENAI_API_KEY`
  - All three implement both `complete()` and `complete_stream()`
- **`router.py`** is now a thin orchestrator over the provider chain:
  - Picks first provider whose `handles(model_id)` returns True
  - Walks role's escalation chain on errors
  - Per-provider cost accounting in `cost_summary().by_provider`
  - **Auto-detects API keys**: if `ANTHROPIC_API_KEY` is set, `claude-sonnet-4-6` is appended to driver's escalation chain. Same for OpenAI.
- **Escalation triggers** — wired in `session.turn()`:
  - 2× consecutive intent-mismatch → next call escalates
  - 2× consecutive parse-format-fail → next call escalates
  - `/escalate` slash command → explicit, one-shot
  - Successful cell resets escalation state
- Tests: 24 new (`test_router_providers.py`)

### Wave 2b — MCP integration (commit 1a9f486)

- **`forge.mcp`** module — stdio MCP client (~430 LOC, no third-party SDK):
  - JSON-RPC 2.0 over stdin/stdout
  - Initialize handshake (protocol version 2024-11-05)
  - Lazy server spawn on first `call_mcp(...)`
  - Per-server lock serializes concurrent tool calls
  - Background stderr drainer prevents pipe blocking
  - Tool list cached after handshake
  - Error responses (isError) raise `MCPCallError`
- **`call_mcp(server, tool, **args)`** is now a real kernel global. Wired by `Session.start()` from `MCPRegistry`.
- Configure servers in `~/.forge/mcp.toml`:
  ```toml
  [servers.gh]
  cmd = "npx"
  args = ["-y", "@modelcontextprotocol/server-github"]
  ```
- Tests: 20 new (`test_mcp.py`) — including a tiny in-process fake JSON-RPC server.

### Wave 3a — Skill installer + AST scanner (commit 5dbe7db)

- **`forge.installer`** module + `forge skill install` / `forge skill diff`:
  - Spec parsing: `alice/repo@sha`, full `https://...` URL, `git@host:path` SSH form, `:subdir` selector
  - **Refuses floating refs** (main, master, HEAD, develop) without `--pin`
  - Content-addressed pinning to `~/.skills/installed/<source>/<name>@<sha>/`
  - **AST scanner** flags at install time: `eval`, `exec`, `compile`, `os.system`, `subprocess.*`, `getattr(__builtins__, ...)`, `import ctypes`
  - 5-second cooldown before confirmation prompt (anti-muscle-memory)
  - Critical findings → red warning before confirm prompt
  - Manifest at `~/.skills/manifest.toml` round-tripped via tomllib + tomli_w
- Tests: 39 new (`test_installer.py`) — including end-to-end installs from real local git repos built per test.

### Wave 3b — Dry-run preview engine (commit d2dec5e)

- **`Preview.from_dry_run()`** replaces static AST-only previews with REAL diffs:
  - Copies workspace → overlay tmpdir (skipping `.git`, `.forge`, `__pycache__`, `.venv`, `node_modules`)
  - Runs the cell in a fresh subprocess with `cwd=overlay`
  - **Stubs Bash + network** so dry-run is filesystem-only
  - Walks both trees afterward; computes unified diffs for create/modify/delete
  - Discards overlay; **nothing in the user's workspace changed**
- New `delete` `FileChange.kind` for files the cell removes (red `-` marker in render)
- `Session(dry_run=True)` (default) — falls back silently to static for syntax errors, oversized workspaces (>50MB), or subprocess crashes
- CLI: `--no-dry-run` flag on both `forge run` and `forge chat`
- Tests: 12 new (`test_dry_run.py`) — including the safety invariant *"dry-run doesn't touch the real workspace"*.

### Wave 3c — Real macOS sandbox-exec boundary (commit a519c5b)

- **`forge.sandbox`** module — generates sandbox-exec TinyScheme profiles:
  - `deny default`
  - `file-write*` only to: workspace, `~/.forge`, `~/.skills`, `/tmp`, `__pycache__/*.pyc`
  - `file-read*` allowed (in-process protected-paths handles read exfil)
  - `process-fork` + `process-exec*` allowed for `/bin/sh`, `/usr/bin/*`, `/opt/homebrew/*`
  - `network-bind` only on localhost; outbound denied unless `allowed_network_hosts` is non-empty
- **`Kernel(sandboxed=True)`** (default) wraps the worker subprocess in `sandbox-exec`. Silently no-ops on non-macOS.
- `Session(sandboxed=True)` parameter propagates to Kernel.
- `FORGE_DISABLE_SANDBOX=1` escape hatch.
- **Two-layer defense in depth**: in-process `forge.tools` denylist + OS-level `sandbox-exec`. A bypass via `os.open` or `ctypes` still hits the OS layer.
- Tests: 24 new (`test_sandbox.py`) — including a real `sandbox-exec` boundary test that actually fires on macOS.

### Test summary

| | v0.1.0 | After Wave 1 | After Wave 2+3 |
|---|---:|---:|---:|
| Tests | 144 | 167 | **286** |
| Source LOC | 3,704 | 4,718 | **~7,500** |
| Modules | 11 | 12 | **17** *(+providers, mcp, installer, sandbox, repl)* |

### Deps

- `+anthropic>=0.40` (Wave 2a)
- `+prompt_toolkit>=3.0` (Wave 1)
- `+tomli-w>=1.0` (already there from v0.1)

---

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
