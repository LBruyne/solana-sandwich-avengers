"""Section 6 and Appendix D figures.

    6.1_volume_and_profit.pdf     Fig. 4   S5     daily volume and profit by block geometry
    6.1_profit_concentration.pdf  Fig. 5   S6.1   profit concentration across attackers
    6.1_distance_profit.pdf       Fig. 6   S6.2   distance against profit
    6.3_rotation_heatmap.pdf      Fig. 7   S6.2   where a multi-leader sandwich's legs sit
    6.5_cost.pdf                  Fig. 15  App D  per-leg execution cost
    6.1_profit_range.pdf          Fig. 16  App D  sandwiches and profit by profit band
    6.6_program.pdf               Fig. 17  App D  daily share by attacker custom program

Geometry classes, mutually exclusive and exhaustive:

    in-block               NOT cross_block
    same-leader (SL-CB)    cross_block AND NOT cross_leader
    cross-leader (ML-CB)   cross_leader            (a subset of cross_block)

The per-sandwich frame is phase 3's output. Two things it does not carry are queried from
ClickHouse and cached under ./data: the per-leg frame (`load_legs`) and the front-to-back
distance (`sandwich_distance`).

Usage:
    python sec6_measure_charts.py
    python sec6_measure_charts.py --refresh-legs
    python sec6_measure_charts.py --tips known-only
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

sys.path.insert(0, str(figcfg.INTENT))

# One hue per geometry class, held constant across the figures. Palette lives in `figcfg`.
C_IB, C_SL, C_XL = figcfg.BLUE_L, figcfg.BLUE_M, figcfg.BLUE_D
C_IB_P, C_SL_P, C_XL_P = figcfg.CORAL_L, figcfg.CORAL_M, figcfg.CORAL_D
C_LINE = figcfg.GREY
CLASSES = ["in-block", "same-leader", "cross-leader"]
# Legend names: IB in-block, SL-CB same-leader cross-block, ML-CB multi-leader cross-block.
CLASS_LABEL = {"in-block": "IB", "same-leader": "SL-CB", "cross-leader": "ML-CB"}

# Venue panel, grouped by operator: `pumpfun` and `pumpfun_amm` are one venue, and Meteora
# and Raydium each ship several pool types. Every label the detector can emit is mapped
# (`sol/pool_dex.go`), so "other" means a venue outside the three named families.
DEX_FAMILY = {
    "pumpfun": "pumpfun", "pumpfun_amm": "pumpfun",
    "meteora_damm_v2": "meteora", "meteora_dbc": "meteora",
    "meteora_dlmm": "meteora", "meteora_pools": "meteora",
    "raydium_cpmm": "raydium", "raydium_v4": "raydium", "raydium_clmm": "raydium",
    "raydium_route": "raydium",
    "whirlpool": "other", "orca": "other", "pancakeswap": "other", "byreal": "other",
    "stepn_dooar": "other", "solfi": "other", "bisonfi": "other", "humidifi": "other",
    "fusion": "other", "tessera": "other", "goonfi": "other", "alphaq": "other",
    "obric": "other", "zerofi": "other",
}
DEX_KEEP = ["pumpfun", "meteora", "raydium"]
DEX_LABEL = {"pumpfun": "Pump.fun", "meteora": "Meteora", "raydium": "Raydium",
             "other": "Others"}
DEX_COLOR = {"pumpfun": "#C9A227", "meteora": figcfg.BLUE_L, "raydium": figcfg.BLUE_D,
             "other": "#BDBDBD"}

plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False,
                     "figure.facecolor": "white", "savefig.facecolor": "white"})


def sci_fmt():
    f = mticker.ScalarFormatter(useMathText=True)
    f.set_scientific(True)
    f.set_powerlimits((-2, 3))
    return f


def grid(ax):
    ax.grid(axis="y", linestyle=":", linewidth=0.8, color="#CCCCCC", alpha=0.7)
    ax.set_axisbelow(True)


def classify(sw):
    """Three mutually exclusive geometry classes. cross_leader is a SUBSET of cross_block."""
    xl = sw["cross_leader"].astype(bool)
    xb = sw["cross_block"].astype(bool)
    assert not (xl & ~xb).any(), "cross_leader outside cross_block — the invariant broke"
    return np.select([~xb, xb & ~xl], ["in-block", "same-leader"], default="cross-leader")


def daily_user_txs(a):
    """Daily non-vote, non-failed transaction counts. The only thing not already in phase 3."""
    from utils.db import get_client
    cl = get_client(a.database)
    lo, hi = a.start_epoch * 432_000, (a.end_epoch + 1) * 432_000
    d = cl.query_df(f"""
        SELECT toDate(s.timestamp) AS date, sum(t.validTxCount) AS total_txs
        FROM {a.database}.slot_txs t
        INNER JOIN (SELECT slot, min(timestamp) AS timestamp FROM {a.database}.sandwiches
                    WHERE slot >= {lo} AND slot < {hi} GROUP BY slot) s ON t.slot = s.slot
        WHERE t.slot >= {lo} AND t.slot < {hi}
        GROUP BY date ORDER BY date""")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")["total_txs"]


# ── the per-leg frame ────────────────────────────────────────────────────────
#
# The distance, cost and program figures are about individual LEGS; phase 3 aggregates to
# the sandwich, so those columns come from `sandwich_txs`. One pass builds them and caches
# the result under ./data.
#
# `tip_sol` has three states: 0.0 for a leg that never entered a bundle, the tip for a leg
# whose bundle row is present, and NaN for a leg whose bundle row is not in `jito_bundles`.
LEG_TYPES = ("frontRun", "victim", "backRun")


def legs_cache_path(a):
    return Path(__file__).resolve().parent / "data" / f"legs_{a.database}_{figcfg.tag(a)}.parquet"


def load_legs(a, sw, refresh=False):
    """One row per leg of the attacker set: sid, type, slot, position, fee_sol, tip_sol, progs."""
    cache = legs_cache_path(a)
    if cache.exists() and not refresh:
        d = pd.read_parquet(cache)
        print(f"  legs: {len(d):,} from cache {cache.name}")
        return _annotate_legs(d, sw)

    from utils.db import get_client
    cl = get_client(a.database)
    ids = pd.Index(sw.index.astype(str).unique())
    tmp = "default._an_sw_legs"
    cl.command(f"DROP TABLE IF EXISTS {tmp}")
    cl.command(f"CREATE TABLE {tmp} (sandwichId String) ENGINE=Memory")
    cl.insert(tmp, [[x] for x in ids], column_names=["sandwichId"])

    parts = []
    for epoch in range(a.start_epoch, a.end_epoch + 1):
        lo, hi = epoch * 432_000, (epoch + 1) * 432_000
        # The bundle side is restricted to this epoch's slots before the arrayJoin;
        # unrestricted it expands half a billion rows and the server runs out of memory.
        d = cl.query_df(f"""
            SELECT st.sandwichId AS sid, st.type AS type, st.slot AS slot,
                   st.position AS position, st.fee / 1e9 AS fee_sol,
                   st.inBundle AS in_bundle, st.programs AS progs,
                   jb.tip_sol AS tip_sol, jb.sig != '' AS tip_found
            FROM sandwich_txs st
            LEFT JOIN (
                SELECT sig, max(tip) AS tip_sol FROM (
                    SELECT arrayJoin(transactions) AS sig,
                           landedTipLamports / 1e9 AS tip
                    FROM jito_bundles WHERE slot >= {lo} AND slot < {hi})
                GROUP BY sig
            ) jb ON st.signature = jb.sig
            WHERE st.slot >= {lo} AND st.slot < {hi}
              AND st.type IN {LEG_TYPES}
              AND st.sandwichId IN (SELECT sandwichId FROM {tmp})""")
        if len(d):
            parts.append(d)
        print(f"    epoch {epoch}: {len(d):,} legs", end="\r")
    cl.command(f"DROP TABLE IF EXISTS {tmp}")

    d = pd.concat(parts, ignore_index=True)
    # ClickHouse fills an unmatched LEFT JOIN with the column default (0.0), not NULL, so
    # `tip_found` is what separates a missing bundle row from a genuinely untipped leg.
    bundled = d["in_bundle"].astype(bool)
    found = d["tip_found"].astype(bool)
    d["tip_sol"] = np.where(~bundled, 0.0, np.where(found, d["tip_sol"], np.nan))
    d = d.drop(columns=["tip_found"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    d.to_parquet(cache, index=False)
    print(f"  legs: {len(d):,} written to {cache.name}")
    return _annotate_legs(d, sw)


def _annotate_legs(d, sw):
    """Attach the sandwich's block geometry and the leg's total cost."""
    d = d.join(sw[["cross_block"]].rename(columns={"cross_block": "_xb"}), on="sid")
    d["cls2"] = np.where(d["_xb"].astype(bool), "cross-block", "in-block")
    d = d.drop(columns=["_xb"])
    d["cost"] = d["fee_sol"] + d["tip_sol"]
    unknown = int(d["tip_sol"].isna().sum())
    bundled = int(d["in_bundle"].astype(bool).sum())
    print(f"  tips: {bundled:,} legs entered a bundle; {bundled - unknown:,} have a recoverable "
          f"tip, {unknown:,} do not (Jito reclaimed the bundle) and are left unmeasured")
    return d


def leg_programs(legs):
    """(sid, prog) for the attacker's own legs, one row per invoked program."""
    d = legs[legs["type"].isin(("frontRun", "backRun"))][["sid", "progs"]]
    return d.explode("progs").rename(columns={"progs": "prog"}).dropna(subset=["prog"])


def sandwich_distance(a, legs):
    """Transactions between the FIRST front-run leg and the LAST back-run leg.

    Uses phase 1's `_extreme_leg` and `_build_slot_coord`, the same two functions that
    produce `front_gap` and `back_gap`.
    """
    ph1, _ = figcfg.phase_module(1)
    from utils.db import get_client
    cl = get_client(a.database)

    f = legs[legs["type"] == "frontRun"][["sid", "slot", "position"]]
    b = legs[legs["type"] == "backRun"][["sid", "slot", "position"]]
    f = f.rename(columns={"sid": "sandwichId"})
    b = b.rename(columns={"sid": "sandwichId"})
    first_f = ph1._extreme_leg(f, "first")
    last_b = ph1._extreme_leg(b, "last")

    lo = int(min(first_f["slot"].min(), last_b["slot"].min()))
    hi = int(max(first_f["slot"].max(), last_b["slot"].max()))
    counts = cl.query_df(
        f"SELECT slot, txCount FROM slot_txs WHERE slot >= {lo} AND slot <= {hi}"
    ).set_index("slot")["txCount"]
    diag = {}
    prefix = ph1._build_slot_coord(counts, lo, hi, diag)

    j = first_f.join(last_b, lsuffix="_f", rsuffix="_b", how="inner")
    dist = (ph1._coord(prefix, lo, j["slot_b"], j["position_b"])
            - ph1._coord(prefix, lo, j["slot_f"], j["position_f"]))
    return pd.Series(dist.to_numpy(dtype="int64"), index=j.index, name="distance")


# ── 6.1 volume and profit over time ──────────────────────────────────────────

def fig_volume_profit(a, sw, txs):
    """Three stacked panels sharing one daily axis: count, venue mix, and profit.

    Bars sit on integer positions rather than a datetime axis, so the venue panel's
    grouping offsets (a fixed number of bar widths) keep all three panels aligned.
    """
    d = sw.copy()
    d["date"] = pd.to_datetime(d["ts"], utc=True).dt.floor("D")
    g = (d.groupby(["date", "cls"])
           .agg(count=("signer", "size"), profit=("usd_profit", "sum")).reset_index())
    cnt = g.pivot(index="date", columns="cls", values="count").reindex(columns=CLASSES).fillna(0)
    prf = g.pivot(index="date", columns="cls", values="profit").reindex(columns=CLASSES).fillna(0)

    fam = d["pool_dex"].map(DEX_FAMILY)
    unknown = sorted(d.loc[fam.isna(), "pool_dex"].dropna().unique())
    if unknown:
        # Never silently drop a venue: an unmapped pool program becomes "Other" and is named.
        print(f"  NOTE: pool_dex not in DEX_FAMILY, folded into Other: {unknown}")
    ven = (d.assign(v=fam.fillna("other")).groupby(["date", "v"]).size()
             .unstack(fill_value=0).reindex(columns=DEX_KEEP + ["other"], fill_value=0))
    # "Others" is drawn only when it holds something.
    if ven["other"].sum() == 0:
        ven = ven.drop(columns=["other"])
    else:
        print(f"  NOTE: Others is non-empty ({int(ven['other'].sum()):,} sandwiches)")

    # Drop the window's last, partial day. Two statements, not one tuple assignment: a
    # tuple's right-hand side is evaluated before any binding, so the reindex would use the
    # untruncated index.
    cnt, prf = cnt.iloc[:-1], prf.iloc[:-1]
    ven = ven.reindex(cnt.index).fillna(0)
    t = txs.reindex(cnt.index)

    dates = cnt.index
    x = np.arange(len(dates), dtype=float)
    # Panel order: count, venue, profit; profit carries the shared date axis. 24 x 9.5
    # renders to about 183 pt tall at USENIX's 505.9 pt \textwidth.
    fig, (av, ad, ap) = plt.subplots(3, 1, figsize=(24, 9.5), sharex=True,
                                     gridspec_kw={"height_ratios": [1.0, 1.10, 0.95]})
    # An explicit right margin: the twinx label is the rightmost artist and the tight box
    # clips it.
    fig.subplots_adjust(hspace=0.16, right=0.93, left=0.075)
    FS_LAB, FS_TICK, FS_LEG = 30, 26, 25
    # y labels are smaller than x: rotated, they are laid out along the panel height, and
    # with three stacked panels each panel is short. Magnitudes go on the ticks instead.
    FS_YLAB = 26

    # ── panel 1: daily count by geometry ───────────────────────────────────────
    bot = np.zeros(len(cnt))
    for c, col in zip(CLASSES, (C_IB, C_SL, C_XL)):
        av.bar(x, cnt[c], width=0.82, bottom=bot, color=col, label=CLASS_LABEL[c])
        bot = bot + cnt[c].to_numpy()
    # No scientific offset: matplotlib parks it where the legend row goes.
    av.set_ylabel("# Sandwich", fontsize=FS_YLAB)
    av.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    av.tick_params(axis="x", length=0, labelbottom=False)
    av.tick_params(axis="y", labelsize=FS_TICK)
    av.set_ylim(0, bot.max() * 1.42)
    av.set_yticks([0, 2000, 4000, 6000])
    grid(av)

    avr = av.twinx()
    tm = t.to_numpy(dtype=float) / 1e6
    avr.plot(x, tm, color=C_LINE, marker="s", markersize=5,
             linewidth=1.8, linestyle="--", label="Solana Tx Volume")
    # Kept short: rotated, a longer label overruns the panel height.
    avr.set_ylabel("Solana Tx", fontsize=FS_YLAB)
    avr.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}M"))
    avr.tick_params(axis="y", labelsize=FS_TICK)
    avr.spines["top"].set_visible(False)
    fin = pd.Series(tm).dropna()
    if len(fin):
        # Headroom on BOTH axes so the top of the panel is clear for the legend: the bars are
        # on the left axis and the transaction line on the right, and either can run into it.
        avr.set_ylim(fin.min() * 0.90, fin.max() * 1.30)
        avr.set_yticks([40, 50, 60])
    h1, l1 = av.get_legend_handles_labels()
    h2, l2 = avr.get_legend_handles_labels()
    # Above the panel, not inside it: the user-transaction line uses the full height of the
    # right axis, so any in-panel corner it could sit in is a corner the line passes through.
    av.legend(h1 + h2, l1 + l2, loc="upper left", ncol=4, fontsize=FS_LEG, frameon=False,
              borderaxespad=0.4, handlelength=1.8, columnspacing=2.2)

    # ── panel 3: daily profit by geometry ──────────────────────────────────────
    bot = np.zeros(len(prf))
    for c, col in zip(CLASSES, (C_IB_P, C_SL_P, C_XL_P)):
        ap.bar(x, prf[c] / 1e3, width=0.82, bottom=bot, color=col, label=CLASS_LABEL[c])
        bot = bot + prf[c].to_numpy() / 1e3
    ap.set_ylabel("Profit (\\$)", fontsize=FS_YLAB)
    ap.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: "0" if v == 0 else f"{v:,.0f}k"))
    step = max(1, len(x) // 18)
    ap.set_xticks(x[::step])
    ap.set_xticklabels([dt.strftime("%m/%d") for dt in dates[::step]])
    ap.tick_params(axis="x", labelsize=FS_TICK, rotation=30)
    ap.tick_params(axis="y", labelsize=FS_TICK)
    ap.set_xlabel("Date", fontsize=FS_LAB)
    ap.set_ylim(0, bot.max() * 1.42)
    ap.set_yticks([0, 25, 50, 75])
    ap.legend(loc="upper left", ncol=3, fontsize=FS_LEG, frameon=False, borderaxespad=0.4)
    grid(ap)

    # ── panel 2: daily count by venue ──────────────────────────────────────────
    series = [(k, DEX_LABEL[k], DEX_COLOR[k]) for k in ven.columns]
    n = len(series)
    slot = 0.86 / n          # the remainder is the gap BETWEEN days, without which the groups
    tot = ven.sum(axis=1)    # run together into one band
    for i, (key, lab, col) in enumerate(series):
        v = ven[key].to_numpy(dtype=float)
        off = (i - (n - 1) / 2) * slot
        ad.bar(x + off, np.where(v > 0, v, np.nan), width=slot, bottom=0.6,
               color=col, edgecolor="none", linewidth=0.0, label=lab)
    ad.set_yscale("log")
    # A multiple, not an exponent: on a log axis `max ** 1.5` is two decades of empty sky.
    ad.set_ylim(0.6, tot.max() * 12)
    ad.set_ylabel("# Sandwich", fontsize=FS_YLAB)
    ad.set_yticks([1, 10, 100, 1000, 10000])
    ad.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ad.yaxis.set_minor_formatter(mticker.NullFormatter())
    ad.set_xlim(-0.8, len(x) - 0.2)
    ad.tick_params(axis="x", length=0, labelbottom=False)
    ad.tick_params(axis="y", labelsize=FS_TICK)
    ad.legend(loc="upper left", ncol=4, fontsize=FS_LEG, frameon=False,
              borderaxespad=0.4, columnspacing=1.4, handlelength=1.5)
    grid(ad)

    # Without this the three labels sit at whatever x their own tick labels leave free, so
    # "10,000" on one panel and "100" on another push them to different depths.
    fig.align_ylabels([av, ad, ap])
    figcfg.save(fig, a, "6.1_volume_and_profit")
    plt.close(fig)
    print(f"  venues: " + " · ".join(
        f"{DEX_LABEL.get(k, k)} {int(ven[k].sum()):,}" for k in ven.columns))
    return cnt, prf


# ── 6.1 profit-range breakdown ───────────────────────────────────────────────

BINS = [-np.inf, -100, -10, -1, 0, 1, 10, 100, 1000, np.inf]
LABELS = ["<-100", "-100..-10", "-10..-1", "-1..0", "0..1", "1..10", "10..100",
          "100..1k", ">1k"]


def fig_profit_range(a, sw):
    """Sandwich count and total profit per per-sandwich profit band.

    Bars are the count on a log left axis, the line the aggregate on a right axis. Profit
    is net of the attacker's own fees, so the loss bands sit below zero.
    """
    d = sw[sw["usd_profit_net"].notna()].copy()
    d["bin"] = pd.cut(d["usd_profit_net"], BINS, labels=LABELS, right=False)
    g = d.groupby("bin", observed=False).agg(n=("usd_profit_net", "size"),
                                             usd=("usd_profit_net", "sum")).reindex(LABELS)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 12, "legend.fontsize": 10,
                         "xtick.labelsize": 10, "ytick.labelsize": 10,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    x = np.arange(len(LABELS))
    colors = np.where(g["usd"].to_numpy() < 0, C_IB, C_SL)
    ax.bar(x, g["n"].to_numpy(), width=0.74, color=colors, edgecolor="white", linewidth=0.6)
    ax.set_yscale("log")
    ax.set_ylim(0.6, g["n"].max() * 22)

    # The count axis is logarithmic and the profit axis is not, so the count is printed on
    # each bar.
    def _k(v):
        v = int(v)
        return f"{v:,}" if v < 1000 else (f"{v / 1000:.1f}k" if v < 100_000 else f"{v / 1000:.0f}k")
    for xi, n in zip(x, g["n"].to_numpy()):
        if n > 0:
            ax.annotate(_k(n), (xi, n), textcoords="offset points", xytext=(0, 3),
                        ha="center", va="bottom", fontsize=8.5, color="#333333")
    ax.set_ylabel("Sandwich count")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.set_xticks(x)
    ax.set_xticklabels(LABELS, rotation=30, ha="right")
    ax.set_xlabel("Per-sandwich net profit (USD)")

    ax2 = ax.twinx()
    v = g["usd"].to_numpy(dtype=float) / 1e3
    ax2.plot(x, v, color=C_SL_P, linewidth=1.8, marker="o", markersize=4.5,
             markerfacecolor="white", markeredgewidth=1.3, markeredgecolor=C_SL_P,
             zorder=4, clip_on=False)
    ax2.axhline(0, color=C_SL_P, linewidth=0.7, linestyle=":", zorder=1)
    lo, hi = float(v.min()), float(v.max())
    # Extra headroom on the profit axis, not the count axis: the profit peak and the count label
    # on the same band were landing on each other.
    ax2.set_ylim(lo - 0.25 * (hi - lo), hi + 0.55 * (hi - lo))
    ax2.set_ylabel("Total profit (k USD)", color=C_SL_P)
    ax2.tick_params(axis="y", colors=C_SL_P, length=3.5)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda t, _: f"{t:,.0f}"))
    for sp in ("top", "left"):
        ax2.spines[sp].set_visible(False)
    ax2.spines["right"].set_color(C_SL_P)
    ax2.spines["right"].set_linewidth(0.8)

    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#444444")
        ax.spines[sp].set_linewidth(0.8)
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc")
    ax.set_axisbelow(True)
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor=C_SL, edgecolor="white", label="count, profitable band"),
                       Patch(facecolor=C_IB, edgecolor="white", label="count, loss band"),
                       Line2D([0], [0], color=C_SL_P, linewidth=1.8, marker="o", markersize=4.5,
                              markerfacecolor="white", markeredgecolor=C_SL_P, markeredgewidth=1.3,
                              label="total profit")],
              loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=3, frameon=False,
              handlelength=1.4, columnspacing=1.2, borderaxespad=0.0, fontsize=9.5)
    fig.tight_layout()
    figcfg.save(fig, a, "6.1_profit_range")
    plt.close(fig)

    print(f"  priced {len(d):,}/{len(sw):,} · total ${d['usd_profit_net'].sum():,.0f}")
    print(f"  {'band':>12}{'count':>10}{'share':>8}{'profit':>13}{'share':>8}")
    for k in LABELS:
        r = g.loc[k]
        print(f"  {k:>12}{int(r['n']):>10,}{r['n'] / len(d):>8.1%}"
              f"{'$' + format(r['usd'], ',.0f'):>13}{r['usd'] / d['usd_profit_net'].sum():>8.1%}")
    return g


# ── 6.1 positional distance vs profit ────────────────────────────────────────

def fig_distance_profit(a, sw, x_hi=8000):
    """Sandwich distance against profit, with the two block-geometry regimes marked.

    `distance` is the transaction count between the first frontrun and the last backrun,
    spanning block boundaries via `slot_txs.txCount`.

    The two vertical markers are read off the data: the distances past which more than
    half of sandwiches span two blocks, and two leaders.
    """
    d = sw.dropna(subset=["distance", "usd_profit_net"]).copy()
    d = d[d["distance"] >= 0].sort_values("distance")
    x = d["distance"].to_numpy(dtype=float)
    y = d["usd_profit_net"].to_numpy(dtype=float)

    def crossover(col, tgt=0.5, win=4000):
        v = pd.Series(d[col].astype(bool).to_numpy()).rolling(win, center=True).mean().to_numpy()
        i = np.where(v >= tgt)[0]
        return float(x[i[0]]) if len(i) else None
    d_cb, d_xl = crossover("cross_block"), crossover("cross_leader")

    win = max(2000, len(d) // 60)
    step = win // 4
    xm, qs = [], {q: [] for q in (5, 25, 50, 75, 95)}
    for st in range(0, len(d) - win, step):
        c = y[st:st + win]
        xm.append(np.median(x[st:st + win]))
        for q in qs:
            qs[q].append(np.percentile(c, q))
    xm = np.array(xm)
    qs = {q: np.array(v) for q, v in qs.items()}

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12,
                         "axes.labelsize": 13, "legend.fontsize": 11,
                         "xtick.labelsize": 11, "ytick.labelsize": 11,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    # 5.6 in wide, not 6.4: at \linewidth in a single USENIX column the figure is scaled by
    # 3.34/width, so a narrower canvas is what makes the type larger on the page.
    fig, (ax, ac) = plt.subplots(2, 1, figsize=(5.6, 3.9), sharex=True,
                                 gridspec_kw={"height_ratios": [1.55, 1.0]})
    fig.subplots_adjust(hspace=0.12)

    ax.fill_between(xm, qs[5], qs[95], color=C_IB_P, alpha=0.30, linewidth=0, label="P5\u2013P95")
    ax.fill_between(xm, qs[25], qs[75], color=C_IB_P, alpha=0.75, linewidth=0, label="P25\u2013P75")
    ax.plot(xm, qs[50], color=C_SL_P, linewidth=2.0, label="median", zorder=3)
    ax.axhline(0, color="#999999", linewidth=0.7, linestyle=":", zorder=1)
    ax.set_ylabel("Profit (USD)")
    # Symmetric-log: the distribution spans two decades and reaches negative values.
    ax.set_yscale("symlog", linthresh=1.0, linscale=0.45)
    ax.set_yticks([-10, 0, 1, 10, 100])
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.set_ylim(float(np.min(qs[5])) * 2.2, float(np.max(qs[95])) * 1.6)
    ax.tick_params(axis="x", length=0, labelbottom=False)
    # Above the panel: the P5-P95 band spans the full width, so every in-panel corner is
    # occupied by the data it would annotate.
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=3, frameon=False,
              handlelength=1.5, columnspacing=1.6, borderaxespad=0.0)

    bins = np.linspace(0, x_hi, 41)
    cnt, _ = np.histogram(x, bins=bins)
    ac.bar(0.5 * (bins[:-1] + bins[1:]), cnt, width=(bins[1] - bins[0]) * 0.92,
           color=C_SL, edgecolor="white", linewidth=0.4)
    ac.set_ylabel("Count")
    ac.set_xlabel("Distance (transactions between $T_F$ and $T_B$)")
    ac.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"{v/1000:g}k" if v >= 1000 else f"{v:g}"))
    ac.set_ylim(0, cnt.max() * 1.42)

    for axis in (ax, ac):
        axis.set_xlim(0, x_hi)
        for sp in ("top", "right"):
            axis.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            axis.spines[sp].set_color("#444444")
            axis.spines[sp].set_linewidth(0.8)
        axis.tick_params(axis="both", which="major", length=3.5, color="#444444")
        axis.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc")
        axis.set_axisbelow(True)
        for v in (d_cb, d_xl):
            if v:
                axis.axvline(v, color="#222222", linestyle=(0, (4, 2)), linewidth=1.3, zorder=4)

    box = dict(boxstyle="round,pad=0.24", fc="white", ec="#222222", lw=0.7)
    for v, lab in ((d_cb, "cross-block\nborderline"), (d_xl, "cross-leader\nborderline")):
        if v:
            ac.text(v + x_hi * 0.015, ac.get_ylim()[1] * 0.96, lab, ha="left", va="top",
                    fontsize=10, bbox=box, zorder=5)
    fig.tight_layout()
    figcfg.save(fig, a, "6.1_distance_profit")
    plt.close(fig)

    print(f"  distance: median {np.median(x):,.0f} · mean {x.mean():,.0f} · max {x.max():,.0f}")
    print(f"  P(cross-block) reaches 50% at distance {d_cb:,.0f}")
    print(f"  P(cross-leader) reaches 50% at distance {d_xl:,.0f}")
    for c in CLASSES:
        s = d[d["cls"] == c]["distance"]
        print(f"    {CLASS_LABEL[c]:>6}: median {s.median():>7,.0f} · p25 {s.quantile(.25):>7,.0f} "
              f"· p75 {s.quantile(.75):>7,.0f}")
    return d_cb, d_xl


def fig_cost(a, legs):
    """Per-leg execution cost, split by leg role and by block geometry.

    Cost is the leg's own transaction fee plus the tip of the bundle it landed in. Legs
    whose `tip_sol` is NaN are handled by `--tips`. Geometry here is the two-way
    in-block / cross-block split, not the three-way split of the daily figure.
    """
    n0 = len(legs)
    unknown = int(legs["cost"].isna().sum())
    if unknown:
        if a.tips == "fee-floor":
            # The leg's fee alone is a lower bound on its cost.
            legs = legs.assign(cost=legs["cost"].fillna(legs["fee_sol"]))
            print(f"  {unknown:,} legs of {n0:,} have an unrecoverable tip; counted at their "
                  f"fee alone, so their cost is a LOWER BOUND (--tips known-only to drop them)")
        else:
            legs = legs[legs["cost"].notna()]
            print(f"  dropped {unknown:,} legs of {n0:,} whose tip is unrecoverable "
                  f"(--tips fee-floor to keep them at their fee)")
    order = [("frontRun", "Frontrun"), ("victim", "Victim"), ("backRun", "Backrun")]
    groups = [("in-block", "In-block", C_IB), ("cross-block", "Cross-block", C_SL)]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 12, "legend.fontsize": 10,
                         "xtick.labelsize": 11, "ytick.labelsize": 10,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    w = 0.34
    for gi, (g, glab, col) in enumerate(groups):
        data = [legs[(legs["type"] == t) & (legs["cls2"] == g)]["cost"].to_numpy()
                for t, _ in order]
        pos = np.arange(len(order)) + (gi - 0.5) * w
        bp = ax.boxplot(data, positions=pos, widths=w * 0.86, showfliers=False,
                        patch_artist=True, medianprops=dict(color="black", linewidth=1.4),
                        whiskerprops=dict(color="#555555", linewidth=0.9),
                        capprops=dict(color="#555555", linewidth=0.9))
        for patch in bp["boxes"]:
            patch.set_facecolor(col)
            patch.set_edgecolor("#555555")
            patch.set_linewidth(0.8)
        # Mean as well as median: tips sit outside the quartiles the box shows.
        ax.plot(pos, [np.mean(v) for v in data], "D", ms=4.2, color="#222222",
                mec="white", mew=0.8, zorder=5)
        ax.plot([], [], "s", color=col, ms=9, label=glab)

    ax.set_yscale("log")
    ax.set_xticks(np.arange(len(order)))
    ax.set_xticklabels([lab for _, lab in order])
    ax.set_ylabel("Cost per transaction (SOL)")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#444444")
        ax.spines[sp].set_linewidth(0.8)
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc")
    ax.set_axisbelow(True)
    from matplotlib.lines import Line2D
    h, l = ax.get_legend_handles_labels()
    h.append(Line2D([0], [0], marker="D", color="none", markerfacecolor="#222222",
                    markeredgecolor="white", markersize=5.5, label="mean"))
    l.append("mean")
    ax.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=3, frameon=False,
              handlelength=1.2, columnspacing=1.6, borderaxespad=0.0)
    fig.tight_layout()
    figcfg.save(fig, a, "6.5_cost")
    plt.close(fig)

    print(f"  {'leg':>9}{'group':>13}{'n':>10}{'median':>12}{'mean':>12}")
    for t, lab in order:
        for g, glab, _ in groups:
            d = legs[(legs["type"] == t) & (legs["cls2"] == g)]["cost"]
            print(f"  {lab:>9}{glab:>13}{len(d):>10,}{d.median():>12.3e}{d.mean():>12.3e}")
    for g, glab, _ in groups:
        f = legs[(legs["type"] == "frontRun") & (legs["cls2"] == g)]["cost"]
        b = legs[(legs["type"] == "backRun") & (legs["cls2"] == g)]["cost"]
        print(f"  {glab:>13} front/back  median {f.median() / b.median():.2f}x  "
              f"mean {f.mean() / b.mean():.2f}x")
    return legs


# ── 6.6 venue mix ────────────────────────────────────────────────────────────

# Core Solana programs plus the Pump.fun venue programs. Anything else a front or back run
# invokes is a candidate "custom program".
CORE_PROGRAMS = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "SysvarRent111111111111111111111111111111111",
    "Sysvar1nstructions1111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
}
VENUE_PROGRAMS = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                  "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}
# Ten hues plus grey, drawn from the `figcfg` ramps.
PROG_COLORS = [figcfg.BLUE_M, "#4393C3", figcfg.BLUE_L, figcfg.BLUE_D, figcfg.ACCENT,
               "#7FBF7B", "#C9A227", figcfg.CORAL_M, figcfg.CORAL_D, "#8C6BB1", "#BDBDBD"]


def fig_program(a, sw, progs, top_n=10):
    """Daily share of sandwiches executed through each of the top custom attacker programs.

    `progs` is a (sid, prog) frame for the front and back legs. A sandwich is assigned to
    the single highest-ranked custom program it invokes, so the bands partition each day;
    the residual band is every sandwich touching no custom program.
    """
    cand = progs[~progs["prog"].isin(CORE_PROGRAMS | VENUE_PROGRAMS)]
    rank = cand["prog"].value_counts()
    top = list(rank.head(top_n).index)
    pri = {p: i for i, p in enumerate(top)}

    pick = (cand[cand["prog"].isin(pri)]
            .assign(r=lambda d: d["prog"].map(pri))
            .sort_values("r").groupby("sid", as_index=True)["prog"].first())
    d = sw.copy()
    d["date"] = pd.to_datetime(d["ts"], utc=True).dt.floor("D")
    d["prog"] = d.index.astype(str).map(pick).fillna("__none__")
    d = d[d["date"] < d["date"].max()]

    tab = (d.pivot_table(index="date", columns="prog", values="slot", aggfunc="size")
             .reindex(columns=top + ["__none__"], fill_value=0).fillna(0))
    share = tab.div(tab.sum(axis=1), axis=0) * 100

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 12, "legend.fontsize": 8.5,
                         "xtick.labelsize": 10, "ytick.labelsize": 10,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    x = np.arange(len(share))
    labels = [p[:5] for p in top] + ["No custom program"]
    ax.stackplot(x, [share[c].to_numpy() for c in top + ["__none__"]],
                 colors=PROG_COLORS[:top_n] + [PROG_COLORS[-1]], labels=labels,
                 edgecolor="none")
    ax.set_xlim(0, len(share) - 1)
    ax.set_ylim(0, 100)
    step = max(1, len(share) // 9)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([t.strftime("%m/%d") for t in share.index[::step]], rotation=30)
    ax.set_ylabel("Share of daily sandwiches")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}%"))
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#444444")
        ax.spines[sp].set_linewidth(0.8)
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=6, frameon=False,
              handlelength=1.1, columnspacing=1.0, borderaxespad=0.0, handletextpad=0.5)
    fig.tight_layout()
    figcfg.save(fig, a, "6.6_program")
    plt.close(fig)

    ids = set(pick.index)
    sub = sw.loc[[i for i in sw.index if str(i) in ids]]
    print(f"  top-{top_n} custom programs: {len(sub):,} sandwiches "
          f"({len(sub) / len(sw):.2%}) · ${sub['usd_profit_net'].sum():,.0f} "
          f"({sub['usd_profit_net'].sum() / sw['usd_profit_net'].sum():.1%}) · "
          f"{sub['signer'].astype(str).nunique()} attackers")
    return share


# -- 6.1 profit concentration -------------------------------------------------

def fig_profit_concentration(a, sw):
    """Cumulative share of profit and of attacks against the attacker percentile.

    Attackers are ranked by profit, richest first, so x is "the top q% of attackers".
    Drawn over the whole attacker set.
    """
    d = sw.copy()
    d["signer"] = d["signer"].astype(str)
    g = d.groupby("signer").agg(profit=("usd_profit_net", "sum"), n=("slot", "size"))
    g = g.sort_values("profit", ascending=False)
    n_att = len(g)
    x = np.arange(1, n_att + 1) / n_att * 100
    cp = g["profit"].cumsum() / g["profit"].sum() * 100
    cn = g["n"].cumsum() / g["n"].sum() * 100

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 12, "legend.fontsize": 10,
                         "xtick.labelsize": 10, "ytick.labelsize": 10,
                         "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    ax.plot(x, cp, color=C_SL_P, linewidth=2.0, label="Profit", zorder=3)
    ax.plot(x, cn, color=C_SL, linewidth=2.0, linestyle="--", label="# Attacks", zorder=3)

    # Callout on the top 20 attackers by name-count rather than on a round percentile: the paper
    # talks about a handful of operators, and "20 of 322" is the quantity a reader can hold.
    K = 20
    i = min(K, n_att) - 1
    qk = (i + 1) / n_att * 100
    ax.plot([qk, qk], [0, cp.iloc[i]], color="#c8c8c8", linewidth=0.7, zorder=1)
    ax.plot(qk, cp.iloc[i], "o", ms=5.5, color=C_SL_P, mec="white", mew=1.2, zorder=4)
    ax.plot(qk, cn.iloc[i], "s", ms=5.0, color=C_SL, mec="white", mew=1.2, zorder=4)
    ax.annotate(f"top {K} attackers:\n"
                f"${g['profit'].head(K).sum() / 1e6:.2f}M ({cp.iloc[i]:.0f}%) of profit\n"
                f"{int(g['n'].head(K).sum()):,} ({cn.iloc[i]:.0f}%) attacks",
                xy=(qk, cp.iloc[i]), xytext=(28, 34), fontsize=10, color="#333333",
                ha="left", va="center",
                arrowprops=dict(arrowstyle="-", color="#888888", linewidth=0.9,
                                shrinkA=0, shrinkB=4))

    ax.set_xlim(0, 100)
    ax.set_ylim(0, 104)
    ax.set_xlabel("Top $q\\%$ of attackers, ranked by profit")
    ax.set_ylabel("Cumulative share")
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}%"))
    ax.set_xticks([0, 10, 25, 50, 75, 100])
    ax.set_yticks([0, 25, 50, 75, 100])
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#444444")
        ax.spines[sp].set_linewidth(0.8)
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc")
    ax.set_axisbelow(True)
    ax.legend(loc="lower right", frameon=False, handlelength=2.0, borderaxespad=0.8)
    fig.tight_layout()
    figcfg.save(fig, a, "6.1_profit_concentration")
    plt.close(fig)

    print(f"  attackers {n_att} · profit ${g['profit'].sum():,.0f} · attacks {int(g['n'].sum()):,}")
    for q in (1, 5, 10, 25, 50):
        i = max(int(np.ceil(n_att * q / 100)) - 1, 0)
        print(f"    top {q:>3}% ({i + 1:>3} attackers): profit {cp.iloc[i]:>5.1f}% · attacks {cn.iloc[i]:>5.1f}%")
    x_ = np.sort(g["profit"].to_numpy()); m = len(x_)
    print(f"    Gini(profit) = {((2 * np.arange(1, m + 1) - m - 1) * x_).sum() / (m * x_.sum()):.4f}")
    return g


def fig_rotation_heatmap(a, sw, legs, top_n=20):
    """Where a multi-leader sandwich's two legs sit inside their leader rotations.

    Rows are the frontrun's slot within its leader's rotation, columns the backrun's
    within the next leader's. Solana rotations are four slots aligned on multiples of
    four, so both coordinates are `slot mod 4`. Restricted to the top `top_n` attackers
    by profit.
    """
    from matplotlib.colors import LinearSegmentedColormap

    P = sw.groupby("signer")["usd_profit_net"].sum().sort_values(ascending=False)
    top = list(P.head(top_n).index)
    f = legs[legs["type"] == "frontRun"].groupby("sid")["slot"].min().rename("fs")
    b = legs[legs["type"] == "backRun"].groupby("sid")["slot"].max().rename("bs")
    d = pd.concat([f, b], axis=1).join(sw[["signer"]], how="inner")
    d = d[d["signer"].isin(top)]
    m = np.zeros((4, 4))
    for fm, bm in zip((d["fs"] % 4).astype(int), (d["bs"] % 4).astype(int)):
        m[fm, bm] += 1
    pct = m / m.sum() * 100

    cmap = LinearSegmentedColormap.from_list("paper_blues",
                                             ["#FFFFFF", C_IB, C_SL, C_XL])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelsize": 11.5, "xtick.labelsize": 11,
                         "ytick.labelsize": 11, "pdf.fonttype": 42, "ps.fonttype": 42})
    fig, ax = plt.subplots(figsize=(4.3, 3.5))
    im = ax.imshow(pct, cmap=cmap, vmin=0, vmax=pct.max(), origin="upper")
    for i in range(4):
        for j in range(4):
            ax.text(j, i, f"{pct[i, j]:.1f}%", ha="center", va="center", fontsize=10.5,
                    color="white" if pct[i, j] > pct.max() * 0.55 else "#222222",
                    fontweight="bold" if pct[i, j] == pct.max() else "normal")
    ax.set_xticks(range(4), [str(i + 1) for i in range(4)])
    ax.set_yticks(range(4), [str(i + 1) for i in range(4)])
    ax.set_xlabel("Landing slot of $T_B$ in next leader rotation")
    ax.set_ylabel("Landing slot of $T_F$ in\ncurrent leader rotation")
    ax.set_xticks(np.arange(-0.5, 4, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, 4, 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.6)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.03)
    cb.set_label("Share of ML-CB sandwiches", fontsize=10.5)
    cb.ax.tick_params(labelsize=9.5)
    cb.ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}%"))
    cb.outline.set_visible(False)
    fig.tight_layout()
    figcfg.save(fig, a, "6.3_rotation_heatmap")
    plt.close(fig)

    print(f"  top-{top_n} ML-CB sandwiches: {int(m.sum()):,}")
    print(f"  {'':>9}" + "".join(f"{'back ' + str(c + 1):>9}" for c in range(4)) + f"{'row':>9}")
    for r in range(4):
        print(f"  front {r + 1:<3}" + "".join(f"{pct[r, c]:>8.1f}%" for c in range(4))
              + f"{pct[r].sum():>8.1f}%")
    print(f"  {'col':>9}" + "".join(f"{pct[:, c].sum():>8.1f}%" for c in range(4)))
    return pct


def main():
    p = figcfg.add_args(argparse.ArgumentParser(description="Section 6 measurement figures"))
    p.add_argument("--refresh-legs", action="store_true",
                   help="Re-query the per-leg frame instead of reading its cache")
    p.add_argument("--tips", default="fee-floor", choices=["fee-floor", "known-only"],
                   help="How the cost figure treats a leg that landed in a bundle Jito has "
                        "since reclaimed: count it at its fee alone, a lower bound on its "
                        "cost (default, and what the published figure shows), or drop it")
    a = p.parse_args()
    figcfg.banner(a, "Section 6 figures")
    att = figcfg.load_attackers(a)
    sw = figcfg.load_sandwiches(a, att)
    if not len(sw):
        raise SystemExit("no sandwiches for this window")
    sw["cls"] = classify(sw)
    print(f"  geometry: " + " · ".join(
        f"{c} {int((sw['cls'] == c).sum()):,}" for c in CLASSES))
    print(f"  priced: {sw['usd_profit'].notna().mean():.2%} of sandwiches")

    print("\nQuerying daily user transactions...")
    txs = daily_user_txs(a)
    print(f"  {len(txs)} days")

    print("\nLoading legs...")
    legs = load_legs(a, sw, refresh=a.refresh_legs)
    sw["distance"] = sandwich_distance(a, legs)
    print(f"  distance: {sw['distance'].notna().sum():,} sandwiches measured")

    print("\nDrawing:")
    fig_volume_profit(a, sw, txs)
    fig_profit_concentration(a, sw)
    fig_profit_range(a, sw)
    fig_distance_profit(a, sw)
    fig_cost(a, legs)
    fig_program(a, sw, leg_programs(legs))
    fig_rotation_heatmap(a, sw, legs)
    print(f"\nAll seven written to {figcfg.outdir(a)}")


if __name__ == "__main__":
    main()
