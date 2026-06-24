# W4 dogfood — findings + fixes

Pre-release shake-out of all Wave-4 features via parallel workflow (5
component probes + 6 real agent tasks + adversarial synthesis). Findings
synthesized into a ranked v0.2.1 fix list; top 5 fixed in this release.

**No security, correctness, or data-loss defects found.** The top issues
are review-mode UX (plan refusing instead of structured-plan-with-risk),
stats math (small-N percentile collapse), and shell-friendliness (silent
deny when stdin isn't a TTY).

Full structured report: [`W4-DOGFOOD-REPORT.json`](W4-DOGFOOD-REPORT.json).

## Fixed in v0.2.0

### #1 (HIGH) — Plan mode flat-refused destructive intents

`forge plan "delete /etc"` returned `I'm sorry, but I can't help with that.`
with none of the 5 standard sections. That defeats the whole point of plan
mode: showing the user what the agent WOULD do for a risky intent.

**Fix:** planner system prompt rewritten to require structured output even
for destructive tasks (with explicit `Risk: critical` markers and the
refusal rationale moved to Open questions). Plus a defense-in-depth wrap:
if the model still flat-refuses, we synthesize a structured "decline plan"
post-hoc so the contract holds. Test in `test_w4_fixups.py::TestPlanRefusalWrapping`.

### #2 (MEDIUM) — `forge stats` p95 collapsed onto p50 for small N

At N=4 with samples `[0.85, 2.27, 12.56, 13.10]`, both `p50_idx` and
`p95_idx` resolved to index 2, showing `p50 12.56s / p95 12.56s` and hiding
the true tail (13.10s).

**Fix:** nearest-rank percentile (`math.ceil(p*N) - 1`, clamped). For
N < 10 we don't show p50/p95 at all — instead `min / avg / max (N=4)` so
users know it's a small sample. Test in `TestStatsPercentile`.

### #3 (MEDIUM) — Non-TTY `forge run` silently denied piped invocations

A `forge run "task"` from a script (no stdin TTY) hung on the interactive
approval prompt, then got recorded as `gate.user_deny`. Painful in CI and
for daemon trigger scripts.

**Fix:** detect `not sys.stdin.isatty()` in `forge run` and auto-fall-back
to `--preview never` with a printed note. `--auto` still works the same way.

### #7 (LOW) — `--days` validation + pluralization

`forge stats --days 0` printed `last 0 days · 0 sessions`; `--days -1` was
cheerfully accepted; `--days 1` said `last 1 days` (wrong plural).

**Fix:** `typer.Option(min=1)` on the parameter, and pluralization helper
in the header. Tests in `TestStatsDays`.

### #8 (LOW) — Recent-sessions `started` column unreadable

The column was a 12-char tail of the ISO timestamp, producing entries like
`5:02:35.008Z` — single-digit hour, no date, milliseconds dominate.

**Fix:** parse the timestamp and format as `MM-DD HH:MM`. Falls back to the
old slice if parsing fails. Test in `TestStartedColumn`.

## Deferred to v0.2.1 (acknowledged but not fixed in this release)

These are the findings ranked 4-12 from the synthesis. None are blocking
v0.2.0; all have clear suggested fixes in the report.

### #4 (LOW) — `forge plan` creates `.forge/` despite "no files change" promise

The plan command bootstraps the workspace state directory on first use
(creates `.forge/audit.jsonl` and a `shadow:init` commit). User content is
never modified, but the filesystem IS modified. Either defer creation
until first `run` or tighten the help text.

### #5 (LOW) — Planner over-engineers read-only intents

For "count python files" the planner proposed a new `count_py_files.py`
with argparse + tests; the executor ran `Path('.').rglob('*.py')` and
ignored the planner's open questions about `__pycache__`/hidden-dirs/etc.
For this specific tree the answer's correct, but the plan/run divergence
is a latent bug.

### #6 (LOW) — Cell stdout invisible in TUI auto-mode reply panel

`forge run --auto` shows the model's prose reply but not the actual cell
stdout. For verification workflows you have to grep the audit log.

### #9 (LOW) — `forge skill search` empty-vs-rate-limited conflated

Same "no skills found" message regardless of cause. Should inspect
`X-RateLimit-Remaining` and only hint at `GITHUB_TOKEN` when actually
rate-limited.

### #10 (LOW) — `_load_pricing_override` silently swallows malformed entries

User edits `pricing.toml`, types `input = [3.0]` (list instead of float),
the entry vanishes with no warning. Should log via `logging.warning`.

### #11 (LOW) — Pricing override cached at import — daemon serves stale prices

`_PRICING_OVERRIDE` is loaded once at module import. Long-running daemons
won't pick up edits without restart. Either stat the file inside `price()`
or add a CLI `forge reload-pricing` verb.

### #12 (LOW) — `_PRICING` merged dict is exposed but `price()` doesn't read it

Duplicate source of truth. Current code is correct; future refactors
could unify them and silently lose precedence ordering. One-line
inspection-only comment would fix.

## What worked surprisingly well

- **Workspace sandbox correctly blocked `~/.zshrc.bak` write** from auto
  mode and produced a polite, actionable refusal. The security boundary
  holds end-to-end.
- **`FORGE_RESULT_FD` is stripped from the sandboxed cell environment** —
  no host secret leakage to the cell process. Verified by direct probe.
- **Pricing override TOML corruption** caught by `tomllib.TOMLDecodeError`
  without crashing; override resets to `{}` and `price()` falls back to
  baseline.
- **Audit log captures the full plan/deny/auto-run lifecycle** including
  `gate.user_deny`, pre/post shadow-git SHAs, and `turn.end.prose`.
  Observability is strong.
- **Daemon control-plane** (`--status`, `--stop`, `--background`,
  missing config) all behave cleanly with no tracebacks and correct exit
  codes.
- **`forge skill update <nonexistent>`** errors with a clean message and
  exit 1 — no traceback when target isn't installed.
- **`forge stats` empty-state** includes a helpful seed hint (`forge run
  "hello"`) instead of just zero counts.

## Methodology

Workflow ran 5 feature probes in parallel (one per W4 module) + 6 real
agent tasks against gpt-oss:20b under the full v0.2 stack (sandbox +
dry-run + streaming + protected paths), then synthesized findings with
an adversarial ranker. All findings include a reproducer and a suggested
fix. 11 agents, ~285k tokens, ~8 minutes wall time.
