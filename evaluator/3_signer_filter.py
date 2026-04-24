"""
Phase 3: Signer Filter — Identify Intentional Sandwich Attackers
================================================================
Three-track classification, applied uniformly across categories:

  Track 1 (Jito Bot): >=1 Jito same-bundle sandwich (front + all victims +
    back share one bundleId) AND USD total profit >= jito_usd_min ($10).
    The USD floor filters structurally-matching but unprofitable bundle
    candidates (notably ~150 entities in diff_signer_owner that aggregate
    to ~$0).

  Track 2 (Signal Bot): All of the following must hold:
    - cnt >= n_min (statistical reliability, default 10)
    - win_rate >= 0.8 (economic motivation)
    - mean_slippage >= 0.75 (victim observation)
    - P(fg<=100) >= 0.6 (ordering control)
    - usd_total >= signal_usd_min ($10)

  Track 2b (Oneshot Bot, multi_split only): CNT <= oneshot_cnt_max (5) AND
    WR >= 0.8 AND usd_total >= oneshot_usd_min ($100). Captures single
    high-value attackers (e.g. pump.fun launch races) below Signal Bot's
    CNT gate.

Pre-filter diagnostic charts (auto-generated per run):
  1. WR distribution over all signers in the category (10% buckets).
  2. Avg USD/signer by mean_slippage bin, restricted to the pool
     {USD>=signal_usd_min, CNT>=n_min, WR>=wr_min}.
  3. Avg USD/signer by P(fg<=100) bin, same pool.

Usage:
    python 3_signer_filter.py --start-epoch 946 --end-epoch 960
    python 3_signer_filter.py --category multi_split
    python 3_signer_filter.py --category multi_split --multi-variant multi_back
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

SLOTS_PER_EPOCH = 432_000


# diff_signer_transfer is excluded: empirically 0 reliable attackers
# (6,920 sandwiches, $-46 net, $1.78 positive-USD total; the only 3
# Signal-Bot matches all hit CNT=5 with $0 USD). See
# docs/evaluator_design.md.
CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]


MULTI_VARIANTS = ["all", "multi_front", "multi_back", "both"]


def parse_args():
    p = argparse.ArgumentParser(description="Phase 3: signer filter")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=956)
    p.add_argument("--category", type=str, default="standard",
                   choices=CATEGORIES, help="Sandwich category")
    p.add_argument("--multi-variant", type=str, default="all",
                   choices=MULTI_VARIANTS,
                   help="Structural sub-variant (multi_split only). "
                        "multi_front: multiFrontRun=true AND multiBackRun=false; "
                        "multi_back: multiFrontRun=false AND multiBackRun=true; "
                        "both: both flags true; all: everything in multi_split.")
    p.add_argument("--n-min", type=int, default=10,
                   help="Signal Bot minimum sandwich count")
    p.add_argument("--wr-min", type=float, default=0.8,
                   help="Minimum win rate")
    p.add_argument("--slip-min", type=float, default=0.75,
                   help="Minimum mean slippage consumption")
    p.add_argument("--fg100-min", type=float, default=0.6,
                   help="Minimum P(fg<=100) ratio")
    p.add_argument("--signal-usd-min", type=float, default=10.0,
                   help="Signal Bot minimum USD total profit")
    p.add_argument("--oneshot-cnt-max", type=int, default=5,
                   help="Multi-split Track 2b: maximum sandwich count for "
                        "one-shot attackers (decoupled from Signal Bot's CNT).")
    p.add_argument("--oneshot-usd-min", type=float, default=100.0,
                   help="Multi-split Track 2b: USD total threshold for "
                        "one-shot attackers (multi_split only).")
    p.add_argument("--jito-usd-min", type=float, default=10.0,
                   help="Jito Bot track: minimum USD total profit. Filters "
                        "structurally-matching but unprofitable bundle "
                        "candidates (notably in diff_signer_owner).")
    return p.parse_args()


# ── Token Prices ─────────────────────────────────────────────────────────────

def load_token_prices():
    price_path = os.path.join(os.path.dirname(__file__), "data", "token_prices", "prices.csv")
    prices = {}
    if os.path.exists(price_path):
        pdf = pd.read_csv(price_path)
        for _, row in pdf.iterrows():
            if pd.notna(row.get("usd_price")):
                prices[row["token"]] = row["usd_price"]
    if "SOL" not in prices:
        prices["SOL"] = 86.0
    return prices


# ── Signer feature rebuild (for multi-split sub-variants) ────────────────────

def _rebuild_signer_features(ps):
    """Recompute signer-level features from a filtered per-sandwich slice.

    Mirrors the aggregation in Phase 1 just for the fields used by Phase 3
    (sandwich_count, win_rate, mean_slippage, jito_rate, jito_count,
    median_proximity, cross_block_count, sol_avg_profit, in_block_count).
    Fields not required by the classifier are left absent to avoid silently
    carrying stale all-multi values.
    """
    if len(ps) == 0:
        return pd.DataFrame(columns=["sandwich_count", "win_rate"])
    g = ps.groupby("signer")
    sf = pd.DataFrame({
        "sandwich_count": g.size(),
        "win_rate": g["is_profitable"].mean(),
        "jito_rate": g["jito_bundle"].mean(),
        "jito_count": g["jito_bundle"].sum(),
        "median_proximity": g["proximity"].median(),
        "in_block_count": (~ps["cross_block"].astype(bool)).groupby(ps["signer"]).sum(),
        "cross_block_count": ps["cross_block"].astype(bool).groupby(ps["signer"]).sum(),
    })

    def _valid_slip(s):
        v = s[(s >= 0) & (s <= 1)]
        return v.mean() if len(v) > 0 else np.nan
    sf["mean_slippage"] = g["slippage_consumption"].apply(_valid_slip)
    sol_mask = ps["token_a"] == "SOL"
    if sol_mask.any():
        sol_avg = ps[sol_mask].groupby("signer")["profit"].mean()
        sf["sol_avg_profit"] = sol_avg
    else:
        sf["sol_avg_profit"] = np.nan
    return sf


# ── Classification ───────────────────────────────────────────────────────────

def classify_signers(sf, ps, n_min, wr_min, slip_min, fg100_min,
                     category="standard", token_prices=None,
                     signal_usd_min=10.0,
                     oneshot_cnt_max=5, oneshot_usd_min=100.0,
                     jito_usd_min=10.0):
    """Classify signers into Jito Bot, Signal Bot, Oneshot Bot, or Unclassified.

    Track 1 (Jito Bot): >=1 same-bundle sandwich AND USD>=jito_usd_min.
      Independent track — does NOT use the Signal Bot pre-filter pool.

    Track 2 (Signal Bot, two-stage):
      Stage 1 — pre-filter pool:
        usd_total >= signal_usd_min  AND
        sandwich_count >= n_min      AND
        win_rate >= wr_min
      Stage 2 — final attacker set within the pool:
        mean_slippage >= slip_min   AND
        fg100_ratio >= fg100_min

    Track 2b (Oneshot Bot, multi_split only): CNT <= oneshot_cnt_max AND
      WR >= wr_min AND USD >= oneshot_usd_min.

    Returns sf (with usd_total + fg100_ratio + pool/signal masks), tier
    series, and the four signer sets including the intermediate pool.
    """
    # Per-signer fg100 ratio
    fg100_ratio = ps.groupby("signer").apply(
        lambda g: (g["proximity"] <= 100).mean()
    ).rename("fg100_ratio")
    sf = sf.copy()
    sf["fg100_ratio"] = fg100_ratio

    # Per-signer USD totals
    ps_priced = ps.copy()
    if token_prices is not None:
        ps_priced["usd_profit"] = (
            ps_priced["profit"] * ps_priced["token_a"].map(token_prices))
    else:
        ps_priced["usd_profit"] = np.nan
    usd_by_sig = ps_priced.groupby("signer")["usd_profit"].sum()
    sf["usd_total"] = usd_by_sig

    # Track 1 — Jito Bot (independent of Signal pool)
    jito_candidates = set(ps[ps["jito_bundle"] == True]["signer"].unique())
    jito_signers = {
        s for s in jito_candidates
        if s in sf.index and pd.notna(sf.at[s, "usd_total"])
        and sf.at[s, "usd_total"] >= jito_usd_min
    }
    n_dropped = len(jito_candidates) - len(jito_signers)
    if jito_candidates:
        print(f"  Jito Bot: {len(jito_candidates)} same-bundle candidates -> "
              f"{len(jito_signers)} retained "
              f"(dropped {n_dropped} with USD < ${jito_usd_min:.0f})")

    # Track 2 Stage 1 — Signal Bot pre-filter pool
    pool_mask = (
        (sf["usd_total"] >= signal_usd_min) &
        (sf["sandwich_count"] >= n_min) &
        (sf["win_rate"] >= wr_min)
    )
    pool_signers = set(sf[pool_mask].index)

    # Track 2 Stage 2 — Signal Bot final attacker set
    signal_mask = (
        pool_mask &
        (sf["mean_slippage"] >= slip_min) &
        (sf["fg100_ratio"] >= fg100_min)
    )
    signal_signers = set(sf[signal_mask].index)

    # Track 2b — Oneshot Bot (multi_split only)
    oneshot_signers = set()
    if category == "multi_split":
        oneshot_mask = (
            (sf["sandwich_count"] <= oneshot_cnt_max) &
            (sf["win_rate"] >= wr_min) &
            (sf["usd_total"] >= oneshot_usd_min)
        )
        oneshot_signers = set(sf[oneshot_mask].index)

    # Assign tiers
    tiers = {}
    for signer in sf.index:
        is_jito = signer in jito_signers
        is_signal = signer in signal_signers
        is_oneshot = signer in oneshot_signers

        if is_jito and is_signal:
            tiers[signer] = "jito_and_signal"
        elif is_jito:
            tiers[signer] = "jito_only"
        elif is_signal and is_oneshot:
            tiers[signer] = "signal_and_oneshot"
        elif is_signal:
            tiers[signer] = "signal"
        elif is_oneshot:
            tiers[signer] = "oneshot"
        else:
            tiers[signer] = "unclassified"

    tier_series = pd.Series(tiers, name="tier")
    bot_signers = jito_signers | signal_signers | oneshot_signers
    return (sf, tier_series, bot_signers, jito_signers, signal_signers,
            oneshot_signers, pool_signers)


# ── Summary Report ───────────────────────────────────────────────────────────

def print_report(sf, tier_series, ps, token_prices,
                 bot_signers, jito_signers, signal_signers,
                 n_min, wr_min, slip_min, fg100_min):
    """Print detailed classification results."""
    total_signers = len(sf)
    total_sw = int(sf["sandwich_count"].sum())

    ps_copy = ps.copy()
    ps_copy["usd_profit"] = ps_copy["profit"] * ps_copy["token_a"].map(token_prices)
    total_usd = ps_copy["usd_profit"].sum()
    pos_usd = ps_copy.groupby("signer")["usd_profit"].sum()
    pos_total = pos_usd[pos_usd > 0].sum()

    print(f"\n{'='*80}")
    print(f"  SIGNER CLASSIFICATION RESULTS")
    print(f"  Parameters: N_min={n_min}, WR>={wr_min}, slip>={slip_min}, "
          f"fg100>={fg100_min}")
    print(f"{'='*80}")

    # Per-tier summary (includes multi_split-only oneshot tiers; they simply
    # have zero members in other categories.)
    tier_order = ["jito_and_signal", "jito_only", "signal",
                  "signal_and_oneshot", "oneshot", "unclassified"]
    tier_labels = {
        "jito_and_signal": "Jito + Signal",
        "jito_only": "Jito Only",
        "signal": "Signal Bot",
        "signal_and_oneshot": "Signal + Oneshot",
        "oneshot": "Oneshot Bot",
        "unclassified": "Unclassified",
    }

    print(f"\n  --- Tier Breakdown ---")
    print(f"  {'Tier':<25} {'Signers':>8} {'Sandwiches':>12} "
          f"{'SOL Profit':>12} {'USD Profit':>14}")
    print(f"  {'-'*75}")

    for tier in tier_order:
        signers_in_tier = set(tier_series[tier_series == tier].index)
        tier_sf = sf.loc[sf.index.isin(signers_in_tier)]
        n_sw = int(tier_sf["sandwich_count"].sum())
        tier_ps = ps_copy[ps_copy["signer"].isin(signers_in_tier)]
        sol_p = tier_ps[tier_ps["token_a"] == "SOL"]["profit"].sum()
        usd_p = tier_ps["usd_profit"].sum()
        label = tier_labels.get(tier, tier)
        print(f"  {label:<25} {len(signers_in_tier):>8,} {n_sw:>12,} "
              f"{sol_p:>12,.1f} ${usd_p:>13,.0f}")

    # Bot vs Non-bot
    bot_ps = ps_copy[ps_copy["signer"].isin(bot_signers)]
    non_bot_ps = ps_copy[~ps_copy["signer"].isin(bot_signers)]
    bot_sf = sf.loc[sf.index.isin(bot_signers)]
    bot_sw = int(bot_sf["sandwich_count"].sum())
    non_bot_sw = total_sw - bot_sw
    bot_usd = bot_ps["usd_profit"].sum()
    non_bot_usd = non_bot_ps["usd_profit"].sum()
    bot_sol = bot_ps[bot_ps["token_a"] == "SOL"]["profit"].sum()

    print(f"\n  --- Bot vs Non-Bot ---")
    print(f"  {'':>20} {'Signers':>10} {'%':>7} {'Sandwiches':>12} {'%':>7} "
          f"{'SOL':>12} {'USD':>14}")
    print(f"  {'-'*85}")
    print(f"  {'Bot (intentional)':<20} {len(bot_signers):>10,} "
          f"{len(bot_signers)/total_signers*100:>6.2f}% {bot_sw:>12,} "
          f"{bot_sw/total_sw*100:>6.1f}% {bot_sol:>12,.1f} ${bot_usd:>13,.0f}")
    print(f"  {'Non-bot':<20} {total_signers-len(bot_signers):>10,} "
          f"{(total_signers-len(bot_signers))/total_signers*100:>6.2f}% {non_bot_sw:>12,} "
          f"{non_bot_sw/total_sw*100:>6.1f}% "
          f"{non_bot_ps[non_bot_ps['token_a']=='SOL']['profit'].sum():>12,.1f} "
          f"${non_bot_usd:>13,.0f}")

    print(f"\n  --- Profit Coverage ---")
    print(f"  Bot USD / Total USD:    ${bot_usd:>10,.0f} / ${total_usd:>10,.0f} "
          f"({bot_usd/total_usd*100:.1f}%)")
    print(f"  Bot USD / Positive USD: ${bot_usd:>10,.0f} / ${pos_total:>10,.0f} "
          f"({bot_usd/pos_total*100:.1f}%)")

    # Bot signer profiles
    print(f"\n  --- Bot Signer Profiles ---")
    if len(bot_sf) > 0:
        print(f"  Count:    median={bot_sf['sandwich_count'].median():.0f}  "
              f"mean={bot_sf['sandwich_count'].mean():.1f}  "
              f"max={bot_sf['sandwich_count'].max():.0f}")
        print(f"  Win rate: median={bot_sf['win_rate'].median():.3f}  "
              f"mean={bot_sf['win_rate'].mean():.3f}")
        slip = bot_sf["mean_slippage"].dropna()
        if len(slip) > 0:
            print(f"  Slippage: median={slip.median():.3f}  mean={slip.mean():.3f}")
        fg100 = bot_sf["fg100_ratio"].dropna()
        if len(fg100) > 0:
            print(f"  FG100:    median={fg100.median():.3f}  mean={fg100.mean():.3f}")

    # Top 20 bot signers
    print(f"\n  --- Top 20 Bot Signers (by sandwich count) ---")
    bot_usd_map = ps_copy.groupby("signer")["usd_profit"].sum()
    top = bot_sf.nlargest(20, "sandwich_count")
    print(f"  {'Signer':<14} {'Tier':<16} {'CNT':>5} {'WR':>5} {'Slip':>5} "
          f"{'FG100':>5} {'USD':>10}")
    print(f"  {'-'*70}")
    for signer, row in top.iterrows():
        tier = tier_series.get(signer, "?")
        usd = bot_usd_map.get(signer, 0)
        slip_val = row["mean_slippage"] if pd.notna(row["mean_slippage"]) else 0
        fg100_val = row.get("fg100_ratio", 0)
        print(f"  {signer[:12]:<14} {tier:<16} {int(row['sandwich_count']):>5} "
              f"{row['win_rate']:>5.2f} {slip_val:>5.3f} {fg100_val:>5.2f} "
              f"${usd:>9,.0f}")

    return bot_signers


# ── Charts ───────────────────────────────────────────────────────────────────

def plot_wr_distribution_pre_filter(sf, chart_dir, tag, n_min, wr_min,
                                    slip_min, fg100_min, signal_usd_min):
    """Pre-filter WR histogram for ALL signers in this category.

    The plot answers: "across all candidate signers (no filter applied yet),
    how do they distribute over win-rate buckets?" Annotated with the
    Signal-Bot thresholds so the reader sees where the cut-offs sit.
    """
    fig, ax = plt.subplots(figsize=(11, 6))
    bins = np.linspace(0, 1, 11)
    bin_labels = [f"{int(bins[i]*100)}-{int(bins[i+1]*100)}%"
                  for i in range(len(bins) - 1)]
    counts, _ = np.histogram(sf["win_rate"].dropna().values, bins=bins)
    bars = ax.bar(range(len(counts)), counts, color="#4a90d9",
                  edgecolor="black", alpha=0.85)
    for bar, c in zip(bars, counts):
        if c > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, c, f"{c:,}",
                    ha="center", va="bottom", fontsize=9)
    ax.axvline(np.searchsorted(bins, wr_min) - 0.5, color="#cc0000",
               linestyle="--", linewidth=1.5,
               label=f"WR threshold = {wr_min}")
    ax.set_xticks(range(len(bin_labels)))
    ax.set_xticklabels(bin_labels, rotation=30, ha="right")
    ax.set_xlabel("Win Rate Bucket")
    ax.set_ylabel("Number of Signers")
    ax.set_title(f"Pre-filter Win-Rate Distribution "
                 f"(N={int(sf['win_rate'].notna().sum()):,} signers)\n"
                 f"Signal Bot gates: CNT>={n_min}, WR>={wr_min}, "
                 f"slip>={slip_min}, fg100>={fg100_min}, "
                 f"USD>=${signal_usd_min:.0f}")
    ax.legend(loc="upper left")
    ax.set_yscale("log")
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"wr_distribution_pre_filter_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


def _avg_usd_by_bin(sub, value_col, bin_edges):
    """Group sub by binned value_col and return (labels, n_signers, avg_usd)."""
    cuts = pd.cut(sub[value_col], bins=bin_edges, include_lowest=True,
                  right=False)
    g = sub.groupby(cuts, observed=False)
    n = g.size().values
    avg = g["usd_total"].mean().values
    labels = [f"{bin_edges[i]:.1f}-{bin_edges[i+1]:.1f}"
              for i in range(len(bin_edges) - 1)]
    return labels, n, avg


def plot_avg_usd_by_signal(sub, chart_dir, tag, signal_col, signal_label,
                           threshold, signal_min):
    """Bar plot of avg USD/signer per signal bin within the pre-filter pool.

    Pool: signers who already pass `usd_total>=signal_min AND CNT>=n_min
    AND WR>=wr_min`. Bin width = 0.05 (20 bins over [0, 1]), matching the
    Phase-2 threshold-analysis convention in docs/evaluator_design.md §5.2.
    """
    bin_edges = np.arange(0.0, 1.0 + 1e-9, 0.05)
    labels, n, avg = _avg_usd_by_bin(sub, signal_col, bin_edges)

    fig, ax = plt.subplots(figsize=(14, 6))
    bars = ax.bar(range(len(labels)),
                  np.where(np.isnan(avg), 0, avg),
                  color="#e88a3c", edgecolor="black", alpha=0.85)
    for bar, count, val in zip(bars, n, avg):
        if count > 0:
            top = bar.get_height()
            usd_lbl = "$0" if (np.isnan(val) or val == 0) else f"${val:,.0f}"
            ax.text(bar.get_x() + bar.get_width() / 2, top,
                    f"{usd_lbl}\nn={count}",
                    ha="center", va="bottom", fontsize=7)

    # Threshold marker positioned on the bin boundary
    thr_pos = (threshold - bin_edges[0]) / 0.05 - 0.5
    ax.axvline(thr_pos, color="#cc0000", linestyle="--", linewidth=1.8,
               label=f"Signal Bot threshold = {threshold}")

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels([f"{e:.2f}" for e in bin_edges[:-1]],
                       rotation=45, ha="right", fontsize=9)
    ax.set_xlabel(f"{signal_label} (lower edge of 0.05-width bin)")
    ax.set_ylabel("Avg USD Profit per Signer")
    ax.set_title(f"Avg USD/Signer by {signal_label} "
                 f"(Pool: USD>=${signal_min:.0f}, CNT>=10, WR>=0.8 — "
                 f"N={len(sub):,} signers)")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    suffix = signal_col.replace("mean_", "").replace("_ratio", "")
    plt.savefig(os.path.join(chart_dir, f"avg_usd_by_{suffix}_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


def _signal_stats(values):
    """Return basic descriptive stats for a numeric Series."""
    v = pd.Series(values).dropna()
    if len(v) == 0:
        return {"n": 0}
    return {
        "n": int(len(v)),
        "mean": float(v.mean()),
        "median": float(v.median()),
        "std": float(v.std()),
        "min": float(v.min()),
        "max": float(v.max()),
        "p25": float(v.quantile(0.25)),
        "p75": float(v.quantile(0.75)),
    }


def print_pool_signal_stats(pool_sf, slip_min, fg100_min):
    """Mean/median/distribution stats for slippage and fg100 in the pool."""
    print("\n  --- Stage-1 Pool Signal Stats ---")
    print(f"  Pool size: {len(pool_sf):,} signers "
          f"(passed USD>=signal_usd_min, CNT>=n_min, WR>=wr_min)")

    for col, label, thr in [("mean_slippage", "Mean Slippage Consumption", slip_min),
                            ("fg100_ratio", "P(fg <= 100)", fg100_min)]:
        s = _signal_stats(pool_sf[col])
        if s["n"] == 0:
            print(f"\n  {label}: no data")
            continue
        passing = int((pool_sf[col] >= thr).sum())
        print(f"\n  {label} (n={s['n']:,} non-NaN):")
        print(f"    mean={s['mean']:.3f}  median={s['median']:.3f}  "
              f"std={s['std']:.3f}")
        print(f"    min={s['min']:.3f}  p25={s['p25']:.3f}  "
              f"p75={s['p75']:.3f}  max={s['max']:.3f}")
        print(f"    >= threshold ({thr}): {passing:,} "
              f"({passing/s['n']*100:.1f}% of non-NaN)")


def plot_signal_distribution(pool_sf, chart_dir, tag, signal_col, signal_label,
                             threshold):
    """Histogram of a signal over the Stage-1 pool, with mean/median lines."""
    fig, ax = plt.subplots(figsize=(11, 6))
    bin_edges = np.arange(0.0, 1.0 + 1e-9, 0.05)
    vals = pool_sf[signal_col].dropna()
    if len(vals) == 0:
        plt.close(fig)
        return
    counts, _ = np.histogram(vals.values, bins=bin_edges)
    bars = ax.bar(range(len(counts)), counts, color="#4a90d9",
                  edgecolor="black", alpha=0.85)
    for bar, c in zip(bars, counts):
        if c > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, c, f"{c:,}",
                    ha="center", va="bottom", fontsize=8)

    mean_v = float(vals.mean())
    med_v = float(vals.median())
    mean_pos = (mean_v - bin_edges[0]) / 0.05 - 0.5
    med_pos = (med_v - bin_edges[0]) / 0.05 - 0.5
    thr_pos = (threshold - bin_edges[0]) / 0.05 - 0.5
    ax.axvline(thr_pos, color="#cc0000", linestyle="--", linewidth=1.8,
               label=f"Threshold = {threshold}")
    ax.axvline(mean_pos, color="#2ca02c", linestyle="-", linewidth=1.5,
               label=f"Mean = {mean_v:.3f}")
    ax.axvline(med_pos, color="#9467bd", linestyle=":", linewidth=1.5,
               label=f"Median = {med_v:.3f}")

    ax.set_xticks(range(len(bin_edges) - 1))
    ax.set_xticklabels([f"{e:.2f}" for e in bin_edges[:-1]],
                       rotation=45, ha="right", fontsize=9)
    ax.set_xlabel(f"{signal_label} (lower edge of 0.05 bin)")
    ax.set_ylabel("Number of Signers")
    ax.set_title(f"Stage-1 Pool: {signal_label} Distribution "
                 f"(N={len(vals):,} non-NaN signers)")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    suffix = signal_col.replace("mean_", "").replace("_ratio", "")
    plt.savefig(os.path.join(chart_dir, f"pool_{suffix}_distribution_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


def plot_attacker_profit_cnt_scatter(attacker_sf, chart_dir, tag, n_min):
    """Scatter of USD profit (y) vs sandwich count (x) for the final attacker set."""
    if len(attacker_sf) == 0:
        return
    fig, ax = plt.subplots(figsize=(11, 7))
    df = attacker_sf.copy()
    df["usd"] = df["usd_total"]

    cnt = df["sandwich_count"].values.astype(float)
    usd = df["usd"].values.astype(float)
    ax.scatter(cnt, usd, s=40, c="#cc6633", alpha=0.7,
               edgecolors="black", linewidths=0.5)

    ax.axvline(n_min, color="gray", linestyle=":", alpha=0.6,
               label=f"CNT = {n_min}")
    ax.set_xscale("log")
    if (usd > 0).any():
        ax.set_yscale("symlog", linthresh=10)

    # Annotate top-5 by USD
    top = df.nlargest(5, "usd")
    for s, row in top.iterrows():
        ax.annotate(s[:8], (row["sandwich_count"], row["usd"]),
                    fontsize=8, alpha=0.8,
                    xytext=(4, 4), textcoords="offset points")

    ax.set_xlabel("Sandwich Count (CNT, log scale)")
    ax.set_ylabel("USD Total Profit (symlog)")
    ax.set_title(f"Final Attackers: USD Profit vs Sandwich Count "
                 f"(N={len(df):,} signers, total ${df['usd'].sum():,.0f})")
    ax.legend()
    ax.grid(True, alpha=0.3, which="both")
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"attacker_profit_cnt_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


def generate_pre_filter_charts(sf, chart_dir, tag, n_min, wr_min,
                               slip_min, fg100_min, signal_usd_min):
    """Pre-filter diagnostic charts (Phase-3 entry, before any track applies).

    Produces:
      1. Pre-filter WR distribution over all signers in the category.
      2. Avg USD/signer by mean_slippage bin, restricted to the pool
         {USD>=signal_usd_min, CNT>=n_min, WR>=wr_min}.
      3. Avg USD/signer by P(fg<=100) bin, same pool.
    """
    plot_wr_distribution_pre_filter(sf, chart_dir, tag, n_min, wr_min,
                                    slip_min, fg100_min, signal_usd_min)

    pool_mask = (
        (sf["usd_total"] >= signal_usd_min) &
        (sf["sandwich_count"] >= n_min) &
        (sf["win_rate"] >= wr_min)
    )
    pool = sf[pool_mask].copy()
    if len(pool) == 0:
        print(f"  [pre-filter charts] empty pool — skipping slip/fg100 plots")
        return

    plot_avg_usd_by_signal(
        pool, chart_dir, tag,
        signal_col="mean_slippage", signal_label="Mean Slippage Consumption",
        threshold=slip_min, signal_min=signal_usd_min,
    )
    plot_avg_usd_by_signal(
        pool, chart_dir, tag,
        signal_col="fg100_ratio", signal_label="P(fg <= 100)",
        threshold=fg100_min, signal_min=signal_usd_min,
    )

    # Stage-1 pool signal-distribution histograms (mean/median/threshold lines)
    plot_signal_distribution(
        pool, chart_dir, tag,
        signal_col="mean_slippage",
        signal_label="Mean Slippage Consumption",
        threshold=slip_min,
    )
    plot_signal_distribution(
        pool, chart_dir, tag,
        signal_col="fg100_ratio",
        signal_label="P(fg <= 100)",
        threshold=fg100_min,
    )


def generate_charts(sf, ps, tier_series, bot_signers, token_prices, chart_dir, tag,
                    pool_signers=None, signal_signers=None,
                    slip_min=0.75, fg100_min=0.6):
    """Generate classification summary charts."""
    ps_copy = ps.copy()
    ps_copy["usd_profit"] = ps_copy["profit"] * ps_copy["token_a"].map(token_prices)
    usd_by_sig = ps_copy.groupby("signer")["usd_profit"].sum()

    bot_sf = sf.loc[sf.index.isin(bot_signers)].copy()
    non_bot_sf = sf.loc[~sf.index.isin(bot_signers)].copy()

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # 1. WR distribution
    ax = axes[0, 0]
    ax.hist(non_bot_sf["win_rate"].dropna(), bins=20, alpha=0.5,
            label=f"Non-bot ({len(non_bot_sf):,})", color="gray", edgecolor="black")
    ax.hist(bot_sf["win_rate"].dropna(), bins=20, alpha=0.7,
            label=f"Bot ({len(bot_sf):,})", color="red", edgecolor="black")
    ax.set_xlabel("Win Rate")
    ax.set_ylabel("Signers")
    ax.set_title("Win Rate: Bot vs Non-bot")
    ax.legend()

    # 2. Slippage distribution
    ax = axes[0, 1]
    ax.hist(non_bot_sf["mean_slippage"].dropna(), bins=20, alpha=0.5,
            label="Non-bot", color="gray", edgecolor="black")
    ax.hist(bot_sf["mean_slippage"].dropna(), bins=20, alpha=0.7,
            label="Bot", color="red", edgecolor="black")
    ax.set_xlabel("Mean Slippage Consumption")
    ax.set_ylabel("Signers")
    ax.set_title("Slippage: Bot vs Non-bot")
    ax.axvline(0.75, color="blue", linestyle="--", alpha=0.5, label="Threshold=0.75")
    ax.legend()

    # 3. FG100 ratio distribution
    ax = axes[1, 0]
    ax.hist(non_bot_sf["fg100_ratio"].dropna(), bins=20, alpha=0.5,
            label="Non-bot", color="gray", edgecolor="black")
    ax.hist(bot_sf["fg100_ratio"].dropna(), bins=20, alpha=0.7,
            label="Bot", color="red", edgecolor="black")
    ax.set_xlabel("P(FG≤100)")
    ax.set_ylabel("Signers")
    ax.set_title("FG≤100 Ratio: Bot vs Non-bot")
    ax.axvline(0.6, color="blue", linestyle="--", alpha=0.5, label="Threshold=0.6")
    ax.legend()

    # 4. USD profit: bot vs non-bot
    ax = axes[1, 1]
    bot_usd_vals = usd_by_sig.loc[usd_by_sig.index.isin(bot_signers)].dropna()
    non_bot_usd_vals = usd_by_sig.loc[~usd_by_sig.index.isin(bot_signers)].dropna()
    ax.hist(non_bot_usd_vals.clip(-500, 5000), bins=50, alpha=0.5,
            label=f"Non-bot (${non_bot_usd_vals.sum()/1000:.0f}K)", color="gray")
    ax.hist(bot_usd_vals.clip(-500, 5000), bins=50, alpha=0.7,
            label=f"Bot (${bot_usd_vals.sum()/1000:.0f}K)", color="red")
    ax.set_xlabel("USD Profit per Signer (clipped)")
    ax.set_ylabel("Signers")
    ax.set_title("USD Profit Distribution: Bot vs Non-bot")
    ax.legend()

    plt.suptitle("Signer Classification Results", fontsize=14)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"classification_summary_{tag}.png"), dpi=150)
    plt.close()

    # 5/6. Scatters over the Stage-1 pool, with Stage-2 (slip+fg100) cuts.
    # Population restricted to the pool so the upper-right region (passed
    # Stage-2) contains exclusively Signal Bots — no non-bot leakage.
    if pool_signers is None or signal_signers is None:
        return  # backward-compatibility safeguard

    pool_sf = sf.loc[sf.index.isin(pool_signers) &
                     sf["mean_slippage"].notna() &
                     sf["fg100_ratio"].notna()].copy()
    if len(pool_sf) == 0:
        return
    pool_sf["usd"] = usd_by_sig
    pool_sf["passed_stage2"] = pool_sf.index.isin(signal_signers)
    passed = pool_sf[pool_sf["passed_stage2"]]
    failed = pool_sf[~pool_sf["passed_stage2"]]

    from matplotlib.patches import Rectangle

    def _jitter(values, amount=0.008):
        rng = np.random.default_rng(seed=42)
        return values.values + rng.uniform(-amount, amount, size=len(values))

    def _draw_scatter(color_col, color_label, vmin, vmax, fname):
        # Single pane focused on [0.2, 1.02]; the [0, 0.2] interval is shown
        # only as a "0" tick mark followed by a break (//) so the reader sees
        # the axis starts at 0 but understands that range is collapsed.
        fig, ax = plt.subplots(figsize=(13, 10))
        ax.set_facecolor("#f5f5f5")
        ax.add_patch(Rectangle((slip_min, fg100_min),
                               1.02 - slip_min, 1.02 - fg100_min,
                               facecolor="white", edgecolor="none", zorder=0))

        f_sorted = (failed.sort_values("sandwich_count", ascending=False)
                    if len(failed) > 0 else failed)
        p_sorted = passed.sort_values("sandwich_count", ascending=False)

        if len(f_sorted) > 0:
            ax.scatter(_jitter(f_sorted["mean_slippage"]),
                       _jitter(f_sorted["fg100_ratio"]),
                       s=np.clip(f_sorted["sandwich_count"] / 5, 6, 35),
                       c="#4a90d9", alpha=0.35, edgecolors="#2a5a9a",
                       linewidths=0.3, zorder=2,
                       label=f"Failed Stage-2 (n={len(failed):,})")

        sc = ax.scatter(_jitter(p_sorted["mean_slippage"]),
                        _jitter(p_sorted["fg100_ratio"]),
                        s=np.clip(p_sorted["sandwich_count"] / 5, 18, 140),
                        c=p_sorted[color_col], cmap="YlOrRd", alpha=0.85,
                        vmin=vmin, vmax=vmax,
                        edgecolors="black", linewidths=0.5, zorder=3,
                        label=f"Signal Bot (n={len(p_sorted):,})")

        ax.axvline(slip_min, color="#cc0000", linestyle="--", linewidth=1.8,
                   alpha=0.8, label=f"Slip threshold = {slip_min}")
        ax.axhline(fg100_min, color="#cc0000", linestyle="--", linewidth=1.8,
                   alpha=0.8, label=f"FG100 threshold = {fg100_min}")

        # Focus range; leave a small gap before 0.2 to host the "0" tick + break.
        gap = 0.04
        ax.set_xlim(0.2 - gap, 1.02)
        ax.set_ylim(0.2 - gap, 1.02)

        # Custom tick layout: "0" pinned to the left edge / bottom edge,
        # then a break, then ticks every 0.1 from 0.2 to 1.0.
        x_ticks = [0.2 - gap] + list(np.round(np.arange(0.2, 1.01, 0.1), 2))
        x_lbls  = ["0"] + [f"{v:.1f}" for v in np.round(np.arange(0.2, 1.01, 0.1), 2)]
        ax.set_xticks(x_ticks);  ax.set_xticklabels(x_lbls)
        ax.set_yticks(x_ticks);  ax.set_yticklabels(x_lbls)

        # Break marks: small slash pair across the 0..0.2 collapsed gap on
        # both axes, drawn in axis coordinates.
        kw = dict(transform=ax.transAxes, color="black",
                  linewidth=1.2, clip_on=False)
        d = 0.012
        # x axis break — sits between "0" and 0.2 at the bottom spine
        x_break_axis = (gap / 2) / (1.02 - (0.2 - gap))
        ax.add_artist(plt.Line2D([x_break_axis - d, x_break_axis + d],
                                 [-d, d], **kw))
        ax.add_artist(plt.Line2D([x_break_axis - d + 0.005,
                                  x_break_axis + d + 0.005],
                                 [-d, d], **kw))
        # y axis break — sits between "0" and 0.2 at the left spine
        y_break_axis = (gap / 2) / (1.02 - (0.2 - gap))
        ax.add_artist(plt.Line2D([-d, d],
                                 [y_break_axis - d, y_break_axis + d], **kw))
        ax.add_artist(plt.Line2D([-d, d],
                                 [y_break_axis - d + 0.005,
                                  y_break_axis + d + 0.005], **kw))

        ax.set_xlabel("Mean Slippage Consumption", fontsize=13)
        ax.set_ylabel(r"P(FG $\leq$ 100)", fontsize=13)
        ax.set_title(f"Stage-1 Pool — Stage-2 Cut "
                     f"(N={len(pool_sf):,}; passed={len(passed):,})\n"
                     f"size = sandwich count, color = {color_label} "
                     f"(axes broken between 0 and 0.2)", fontsize=12)
        ax.legend(fontsize=10, loc="lower left", framealpha=0.9,
                  edgecolor="gray")
        ax.grid(True, alpha=0.15, color="gray")

        if sc is not None:
            cbar = plt.colorbar(sc, ax=ax, shrink=0.75, pad=0.02)
            cbar.set_label(color_label, fontsize=11)

        plt.tight_layout()
        plt.savefig(os.path.join(chart_dir, fname),
                    dpi=150, bbox_inches="tight")
        plt.close()

    # Pool requires usd_total >= $10, so log10 starts at 1.
    pool_sf["log_usd"] = np.log10(pool_sf["usd"].clip(10))
    passed = pool_sf[pool_sf["passed_stage2"]]
    failed = pool_sf[~pool_sf["passed_stage2"]]
    _draw_scatter("log_usd", "log$_{10}$(USD profit)", 1, 5,
                  f"scatter_slip_fg100_{tag}.png")

    # Avg USD per sandwich is positive within the pool. Floor at $1
    # (log10=0) to keep the scale non-negative; cap at $100 (log10=2)
    # because >99% of pool members sit in $1-$100, so a wider range
    # would push the bulk of colours into the pale-yellow end.
    pool_sf["avg_usd"] = pool_sf["usd"] / pool_sf["sandwich_count"]
    pool_sf["log_avg_usd"] = np.log10(pool_sf["avg_usd"].clip(1))
    passed = pool_sf[pool_sf["passed_stage2"]]
    failed = pool_sf[~pool_sf["passed_stage2"]]
    _draw_scatter("log_avg_usd", "log$_{10}$(avg USD per sandwich)",
                  0, 2, f"scatter_slip_fg100_avgprofit_{tag}.png")


# ── Main ─────────────────────────────────────────────────────────────────────

def _filter_multi_variant(ps, variant):
    """Restrict per-sandwich metrics to a structural variant of multi_split.

    variant == "all"         -> no-op (everything)
    variant == "multi_front" -> multiFrontRun=true AND multiBackRun=false
    variant == "multi_back"  -> multiFrontRun=false AND multiBackRun=true
    variant == "both"        -> both flags true
    """
    if variant == "all":
        return ps
    if "multi_front" not in ps.columns or "multi_back" not in ps.columns:
        raise ValueError(
            "Per-sandwich metrics missing multi_front/multi_back columns. "
            "Re-run Phase 1 (1_signer_data_preparation_and_summary.py) to "
            "regenerate with flag support.")
    mf = ps["multi_front"].astype(bool)
    mb = ps["multi_back"].astype(bool)
    if variant == "multi_front":
        return ps[mf & ~mb]
    if variant == "multi_back":
        return ps[~mf & mb]
    if variant == "both":
        return ps[mf & mb]
    raise ValueError(f"Unknown variant: {variant}")


def main():
    args = parse_args()
    tag = f"{args.start_epoch}_{args.end_epoch}"
    category = args.category
    variant = args.multi_variant

    step1_dir = f"data/1_signer_data_preparation_and_summary/{category}"
    # Multi-split variant output goes to a sub-directory; standard categories
    # keep their existing path (variant="all" is a no-op, so this is safe).
    if category == "multi_split" and variant != "all":
        out_dir = f"data/3_signer_filter/{category}/{variant}"
    else:
        out_dir = f"data/3_signer_filter/{category}"
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)

    n_min = args.n_min
    wr_min = args.wr_min
    slip_min = args.slip_min
    fg100_min = args.fg100_min
    signal_usd_min = args.signal_usd_min
    oneshot_cnt_max = args.oneshot_cnt_max
    oneshot_usd_min = args.oneshot_usd_min
    jito_usd_min = args.jito_usd_min

    print(f"=== Phase 3: Signer Filter ===")
    print(f"Category: {category}" +
          (f" / variant={variant}" if category == "multi_split" else ""))
    print(f"Signal Bot gates: CNT>={n_min}, WR>={wr_min}, slip>={slip_min}, "
          f"fg100>={fg100_min}, USD>=${signal_usd_min:.0f}")
    print(f"Jito Bot track: same-bundle AND USD>=${jito_usd_min:.0f}")
    if category == "multi_split":
        print(f"Oneshot track: CNT<={oneshot_cnt_max} AND WR>={wr_min} "
              f"AND USD>=${oneshot_usd_min:.0f}")
    print(f"Epoch range: {args.start_epoch}-{args.end_epoch}")

    # Load data
    print(f"\nLoading data...")
    ps = pd.read_parquet(f"{step1_dir}/per_sandwich_metrics_{tag}.parquet")
    sf = pd.read_parquet(f"{step1_dir}/signer_features_{tag}.parquet")
    token_prices = load_token_prices()
    print(f"  Per-sandwich: {len(ps):,}")
    print(f"  Signers: {len(sf):,}")

    # Optional structural sub-selection for multi-split
    if category == "multi_split" and variant != "all":
        ps = _filter_multi_variant(ps, variant)
        # Rebuild signer features restricted to this sub-variant so thresholds
        # (CNT, WR, slippage, fg100) reflect only the signer's behaviour in
        # the selected structural pattern.
        sf = _rebuild_signer_features(ps)
        print(f"  After variant '{variant}' filter: "
              f"{len(ps):,} sandwiches, {len(sf):,} signers")

    # Classify
    print(f"\nClassifying signers...")
    (sf, tier_series, bot_signers, jito_signers, signal_signers,
     oneshot_signers, pool_signers) = classify_signers(
        sf, ps, n_min, wr_min, slip_min, fg100_min,
        category=category, token_prices=token_prices,
        signal_usd_min=signal_usd_min,
        oneshot_cnt_max=oneshot_cnt_max, oneshot_usd_min=oneshot_usd_min,
        jito_usd_min=jito_usd_min)

    pool_sf = sf.loc[sf.index.isin(pool_signers)].copy()
    print(f"\nStage-1 pool: {len(pool_sf):,} signers "
          f"(USD>=${signal_usd_min:.0f} AND CNT>={n_min} AND WR>={wr_min})")
    print_pool_signal_stats(pool_sf, slip_min, fg100_min)
    pool_sf.to_csv(f"{out_dir}/pool_signers_{tag}.csv")

    # Pre-filter diagnostic charts (WR + per-bin avg-USD + pool distributions)
    print(f"\nGenerating pre-filter diagnostic charts...")
    generate_pre_filter_charts(sf, chart_dir, tag, n_min, wr_min,
                               slip_min, fg100_min, signal_usd_min)

    # Final attacker scatter (Signal Bot final set after Stage-2 filter)
    final_attacker_sf = sf.loc[sf.index.isin(signal_signers)]
    plot_attacker_profit_cnt_scatter(final_attacker_sf, chart_dir, tag, n_min)

    # Report
    print_report(sf, tier_series, ps, token_prices,
                 bot_signers, jito_signers, signal_signers,
                 n_min, wr_min, slip_min, fg100_min)

    if category == "multi_split":
        print(f"\n  --- Multi-split Track Breakdown ---")
        print(f"  Signal Bot only:  {len(signal_signers - oneshot_signers):>4}")
        print(f"  Oneshot only:     {len(oneshot_signers - signal_signers):>4}")
        print(f"  Both tracks:      {len(signal_signers & oneshot_signers):>4}")
        print(f"  Bot total:        {len(bot_signers):>4}")

    # Save outputs
    print(f"\nSaving outputs to {out_dir}/...")

    bot_sf = sf.loc[sf.index.isin(bot_signers)].copy()
    bot_sf["tier"] = tier_series.loc[bot_sf.index]
    bot_ps = ps[ps["signer"].isin(bot_signers)].copy()

    # Add USD profit columns (total, sol_part, nonsol_part, price coverage).
    # Splitting SOL vs non-SOL helps the reader see whether a signer's dollar
    # impact comes from SOL-denominated pairs or from priced SPL tokens, and
    # flags signers whose profit is concentrated in unpriced tokens (coverage
    # low => total USD may understate real impact).
    ps_priced = ps.copy()
    ps_priced["usd_profit"] = ps_priced["profit"] * ps_priced["token_a"].map(token_prices)
    ps_priced["is_sol"] = ps_priced["token_a"] == "SOL"
    bot_priced = ps_priced[ps_priced["signer"].isin(bot_signers)]

    usd_by_signer = bot_priced.groupby("signer").agg(
        usd_total_profit=("usd_profit", "sum"),
        usd_avg_profit=("usd_profit", "mean"),
    )
    sol_profit = bot_priced[bot_priced["is_sol"]].groupby("signer")["profit"].sum()
    usd_by_signer["sol_total_profit"] = sol_profit.reindex(usd_by_signer.index).fillna(0)

    sol_price = token_prices.get("SOL", 86.0)
    usd_by_signer["usd_profit_sol_part"] = usd_by_signer["sol_total_profit"] * sol_price
    usd_by_signer["usd_profit_nonsol_part"] = (
        usd_by_signer["usd_total_profit"] - usd_by_signer["usd_profit_sol_part"])

    nonsol = bot_priced[~bot_priced["is_sol"]]
    if len(nonsol) > 0:
        nonsol_counts = nonsol.groupby("signer").size()
        nonsol_priced = nonsol[nonsol["usd_profit"].notna()].groupby("signer").size()
        usd_by_signer["usd_price_coverage"] = (
            nonsol_priced.reindex(usd_by_signer.index).fillna(0) /
            nonsol_counts.reindex(usd_by_signer.index).replace(0, np.nan)
        )
    else:
        usd_by_signer["usd_price_coverage"] = np.nan
    bot_sf = bot_sf.join(usd_by_signer)

    bot_sf.to_csv(f"{out_dir}/bot_signers_{tag}.csv")
    bot_sf.to_parquet(f"{out_dir}/bot_signers_{tag}.parquet")
    bot_ps.to_parquet(f"{out_dir}/bot_sandwiches_{tag}.parquet")

    tier_df = pd.DataFrame({"tier": tier_series})
    tier_df.to_csv(f"{out_dir}/all_signer_tiers_{tag}.csv")

    print(f"  bot_signers_{tag}.csv/parquet     ({len(bot_sf):,} signers)")
    print(f"  bot_sandwiches_{tag}.parquet       ({len(bot_ps):,} sandwiches)")
    print(f"  all_signer_tiers_{tag}.csv         ({len(tier_df):,} signers)")

    # Charts
    print(f"\nGenerating charts...")
    generate_charts(sf, ps, tier_series, bot_signers, token_prices, chart_dir, tag,
                    pool_signers=pool_signers, signal_signers=signal_signers,
                    slip_min=slip_min, fg100_min=fg100_min)
    print(f"  Charts saved to {chart_dir}/")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
