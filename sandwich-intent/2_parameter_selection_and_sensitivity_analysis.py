"""Phase 2: parameter selection and sensitivity analysis.

Each gate is measured on the population the previous gate leaves behind:

  Stage 1   sandwich_count >= N_MIN          over every signer
  Stage 2   sol_win_rate   >= WR_MIN         over the survivors of stage 1
  Stage 3a  mean_SC        >= SLIP_MIN       over the survivors of stage 2
  Stage 3b  front_gap_p50  <= FG_MEDIAN_MAX  over the survivors of stage 2
  Stage 4   the selected set, after all four gates

Stages 3a and 3b are parallel, both measured on stage 2's output.

Each stage gets two views:

  BUCKETS      attackers, sandwiches and profit per band of the metric.
  CUMULATIVE   running totals at or below each candidate threshold (`le_*`, the CDF) and
               the `>=` complement (`ge_*`). Both live gates are `>=`, so `le_*` at the
               operating point is what the gate discards.

Profit is net of the attacker's own front-run and back-run fees, in USD, priced by the
table `0_crawl_token_price.py` writes. A sandwich whose tokenA carries no price contributes
NaN, not 0 (see `utils.intent.usd_series`), and `priced_share` reports the priceable size.

Both cross-leader variants are produced, into separate directories.

Nothing here feeds phase 3; it is a diagnostic.

Usage:
    python 2_parameter_selection_and_sensitivity_analysis.py \\
        --database solwich --start-epoch 946 --end-epoch 990
    python 2_parameter_selection_and_sensitivity_analysis.py --n-min 50 --wr-min 0.80
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from utils.intent import load_phase1, tag_for

# ── Gate thresholds ──────────────────────────────────────────────────────────
#
# The values `3_attacker_filter.py` applies. Every one is overridable from the command line.
N_MIN = 100            # sandwich_count >= N_MIN         auditability budget
WR_MIN = 0.85          # sol_win_rate   >= WR_MIN         net, SOL-denominated
SLIP_MIN = 0.90        # mean_SC        >= SLIP_MIN       stage 3, not built yet
FG_MEDIAN_MAX = 75.0   # front_gap_p50  <= FG_MEDIAN_MAX  stage 4, not built yet
USD_MIN = 10.0         # usd_net_total  >= USD_MIN        effectively vacuous

# Bucket edges and sweep grids, one pair per stage. Each operating point IS an edge,
# so the threshold falls on a bucket boundary instead of cutting through a bar.
# PROFIT_EDGES is for stage 4 only, where the population is already selected and the
# question is how the survivors are distributed rather than where to cut.
COUNT_EDGES = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1_000, 5_000, 10**12]
COUNT_SWEEP = [1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 1_000, 2_000]
WR_EDGES = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 1.0001]
WR_SWEEP = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70,
            0.75, 0.80, 0.85, 0.90, 0.95, 1.0]
SC_EDGES = [0.0, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0001]
SC_SWEEP = [0.0, 0.40, 0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0]
# front_gap_p50 spans 1 to ~6,400 transactions, so the bands are log-ish. 75 is an
# edge because it is the operating point.
FG_EDGES = [0, 1, 2, 5, 10, 20, 50, 75, 100, 200, 500, 1_000, 10**12]
FG_SWEEP = [1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 1_000, 5_000]
PROFIT_EDGES = [0, 100, 500, 1_000, 5_000, 10_000, 50_000, 100_000, 10**12]

# ── Style ────────────────────────────────────────────────────────────────────
#
# Cross-leader included / excluded hold fixed palette slots, so "include" is blue in every
# figure this script writes.
SURFACE = "#fcfcfb"
SERIES = {"include": "#2a78d6", "exclude": "#eb6834"}
INK = "#1c1c1a"
INK_MUTED = "#6b6b66"
GRID = "#e4e4e0"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK, "axes.titlecolor": INK,
    "xtick.color": INK_MUTED, "ytick.color": INK_MUTED,
    "text.color": INK, "font.size": 9.5,
    "axes.spines.top": False, "axes.spines.right": False,
    "grid.color": GRID, "grid.linewidth": 0.8,
})


def parse_args():
    p = argparse.ArgumentParser(description="Phase 2: gate selection and sensitivity")
    p.add_argument("--database", default="solwich")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--category", default="standard",
                   help="Only `standard` by default; the other two hold under 2 %% of attackers")
    p.add_argument("--n-min", type=int, default=N_MIN,
                   help=f"Stage-1 gate: sandwich_count >= this (default {N_MIN})")
    p.add_argument("--wr-min", type=float, default=WR_MIN,
                   help=f"Stage-2 gate: sol_win_rate >= this (default {WR_MIN})")
    p.add_argument("--slip-min", type=float, default=SLIP_MIN,
                   help=f"Stage-3 gate, echoed for the record (default {SLIP_MIN})")
    p.add_argument("--fg-median-max", type=float, default=FG_MEDIAN_MAX,
                   help=f"Stage-4 gate, echoed for the record (default {FG_MEDIAN_MAX:g})")
    p.add_argument("--usd-min", type=float, default=USD_MIN,
                   help=f"Net-USD floor, echoed for the record (default {USD_MIN:g})")
    p.add_argument("--out-root",
                   default="data/2_parameter_selection_and_sensitivity_analysis",
                   help="Mirrors the script name, so a reader can tell which script wrote a "
                        "directory without opening it")
    return p.parse_args()


def load_variant(category, database, start, end, cross_leader):
    _, sf = load_phase1(category, database, tag_for(start, end, cross_leader))
    d = sf[["sandwich_count", "sol_win_rate", "mean_SC", "front_gap_p50",
            "usd_net_total", "sol_net_profit", "win_rate_n"]].copy()
    d["priced_share"] = (d["win_rate_n"] / d["sandwich_count"]).clip(upper=1.0)
    return d


# ── Buckets & cumulative, generic over the metric ────────────────────────────

def bucket_table(d, col, edges, fmt_band):
    """Attackers, sandwiches and net profit per band of `col`. Bands are [lo, hi)."""
    idx = np.clip(np.searchsorted(edges, d[col].to_numpy(), side="right") - 1,
                  0, len(edges) - 2)
    labels = [fmt_band(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    dd = d.assign(band=pd.Categorical([labels[i] for i in idx],
                                      categories=labels, ordered=True))
    return dd.groupby("band", observed=False).agg(
        attackers=("sandwich_count", "size"),
        sandwiches=("sandwich_count", "sum"),
        usd_net=("usd_net_total", "sum"),
        usd_mean=("usd_net_total", "mean"),
        usd_median=("usd_net_total", "median"),
        sol_net=("sol_net_profit", "sum"),
        priced_share=("priced_share", "median")).fillna(0.0)


def sweep_table(d, col, grid):
    """Running totals AT OR BELOW each threshold, plus the `>=` complement.

    `le_*` is the CDF and rises to the right, which is how a cumulative curve should
    read. Both live gates are `>=`, so `ge_*` is what a gate keeps and `le_*` at the
    operating point is what it discards; carrying both puts the cost and the yield of
    a threshold on one row instead of leaving one to be inferred by subtraction.
    """
    rows = []
    tot_n = len(d)
    tot_sw = d["sandwich_count"].sum()
    tot_usd = d["usd_net_total"].sum()
    for k in grid:
        le, ge = d[d[col] <= k], d[d[col] >= k]
        rows.append({
            "threshold": k,
            "le_attackers": len(le),
            "le_attackers_share": len(le) / tot_n if tot_n else np.nan,
            "le_sandwiches": int(le["sandwich_count"].sum()),
            "le_sandwiches_share": (le["sandwich_count"].sum() / tot_sw) if tot_sw else np.nan,
            "le_usd": le["usd_net_total"].sum(),
            "le_usd_share": (le["usd_net_total"].sum() / tot_usd) if tot_usd else np.nan,
            "le_usd_mean": le["usd_net_total"].mean(),
            "ge_attackers": len(ge),
            "ge_sandwiches": int(ge["sandwich_count"].sum()),
            "ge_usd": ge["usd_net_total"].sum(),
            "ge_usd_mean": ge["usd_net_total"].mean(),
        })
    return pd.DataFrame(rows)


# ── Report ───────────────────────────────────────────────────────────────────

def print_buckets(title, g, tot_n, tot_usd):
    print(f"\n  {title}")
    print(f"    {'band':>14}{'attackers':>11}{'share':>8}{'sandwiches':>13}{'net USD':>14}"
          f"{'share':>8}{'mean USD':>11}{'median':>10}{'priced':>8}")
    for band, r in g.iterrows():
        if r.attackers == 0:
            continue
        print(f"    {band:>14}{int(r.attackers):>11,}{r.attackers / tot_n:>7.1%}"
              f"{int(r.sandwiches):>13,}{r.usd_net:>14,.0f}"
              f"{(r.usd_net / tot_usd if tot_usd else 0):>7.1%}"
              f"{r.usd_mean:>11,.0f}{r.usd_median:>10,.0f}{r.priced_share:>7.0%}")


def print_sweep(title, s, unit):
    print(f"\n  {title}   (le_* = running total at or below)")
    print(f"    {unit:>10}{'le attackers':>14}{'share':>8}{'le sandwiches':>15}"
          f"{'le net USD':>14}{'share':>8}{'ge attackers':>14}{'ge net USD':>14}")
    for r in s.itertuples():
        t = f"{r.threshold:.2f}" if isinstance(r.threshold, float) else f"{r.threshold:,}"
        print(f"    {t:>10}{r.le_attackers:>14,}{r.le_attackers_share:>7.1%}"
              f"{r.le_sandwiches:>15,}{r.le_usd:>14,.0f}{r.le_usd_share:>7.1%}"
              f"{r.ge_attackers:>14,}{r.ge_usd:>14,.0f}")


# ── Charts ───────────────────────────────────────────────────────────────────

def _fmt_usd(ax):
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"${v/1e6:.1f}M" if abs(v) >= 1e6 else
        (f"${v/1e3:.0f}k" if abs(v) >= 1e3 else f"${v:,.0f}")))


def _fmt_count(ax):
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"{v/1e6:.1f}M" if abs(v) >= 1e6 else
        (f"{v/1e3:.0f}k" if abs(v) >= 1e3 else f"{v:,.0f}")))


def _slug(name):
    return name.lower().replace(" ", "_")


def chart_buckets(var, g, chart_dir, name, subtitle, xlabel, gate_label, gate_edge):
    """Three stacked panels on one x: attackers, sandwiches, net profit per band.

    Three panels rather than one frame with three y-scales -- two counts and a
    dollar amount are incommensurable, and stacking scales in one frame invites the
    reader to see crossings that do not exist.
    """
    bands = [b for b in g.index if g.loc[b, "attackers"] > 0]
    gg = g.loc[bands]
    x = np.arange(len(bands))
    colour = SERIES[var]
    fig, ax = plt.subplots(3, 1, figsize=(10.5, 9.6), sharex=True)
    for a, vals, ylab, log in ((ax[0], gg.attackers, "attackers (log)", True),
                               (ax[1], gg.sandwiches, "sandwiches (log)", True),
                               (ax[2], gg.usd_net, "net USD in band", False)):
        a.bar(x, vals, 0.68, color=colour, edgecolor=SURFACE, linewidth=2)
        a.set_ylabel(ylab)
        if log:
            a.set_yscale("log")
        a.grid(axis="y", alpha=0.55)
        a.set_axisbelow(True)
        if gate_edge is not None:
            a.axvline(gate_edge, color=INK, linestyle="--", linewidth=1.1, alpha=0.55)
    _fmt_usd(ax[2])
    ax[2].axhline(0, color=INK_MUTED, linewidth=0.8)
    ax[0].set_title(f"{name} — population by band\n{subtitle}", loc="left", fontsize=11, pad=12)
    for b in gg.usd_net.nlargest(3).index:                     # label only what carries the profit
        k = bands.index(b)
        ax[2].annotate(f"{gg.loc[b, 'usd_net'] / 1e6:.2f}M", (k, gg.loc[b, "usd_net"]),
                       xytext=(0, 5), textcoords="offset points", ha="center",
                       fontsize=9, color=INK)
    if gate_edge is not None:
        ax[0].annotate(gate_label, (gate_edge, ax[0].get_ylim()[1]),
                       xytext=(6, -14), textcoords="offset points", fontsize=9, color=INK_MUTED)
    plt.xticks(x, bands, rotation=35, ha="right")
    ax[2].set_xlabel(xlabel)
    plt.tight_layout()
    out = os.path.join(chart_dir, f"{_slug(name)}_buckets.png")
    plt.savefig(out, dpi=160, bbox_inches="tight")
    plt.close()
    return out


def chart_sweep(var, s, chart_dir, name, subtitle, xlabel, gate_at, logx, gate_dir):
    """Cumulative curves, all rising to the right.

    Each panel is a running total AT OR BELOW the threshold, so the reader sees
    accumulation rather than depletion. The dashed line is the operating point.

    What the annotation says depends on which way the gate points, and getting this
    wrong would invert the reading: a `>=` gate (count, win rate, SC) DISCARDS
    everything at or below its threshold, so the curve's value there is the loss; a
    `<=` gate (front-gap median) KEEPS exactly that, so the same number is the yield.
    """
    colour = SERIES[var]
    gate_dir_sym = ">=" if gate_dir == "ge" else "<="
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.5))
    panels = ((ax[0], s.le_attackers, "attackers at or below",
               "Attackers accumulated", _fmt_count, False),
              (ax[1], s.le_sandwiches, "sandwiches at or below",
               "Sandwiches accumulated", _fmt_count, False),
              (ax[2], s.le_usd, "net USD at or below",
               "Net profit accumulated", _fmt_usd, True))
    for a, vals, ylab, title, fmt, is_usd in panels:
        a.plot(s.threshold, vals, "o-", color=colour, linewidth=2, markersize=5)
        a.set_xlabel(xlabel)
        a.set_ylabel(ylab)
        a.set_title(title, loc="left", fontsize=10.5, pad=8)
        a.grid(alpha=0.55)
        a.set_axisbelow(True)
        if logx:
            a.set_xscale("log")
        a.axvline(gate_at, color=INK, linestyle="--", linewidth=1.1, alpha=0.55)
        fmt(a)
        row = s[np.isclose(s.threshold.astype(float), float(gate_at))]
        if len(row):
            y = float(vals.loc[row.index[0]])
            txt = (f"${y/1e6:.2f}M" if is_usd else
                   (f"{y/1e6:.2f}M" if y >= 1e6 else f"{y:,.0f}"))
            # Place the callout on the empty side of the curve. A cumulative curve
            # is non-decreasing, so the space below-right of a high point is free and
            # the space above-right of a low one is; picking by the point's height
            # keeps the text off the line at every threshold without hand-tuning.
            lo, hi = a.get_ylim()
            high = (y - lo) / (hi - lo) > 0.55
            verb = "discards" if gate_dir == "ge" else "keeps"
            a.annotate(f"gate {gate_dir_sym} {gate_at:g}\n{verb} {txt}", (gate_at, y),
                       xytext=(12, -30 if high else 14), textcoords="offset points",
                       fontsize=9, color=INK,
                       bbox=dict(boxstyle="round,pad=0.28", facecolor=SURFACE,
                                 edgecolor=GRID, linewidth=0.8))
    fig.suptitle(f"{name} — cumulative sensitivity · {subtitle}",
                 x=0.005, ha="left", fontsize=11, y=1.03)
    plt.tight_layout()
    out = os.path.join(chart_dir, f"{_slug(name)}_sweep.png")
    plt.savefig(out, dpi=160, bbox_inches="tight")
    plt.close()
    return out


def chart_final_dist(var, g, chart_dir, name, subtitle, xlabel, slug):
    """The selected set, by band: how many attackers, and what each earns on average.

    Two panels, count and mean -- the pair a reader needs to tell "many small" from
    "few large". A total would hide that distinction, and is already in the CSV.
    """
    bands = [b for b in g.index if g.loc[b, "attackers"] > 0]
    gg = g.loc[bands]
    x = np.arange(len(bands))
    colour = SERIES[var]
    fig, ax = plt.subplots(2, 1, figsize=(10.5, 7.0), sharex=True)
    ax[0].bar(x, gg.attackers, 0.68, color=colour, edgecolor=SURFACE, linewidth=2)
    ax[0].set_ylabel("attackers")
    ax[0].set_title(f"{name}\n{subtitle}", loc="left", fontsize=11, pad=12)
    for k, v in zip(x, gg.attackers):
        ax[0].annotate(f"{int(v):,}", (k, v), xytext=(0, 4), textcoords="offset points",
                       ha="center", fontsize=9, color=INK)
    ax[1].bar(x, gg.usd_mean, 0.68, color=colour, edgecolor=SURFACE, linewidth=2)
    ax[1].set_ylabel("mean net USD per attacker")
    ax[1].set_xlabel(xlabel)
    _fmt_usd(ax[1])
    for k, v in zip(x, gg.usd_mean):
        ax[1].annotate(f"${v/1e3:.0f}k" if abs(v) >= 1e3 else f"${v:,.0f}", (k, v),
                       xytext=(0, 4), textcoords="offset points", ha="center",
                       fontsize=9, color=INK)
    for a in ax:
        a.grid(axis="y", alpha=0.55)
        a.set_axisbelow(True)
    plt.xticks(x, bands, rotation=35, ha="right")
    plt.tight_layout()
    out = os.path.join(chart_dir, f"{slug}.png")
    plt.savefig(out, dpi=160, bbox_inches="tight")
    plt.close()
    return out


# ── Stage driver ─────────────────────────────────────────────────────────────

def _gate_edge(g, gate_at):
    """x position of the boundary just below the first band at or above `gate_at`."""
    labels = [b for b in g.index if g.loc[b, "attackers"] > 0]
    for i, b in enumerate(labels):
        lo = b.split("-")[0].split("+")[0].replace(",", "")
        try:
            if float(lo) >= gate_at:
                return i - 0.5
        except ValueError:
            continue
    return None


def run_stage(var, d, dirs, name, col, edges, sweep, fmt_band,
              xlabel, gate_at, gate_label, logx, subtitle, gate_dir="ge"):
    n_nan = int(d[col].isna().sum())
    if n_nan:
        print(f"\n  [{name}] {n_nan:,} of {len(d):,} signers have no {col} "
              f"({n_nan/len(d):.1%}); they are outside every band and fail the gate by "
              f"NaN comparison, which is the intended reading -- an unmeasurable signer "
              f"is not a certified one.")
    g = bucket_table(d, col, edges, fmt_band)
    s = sweep_table(d, col, sweep)
    print_buckets(f"{name} — by {col}   [{subtitle}]", g, len(d), d["usd_net_total"].sum())
    print_sweep(f"{name} — cumulative", s, col)
    g.to_csv(os.path.join(dirs["data"], f"{_slug(name)}_buckets.csv"))
    s.to_csv(os.path.join(dirs["data"], f"{_slug(name)}_sweep.csv"), index=False)
    return [chart_buckets(var, g, dirs["charts"], name, subtitle, xlabel,
                          gate_label, _gate_edge(g, gate_at)),
            chart_sweep(var, s, dirs["charts"], name, subtitle, xlabel, gate_at, logx,
                        gate_dir)]


def main():
    a = parse_args()
    tag = f"{a.start_epoch}_{a.end_epoch}"
    root = os.path.join(a.out_root, a.category, a.database, tag)
    print("=== Phase 2: gate selection & sensitivity ===")
    print(f"Database {a.database} · category {a.category} · epochs {a.start_epoch}-{a.end_epoch}")
    print(f"Thresholds: n_min={a.n_min}  wr_min={a.wr_min}  slip_min={a.slip_min}  "
          f"fg_median_max={a.fg_median_max:g}  usd_min={a.usd_min:g}")

    written = []
    for var in ("include", "exclude"):
        dirs = {k: os.path.join(root, f"{var}_XL", k) for k in ("charts", "data")}
        for p in dirs.values():
            os.makedirs(p, exist_ok=True)
        d = load_variant(a.category, a.database, a.start_epoch, a.end_epoch, var)
        base = f"epochs {tag.replace('_', '–')} · standard · cross-leader {var}d"
        print(f"\n{'=' * 104}\ncross-leader {var}d: {len(d):,} signers · "
              f"{int(d.sandwich_count.sum()):,} sandwiches · net ${d.usd_net_total.sum():,.0f}")

        written += run_stage(
            var, d, dirs, "Stage 1 count", "sandwich_count", COUNT_EDGES, COUNT_SWEEP,
            lambda lo, hi: f"{lo:,}+" if hi > 10**11 else f"{lo:,}-{hi - 1:,}",
            "sandwiches per attacker", a.n_min, f"gate: n_min = {a.n_min}", True,
            f"{base} · {len(d):,} signers")

        # Stage 2 sees only what stage 1 admits. Measuring the win-rate gate on the
        # full population would describe a set it never meets.
        d2 = d[d.sandwich_count >= a.n_min]
        written += run_stage(
            var, d2, dirs, "Stage 2 win rate", "sol_win_rate", WR_EDGES, WR_SWEEP,
            lambda lo, hi: f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "SOL win rate, net of own fees", a.wr_min, f"gate: WR = {a.wr_min}", False,
            f"{base} · after n_min>={a.n_min}: {len(d2):,} signers")

        # Stages 3a and 3b are both measured on stage 2's output, in parallel. Neither
        # is conditioned on the other, so each curve shows that gate's own contribution.
        d3 = d2[d2.sol_win_rate >= a.wr_min]
        sub3 = f"{base} · after n_min>={a.n_min}, WR>={a.wr_min}: {len(d3):,} signers"
        written += run_stage(
            var, d3, dirs, "Stage 3a slippage", "mean_SC", SC_EDGES, SC_SWEEP,
            lambda lo, hi: f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "mean slippage consumption", a.slip_min, f"gate: SC = {a.slip_min}", False, sub3)
        written += run_stage(
            var, d3, dirs, "Stage 3b front gap", "front_gap_p50", FG_EDGES, FG_SWEEP,
            lambda lo, hi: f"{lo:,}+" if hi > 10**11 else f"{lo:,}-{hi - 1:,}",
            "median front_gap (transactions)", a.fg_median_max,
            f"gate: median <= {a.fg_median_max:g}", True, sub3, gate_dir="le")

        # Stage 4: the selected set. Every gate has been applied; the question is no
        # longer where to cut but what the survivors look like.
        final = d3[(d3.mean_SC >= a.slip_min) & (d3.front_gap_p50 <= a.fg_median_max)]
        only_sc = d3[d3.mean_SC >= a.slip_min]
        only_fg = d3[d3.front_gap_p50 <= a.fg_median_max]
        print(f"\n  >>> {var}_XL funnel: {len(d):,} -> {len(d2):,} (n_min) -> {len(d3):,} (WR) "
              f"-> {len(final):,} (SC and front gap) · net ${final.usd_net_total.sum():,.0f}")
        print(f"      gate overlap on stage 2's {len(d3):,}: SC alone keeps {len(only_sc):,}, "
              f"front gap alone keeps {len(only_fg):,}, both keep {len(final):,} "
              f"-- SC removes {len(only_fg) - len(final):,} that front gap would have kept")

        sub4 = f"{base} · SELECTED SET after all four gates: {len(final):,} attackers · " \
               f"net ${final.usd_net_total.sum():,.0f}"
        for col, edges, fmt_band, xlabel, slug in (
                ("sandwich_count", COUNT_EDGES,
                 lambda lo, hi: f"{lo:,}+" if hi > 10**11 else f"{lo:,}-{hi - 1:,}",
                 "sandwiches per attacker", "stage_4_selected_by_count"),
                # No "$" in the band label: matplotlib reads a dollar sign as the start
                # of mathtext, which swallows it and re-renders the hyphen as a minus in
                # an italic font. The unit lives on the axis label instead.
                ("usd_net_total", PROFIT_EDGES,
                 lambda lo, hi: (f"{lo:,.0f}+" if hi > 10**11
                                 else f"{lo:,.0f}-{hi - 1:,.0f}"),
                 "net USD per attacker", "stage_4_selected_by_profit")):
            g4 = bucket_table(final, col, edges, fmt_band)
            print_buckets(f"Stage 4 selected set — by {col}   [{len(final):,} attackers]",
                          g4, len(final), final["usd_net_total"].sum())
            g4.to_csv(os.path.join(dirs["data"], f"{slug}.csv"))
            written.append(chart_final_dist(var, g4, dirs["charts"],
                                            f"Selected attackers by {col}", sub4, xlabel, slug))
        final.to_csv(os.path.join(dirs["data"], "stage_4_selected_attackers.csv"))

    print(f"\nSaved under {root}/")
    for w in written:
        print(f"  {w.replace(root + '/', '')}")


if __name__ == "__main__":
    main()
