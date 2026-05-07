"""
Section 7.1 charts:
  (1) 7.1_enrichment_distribution.pdf
      Distribution of max leader-window enrichment across standard-category bots.
  (2) 7.1_cohort_heatmap.pdf
      Cohort B (18 signers x 12 shared validators) enrichment matrix +
      offset distribution showing the +1 peak.

Inputs: sandwich-intent/data/4_validator_association/standard/{flagged_pairs,signer_leader_summary,cohort_members}_<tag>.csv
Outputs: sandwich-intent/data/sec7_validator/{enrichment_distribution,cohort_heatmap}.pdf
         (also copied to overleaf-paper/CCS/figures/7.1_*.pdf)

Run: python sec7_validator_charts.py --start-epoch 946 --end-epoch 960
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

# sandwich-intent/utils provides shared ClickHouse client; the chart scripts live
# in analyst/ but reuse the same DB helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandwich-intent"))

OFFSETS = list(range(-4, 5))


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=960)
    p.add_argument("--out-dir", default="data/sec7_validator")
    p.add_argument("--copy-to",
                   default="../overleaf-paper/CCS/figures",
                   help="Copy final PDFs to overleaf figures dir; '' to skip")
    return p.parse_args()


# ────────────────────────────────────────────────────────────────────────────
# Figure 1: enrichment distribution
# ────────────────────────────────────────────────────────────────────────────

def fig_enrichment_distribution(ssum, out_pdf):
    enr = ssum["w4_top_enrich"].fillna(0).values
    n_total = len(enr)
    n_lt2 = int((enr < 2).sum())
    n_2to5 = int(((enr >= 2) & (enr < 5)).sum())
    n_ge5 = int((enr >= 5).sum())

    fig, ax = plt.subplots(figsize=(7.0, 3.0))

    # Use log-x bins so we see both the bulk and the long tail in one view.
    pos = np.maximum(enr, 0.05)
    bins = np.logspace(np.log10(0.05), np.log10(200), 40)
    counts, edges = np.histogram(pos, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    widths = edges[1:] - edges[:-1]

    # Colour bars by bucket: baseline / moderate / flagged.
    colors = []
    for c in centers:
        if c < 2:
            colors.append("#7a9bc4")        # baseline blue
        elif c < 5:
            colors.append("#e0a060")        # moderate amber
        else:
            colors.append("#c44a4a")        # flagged red
    ax.bar(centers, counts, width=widths, color=colors,
           edgecolor="black", linewidth=0.4, align="center")

    ax.set_xscale("log")
    ax.set_xlim(0.05, 200)
    ax.set_xlabel(r"Strongest leader-window enrichment $\eta^{\max}_a$",
                  fontsize=11)
    ax.set_ylabel("Number of attackers", fontsize=11)
    ax.tick_params(axis="both", which="major", labelsize=10)
    ax.grid(True, axis="y", linestyle=":", alpha=0.4)

    ax.axvline(2, color="#888", linestyle="--", linewidth=1, alpha=0.7)
    ax.axvline(5, color="black", linestyle="--", linewidth=1.2)
    ymax = ax.get_ylim()[1]
    ax.text(5 * 1.15, ymax * 0.92, "flag threshold\n($\\eta \\geq 5$)",
            fontsize=9, va="top", ha="left")

    # Legend via proxy patches
    from matplotlib.patches import Patch
    legend_elems = [
        Patch(facecolor="#7a9bc4", edgecolor="black", linewidth=0.4,
              label=f"baseline-like  ($\\eta<2$): {n_lt2} attackers"),
        Patch(facecolor="#e0a060", edgecolor="black", linewidth=0.4,
              label=f"moderate ($2 \\leq \\eta<5$): {n_2to5}"),
        Patch(facecolor="#c44a4a", edgecolor="black", linewidth=0.4,
              label=f"flagged ($\\eta \\geq 5$): {n_ge5}"),
    ]
    ax.legend(handles=legend_elems, loc="upper right", fontsize=9,
              frameon=True, framealpha=0.95)

    plt.tight_layout()
    plt.savefig(out_pdf, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"  wrote {out_pdf}  (n_total={n_total}, "
          f"<2={n_lt2}, 2-5={n_2to5}, >=5={n_ge5})")


# ────────────────────────────────────────────────────────────────────────────
# Figure 2: cohort B heatmap + offset distribution
# ────────────────────────────────────────────────────────────────────────────

def fig_cohort_heatmap(flagged, cohort_members, out_pdf):
    """Cohort B heatmap: 9 core attackers × 11 shared validators, plus a
    Gantt-style time strip showing each attacker's active span (visualizing
    the T2→T1 hand-off across the 30-day window)."""
    # Restrict to the 9 core members of Cohort B that the paper §7.1 names.
    CORE9 = [
        "HqXf7BH8F35yyZnRrCMgGgj2yWUqdr8nGvH7ZC9xW1HB",
        "7CtW3vMTyNm9u8ByWWoTjGg7XYZteUjYwjEevHypVHb4",
        "7nCRbePZDbVyybQjZx5qXFh4NnBadVNSV13Djef6z7R2",
        "8qAsHZNPsWriCcPfkjcDwTSM5iWHK84RWUXL5V3ZSX1F",
        "6m4bqcJyyrvSoyi4hDs4UktW2VarSyJ8caTrPCU2NwR",
        "FSSFn3JDeo68dyyzuzSCEYiymXwD82iXrPKG4nqdSfFE",
        "4hASKAobJourFvMS5SktVhB8B4mnPXUzekdS3ihm1VGg",
        "DDm1Bc9KuXB7Q2UbxyMRLGmbcV2J93xmiWDPt7Edhqbs",
        "F8LoqWUjjbD3xqHfrYAffPZMCoNjwbB1jQC3k6MWgfbs",
    ]
    member_set = set(CORE9)
    sub = flagged[flagged["signer"].isin(member_set)].copy()
    if sub.empty:
        raise RuntimeError("No flagged pairs found for the 9 core members")

    # Pull each member's first-/last-sandwich slot from the pooled bot
    # sandwich tables, so we can sort by chronological onset and draw a
    # Gantt-style time strip on the right of the heatmap.
    SF_BASE = Path(__file__).resolve().parent / "data" / "3_attacker_filter"
    sw_frames = []
    bot_frames = []
    for cat in ("standard", "multi_split", "diff_signer_owner"):
        psw = SF_BASE / cat / "bot_sandwiches_946_960.parquet"
        if psw.exists():
            sw_frames.append(pd.read_parquet(psw))
        pbot = SF_BASE / cat / "bot_attackers_946_960.parquet"
        if pbot.exists():
            bot_frames.append(pd.read_parquet(pbot)[["usd_total_profit"]])
    sw = pd.concat(sw_frames, ignore_index=True)
    sw = sw[sw["signer"].isin(member_set)]
    bot = pd.concat(bot_frames)
    bot = bot[~bot.index.duplicated(keep="first")]
    span = sw.groupby("signer")["slot"].agg(["min", "max", "size"]).rename(
        columns={"min": "first_slot", "max": "last_slot", "size": "cnt"})
    span["usd"] = bot.loc[span.index, "usd_total_profit"]

    # Sort attackers by FIRST sandwich slot (chronological onset).
    members = span.sort_values("first_slot").index.tolist()
    span = span.loc[members]

    # 11 most-shared validators across the 9 core members.
    val_member_count = sub.groupby("validator")["signer"].nunique()
    top_vals = val_member_count.sort_values(ascending=False).head(11).index.tolist()

    # Triplet labels are renumbered chronologically: T_1 = first wave (days
    # 0--5.5), T_2 = second (5.5--13.7), T_3 = third (13.7--30). Colors
    # follow the same blue/amber/red palette used in Figure 8 to keep §7.1
    # visually coherent.
    TRIPLET = {
        "4hASKAobJourFv": ("T1", "#5d7fa6"),  # 1st wave (early)
        "DDm1Bc9KuXB7Q2": ("T1", "#5d7fa6"),
        "F8LoqWUjjbD3xq": ("T1", "#5d7fa6"),
        "6m4bqcJyyrvSoy": ("T2", "#e0a060"),  # 2nd wave (middle)
        "8qAsHZNPsWriCc": ("T2", "#e0a060"),
        "FSSFn3JDeo68dy": ("T2", "#e0a060"),
        "7CtW3vMTyNm9u8": ("T3", "#c44a4a"),  # 3rd wave (late)
        "7nCRbePZDbVyyb": ("T3", "#c44a4a"),
        "HqXf7BH8F35yyZ": ("T3", "#c44a4a"),
    }
    def trip(addr): return TRIPLET.get(addr[:14], ("?", "#444"))

    # Build enrichment matrix for ALL 9x11 pairs (not only flagged eta>=10);
    # we recompute eta from the leader schedule + sandwich slots so that
    # below-threshold cells (eta < 10) are still shown.
    from utils.db import get_client as _get_client
    _cli = _get_client()
    _ld = _cli.query_df("""
        SELECT slot, leader FROM slot_leaders
        WHERE slot >= 408672000 AND slot < 415152000 ORDER BY slot
    """).drop_duplicates("slot").set_index("slot")["leader"]
    _slots = _ld.index.values
    _leaders = _ld.values
    _block_id = np.zeros(len(_slots), dtype=int)
    for _i in range(1, len(_slots)):
        _block_id[_i] = _block_id[_i - 1] + (1 if _leaders[_i] != _leaders[_i - 1] else 0)
    _slot_to_block = dict(zip(_slots, _block_id))
    _block_leader = pd.DataFrame({"b": _block_id, "l": _leaders}).drop_duplicates(
        "b").set_index("b")["l"]
    _N = int(len(_slots))
    _Nv = pd.DataFrame({"l": _leaders}).groupby("l").size()
    _OFFS = list(range(-4, 5))

    matrix = np.zeros((len(members), len(top_vals)))
    for i, sig in enumerate(members):
        sig_slots = sw.loc[sw["signer"] == sig, "slot"].astype(int).values
        Sa = len(sig_slots)
        if Sa == 0:
            continue
        for j, val in enumerate(top_vals):
            near_cnt = 0
            for s_ in sig_slots:
                b = _slot_to_block.get(int(s_))
                if b is None:
                    continue
                for o in _OFFS:
                    bb = b + o
                    if bb in _block_leader.index and _block_leader.loc[bb] == val:
                        near_cnt += 1
            Nv_v = int(_Nv.get(val, 0))
            if Nv_v > 0:
                matrix[i, j] = (near_cnt * _N) / (len(_OFFS) * Nv_v * Sa)

    # Validator label: address[:5] only.
    col_labels = [v[:5] for v in top_vals]

    # Slot-window bounds for the Gantt strip
    EPOCH_SLOTS = 432_000
    win_start = 946 * EPOCH_SLOTS
    win_end = 961 * EPOCH_SLOTS

    # ── Layout: heatmap | colorbar | Gantt (3 columns) ─────────────────────
    # Heatmap takes most of the width; colorbar sits flush against it; Gantt
    # gets its own panel with comfortable separation.
    plt.rcParams.update({"font.size": 19})
    fig = plt.figure(figsize=(15.5, 7))
    gs = fig.add_gridspec(1, 3, width_ratios=[3.4, 0.08, 1.1],
                          wspace=0.05)

    ax = fig.add_subplot(gs[0, 0])
    cax = fig.add_subplot(gs[0, 1])
    ax2 = fig.add_subplot(gs[0, 2])
    # Add manual gap between colorbar (col 1) and Gantt (col 2)
    pos2 = ax2.get_position()
    ax2.set_position([pos2.x0 + 0.04, pos2.y0, pos2.width, pos2.height])
    cap = 30
    im = ax.imshow(np.minimum(matrix, cap), aspect="auto",
                   cmap="YlOrRd", vmin=0, vmax=cap)
    ax.set_xticks(range(len(top_vals)))
    ax.set_xticklabels(col_labels, rotation=35, ha="right", fontsize=18)
    ax.set_yticks(range(len(members)))
    yticklabs = []
    for m in members:
        tag, _ = trip(m)
        yticklabs.append(f"{m[:6]}.. ({tag})")
    ax.set_yticklabels(yticklabs, fontsize=18)
    for tl, m in zip(ax.get_yticklabels(), members):
        tl.set_color(trip(m)[1])
    ax.set_xlabel("Validator (address prefix)", fontsize=20)
    ax.tick_params(axis="both", which="both", length=4)

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            v = matrix[i, j]
            if v <= 0:
                continue
            color = "white" if v > cap * 0.55 else "black"
            txt = f"{v:.1f}" + r"$\times$" if v < 10 else f"{v:.0f}" + r"$\times$"
            ax.text(j, i, txt, ha="center", va="center",
                    fontsize=16, color=color)

    cbar = fig.colorbar(im, cax=cax)
    cbar.set_label(r"$\eta_{a,v}$ (clipped at %d)" % cap, fontsize=18)
    cbar.ax.tick_params(labelsize=16)

    # ── Right: chronological active-span strip (Gantt) ─────────────────────
    days_total = (win_end - win_start) / EPOCH_SLOTS * 2  # 1 epoch ≈ 2 days
    for i, sig in enumerate(members):
        s0 = (span.loc[sig, "first_slot"] - win_start) / EPOCH_SLOTS * 2
        s1 = (span.loc[sig, "last_slot"]  - win_start) / EPOCH_SLOTS * 2
        _, color = trip(sig)
        ax2.barh(i, s1 - s0, left=s0, height=0.7, color=color,
                 edgecolor="black", linewidth=0.5)
        ax2.text(s1 + 0.4, i,
                 f"{int(span.loc[sig, 'cnt']):,} sw\n\\${span.loc[sig, 'usd']:,.0f}",
                 va="center", ha="left", fontsize=12, linespacing=1.05)
    ax2.set_xlim(-0.5, days_total + 11)
    ax2.set_ylim(len(members) - 0.5, -0.5)   # match heatmap (top = first row)
    ax2.set_xlabel("Active span in epoch 946-960", fontsize=20)
    ax2.xaxis.set_label_coords(0.35, -0.12)
    ax2.tick_params(axis="x", labelsize=16)
    ax2.set_yticks([])
    for sp in ("top", "right", "left"):
        ax2.spines[sp].set_visible(False)
    ax2.grid(True, axis="x", linestyle=":", alpha=0.4)

    # Lines on actual hand-off x; labels placed at x=4 and x=14 so they sit
    # on the same y level without colliding.
    for x_line, x_text, lbl in [
        (5.6,  4.0,  r"$T_1\!\to\!T_2$"),
        (13.7, 14.0, r"$T_2\!\to\!T_3$"),
    ]:
        ax2.axvline(x_line, color="gray", linestyle="--", linewidth=1.4, alpha=0.7)
        ax2.text(x_text, -1.0, lbl, ha="center", va="top",
                 fontsize=16, color="gray", style="italic")

    # Heatmap y-axis: invert so first row at top
    ax.set_ylim(len(members) - 0.5, -0.5)

    plt.savefig(out_pdf, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"  wrote {out_pdf}  "
          f"(cohort_size={len(members)}, n_validators={len(top_vals)})")


# ────────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    tag = f"{args.start_epoch}_{args.end_epoch}"
    base = "data/4_validator_association/all_attackers"
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    flagged = pd.read_csv(f"{base}/flagged_pairs_{tag}.csv")
    cohort = pd.read_csv(f"{base}/cohort_members_{tag}.csv")

    pdf2 = os.path.join(out_dir, "cohort_heatmap.pdf")

    # Figure 8 (enrichment_distribution) is owned by sec7_1_enrichment_distribution.py
    # — that script applies the correct NEAR_CNT_MIN=5 filter and tier exclusion.
    # We only render Figure 9 (cohort heatmap) here.
    print("Generating §7.1 Figure 9 (cohort heatmap)...")
    fig_cohort_heatmap(flagged, cohort, pdf2)

    if args.copy_to:
        os.makedirs(args.copy_to, exist_ok=True)
        shutil.copy(pdf2, os.path.join(args.copy_to, "7.1_cohort_heatmap.pdf"))
        print(f"  copied to {args.copy_to}/7.1_cohort_heatmap.pdf")

    print("Done.")


if __name__ == "__main__":
    main()
