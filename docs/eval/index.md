# Evaluation

Forge ships with an **end-to-end evaluation harness** at `docs/eval/` that runs a curated dataset against a live `forge run` process, captures mechanical metrics per task from the audit log, and produces charts + a Markdown report.

## Structure

```
docs/eval/
├── dataset.jsonl        30 curated utterances (9 categories) with reference answers
├── workspace/           deterministic reference workspace (~11 files)
├── run.py               the runner — spawns forge, parses audit, writes CSV
├── plot.py              matplotlib chart generator
├── report.md            latest evaluation report
├── runs/                per-task JSON (one file per utterance)
├── charts/              generated PNGs
└── results.csv          consolidated CSV of every metric per task
```

## Latest results

**v0.2.4 baseline**, 30 tasks, 7 minutes wall clock, 27/30 robust (90%).

See [report.md](report.md) for headline stats, all 6 charts, per-task table, and observations — including the finding that **33% of tasks trigger Ollama's harmony parser** and the v0.2.2/v0.2.3 recovery infrastructure catches 9 out of 10 of those cases invisibly.

## Running it yourself

Requires an Ollama running locally with `gpt-oss:20b` pulled. From the repo root:

```bash
# Full 30-task run (~7 min on Apple Silicon M-series):
python docs/eval/run.py \
    --dataset docs/eval/dataset.jsonl \
    --workspace-src docs/eval/workspace \
    --out docs/eval/runs \
    --results docs/eval/results.csv \
    --timeout-s 300

# Charts from a completed run:
python docs/eval/plot.py \
    --results docs/eval/results.csv \
    --out docs/eval/charts

# Smoke run — first 3 tasks in ~1 min:
python docs/eval/run.py [...same args...] --limit 3
```

The runner spawns each task in a fresh temp copy of `workspace/` so edit tasks don't pollute later runs. Metrics come from the audit JSONL that forge writes into that temp workspace.

## What's measured

7 mechanical metrics per task; **answer correctness** requires an LLM judge (Anthropic Claude recommended) which is a `Judge` protocol slot in `run.py` — currently unwired pending a personal API key.

See the [report](report.md#what-was-measured) for the full metric list and their meanings.

## Adding new utterances

Append a JSON line to `dataset.jsonl`:

```json
{"id":"my-task-01","category":"filesystem","prompt":"...","reference_answer":"...","ideal_cells":1,"notes":"...","source":"forge-specific"}
```

Fields:

- **`id`** — kebab-case, unique.
- **`category`** — one of the 9 existing, or a new one.
- **`prompt`** — the exact utterance passed to `forge run`.
- **`reference_answer`** — ground-truth prose. Verified against `workspace/`.
- **`ideal_cells`** — author's guess at minimum cells needed. Used for `cell_efficiency`.
- **`notes`** — free-form; not scored.
- **`source`** — `forge-specific` / `swe-bench-lite-style` / `gaia-mini-style`.

If your task needs a file that isn't in `workspace/`, add it there first and verify ground truth with `find` / `wc -l` / etc.
