"""Plot the eval results as PNG charts.

Reads docs/eval/results.csv and writes:
    docs/eval/charts/latency_hist.png
    docs/eval/charts/cells_by_category.png
    docs/eval/charts/ttfc_vs_total.png
    docs/eval/charts/robustness_summary.png
    docs/eval/charts/efficiency_by_category.png
    docs/eval/charts/finish_kind_bar.png

Usage:
    python docs/eval/plot.py --results docs/eval/results.csv \
                             --out docs/eval/charts

Requires matplotlib. Uses tab10 palette; readable defaults.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # no display needed
import matplotlib.pyplot as plt


def load(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Cast numeric columns
            for k in ("wall_clock_s", "time_to_first_cell_s", "cell_efficiency",
                      "correctness"):
                if row.get(k) not in (None, "", "None"):
                    try:
                        row[k] = float(row[k])
                    except ValueError:
                        row[k] = None
                else:
                    row[k] = None
            for k in ("cells_run", "ideal_cells", "model_calls",
                      "harmony_recoveries", "format_retries", "empty_retries",
                      "escalations", "exit_code"):
                if row.get(k) not in (None, "", "None"):
                    try:
                        row[k] = int(row[k])
                    except ValueError:
                        row[k] = 0
                else:
                    row[k] = 0
            row["robust"] = str(row.get("robust", "")).lower() == "true"
            row["kernel_wedged"] = str(row.get("kernel_wedged", "")).lower() == "true"
            rows.append(row)
    return rows


def _style(ax, title: str, xlabel: str, ylabel: str):
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, alpha=0.3, linestyle=":", axis="y")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def latency_hist(rows: list[dict], out: Path):
    """Distribution of end-to-end latency."""
    latencies = [r["wall_clock_s"] for r in rows if r["wall_clock_s"] is not None]
    if not latencies:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(latencies, bins=15, color="#1f77b4", edgecolor="white", linewidth=1.2)
    median = sorted(latencies)[len(latencies) // 2]
    ax.axvline(median, color="#d62728", linestyle="--", linewidth=1.5,
               label=f"median = {median:.1f}s")
    ax.legend(frameon=False)
    _style(ax, f"End-to-end latency distribution (n={len(latencies)})",
           "wall-clock (s)", "task count")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def cells_by_category(rows: list[dict], out: Path):
    """Mean cells_run vs ideal_cells, by category."""
    cats = defaultdict(lambda: {"actual": [], "ideal": []})
    for r in rows:
        cats[r["category"]]["actual"].append(r["cells_run"])
        cats[r["category"]]["ideal"].append(r["ideal_cells"])
    labels = sorted(cats.keys())
    actual = [sum(cats[c]["actual"]) / len(cats[c]["actual"]) for c in labels]
    ideal = [sum(cats[c]["ideal"]) / len(cats[c]["ideal"]) for c in labels]

    fig, ax = plt.subplots(figsize=(11, 5))
    x = range(len(labels))
    w = 0.4
    ax.bar([i - w/2 for i in x], ideal, w, color="#2ca02c",
           label="ideal", edgecolor="white", linewidth=1)
    ax.bar([i + w/2 for i in x], actual, w, color="#1f77b4",
           label="actual (mean)", edgecolor="white", linewidth=1)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=25, ha="right")
    ax.legend(frameon=False)
    _style(ax, "Cells: ideal vs actual, by category", "", "mean cells per task")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def ttfc_vs_total(rows: list[dict], out: Path):
    """Time-to-first-cell vs total wall clock. Reveals the tail
    (how much time is spent AFTER the first cell)."""
    pairs = [(r["time_to_first_cell_s"], r["wall_clock_s"], r["category"])
             for r in rows
             if r["time_to_first_cell_s"] is not None
             and r["wall_clock_s"] is not None]
    if not pairs:
        return
    cats = sorted({p[2] for p in pairs})
    cmap = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(9, 6))
    for i, cat in enumerate(cats):
        xs = [p[0] for p in pairs if p[2] == cat]
        ys = [p[1] for p in pairs if p[2] == cat]
        ax.scatter(xs, ys, color=cmap(i), label=cat, s=60, alpha=0.8,
                   edgecolor="white", linewidth=1)
    # y = x reference
    mx = max(max(p[0] for p in pairs), max(p[1] for p in pairs))
    ax.plot([0, mx], [0, mx], "k--", alpha=0.3, linewidth=1,
            label="y = x (all time in cell 1)")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _style(ax, "Time to first cell vs. total latency",
           "time to first cell (s)", "total wall clock (s)")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def robustness_summary(rows: list[dict], out: Path):
    """Stacked bar per category: robust vs non-robust."""
    cats = defaultdict(lambda: {"robust": 0, "not_robust": 0})
    for r in rows:
        key = "robust" if r["robust"] else "not_robust"
        cats[r["category"]][key] += 1
    labels = sorted(cats.keys())
    robust = [cats[c]["robust"] for c in labels]
    not_robust = [cats[c]["not_robust"] for c in labels]

    fig, ax = plt.subplots(figsize=(11, 5))
    x = range(len(labels))
    ax.bar(x, robust, color="#2ca02c", label="robust",
           edgecolor="white", linewidth=1)
    ax.bar(x, not_robust, bottom=robust, color="#d62728",
           label="not robust", edgecolor="white", linewidth=1)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=25, ha="right")
    ax.legend(frameon=False)
    _style(ax, "Robustness (ended in prose, no wedge, exit=0) by category",
           "", "task count")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def efficiency_by_category(rows: list[dict], out: Path):
    """Cell efficiency (cells_run / ideal_cells) per category as boxplot."""
    cats = defaultdict(list)
    for r in rows:
        if r["cell_efficiency"] is not None:
            cats[r["category"]].append(r["cell_efficiency"])
    labels = sorted(cats.keys())
    data = [cats[c] for c in labels]

    fig, ax = plt.subplots(figsize=(11, 5))
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True,
                    boxprops=dict(facecolor="#1f77b4", alpha=0.5),
                    medianprops=dict(color="black", linewidth=2))
    ax.axhline(1.0, color="#2ca02c", linestyle="--", linewidth=1.5,
               alpha=0.7, label="ideal (1.0x)")
    ax.legend(frameon=False, loc="upper right")
    plt.setp(ax.get_xticklabels(), rotation=25, ha="right")
    _style(ax, "Cell efficiency (cells_run / ideal_cells) by category",
           "", "efficiency ratio")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def finish_kind_bar(rows: list[dict], out: Path):
    """Overall distribution of how turns ended."""
    kinds = defaultdict(int)
    for r in rows:
        kinds[r.get("finish_kind") or "unknown"] += 1
    # Order: happy path first, then failure modes
    order = ["prose", "max_cells", "format_failure", "empty",
             "tool_call_parse_unrecovered", "timeout", "error", "unknown"]
    labels = [k for k in order if k in kinds] + \
             [k for k in kinds if k not in order]
    values = [kinds[k] for k in labels]
    colors = ["#2ca02c" if k == "prose" else "#d62728" for k in labels]

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(labels, values, color=colors, edgecolor="white", linewidth=1)
    for i, v in enumerate(values):
        ax.text(i, v + 0.15, str(v), ha="center", fontsize=10)
    _style(ax, "Turn finish kind (how each task ended)", "", "task count")
    plt.setp(ax.get_xticklabels(), rotation=15, ha="right")
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    csv_path = Path(args.results).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    latency_hist(rows, out_dir / "latency_hist.png")
    cells_by_category(rows, out_dir / "cells_by_category.png")
    ttfc_vs_total(rows, out_dir / "ttfc_vs_total.png")
    robustness_summary(rows, out_dir / "robustness_summary.png")
    efficiency_by_category(rows, out_dir / "efficiency_by_category.png")
    finish_kind_bar(rows, out_dir / "finish_kind_bar.png")

    print("Wrote 6 charts to", out_dir)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
