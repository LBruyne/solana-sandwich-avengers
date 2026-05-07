"""
Generate Sec 7.1 Figure 8: distribution of the strongest enrichment
ratio eta_max_a across the unified attacker set defined in §5.2.

Pool definition (matches sandwich-intent/4_validator_association.py pooled mode):
  - bot_attackers + bot_sandwiches across {standard, multi_split, diff_signer_owner}
  - tier in {jito_only, jito_and_signal, signal} (excludes Oneshot)
  - sandwich_count (pooled across categories) >= CNT_MIN (default 10)
This yields the 282 attackers the paper §7.1 reports on.

Inputs:
  sandwich-intent/data/3_attacker_filter/<cat>/bot_attackers_<tag>.parquet
  sandwich-intent/data/3_attacker_filter/<cat>/bot_sandwiches_<tag>.parquet
  ClickHouse: slot_leaders for the measurement window
Outputs:
  sandwich-intent/data/4_validator_association/all_attackers/
      signer_eta_max_<tag>.csv
      enrichment_distribution.pdf / .png
  overleaf-paper/CCS/figures/7.1_enrichment_distribution.pdf  (mirror)

The enrichment math matches sandwich-intent/4_validator_association.py:
  near_cnt(a, v)  = sum over a's sandwiches of #(offsets in [-K,+K] where v leads)
  expected_pct(v) = sum over offsets of (slot share of v at that offset)
  eta            = (near_cnt / |S_a|) / expected_pct
which equals the paper's eta_{a,v} = N_{a,v} * N / ((2k+1) * N_v * |S_a|)
under stationary leader-rotation length.
"""

from __future__ import annotations

import shutil
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sandwich-intent"))
from utils.db import get_client  # noqa: E402


SF_BASE = REPO / "sandwich-intent" / "data" / "3_attacker_filter"
OUT_DIR = REPO / "sandwich-intent" / "data" / "4_validator_association" / "all_attackers"
PAPER_FIG = REPO / "overleaf-paper" / "CCS" / "figures" / "7.1_enrichment_distribution.pdf"

CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]
TAG = "946_960"
START_EPOCH, END_EPOCH = 946, 960
SLOTS_PER_EPOCH = 432_000
OFFSETS = list(range(-4, 5))   # +/- 4 leader rotations
CNT_MIN = 10
NEAR_CNT_MIN = 5  # min co-occurrence count to consider a (a, v) pair (matches flag threshold)
EXCLUDE_TIERS = {"oneshot", "signal_and_oneshot"}   # exclude Oneshot Bots


# ── leader schedule helpers (mirror sandwich-intent/4_validator_association.py) ──

def load_leader_blocks(client, start_slot, end_slot):
    df = client.query_df(f"""
        SELECT slot, leader FROM slot_leaders
        WHERE slot >= {start_slot} AND slot < {end_slot}
        ORDER BY slot
    """)
    slots = df["slot"].values.astype(int)
    ldrs = df["leader"].values
    blocks = []
    cur_leader = ldrs[0]
    cur_start = slots[0]
    for i in range(1, len(slots)):
        if ldrs[i] != cur_leader or slots[i] != slots[i - 1] + 1:
            blocks.append((cur_start, slots[i - 1], cur_leader))
            cur_leader = ldrs[i]
            cur_start = slots[i]
    blocks.append((cur_start, slots[-1], cur_leader))
    return blocks


def build_slot_to_block_idx(blocks):
    mapping = {}
    for idx, (s, e, _) in enumerate(blocks):
        for sl in range(s, e + 1):
            mapping[sl] = idx
    return mapping


def compute_global_offset_freq(blocks):
    freq = {off: defaultdict(float) for off in OFFSETS}
    total_slots = sum(e - s + 1 for s, e, _ in blocks)
    for idx, (s, e, _) in enumerate(blocks):
        size = e - s + 1
        for off in OFFSETS:
            ni = idx + off
            if 0 <= ni < len(blocks):
                freq[off][blocks[ni][2]] += size
    for off in OFFSETS:
        for l in freq[off]:
            freq[off][l] /= total_slots
    return freq


def compute_signer_validator_counts(sw, slot_to_block, blocks):
    per_signer = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    signers = sw["signer"].values
    slots = sw["slot"].astype(int).values
    for i in range(len(sw)):
        bidx = slot_to_block.get(int(slots[i]))
        if bidx is None:
            continue
        for off in OFFSETS:
            ni = bidx + off
            if 0 <= ni < len(blocks):
                per_signer[signers[i]][blocks[ni][2]][off] += 1
    return per_signer


# ── pooling ──

def pool_attacker_sandwiches() -> pd.DataFrame:
    """Pool bot_sandwiches across the three categories, restricted to bot
    signers whose tier is NOT in EXCLUDE_TIERS (i.e. drop Oneshot Bots).

    The tier filter is applied via bot_attackers; only sandwiches by signers
    that survived the tier filter are kept.
    """
    frames = []
    for cat in CATEGORIES:
        sf_path = SF_BASE / cat / f"bot_attackers_{TAG}.parquet"
        sw_path = SF_BASE / cat / f"bot_sandwiches_{TAG}.parquet"
        if not (sf_path.exists() and sw_path.exists()):
            print(f"  WARN: missing {cat} outputs")
            continue
        sf = pd.read_parquet(sf_path)
        keep_signers = set(sf[~sf["tier"].isin(EXCLUDE_TIERS)].index)
        sw = pd.read_parquet(sw_path)
        before = sw["signer"].nunique()
        sw = sw[sw["signer"].isin(keep_signers)]
        sw["category"] = cat
        frames.append(sw)
        print(f"  {cat:>20s}: {len(sw):>8,} sandwiches, "
              f"{sw['signer'].nunique():>5,} signers (was {before}, "
              f"dropped {before - sw['signer'].nunique()} oneshot-tier)")
    return pd.concat(frames, ignore_index=True)


# ── plot ──

def plot_distribution(eta_arr: np.ndarray, out_pdf: Path) -> None:
    n_lt2 = int((eta_arr < 2).sum())
    n_2to10 = int(((eta_arr >= 2) & (eta_arr < 10)).sum())
    n_ge10 = int((eta_arr >= 10).sum())

    # Categorical bins matching the user-requested tick spec
    bin_edges = np.array([0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, np.inf])
    counts, _ = np.histogram(eta_arr, bins=bin_edges)

    n_bins = len(counts)
    centers = np.arange(n_bins)

    color_blue = "#5d7fa6"
    color_amber = "#e0a060"
    color_red = "#c44a4a"
    colors = []
    for low, high in zip(bin_edges[:-1], bin_edges[1:]):
        if high <= 2:
            colors.append(color_blue)
        elif high <= 10:
            colors.append(color_amber)
        else:
            colors.append(color_red)

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
    ax.bar(centers, counts, width=0.92, color=colors,
           edgecolor="white", linewidth=0.6, zorder=2)

    # Count labels above non-zero bars
    ymax = counts.max()
    for c, n in zip(centers, counts):
        if n > 0:
            ax.text(c, n + ymax * 0.025, f"{int(n):,}",
                    ha="center", va="bottom", fontsize=8.5,
                    color="#222222")

    # Ticks at bin boundaries (-0.5, 0.5, 1.5, ..., n_bins-0.5)
    tick_positions = np.arange(n_bins + 1) - 0.5
    tick_labels = ["0", "1", "2", "5", "10", "20", "50", "100", ""]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels)

    ax.set_xlim(-0.6, n_bins - 0.4)
    ax.set_ylim(0, ymax * 1.20)
    ax.set_xlabel(r"Strongest enrichment ratio $\eta^{\max}_a$ for each attacker")
    ax.set_ylabel("Number of attackers")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))

    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#444444")
        ax.spines[s].set_linewidth(0.8)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc", zorder=0)
    ax.set_axisbelow(True)

    legend_elems = [
        Patch(facecolor=color_blue, edgecolor="white", linewidth=0.5,
              label=fr"No apparent association  ($\eta^{{\max}}_a < 2$): {n_lt2:,}"),
        Patch(facecolor=color_amber, edgecolor="white", linewidth=0.5,
              label=fr"Moderate association  ($\eta^{{\max}}_a \in [2,10)$): {n_2to10:,}"),
        Patch(facecolor=color_red, edgecolor="white", linewidth=0.5,
              label=fr"Strong association  ($\eta^{{\max}}_a \geq 10$): {n_ge10:,}"),
    ]
    ax.legend(handles=legend_elems, loc="upper right", fontsize=9.5,
              frameon=True, framealpha=0.95)

    fig.tight_layout()
    fig.savefig(out_pdf, bbox_inches="tight")
    fig.savefig(out_pdf.with_suffix(".png"), bbox_inches="tight", dpi=200)
    plt.close(fig)


# ── main ──

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_FIG.parent.mkdir(parents=True, exist_ok=True)

    print("Pooling bot sandwiches across classifier categories...")
    sw = pool_attacker_sandwiches()
    n_unique = sw["signer"].nunique()
    print(f"  TOTAL: {len(sw):,} sandwiches, {n_unique:,} unique attacker signers")

    cnts = sw.groupby("signer").size()
    kept = set(cnts[cnts >= CNT_MIN].index)
    sw = sw[sw["signer"].isin(kept)].reset_index(drop=True)
    print(f"  After |S_a|>={CNT_MIN}: {len(kept):,} attackers, {len(sw):,} sandwiches")

    print("\nLoading leader schedule from ClickHouse...")
    client = get_client()
    start_slot = START_EPOCH * SLOTS_PER_EPOCH
    end_slot = (END_EPOCH + 1) * SLOTS_PER_EPOCH
    blocks = load_leader_blocks(client, start_slot, end_slot)
    slot_to_block = build_slot_to_block_idx(blocks)
    global_freq = compute_global_offset_freq(blocks)
    print(f"  {len(blocks):,} leader rotations in epochs {START_EPOCH}-{END_EPOCH}")

    print("\nComputing per-signer eta_max...")
    per_signer = compute_signer_validator_counts(sw, slot_to_block, blocks)
    signer_totals = sw.groupby("signer").size().to_dict()

    rows = []
    for signer, vmap in per_signer.items():
        total = signer_totals[signer]
        max_eta = 0.0
        max_v = ""
        max_near = 0
        for v, offset_cnts in vmap.items():
            near_cnt = sum(offset_cnts.values())
            if near_cnt < NEAR_CNT_MIN:
                continue
            near_pct = near_cnt / total
            expected_pct = sum(global_freq[off].get(v, 0) for off in OFFSETS)
            if expected_pct > 0:
                eta = near_pct / expected_pct
                if eta > max_eta:
                    max_eta = eta
                    max_v = v
                    max_near = near_cnt
        rows.append({"signer": signer, "sandwich_count": total,
                     "eta_max": max_eta, "best_validator": max_v,
                     "best_near_cnt": max_near})

    df = pd.DataFrame(rows).sort_values("eta_max", ascending=False).reset_index(drop=True)
    out_csv = OUT_DIR / f"signer_eta_max_{TAG}.csv"
    df.to_csv(out_csv, index=False)

    eta = df["eta_max"].values
    print(f"\nDistribution of eta_max:")
    print(f"  range  : [{eta.min():.3f}, {eta.max():.2f}]")
    print(f"  median : {np.median(eta):.2f}")
    bin_edges = [0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, np.inf]
    for low, high in zip(bin_edges[:-1], bin_edges[1:]):
        n = int(((eta >= low) & (eta < high)).sum())
        hi = "inf" if not np.isfinite(high) else f"{high:g}"
        print(f"  [{low:>4g}, {hi:>4s}): {n:>4d}")

    print(f"\n  <2     : {(eta<2).sum():,}")
    print(f"  [2,5)  : {((eta>=2)&(eta<5)).sum():,}")
    print(f"  >=5    : {(eta>=5).sum():,}")
    print(f"  >=100  : {(eta>=100).sum():,}")

    out_pdf = OUT_DIR / "enrichment_distribution.pdf"
    plot_distribution(eta, out_pdf)
    shutil.copyfile(out_pdf, PAPER_FIG)

    print(f"\nSaved:")
    print(f"  CSV         : {out_csv}")
    print(f"  PDF         : {out_pdf}")
    print(f"  Paper mirror: {PAPER_FIG}")


if __name__ == "__main__":
    main()
