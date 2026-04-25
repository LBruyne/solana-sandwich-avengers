"""
Generate Sec 5.2 win-rate distribution figure.

Pools attacker-entity-level features across the three classifier-eligible
shape-layer categories (standard, multi_split, diff_signer_owner), filters
to entities with CNT >= 10 (the economic-signal first gate), and plots the
win-rate histogram with the three-mode structure annotated and the WR
threshold marked.

Inputs : evaluator/data/1_signer_data_preparation_and_summary/<cat>/signer_features_946_960.parquet
Outputs:
  evaluator/data/sec5_2/wr_distribution_946_960.pdf
  overleaf-paper/CCS/figures/5.2_wr_distribution.pdf  (mirror)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
SRC_BASE = REPO / "evaluator" / "data" / "1_signer_data_preparation_and_summary"
OUT_DIR = REPO / "evaluator" / "data" / "sec5_2"
PAPER_FIG = REPO / "overleaf-paper" / "CCS" / "figures" / "5.2_wr_distribution.pdf"

CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]
TAG = "946_960"
CNT_MIN = 10
WR_THRESHOLD = 0.8


def load_pooled_features() -> pd.DataFrame:
    frames = []
    for cat in CATEGORIES:
        f = SRC_BASE / cat / f"signer_features_{TAG}.parquet"
        df = pd.read_parquet(f).reset_index()
        df["category"] = cat
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_FIG.parent.mkdir(parents=True, exist_ok=True)

    feats = load_pooled_features()
    n_total = len(feats)
    sub = feats[feats["sandwich_count"] >= CNT_MIN].copy()
    n_sub = len(sub)
    n_high = (sub["win_rate"] >= WR_THRESHOLD).sum()

    bins = np.linspace(0.0, 1.0, 21)
    centers = 0.5 * (bins[:-1] + bins[1:])
    counts, _ = np.histogram(sub["win_rate"].dropna(), bins=bins)

    n_low = ((sub["win_rate"] < 0.40)).sum()
    n_mid = ((sub["win_rate"] >= 0.40) & (sub["win_rate"] < WR_THRESHOLD)).sum()

    # Two-tone palette: muted slate-blue for the candidate pool, warm coral
    # for entities that survive the win-rate gate.
    base_color = "#5d7fa6"
    accent_color = "#d2604c"
    colors = np.where(centers >= WR_THRESHOLD, accent_color, base_color)

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.labelsize": 12,
        "axes.titlesize": 12,
        "legend.fontsize": 10,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })

    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    bar_w = (bins[1] - bins[0]) * 0.92
    ax.bar(centers, counts, width=bar_w,
           color=colors, edgecolor="white", linewidth=0.6)

    y_top = counts.max() * 1.18

    # Threshold line + label
    ax.axvline(WR_THRESHOLD, color="#222222", linestyle=(0, (4, 2)),
               linewidth=1.4, zorder=3)
    ax.text(WR_THRESHOLD - 0.012, y_top * 0.96,
            r"$w_{\min}=0.8$",
            ha="right", va="top", fontsize=13,
            bbox=dict(boxstyle="round,pad=0.28", fc="white",
                      ec="#222222", lw=0.7))

    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(0, y_top)
    ax.set_xlabel(r"Win rate $w_e \;=\; \Pr[\Delta x_B > \Delta x_F]$")
    ax.set_ylabel("Number of entities")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    for spine_name in ("top", "right"):
        ax.spines[spine_name].set_visible(False)
    for spine_name in ("left", "bottom"):
        ax.spines[spine_name].set_color("#444444")
        ax.spines[spine_name].set_linewidth(0.8)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc", zorder=0)
    ax.set_axisbelow(True)

    fig.tight_layout()

    out_pdf = OUT_DIR / f"wr_distribution_{TAG}.pdf"
    fig.savefig(out_pdf, bbox_inches="tight")
    fig.savefig(OUT_DIR / f"wr_distribution_{TAG}.png", bbox_inches="tight", dpi=200)
    shutil.copyfile(out_pdf, PAPER_FIG)
    plt.close(fig)

    print(f"Pool size                 : {n_total:>10,}")
    print(f"  CNT >= {CNT_MIN:<3}              : {n_sub:>10,}")
    print(f"  WR >= {WR_THRESHOLD} (within CNT)  : {n_high:>10,}")
    print(f"  low / mid / high (CNT)  : {n_low:,} / {n_mid:,} / {n_high:,}")
    print(f"\nFigure saved to:")
    print(f"  {out_pdf}")
    print(f"  {PAPER_FIG}")


if __name__ == "__main__":
    main()
