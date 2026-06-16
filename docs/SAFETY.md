# Forge — safety model and threat model

**Read this before installing any third-party skill.**

Forge runs LLM-emitted Python on your machine without containment. This is a
deliberate trade-off — the v0.1 design is "trust mode for personal use",
where the safety story rests on **observability + reversibility + careful
defaults**, NOT on isolation.

## What Forge protects against

| Threat | Defense | Effective against |
|---|---|---|
| Buggy code overwrites the wrong file | Diff preview + git auto-commit + `forge undo` | The accidental-mistake case (most common) |
| Cell tries to write `~/.ssh`, `~/.aws`, `~/.zshrc`, etc. | Hardcoded `PROTECTED_PATHS` list (case-insensitive on macOS, sibling-glob `~/.zshrc.*`, fail-closed on parse errors) | All literal-path attempts |
| Cell tries to read `~/.ssh/id_rsa` | `assert_readable` blocks reads of protected paths too (was a Day-0 hole) | Direct read attempts via `open`, `os.open`, `shutil.copy`, `Bash("cat ~/.ssh")`, `Bash("cp ~/.ssh ...")` |
| Cell does `rm -rf /`, `sudo`, `git push --force`, `terraform apply` | Hardcoded `PROTECTED_ACTIONS` list, checked at every Bash + subprocess invocation | All literal command-substring attempts |
| Cell wraps the open() guard via `os.open` / `shutil.copy` | We patch `os.open` and `shutil.copy/copy2/copyfile/copytree/move` too | These specific paths |
| Cell composes destructive ops in a loop | The protected-action denylist is checked PER invocation, not at cell level (composition can't smuggle past) | The named verbs |
| Cell writes broken Python in a refactor | Post-write `ast.parse` self-check; if invalid, cell `ok=False` is recorded honestly | Syntax errors in `.py` writes |
| Want to undo what just happened | Shadow git auto-commits per cell + `forge undo` | All FS mutations within the workspace |
| Long context blowing up | Char-count-based history truncation when context > 80% full | Sessions ≤ 100 turns |

## What Forge does NOT protect against

These are the honest gaps. v0.1 trades safety for usability; v0.2 will close
some via `sandbox-exec` profiles per skill.

| Threat | Status | Notes |
|---|---|---|
| Determined attacker who has read forge's source | **Not protected** | Trust mode means no boundary. Don't run untrusted skills. |
| Skill prompt injection via `references/*.md` | **Partial** | Content is wrapped in `<skill-content trusted="false">` and the system prompt instructs the model to ignore instructions inside. Reduces the bar but doesn't eliminate it. |
| Skill author reaches `forge.tools._OPEN_*` private attributes via gc/inspect | **Not protected** | We hide originals from module surface but a determined Python user can find them. The remediation is: **read skills before installing them**. |
| Resource exhaustion (fork bomb, memory bomb) | **Not protected** | Cell timeout (120s default) prevents infinite loops but doesn't bound memory. v0.2: ulimit on worker. |
| Network exfil via DNS or covert channel | **Not protected** | We block obvious `requests.get(evil)` via undeclared-network gate, but DNS, GET to allowlisted CDNs with encoded data in path, etc., are out of scope. |
| Anything outside the workspace tree | **Partial** | Reads/writes outside `cwd` are flagged in CONFIRM (gate) but allowed if user approves. v0.2: hard `WorkspaceEscapeError` unless explicitly opted in. |

## When Forge IS safe enough

- Your own machine, your own skills you wrote yourself
- First-party / curated skills you've read line-by-line
- Side-effect-free analysis tasks (search, summarization, count)
- Tasks where you sit and watch the previews go by

## When Forge IS NOT safe enough

- Untrusted skills downloaded from the web running unattended
- Multi-tenant or shared machines
- Production systems where one mistake costs money or trust
- Long-running background loops without a human in the loop

For those cases, **wait for v0.2** which will bolt on `sandbox-exec` per-skill
profiles (the kernel is already a child subprocess; the supervisor can spawn
it under a profile without rewriting anything else).

## How the layers actually work

```
┌────────────── User input ──────────────┐
│  forge run / forge chat                │
└──────────────────┬─────────────────────┘
                   │
        ┌──────────▼──────────┐
        │  Model (gpt-oss)    │  emits intent + py fence
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Gate (gate.py)     │  AST lint
        │  • intent honesty   │
        │  • declared writes  │  ⚠ heuristic, NOT authority
        │  • dynamic_code     │
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Preview engine      │  what's about to happen
        │  (preview.py)        │
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  PermissionStore     │  pre-approved actions skip prompt
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  User confirms       │  y / n / a (always for session)
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Shadow git pre      │  undo checkpoint
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Kernel (subprocess) │
        │  ─ install_guards()  │  load-bearing safety
        │     • builtins.open  │
        │     • os.open        │
        │     • shutil.*       │
        │     • subprocess.*   │
        │  ─ exec(cell)        │
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Shadow git post     │  rollback point
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  ast.parse(.py wrt)  │  honest ok=True
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │  Audit log (jsonl)   │  fsync per write
        └─────────────────────┘
```

The **load-bearing** layer is the kernel's installed guards. The gate is a
heuristic that improves UX (better preview, better intent honesty checks) but
its decisions are not authoritative — a cell that the gate said `ALLOW` will
still hit the runtime guards on every protected operation.

## Adding to the protected lists (the safe way)

To add a path or action to the denylist, edit `~/.forge/protected_paths.yaml`
or `~/.forge/protected_actions.yaml`:

```yaml
# ~/.forge/protected_paths.yaml
paths:
  - "~/Documents/Personal"
  - "**/secrets.json"
```

```yaml
# ~/.forge/protected_actions.yaml
actions:
  - "git tag -d"
  - "npm publish"
```

These are **additive** — they extend the hardcoded baseline. They cannot
remove or weaken the baseline. If you need to allow something the baseline
forbids, fork forge and edit `forge/src/forge/config.py`.

## Reporting issues

If you find a way to bypass the protections we DO claim, please file an
issue. Trust-mode bypasses are not a security boundary failure — they're a
strengthening opportunity.
