"""Shared configuration and drawing helpers for every script in this directory.

Holds the command-line flags (`add_args`), the dataset loaders (`load_attackers`,
`load_sandwiches`), the palette, and the two drawing routines the Appendix C figures
share (`gated_distribution`, `signal_plane`).

Values that also exist in the pipeline are imported from it rather than restated:
`gates()` reads phase 3's argparse defaults, `sol_usd()` reads the price snapshot, and
`phase_module(n)` loads a numbered pipeline script by path so its functions can be called
directly.

Flag defaults are the paper's run: epochs 946-990, cross-leader include, database
`solwich_v2`, and the entities carrying `expert_verdict == "no"` removed.
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
INTENT = REPO / "sandwich-intent"
CATEGORIES = ["standard", "multi_split", "diff_signer_owner", "diff_signer_transfer"]

# Value of phase 3's `expert_verdict` column that `load_attackers` drops.
EXPERT_REJECT_LABEL = "no"


_PHASE_FILES = {
    1: "1_signer_data_preparation_and_summary.py",
    3: "3_attacker_filter.py",
    4: "4_validator_association.py",
}


def phase_module(n):
    """Import a numbered pipeline script by path and return (module, its parsed defaults).

    Their names start with a digit, so none is importable by name. The script's own cwd
    and sys.path are set while it loads, since it resolves `utils.*` and `data/` relatively.
    """
    p = INTENT / _PHASE_FILES[n]
    if not p.exists():
        raise SystemExit(f"missing {p}")
    spec = importlib.util.spec_from_file_location(f"phase{n}", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"phase{n}"] = m
    argv, cwd, path0 = sys.argv, os.getcwd(), list(sys.path)
    sys.argv = [_PHASE_FILES[n]]
    # They import `utils.*` by relative package name and resolve data paths relative to
    # their own directory, so both the cwd and sys.path have to point there while loading.
    sys.path.insert(0, str(INTENT))
    os.chdir(INTENT)
    try:
        spec.loader.exec_module(m)
        return m, m.parse_args()
    finally:
        sys.argv, sys.path = argv, path0
        os.chdir(cwd)


def sol_usd():
    """SOL's price, from the snapshot `utils.intent.load_token_prices` reads."""
    argv, cwd, path0 = sys.argv, os.getcwd(), list(sys.path)
    sys.path.insert(0, str(INTENT))
    os.chdir(INTENT)
    try:
        from utils.intent import load_token_prices
        px = load_token_prices()
    finally:
        sys.argv, sys.path = argv, path0
        os.chdir(cwd)
    if "SOL" not in px:
        raise SystemExit("SOL is missing from the price table -- run 0_crawl_token_price.py")
    return float(px["SOL"])


def gates():
    """Threshold values as phase 3 applies them TODAY. Read, never restated."""
    _, a = phase_module(3)
    return {"n_min": a.n_min, "wr_min": a.wr_min, "slip_min": a.slip_min,
            "fg_median_max": a.fg_median_max, "usd_min": a.signal_usd_min}


def add_args(p):
    p.add_argument("--database", default="solwich_v2")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--cross-leader", default="include", choices=["include", "exclude"])
    p.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "figures"),
                   help="Where figures and table CSVs are written.")
    p.add_argument("--keep-audited-rejects", action="store_true",
                   help="Keep the attackers the expert panel rejected. Off by default; the paper "
                        "reports the dataset net of them.")
    return p


def parse(desc):
    return add_args(argparse.ArgumentParser(description=desc)).parse_args()


def tag(a):
    t = f"{a.start_epoch}_{a.end_epoch}"
    return t if a.cross_leader == "exclude" else f"{t}_cl-{a.cross_leader}"


def phase1(a, cat, kind, root=None):
    """Path to a phase-1 output. kind: signer_features | per_sandwich_metrics

    `root` overrides the phase-1 output directory, e.g. `data/1_nofloor` for a run made
    with `--profit-floor-usd 0`.
    """
    base = INTENT / (root or "data/1_signer_data_preparation_and_summary")
    return base / cat / a.database / f"{kind}_{tag(a)}.parquet"


def phase3(a, cat, kind):
    """kind: bot_attackers | bot_sandwiches | bot_sandwiches_geom"""
    return (INTENT / "data" / "3_attacker_filter" / cat / a.database / a.cross_leader
            / f"{kind}_{tag(a)}.parquet")


def validator_dir(a):
    v = "include_XL" if a.cross_leader == "include" else "exclude_XL"
    return INTENT / "data" / "4_validator_association" / a.database / v / "data"


def load_attackers(a, verbose=True):
    """The paper's attacker set: phase 3's selection, pooled, net of the expert-rejected."""
    frames = []
    for cat in CATEGORIES:
        p = phase3(a, cat, "bot_attackers")
        if not p.exists():
            continue
        d = pd.read_parquet(p)
        if not len(d):
            continue
        d = d.reset_index().rename(columns={d.index.name or "index": "attacker"})
        d["category"] = cat
        frames.append(d)
    if not frames:
        raise SystemExit(f"no phase-3 attacker output for {a.database}/{a.cross_leader}/{tag(a)} — "
                         f"run 3_attacker_filter.py for that window first")
    A = pd.concat(frames, ignore_index=True)
    A["attacker"] = A["attacker"].astype(str)
    n0 = A["attacker"].nunique()
    dropped = []
    if not a.keep_audited_rejects:
        if "expert_verdict" not in A.columns:
            raise SystemExit(
                "phase-3 output has no `expert_verdict` column -- re-run 3_attacker_filter.py. "
                "Proceeding would draw the unreviewed population and call it the paper's dataset.")
        hit = A["expert_verdict"].astype(str) == EXPERT_REJECT_LABEL
        dropped = sorted(A.loc[hit, "attacker"].unique())
        A = A[~hit]
    if verbose:
        print(f"  attackers: {n0:,} selected, {len(dropped)} removed by the expert audit, "
              f"{A['attacker'].nunique():,} used")
        for x in dropped:
            print(f"    - {x}")
    return A


def load_sandwiches(a, attackers=None, verbose=True):
    """Per-sandwich rows for the attacker set, pooled over categories."""
    if attackers is None:
        attackers = load_attackers(a, verbose=verbose)
    keep = set(attackers["attacker"])
    frames = []
    for cat in CATEGORIES:
        p = phase3(a, cat, "bot_sandwiches")
        if not p.exists():
            continue
        d = pd.read_parquet(p)
        if not len(d):
            continue
        d = d[d["signer"].astype(str).isin(keep)].copy()
        d["category"] = cat
        frames.append(d)
    S = pd.concat(frames, ignore_index=False) if frames else pd.DataFrame()
    if verbose:
        print(f"  sandwiches: {len(S):,} over {S['signer'].nunique() if len(S) else 0:,} attackers")
    return S


def outdir(a):
    d = Path(a.out_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(fig, a, stem, also_png=True):
    """Write to the staging directory only. Copying into the paper is a separate, human step."""
    d = outdir(a)
    pdf = d / f"{stem}.pdf"
    fig.savefig(pdf, bbox_inches="tight")
    if also_png:
        fig.savefig(d / f"{stem}.png", dpi=200, bbox_inches="tight")
    print(f"  -> {pdf}")
    return pdf


def banner(a, what):
    print(f"=== {what} ===")
    print(f"  {a.database} · epochs {a.start_epoch}-{a.end_epoch} · cross-leader {a.cross_leader} "
          f"· tag {tag(a)}")


# Shared drawing for the Appendix C figures C_1 / C_2 / C_3: bars are the entity count in
# each bucket of the gated quantity, coloured by whether the bucket clears the gate; a line
# on the right-hand axis is the mean net USD profit per entity in that bucket.
#
# `gated_distribution` drops rather than buckets two kinds of value, and returns their
# counts: NaN, and anything outside [bins[0], bins[-1]]. The final bucket is closed on the
# right, so a value on the last edge belongs to it.

# Palette, used by every script here.
#   BLUE  ramp  counts, volumes, and the "rejected / in-block" side of a split
#   CORAL ramp  money, and the "selected / cross-block" side of a split
#   ACCENT      the third channel, a per-entity profit line on a right axis
BLUE_L, BLUE_M, BLUE_D = "#92C5DE", "#2166AC", "#0B2E4F"
CORAL_L, CORAL_M, CORAL_D = "#F4A582", "#D6604D", "#7F2A1D"
ACCENT = "#3F7D5A"       # deep green
GREY = "#737373"

BAR_BELOW = BLUE_M       # entities the gate rejects
BAR_ABOVE = CORAL_M      # entities the gate keeps
LINE_COLOR = ACCENT      # mean profit per entity, right axis
MIN_LINE_N = 10          # buckets thinner than this are left off the profit line


def gated_distribution(df, value_col, bins, threshold, keep, xlabel, gate_label, symbol=None,
                       gate_label_side="left",
                       profit_col="usd_net_total", tick_at=None, tick_fmt=None,
                       uniform_bars=False, figsize=(6.4, 3.2), headroom=1.18,
                       gate_label_frac=0.96, legend_loc="upper left", legend_ncol=1, log_profit=False,
                       log_count=False):
    """Bars = entities per bucket; right-axis line = mean profit per entity.

    keep         'ge' if the gate keeps values >= threshold, 'le' if <= threshold.
    uniform_bars draw against the bucket index with equal widths, for geometric bins.
                 Ticks then come from `tick_at` (bin-edge values).
    log_count    log scale on the entity-count axis.
    log_profit   log scale on the profit axis.
    headroom, gate_label_frac
                 layout: the legend sits above the axes and the threshold box at the top
                 of the plot area; `headroom` buys room for the box above the tallest bar.

    Returns (fig, stats).
    """
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    bins = np.asarray(bins, dtype=float)
    nb = len(bins) - 1

    v = df[value_col]
    n_nan = int(v.isna().sum())
    d = df[v.notna()]
    # The final bucket is closed on the right, so a value on the last edge belongs to it.
    inside = (d[value_col] >= bins[0]) & (d[value_col] <= bins[-1])
    n_out = int((~inside).sum())
    d = d[inside]

    idx = np.clip(np.digitize(d[value_col], bins) - 1, 0, nb - 1)
    grp = d.assign(_b=idx).groupby("_b")[profit_col]
    counts = grp.size().reindex(range(nb)).fillna(0).astype(int)
    means = grp.mean().reindex(range(nb))
    medians = grp.median().reindex(range(nb))
    line = means.where(counts >= MIN_LINE_N)

    passes = (bins[:-1] >= threshold) if keep == "ge" else (bins[1:] <= threshold)
    colors = np.where(passes, BAR_ABOVE, BAR_BELOW)

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 11,
        "axes.labelsize": 12, "axes.titlesize": 12, "legend.fontsize": 10,
        "xtick.labelsize": 10, "ytick.labelsize": 10,
        "pdf.fonttype": 42, "ps.fonttype": 42,
    })
    fig, ax = plt.subplots(figsize=figsize)

    if uniform_bars:
        x = np.arange(nb, dtype=float)
        width = 0.92
        thr_x = float(np.searchsorted(bins, threshold)) - 0.5
        ax.set_xlim(-0.7, nb - 0.3)
        if tick_at is not None:
            pos = [float(np.searchsorted(bins, t)) - 0.5 for t in tick_at]
            ax.set_xticks(pos)
            ax.set_xticklabels([tick_fmt(t) for t in tick_at])
    else:
        x = 0.5 * (bins[:-1] + bins[1:])
        width = (bins[1] - bins[0]) * 0.92
        thr_x = threshold
        pad = (bins[-1] - bins[0]) * 0.02
        ax.set_xlim(bins[0] - pad, bins[-1] + pad)

    if log_count:
        ax.set_yscale("log")
        ax.bar(x, counts.to_numpy(), width=width, color=colors, edgecolor="white",
               linewidth=0.6, bottom=0.6)
        y_top = counts.max() ** headroom
        ax.set_ylim(0.6, y_top)
    else:
        ax.bar(x, counts.to_numpy(), width=width, color=colors,
               edgecolor="white", linewidth=0.6)
        y_top = counts.max() * headroom

    ax2 = ax.twinx()
    ax2.plot(x, line.to_numpy(dtype=float), color=LINE_COLOR, linewidth=1.6,
             marker="o", markersize=4.2, markerfacecolor="white",
             markeredgewidth=1.3, markeredgecolor=LINE_COLOR, zorder=4,
             clip_on=False)
    ax2.set_ylabel("Mean profit per entity (USD)", color=LINE_COLOR)
    ax2.tick_params(axis="y", colors=LINE_COLOR, length=3.5)
    # The profit line can go negative, so the axis is fitted to the data and zero is marked.
    _lv = line.to_numpy(dtype=float)
    _hi, _lo = float(np.nanmax(_lv)), float(np.nanmin(_lv))
    if log_profit:
        if _lo <= 0:
            raise ValueError("log_profit needs every plotted bucket mean to be positive; "
                             f"the smallest here is {_lo:.2f}")
        ax2.set_yscale("log")
        ax2.set_ylim(_lo / 2.2, _hi * 2.2)
    elif _lo < 0:
        ax2.set_ylim(_lo - 0.18 * (_hi - _lo), _hi + 0.18 * (_hi - _lo))
        ax2.axhline(0, color=LINE_COLOR, linewidth=0.7, linestyle=":", zorder=1)
    else:
        ax2.set_ylim(0, _hi * 1.18)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda t, _: f"{t:,.0f}"))
    if log_profit:
        ax2.yaxis.set_minor_formatter(mticker.NullFormatter())
    for sp in ("top", "left"):
        ax2.spines[sp].set_visible(False)
    ax2.spines["right"].set_color(LINE_COLOR)
    ax2.spines["right"].set_linewidth(0.8)

    ax.axvline(thr_x, color="#222222", linestyle=(0, (4, 2)), linewidth=1.4, zorder=3)
    _gy = y_top ** gate_label_frac if log_count else y_top * gate_label_frac
    _dx = (ax.get_xlim()[1] - ax.get_xlim()[0]) * 0.012
    _right = gate_label_side == "right"
    ax.text(thr_x + (_dx if _right else -_dx), _gy, gate_label,
            ha="left" if _right else "right", va="top", fontsize=13,
            bbox=dict(boxstyle="round,pad=0.28", fc="white", ec="#222222", lw=0.7))

    if not log_count:
        ax.set_ylim(0, y_top)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Number of entities")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda t, _: f"{t:,.0f}"))
    if log_count:
        ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#444444")
        ax.spines[sp].set_linewidth(0.8)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc", zorder=0)
    ax.set_axisbelow(True)

    op = r"$\geq$" if keep == "ge" else r"$\leq$"
    inv = r"$<$" if keep == "ge" else r"$>$"
    # Match the gate box's formatting: "%g" renders 0.90 as "0.9", so the legend and the
    # box on the same figure would disagree about the threshold.
    _t = f"{threshold:g}" if float(threshold).is_integer() else f"{threshold:.2f}"
    # No "entities," prefix: the left axis already says what the bars count, and three
    # prefixed labels do not fit on one row above a single-column figure.
    _pass = f"{symbol} {op} {_t}" if symbol else f"entities {op} gate"
    _fail = f"{symbol} {inv} {_t}" if symbol else f"entities {inv} gate"
    ax.legend(handles=[
        Patch(facecolor=BAR_ABOVE, edgecolor="white", label=_pass),
        Patch(facecolor=BAR_BELOW, edgecolor="white", label=_fail),
        Line2D([0], [0], color=LINE_COLOR, linewidth=1.6, marker="o", markersize=4.2,
               markerfacecolor="white", markeredgecolor=LINE_COLOR, markeredgewidth=1.3,
               label="mean profit per entity")],
        loc="lower center", bbox_to_anchor=(0.5, 1.01), frameon=False,
        handlelength=1.5, handletextpad=0.5, ncol=3, borderaxespad=0.0,
        columnspacing=1.4, fontsize=9.5)

    fig.tight_layout()
    return fig, {"bins": bins, "counts": counts, "means": means,
                 "medians": medians, "n_nan": n_nan, "n_out": n_out, "n": len(d)}


def report_buckets(stats, fmt, gate_note=""):
    """Print the per-bucket table that backs one of the C.1 figures."""
    b, c, mu, me = stats["bins"], stats["counts"], stats["means"], stats["medians"]
    print(f"  {'bucket':>20}{'entities':>10}{'mean USD':>11}{'median USD':>12}")
    for k in range(len(b) - 1):
        if c.iloc[k] == 0:
            continue
        off = "" if c.iloc[k] >= MIN_LINE_N else "   (off line)"
        print(f"  [{fmt(b[k])},{fmt(b[k + 1])}){c.iloc[k]:>10,}"
              f"{mu.iloc[k]:>11,.0f}{me.iloc[k]:>12,.0f}{off}")
    print(f"  plotted {stats['n']:,} entities; dropped {stats['n_nan']} unmeasurable, "
          f"{stats['n_out']} outside the axis range{gate_note}")


def pooled_features(a, root=None):
    """Phase-1 per-entity features, pooled over the four categories."""
    frames = []
    for cat in CATEGORIES:
        f = phase1(a, cat, "signer_features", root=root)
        if not f.exists():
            continue
        d = pd.read_parquet(f).reset_index()
        d["category"] = cat
        frames.append(d)
    if not frames:
        raise SystemExit(f"no phase-1 features for {a.database}/{tag(a)}")
    return pd.concat(frames, ignore_index=True)


def stage1_pool(a, feats=None):
    """The entities the economic gate keeps: CNT >= n_min, WR >= wr_min, USD >= usd_min."""
    g = gates()
    f = pooled_features(a) if feats is None else feats
    return f[(f["sandwich_count"] >= g["n_min"])
             & (f["sol_win_rate"].fillna(-1) >= g["wr_min"])
             & (f["usd_net_total"] >= g["usd_min"])]


# The C_4 / C_5 signal plane. Horizontal axis is mean SC, vertical is the median front gap:
# a count from 1 to about 6,400 in which SMALLER is better, so the y axis is logarithmic and
# the accepted quadrant is the LOWER right. Marker area is log-scaled in the sandwich count.
def signal_plane(pool, colour_col, colour_label, vmin, vmax, gate_sc, gate_fg,
                 lo_x=0.60, gap=0.05, figsize=(13.0, 10.0), seed=42):
    """Scatter of mean_SC (x) against front_gap_p50 (y, log), classified by both gates."""
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    from matplotlib.patches import Rectangle

    d = pool.dropna(subset=["mean_SC", "front_gap_p50"]).copy()
    d = d[d["front_gap_p50"] > 0]
    d["pass_gate"] = (d["mean_SC"] >= gate_sc) & (d["front_gap_p50"] <= gate_fg)
    # The x axis is cropped at `lo_x` and broken. Entities left of the crop are dropped,
    # not clamped, and their count is returned.
    n_cropped = int((d["mean_SC"] < lo_x).sum())
    d = d[d["mean_SC"] >= lo_x]
    passed, failed = d[d["pass_gate"]], d[~d["pass_gate"]]

    # y limits are fitted to the data rather than padded by a constant factor.
    y_lo, y_hi = 0.8, 8000.0
    rng = np.random.default_rng(seed=seed)
    JX, JY = 0.008, 0.045          # x in data units, y in log10 units
    EPS_X, EPS_Y = 0.004, 0.012

    def jitter(sub, is_pass, sizes):
        """Random jitter plus pairwise repulsion, clamped so no point crosses a gate."""
        xv = sub["mean_SC"].to_numpy(float)
        yv = np.log10(sub["front_gap_p50"].to_numpy(float))
        n = len(xv)
        if n == 0:
            return xv, yv
        gy = np.log10(gate_fg)
        jx = xv + rng.uniform(-JX, JX, n)
        jy = yv + rng.uniform(-JY, JY, n)

        def clamp(jx, jy):
            if is_pass:
                jx = np.maximum(jx, gate_sc + EPS_X)
                jy = np.minimum(jy, gy - EPS_Y)          # accepted side is BELOW
            else:
                jx = np.where(xv < gate_sc, np.minimum(jx, gate_sc - EPS_X), jx)
                jy = np.where(yv > gy, np.maximum(jy, gy + EPS_Y), jy)
            return jx, np.clip(jy, np.log10(y_lo) + 0.02, np.log10(y_hi) - 0.02)

        jx, jy = clamp(jx, jy)
        radii_x = 0.0009 * np.sqrt(sizes) + 0.004
        for _ in range(6):
            dx = (jx[:, None] - jx[None, :])
            dy = (jy[:, None] - jy[None, :]) / 2.6
            dist = np.sqrt(dx * dx + dy * dy) + 1e-9
            mind = radii_x[:, None] + radii_x[None, :]
            np.fill_diagonal(mind, 0.0)
            ov = np.maximum(mind - dist, 0.0)
            jx = jx + 0.6 * (0.5 * ov * dx / dist).sum(axis=1)
            jy = jy + 0.6 * (0.5 * ov * dy / dist).sum(axis=1) * 2.6
            jx, jy = clamp(jx, jy)
        return jx, jy

    def sizes_of(sub, lo, hi):
        n = sub["sandwich_count"].to_numpy(float)
        t = (np.log10(n) - 2.0) / (np.log10(30_000) - 2.0)
        return lo + (hi - lo) * np.clip(t, 0.0, 1.0)

    fig, ax = plt.subplots(figsize=figsize)
    ax.set_facecolor("#f0f0f0")
    ax.set_yscale("log")
    ax.set_xlim(lo_x - gap, 1.02)
    ax.set_ylim(y_lo, y_hi)
    ax.add_patch(Rectangle((gate_sc, y_lo), 1.02 - gate_sc, gate_fg - y_lo,
                           facecolor="white", edgecolor="none", zorder=0))

    f = failed.sort_values("sandwich_count", ascending=False)
    if len(f):
        fs = sizes_of(f, 12, 55)
        fx, fy = jitter(f, False, fs)
        ax.scatter(fx, 10 ** fy, s=fs, c=BLUE_L, alpha=0.55,
                   edgecolors=BLUE_M, linewidths=0.3, zorder=2,
                   label="filtered-out entity")

    p = passed.sort_values("sandwich_count", ascending=False)
    ps = sizes_of(p, 30, 190)
    px, py = jitter(p, True, ps)
    sc = ax.scatter(px, 10 ** py, s=ps, c=p[colour_col], cmap="YlOrRd", alpha=0.82,
                    vmin=vmin, vmax=vmax, edgecolors="black", linewidths=0.5,
                    zorder=3, label="intentional attacker")

    ax.axvline(gate_sc, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.9, zorder=4)
    ax.axhline(gate_fg, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.9, zorder=4)

    box = dict(boxstyle="round,pad=0.28", fc="white", ec="#222222", lw=0.7)
    # Left of the gate and lifted off the axis: centred on the line it covered the 0.9 tick
    # label, and the strip below FG=10 outside the gate is empty in this pool.
    ax.text(gate_sc - 0.006, y_lo * 2.6, rf"$\overline{{\mathsf{{SC}}}}_{{\min}}={gate_sc:.2f}$",
            ha="right", va="center", fontsize=18, color="#222222", bbox=box, zorder=5)
    ax.text(lo_x - gap + 0.012, gate_fg, rf"$\widetilde{{\mathsf{{FG}}}}_{{\max}}={gate_fg:g}$",
            ha="left", va="center", fontsize=18, color="#222222", bbox=box, zorder=5)

    xt = np.round(np.arange(lo_x, 1.001, 0.1), 2)
    ax.set_xticks([lo_x - gap] + list(xt))
    ax.set_xticklabels(["0"] + [f"{v:.1f}" for v in xt])
    yt = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
    ax.set_yticks(yt)
    ax.set_yticks([], minor=True)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}"))

    kw = dict(transform=ax.transAxes, color="black", linewidth=1.2, clip_on=False)
    dd, xb = 0.012, (gap / 2) / (1.02 - (lo_x - gap))
    for off in (0.0, 0.005):
        ax.add_artist(plt.Line2D([xb - dd + off, xb + dd + off], [-dd, dd], **kw))

    ax.set_xlabel(r"Mean Slippage Consumption $\overline{\mathsf{SC}}_e$ for each entity",
                  fontsize=18)
    ax.set_ylabel(r"Median Frontrun Gap $\widetilde{\mathsf{FG}}_e$ for each entity",
                  fontsize=18)
    ax.tick_params(axis="both", which="major", labelsize=16)
    ax.legend(fontsize=17, loc="lower left", framealpha=0.92, edgecolor="gray")
    ax.grid(True, alpha=0.15, color="gray")

    cb = plt.colorbar(sc, ax=ax, shrink=0.75, pad=0.02)
    cb.set_label(colour_label, fontsize=17)
    cb.ax.tick_params(labelsize=15)
    return fig, {"n": len(d), "passed": len(passed), "failed": len(failed),
                 "cropped": n_cropped}
