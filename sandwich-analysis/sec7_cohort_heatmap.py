"""Figure 9 (S7.3, `fig:7_1_cohort_heatmap`): the Team 2 cohort.

Two panels sharing one row per attacker:

    LEFT   enrichment eta^{delta=+1}_{a,v} against every validator flagged for at least
           MIN_MEMBERS cohort members, plus a control row pooling every other Signal
           attacker against the same validators.
    RIGHT  each attacker's active span.

Cohort membership is derived, not listed: attackers are linked when they are enriched
against at least MIN_SHARED validators in common at delta=+1, and the largest connected
component is the cohort (`core_members`).

Input: the association cache from `sec7_build_assoc.py`.
Output: 7.1_cohort_heatmap.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

# Single-leader association tables, built by sec7_build_assoc.py.
ASSOC = figcfg.INTENT / "data" / "sec7" / "assoc_946_990_cl-include_sl.parquet"
BASE = figcfg.INTENT / "data" / "sec7" / "leader_base_946_990_sl.parquet"
PEAK_OFFSET = 1
MIN_ETA = 10      # a pair is "flagged" at or above this enrichment ...
MIN_PAIR_N = 3    # ... on at least this many sandwiches
MIN_SHARED = 6    # two attackers are linked if they share this many flagged validators
MIN_MEMBERS = 6   # a validator is drawn if it is flagged for at least this many members


def flagged_sets(A, B):
    """attacker -> the set of validators it is enriched against at the peak offset."""
    d = A[A["offset"] == PEAK_OFFSET].copy()
    d["eta"] = (d["n"] / d["n_sand"]) / d["validator"].map(B)
    f = d[(d["eta"] >= MIN_ETA) & (d["n"] >= MIN_PAIR_N)]
    return f.groupby("attacker")["validator"].apply(set)


def core_members(A, B):
    """The largest set of attackers that share a validator set.

    Two attackers are linked when they are enriched against at least MIN_SHARED validators
    in common at delta=+1; the return value is the largest connected component.
    """
    sets = flagged_sets(A, B)
    keep = [x for x, v in sets.items() if len(v) >= MIN_SHARED]
    adj = collections.defaultdict(set)
    for x, y in itertools.combinations(keep, 2):
        if len(sets[x] & sets[y]) >= MIN_SHARED:
            adj[x].add(y)
            adj[y].add(x)
    comps, seen = [], set()
    for x in keep:
        if x in seen or x not in adj:
            continue
        stack, comp = [x], set()
        while stack:
            u = stack.pop()
            if u in comp:
                continue
            comp.add(u)
            seen.add(u)
            stack += [w for w in adj[u] if w not in comp]
        comps.append(comp)
    if not comps:
        raise SystemExit("no cohort found — no two attackers share enough flagged validators")
    comps.sort(key=len, reverse=True)
    return sorted(comps[0])


def main():
    p = figcfg.add_args(argparse.ArgumentParser(description="Fig 9: Type II cohort"))
    a = p.parse_args()
    figcfg.banner(a, "7.1 cohort heatmap")
    if not ASSOC.exists():
        raise SystemExit(f"missing {ASSOC} — run the phase-4 association analysis first")

    A_all = pd.read_parquet(ASSOC)
    # Per-offset baseline, not the validator's share of slots: freq[+1][v] is the chance
    # of meeting v one rotation on, which is the denominator an offset-resolved eta needs.
    Bo = pd.read_parquet(BASE)
    B = Bo[Bo["offset"] == PEAK_OFFSET].set_index("leader")["freq"]

    ids = core_members(A_all, B)
    print(f"  cohort: {len(ids)} attackers sharing >= {MIN_SHARED} flagged validators")
    for x in ids:
        print(f"    {x}")
    # Address prefixes: the section names these attackers by their first five characters
    # everywhere else.
    label = {x: x[:5] for x in ids}

    A = A_all[A_all["attacker"].isin(ids) & (A_all["offset"] == PEAK_OFFSET)].copy()
    A["eta"] = (A["n"] / A["n_sand"]) / A["validator"].map(B)

    # Validators ranked by how many of the nine they are enriched for, then by total count.
    strong = A[(A["eta"] >= 10) & (A["n"] >= MIN_PAIR_N)]
    rank = (strong.groupby("validator")
                  .agg(members=("attacker", "nunique"), n=("n", "sum"))
                  .sort_values(["members", "n"], ascending=False))
    vals = rank[rank["members"] >= MIN_MEMBERS].index.tolist()

    att = figcfg.load_attackers(a, verbose=False)
    sw = figcfg.load_sandwiches(a, att, verbose=False)
    sw["signer"] = sw["signer"].astype(str)
    sw = sw[sw["signer"].isin(ids)]
    sw["date"] = pd.to_datetime(sw["ts"], utc=True).dt.floor("D")
    span = sw.groupby("signer")["date"].agg(["min", "max", "size"])
    order = span.sort_values(["min", "max"]).index.tolist()
    usd = sw.groupby("signer")["usd_profit_net"].sum()

    # Triples are read off the onset dates, not assumed: members sharing a start date run
    # together, and each group hands over to the next within a day.
    starts = sorted(span["min"].unique())
    triple = {s: i for i, s in enumerate(starts)}
    tcol = [figcfg.BLUE_M, figcfg.ACCENT, figcfg.CORAL_M]

    piv = A.pivot_table(index="attacker", columns="validator", values="eta", fill_value=0.0)
    M = np.zeros((len(order) + 1, len(vals)))
    for i, sg in enumerate(order):
        for j, v in enumerate(vals):
            M[i, j] = piv.loc[sg, v] if (sg in piv.index and v in piv.columns) else 0.0
    # Control: every other Signal attacker, pooled. Same offset, same denominator.
    rest = A_all[(A_all["offset"] == PEAK_OFFSET) & (~A_all["attacker"].isin(ids))]
    n_rest = rest.drop_duplicates("attacker")["n_sand"].sum()
    for j, v in enumerate(vals):
        M[len(order), j] = (rest.loc[rest["validator"] == v, "n"].sum() / n_rest) / B[v]

    cmap = LinearSegmentedColormap.from_list("paper_blues",
                                             ["#FFFFFF", figcfg.BLUE_L, figcfg.BLUE_M,
                                              figcfg.BLUE_D])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 11.5, "xtick.labelsize": 10.5,
                         "ytick.labelsize": 10.5, "legend.fontsize": 9.5,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, (ax, ag) = plt.subplots(1, 2, figsize=(9.0, 3.4),
                                 gridspec_kw={"width_ratios": [1.85, 1.0], "wspace": 0.10})

    im = ax.imshow(M, cmap=cmap, vmin=0, vmax=M.max(), aspect="auto")
    for i in range(M.shape[0]):
        for j in range(len(vals)):
            if M[i, j] <= 0:
                continue
            txt = f"{M[i, j]:.0f}" if i < len(order) else f"{M[i, j]:.1f}"
            ax.text(j, i, txt, ha="center", va="center", fontsize=9.5,
                    color="white" if M[i, j] > M.max() * 0.55 else "#222222")
    ax.axhline(len(order) - 0.5, color="#222222", linewidth=1.1)
    ax.set_xticks(range(len(vals)), [v[:5] for v in vals], rotation=35, ha="right")
    ax.set_yticks(range(M.shape[0]), [label[s] for s in order] + ["others"])
    ax.set_xticks(np.arange(-0.5, len(vals), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, M.shape[0], 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.4)
    ax.tick_params(which="both", length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_xlabel("Validator")
    ax.set_ylabel("Attacker")
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.02)
    # Above the bar, not beside it: with the panels this close a rotated side label lands
    # inside the right-hand panel. The wave legend starts further right, so the top is free.
    cb.ax.set_title(r"$\eta_{a,v}$", fontsize=11, pad=4)
    cb.ax.tick_params(labelsize=9)
    cb.outline.set_visible(False)

    t0 = min(span["min"])
    for i, s in enumerate(order):
        lo = (span.loc[s, "min"] - t0).days
        hi = (span.loc[s, "max"] - t0).days
        k = triple[span.loc[s, "min"]]
        ag.barh(i, max(hi - lo, 0.6), left=lo, height=0.55,
                color=tcol[k % len(tcol)], edgecolor="white", linewidth=0.6)
        ag.text(hi + 1.5, i, f"{int(span.loc[s, 'size']):,} · \\${usd[s] / 1000:.0f}k",
                va="center", ha="left", fontsize=9.5, color="#333333")
    ag.set_yticks(range(M.shape[0]), [])
    ag.set_ylim(M.shape[0] - 0.5, -0.5)
    ag.set_xlim(-1, (max(span["max"]) - t0).days + 30)
    ag.set_xlabel("Days since first sandwich")
    fig.align_xlabels([ax, ag])
    for sp in ("top", "right", "left"):
        ag.spines[sp].set_visible(False)
    ag.spines["bottom"].set_color("#444444")
    ag.spines["bottom"].set_linewidth(0.8)
    ag.tick_params(axis="y", length=0)
    ag.tick_params(axis="x", length=3.5, color="#444444")
    ag.grid(axis="x", linestyle=":", linewidth=0.5, color="#cccccc")
    ag.set_axisbelow(True)
    ag.legend(handles=[Patch(facecolor=tcol[k % len(tcol)], edgecolor="white",
                             label=f"wave {k + 1}") for k in range(len(starts))],
              loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=3, frameon=False,
              handlelength=1.2, columnspacing=1.2, borderaxespad=0.0)
    # No tight_layout: the colorbar axes is not compatible with it and matplotlib warns.
    # figcfg.save already writes with bbox_inches="tight", which trims correctly here.
    figcfg.save(fig, a, "7.1_cohort_heatmap")
    plt.close(fig)

    print(f"  validators shown: {[v[:8] for v in vals]}")
    for s in order:
        r = span.loc[s]
        k = triple[r["min"]]
        print(f"    {label[s]:>6}  wave {k + 1}  {r['min'].date()} .. {r['max'].date()}  "
              f"{int(r['size']):>6,} sandwiches  "
              f"eta+1 " + " ".join(f"{v[:5]}={piv.loc[s, v]:.0f}" if v in piv.columns else "-"
                                   for v in vals))


if __name__ == "__main__":
    main()
