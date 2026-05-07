"""
Generate Sec 5.2 main scatter + Appendix E pair figures (signal distributions
and avg-USD-by-signal), all in a unified visual style with the WR figure.

Inputs : sandwich-intent/data/1_signer_data_preparation_and_summary/<cat>/{signer_features,per_sandwich_metrics}_946_960.parquet
Outputs:
  sandwich-intent/data/sec5_2/profit_cnt_classified_946_960.{pdf,png}
  sandwich-intent/data/appE/signal_distribution_946_960.{pdf,png}
  sandwich-intent/data/appE/avg_usd_by_signal_946_960.{pdf,png}
  overleaf-paper/CCS/figures/5.2_profit_cnt_classified.pdf
  overleaf-paper/CCS/figures/E_signal_distribution.pdf
  overleaf-paper/CCS/figures/E_avg_usd_by_signal.pdf
"""

from __future__ import annotations

import shutil
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "sandwich-intent" / "data" / "1_signer_data_preparation_and_summary"
OUT_52 = REPO / "sandwich-intent" / "data" / "sec5_2"
OUT_E = REPO / "sandwich-intent" / "data" / "appE"
PAPER = REPO / "overleaf-paper" / "CCS" / "figures"

CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]
TAG = "946_960"
CNT_MIN = 10
WR_MIN = 0.8
USD_MIN = 10
SLIP_MIN = 0.75
FG100_MIN = 0.6

BASE = "#5d7fa6"      # muted slate-blue
ACCENT = "#d2604c"    # warm coral
LINE_C = "#222222"
GREY_C = "#bcbcbc"
THR_C = "#cc0000"     # vibrant red for threshold lines

# -- shared style ----------------------------------------------------------

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.labelsize": 12,
    "axes.titlesize": 12,
    "legend.fontsize": 9.5,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})


def style_axis(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for spine_name in ("left", "bottom"):
        ax.spines[spine_name].set_color("#444444")
        ax.spines[spine_name].set_linewidth(0.8)
    ax.tick_params(axis="both", which="major", length=3.5, color="#444444")
    ax.grid(axis="y", linestyle=":", linewidth=0.5, color="#cccccc", zorder=0)
    ax.set_axisbelow(True)


def save(fig, stem, out_dir, paper_name):
    out_dir.mkdir(parents=True, exist_ok=True)
    PAPER.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{stem}.pdf"
    png = out_dir / f"{stem}.png"
    fig.savefig(pdf, bbox_inches="tight")
    fig.savefig(png, bbox_inches="tight", dpi=400)
    shutil.copyfile(pdf, PAPER / paper_name)
    plt.close(fig)


# -- data loading ----------------------------------------------------------

def load_eco_pool() -> pd.DataFrame:
    sf = []
    sw = []
    for cat in CATEGORIES:
        f_sf = pd.read_parquet(DATA / cat / f"signer_features_{TAG}.parquet").reset_index()
        f_sf["category"] = cat
        sf.append(f_sf)
        f_sw = pd.read_parquet(DATA / cat / f"per_sandwich_metrics_{TAG}.parquet")
        f_sw["category"] = cat
        sw.append(f_sw)
    sf = pd.concat(sf, ignore_index=True)
    sw = pd.concat(sw, ignore_index=True)

    usd = sw.groupby(["category", "signer"])["usd_profit"].sum(min_count=1)\
            .reset_index().rename(columns={"usd_profit": "usd_total"})
    bundle = sw.groupby(["category", "signer"])["jito_bundle"].max()\
              .reset_index().rename(columns={"jito_bundle": "has_bundle"})
    sf = sf.merge(usd, on=["category", "signer"], how="left")
    sf = sf.merge(bundle, on=["category", "signer"], how="left")

    eco = sf[(sf["sandwich_count"] >= CNT_MIN) &
             (sf["win_rate"] >= WR_MIN) &
             (sf["usd_total"].fillna(-1) > USD_MIN)].copy()
    eco["slip_pass"] = eco["mean_slippage"].fillna(-1) >= SLIP_MIN
    eco["fg_pass"] = eco["fg_le_100_ratio"].fillna(-1) >= FG100_MIN
    eco["bundle_pass"] = eco["has_bundle"].fillna(False).astype(bool)
    eco["intentional"] = (eco["slip_pass"] & eco["fg_pass"]) | eco["bundle_pass"]
    return eco


# -- Figure 1: §5.2 scatter — (Slip, FPG) classification plane -----------
# Ported from sandwich-intent/3_attacker_filter.py::_draw_scatter, with paper-notation
# axis labels, no title, and YlOrRd color = log10(avg USD per sandwich).

def _draw_classification_scatter(eco: pd.DataFrame,
                                  color_col: str, color_label: str,
                                  vmin: float, vmax: float,
                                  out_stem: str, paper_name: str):
    """Generic (Slip, FPG) classifier plane. Color encoding controlled by
    `color_col`/`vmin`/`vmax`/`color_label`."""
    from matplotlib.patches import Rectangle

    df = eco.dropna(subset=["mean_slippage", "fg_le_100_ratio"]).copy()
    df["pass_gate"] = (df["mean_slippage"] >= SLIP_MIN) & (df["fg_le_100_ratio"] >= FG100_MIN)
    df["log_usd_total"] = np.log10(df["usd_total"].clip(lower=10))
    df["avg_usd"] = df["usd_total"] / df["sandwich_count"]
    df["log_avg_usd"] = np.log10(df["avg_usd"].clip(lower=1))
    passed = df[df["pass_gate"]]
    failed = df[~df["pass_gate"]]

    rng = np.random.default_rng(seed=42)

    # Per-axis jitter amplitudes (larger to spread points after tighter crops)
    JX, JY = 0.020, 0.022
    EPS = 0.003   # safety margin from threshold lines

    def _clamp(js, jf, is_pass, slip_v, fg_v):
        if is_pass:
            js = np.maximum(js, SLIP_MIN + EPS)
            jf = np.maximum(jf, FG100_MIN + EPS)
        else:
            slip_below = slip_v < SLIP_MIN
            fg_below = fg_v < FG100_MIN
            js = np.where(slip_below, np.minimum(js, SLIP_MIN - EPS), js)
            jf = np.where(fg_below, np.minimum(jf, FG100_MIN - EPS), jf)
        return js, jf

    def _safe_jitter(slip_s, fg_s, is_pass: bool, sizes=None):
        """Random jitter + iterative pairwise repulsion to reduce overlap.
        Always clamps back to the safe side of the threshold lines.
        `sizes` (matplotlib s in pt^2) controls per-point repulsion radius."""
        slip_v = slip_s.values
        fg_v = fg_s.values
        n = len(slip_v)
        if n == 0:
            return slip_v, fg_v
        # Initial random jitter
        js = slip_v + rng.uniform(-JX, JX, size=n)
        jf = fg_v + rng.uniform(-JY, JY, size=n)
        js, jf = _clamp(js, jf, is_pass, slip_v, fg_v)
        # Iterative repulsion: push pairs apart in data-coords proportional to
        # their marker radii (data-units). At fig 13x10 in, axis ~ 0.7 wide,
        # an 80-pt^2 marker has radius ~5pt ~ 0.013 in data-units. Use
        # conservative coefficient to avoid runaway.
        if sizes is None:
            radii = np.full(n, 0.012)
        else:
            radii = 0.0008 * np.sqrt(sizes) + 0.004
        for _ in range(6):
            dx = js[:, None] - js[None, :]
            dy = jf[:, None] - jf[None, :]
            dist = np.sqrt(dx * dx + dy * dy) + 1e-9
            min_d = radii[:, None] + radii[None, :]
            np.fill_diagonal(min_d, 0.0)
            overlap = np.maximum(min_d - dist, 0.0)
            # Direction (unit vector) and per-pair push
            ux = dx / dist
            uy = dy / dist
            # Each point gets sum of half-overlaps in each direction
            push_x = (0.5 * overlap * ux).sum(axis=1)
            push_y = (0.5 * overlap * uy).sum(axis=1)
            # Damp to avoid oscillation
            js = js + 0.6 * push_x
            jf = jf + 0.6 * push_y
            js, jf = _clamp(js, jf, is_pass, slip_v, fg_v)
        return js, jf

    fig, ax = plt.subplots(figsize=(13.0, 10.0))
    ax.set_facecolor("#f0f0f0")
    ax.add_patch(Rectangle((SLIP_MIN, FG100_MIN),
                           1.02 - SLIP_MIN, 1.02 - FG100_MIN,
                           facecolor="white", edgecolor="none", zorder=0))

    f_sorted = (failed.sort_values("sandwich_count", ascending=False)
                if len(failed) else failed)
    p_sorted = passed.sort_values("sandwich_count", ascending=False)

    if len(f_sorted):
        f_sizes = np.clip(f_sorted["sandwich_count"] / 3, 12, 55).values
        fx, fy = _safe_jitter(f_sorted["mean_slippage"],
                              f_sorted["fg_le_100_ratio"],
                              is_pass=False, sizes=f_sizes)
        ax.scatter(fx, fy,
                   s=np.clip(f_sorted["sandwich_count"] / 3, 12, 55),
                   c="#4a90d9", alpha=0.32, edgecolors="#2a5a9a",
                   linewidths=0.3, zorder=2,
                   label="filtered-out entity")

    p_sizes = np.clip(p_sorted["sandwich_count"] / 3, 30, 190).values
    px, py = _safe_jitter(p_sorted["mean_slippage"],
                          p_sorted["fg_le_100_ratio"],
                          is_pass=True, sizes=p_sizes)
    sc = ax.scatter(px, py,
                    s=np.clip(p_sorted["sandwich_count"] / 3, 30, 190),
                    c=p_sorted[color_col], cmap="YlOrRd", alpha=0.82,
                    vmin=vmin, vmax=vmax,
                    edgecolors="black", linewidths=0.5, zorder=3,
                    label="intentional attacker")

    ax.axvline(SLIP_MIN, color="#cc0000", linestyle="--", linewidth=1.8,
               alpha=0.9, zorder=4)
    ax.axhline(FG100_MIN, color="#cc0000", linestyle="--", linewidth=1.8,
               alpha=0.9, zorder=4)

    LO_X = 0.40
    LO_Y = 0.30
    gap = 0.05
    ax.set_xlim(LO_X - gap, 1.02)
    ax.set_ylim(LO_Y - gap, 1.02)

    label_bbox = dict(boxstyle="round,pad=0.28", fc="white",
                      ec="#222222", lw=0.7)
    ax.text(SLIP_MIN, LO_Y + 0.012,
            r"$\mathrm{SC}_{\min}=" + f"{SLIP_MIN:g}$",
            ha="center", va="center", fontsize=18, color="#222222",
            bbox=label_bbox, zorder=5)
    ax.text(LO_X + 0.012, FG100_MIN,
            r"$\pi_{\min}=" + f"{FG100_MIN:g}$",
            ha="center", va="center", fontsize=18, color="#222222",
            bbox=label_bbox, zorder=5)

    x_ticks = [LO_X - gap] + list(np.round(np.arange(LO_X, 1.01, 0.1), 2))
    x_lbls = ["0"] + [f"{v:.1f}" for v in np.round(np.arange(LO_X, 1.01, 0.1), 2)]
    y_ticks = [LO_Y - gap] + list(np.round(np.arange(LO_Y, 1.01, 0.1), 2))
    y_lbls = ["0"] + [f"{v:.1f}" for v in np.round(np.arange(LO_Y, 1.01, 0.1), 2)]
    ax.set_xticks(x_ticks); ax.set_xticklabels(x_lbls)
    ax.set_yticks(y_ticks); ax.set_yticklabels(y_lbls)

    kw = dict(transform=ax.transAxes, color="black",
              linewidth=1.2, clip_on=False)
    d = 0.012
    x_break = (gap / 2) / (1.02 - (LO_X - gap))
    ax.add_artist(plt.Line2D([x_break - d, x_break + d], [-d, d], **kw))
    ax.add_artist(plt.Line2D([x_break - d + 0.005, x_break + d + 0.005],
                             [-d, d], **kw))
    y_break = (gap / 2) / (1.02 - (LO_Y - gap))
    ax.add_artist(plt.Line2D([-d, d], [y_break - d, y_break + d], **kw))
    ax.add_artist(plt.Line2D([-d, d],
                             [y_break - d + 0.005, y_break + d + 0.005], **kw))

    ax.set_xlabel(r"Mean Slippage Consumption $\overline{\mathsf{SC}}_e$ for each entity",
                  fontsize=18)
    ax.set_ylabel(r"$\Pr[\mathsf{FPG}\leq 100]$ for each entity",
                  fontsize=18)
    ax.tick_params(axis="both", which="major", labelsize=16)

    ax.legend(fontsize=17, loc="lower left", framealpha=0.92,
              edgecolor="gray")
    ax.grid(True, alpha=0.15, color="gray")

    cbar = plt.colorbar(sc, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label(color_label, fontsize=17)
    cbar.ax.tick_params(labelsize=15)

    out_dir = OUT_E if "appE" in paper_name.lower() or paper_name.startswith("E_") else OUT_52
    save(fig, out_stem, out_dir, paper_name)
    print(f"[scatter:{paper_name}] N={len(df):,}  intentional={len(passed):,}  filtered={len(failed):,}")


def plot_slip_fpg_classified(eco: pd.DataFrame):
    # Figure 4 (§5.2): color = log10(USD total profit)
    _draw_classification_scatter(
        eco,
        color_col="log_usd_total",
        color_label=r"$\log_{10}$(USD profit)",
        vmin=1, vmax=5,
        out_stem=f"slip_fpg_classified_{TAG}",
        paper_name="5.2_slip_fpg_classified.pdf",
    )
    # App-E counterpart: color = log10(avg USD per sandwich)
    _draw_classification_scatter(
        eco,
        color_col="log_avg_usd",
        color_label=r"$\log_{10}$(avg USD per sandwich)",
        vmin=0, vmax=2,
        out_stem=f"slip_fpg_classified_avgprofit_{TAG}",
        paper_name="E_slip_fpg_avgprofit.pdf",
    )


# -- Figure 2: App E signal distributions (1x2) ----------------------------

_THR_DASH = "#222222"   # black for threshold line
_GREEN = "#1a8a4a"      # forest green for mean / median lines


def _plot_signal_dist(ax, vals, threshold, thr_label, xlabel, stat_mode):
    """stat_mode in {'mean', 'median'}: which statistic to draw (the other is omitted)."""
    bins = np.linspace(0.0, 1.0, 21)
    centers = 0.5 * (bins[:-1] + bins[1:])
    counts, _ = np.histogram(vals, bins=bins)
    colors = np.where(centers >= threshold, ACCENT, BASE)
    ax.bar(centers, counts, width=(bins[1] - bins[0]) * 0.92,
           color=colors, edgecolor="white", linewidth=0.5)

    if stat_mode == "mean":
        stat_v = float(np.mean(vals))
        stat_label = f"mean$\,=\,${stat_v:.2f}"
    else:  # median
        stat_v = float(np.median(vals))
        stat_label = f"median$\,=\,${stat_v:.2f}"

    y_top = counts.max() * 1.22

    # Threshold (black dashed) + stat (green dashed)
    ax.axvline(threshold, color=_THR_DASH, linestyle=(0, (5, 3)),
               linewidth=1.4, zorder=5)
    ax.axvline(stat_v, color=_GREEN, linestyle=(0, (5, 3)),
               linewidth=1.4, zorder=4)

    # Labels on the LEFT of each line, stacked at distinct y heights to
    # avoid overlap. Box style: white fill, dark border, black text
    # (matching Figure 3).
    box_style = dict(boxstyle="round,pad=0.20", fc="white",
                     ec=_THR_DASH, lw=0.6)
    items = sorted([
        ("thr", threshold, thr_label),
        ("stat", stat_v, stat_label),
    ], key=lambda t: t[1])
    y_slots = [0.94 * y_top, 0.78 * y_top]
    for slot, (_, x, lbl) in zip(y_slots, items):
        ax.text(x - 0.012, slot, lbl, ha="right", va="center",
                fontsize=8, color=_THR_DASH, bbox=box_style, zorder=6)

    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(0, y_top)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel("Number of entities", fontsize=9)
    ax.tick_params(axis="both", which="major", labelsize=7.5)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    style_axis(ax)


def plot_signal_distributions(eco: pd.DataFrame):
    # 1x2 side-by-side, sized to render at single-column width (~3.4 in)
    # without LaTeX scaling -> source font sizes are kept ~7-9pt to remain
    # readable at native size.
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), sharey=False)
    slip = eco["mean_slippage"].dropna().values
    fg = eco["fg_le_100_ratio"].dropna().values
    _plot_signal_dist(axes[0], slip, SLIP_MIN,
                      r"$\mathrm{SC}_{\min}\,=\," + f"{SLIP_MIN:g}$",
                      r"Mean Slippage Consumption $\overline{\mathsf{SC}}_e$",
                      stat_mode="mean")
    _plot_signal_dist(axes[1], fg, FG100_MIN,
                      r"$\pi_{\min}\,=\," + f"{FG100_MIN:g}$",
                      r"$\Pr[\mathsf{FPG}\leq 100]$",
                      stat_mode="median")
    fig.tight_layout()
    save(fig, f"signal_distribution_{TAG}", OUT_E, "E_signal_distribution.pdf")
    print(f"[fig2] slip mean={slip.mean():.3f} median={np.median(slip):.3f} | "
          f"fg mean={fg.mean():.3f} median={np.median(fg):.3f}")


# -- Figure 3: App E avg USD by signal (1x2) -------------------------------

def _plot_avg_usd(ax, vals, usds, threshold, xlabel, label_anchor):
    LO = 0.20  # crop x-axis at 0.2
    bins = np.linspace(0.0, 1.0, 21)
    centers = 0.5 * (bins[:-1] + bins[1:])
    df = pd.DataFrame({"v": vals, "u": usds})
    df = df.dropna(subset=["v"])
    df["bin"] = pd.cut(df["v"], bins=bins, labels=False, include_lowest=True)
    avg = df.groupby("bin")["u"].mean().reindex(range(len(centers)), fill_value=np.nan)

    visible = centers >= LO
    colors = np.where(centers >= threshold, ACCENT, BASE)
    ax.bar(centers[visible], avg.values[visible],
           width=(bins[1] - bins[0]) * 0.92,
           color=colors[visible], edgecolor="white", linewidth=0.5)

    y_top = np.nanmax(avg.values[visible]) * 1.20

    # Threshold: black dashed (Figure-3 style)
    ax.axvline(threshold, color=_THR_DASH, linestyle=(0, (5, 3)),
               linewidth=1.5, zorder=5)
    # Threshold label on the LEFT of the line, in a Figure-3-style box
    ax.text(threshold - 0.012, 0.93 * y_top,
            label_anchor + f"$\,=\,${threshold:g}",
            ha="right", va="center", fontsize=8, color=_THR_DASH,
            bbox=dict(boxstyle="round,pad=0.22", fc="white",
                      ec=_THR_DASH, lw=0.7), zorder=6)

    ax.set_xlim(LO, 1.02)
    ax.set_ylim(0, y_top * 1.05)
    ax.set_xticks(np.arange(0.2, 1.01, 0.1))
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel("Avg USD profit per entity", fontsize=9)
    ax.tick_params(axis="both", which="major", labelsize=7.5)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f"${int(x):,}" if x >= 1 else "$0"))
    style_axis(ax)


def plot_avg_usd_by_signal(eco: pd.DataFrame):
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8))
    sub = eco.dropna(subset=["mean_slippage"]).copy()
    _plot_avg_usd(axes[0], sub["mean_slippage"].values, sub["usd_total"].values,
                  SLIP_MIN,
                  r"Mean Slippage Consumption $\overline{\mathsf{SC}}_e$",
                  r"$\mathrm{SC}_{\min}$")
    sub2 = eco.dropna(subset=["fg_le_100_ratio"]).copy()
    _plot_avg_usd(axes[1], sub2["fg_le_100_ratio"].values, sub2["usd_total"].values,
                  FG100_MIN,
                  r"$\Pr[\mathsf{FPG}\leq 100]$",
                  r"$\pi_{\min}$")
    fig.tight_layout()
    save(fig, f"avg_usd_by_signal_{TAG}", OUT_E, "E_avg_usd_by_signal.pdf")


def main() -> None:
    eco = load_eco_pool()
    plot_slip_fpg_classified(eco)
    plot_signal_distributions(eco)
    plot_avg_usd_by_signal(eco)
    print(f"\nAll figures saved to:")
    print(f"  {OUT_52}/")
    print(f"  {OUT_E}/")
    print(f"  {PAPER}/")


if __name__ == "__main__":
    main()
