"""
Phase 3: Signer Filter — Identify Intentional Sandwich Attackers
================================================================
Two-track classification (default, used for standard / diff_signer_*):

  Track 1 (Jito Bot): Any signer with >= 1 Jito same-bundle sandwich
    -> deterministic signal of ordering control, no other requirement

  Track 2 (Signal Bot): All of the following must hold:
    - cnt >= N_min (statistical reliability, default 5)
    - win_rate >= 0.8 (economic motivation — sustained profitability)
    - mean_slippage >= 0.75 (victim observation — calibrated front-runs)
    - P(fg<=100) >= 0.6 (ordering control — front-run proximity)

  Thresholds are chosen at the transition point where avg USD/signer jumps
  ~2x (from ~$500 to ~$1,400), indicating a qualitative shift from
  coincidental HFT to intentional attackers. See 2_parameter_selection for
  the full analysis.

Multi-split specific adjustments (category=multi_split only):
  - Jito Bot track is skipped: in epoch 946-956, 0 multi-split sandwiches
    appear as Jito same-bundle events (multi-split is by construction a
    non-bundle ordering strategy).
  - Track 2b (High-profit One-shot): adds CNT <= 5 AND WR >= 0.8 AND
    USD total profit >= $100. Rationale: ~58% of multi-split signers have
    CNT=1 and cannot clear Signal Bot's CNT>=5 gate, yet a subset executes
    single high-value attacks (e.g. pump.fun launch races) with prox=1,
    slippage ~0.8. USD>=$100 isolates those from the CNT=1 noise floor.
  - When --multi-variant is set to multi_front/multi_back/both, the same
    filter is applied to the corresponding structural subset only.

Output: filtered bot signers and their sandwiches for downstream analysis.

Usage:
    python 3_signer_filter.py --start-epoch 946 --end-epoch 956
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
    p.add_argument("--n-min", type=int, default=5,
                   help="Minimum sandwich count")
    p.add_argument("--wr-min", type=float, default=0.8,
                   help="Minimum win rate")
    p.add_argument("--slip-min", type=float, default=0.75,
                   help="Minimum mean slippage consumption")
    p.add_argument("--fg100-min", type=float, default=0.6,
                   help="Minimum P(fg<=100) ratio")
    p.add_argument("--oneshot-usd-min", type=float, default=100.0,
                   help="Multi-split Track 2b: USD total threshold for "
                        "CNT<=n_min one-shot attackers (multi_split only).")
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
                     oneshot_usd_min=100.0):
    """Classify signers into Jito Bot, Signal Bot, Oneshot Bot, or Unclassified.

    Track 1: Jito Bot — deterministic ordering control evidence.
      For diff_signer_owner: skipped entirely (Jito diff-signer entities
      are structurally-matching but systematically unprofitable).
      For multi_split: skipped (observed count ~0 in our dataset).

    Track 2 (Signal Bot): WR + slippage + fg100 ratio gates on CNT >= n_min.

    Track 2b (Oneshot Bot, multi_split only): CNT <= n_min AND WR >= wr_min
      AND USD total >= oneshot_usd_min. Captures single high-value attackers
      (e.g. pump.fun launch races) who cannot clear CNT>=n_min but demonstrate
      both economic motivation and real dollar impact.
    """
    # Compute fg100 ratio per signer
    fg100_ratio = ps.groupby("signer").apply(
        lambda g: (g["proximity"] <= 100).mean()
    ).rename("fg100_ratio")
    sf = sf.copy()
    sf["fg100_ratio"] = fg100_ratio

    # Track 1: Jito Bot
    if category == "diff_signer_owner":
        jito_signers = set()
        jito_candidates = set(ps[ps["jito_bundle"] == True]["signer"].unique())
        if jito_candidates:
            print(f"  Jito track skipped for diff_signer_owner "
                  f"({len(jito_candidates)} candidates excluded: "
                  f"structurally-matching but unprofitable)")
    elif category == "multi_split":
        jito_signers = set()
        jito_candidates = set(ps[ps["jito_bundle"] == True]["signer"].unique())
        if jito_candidates:
            print(f"  Jito track skipped for multi_split "
                  f"({len(jito_candidates)} candidates; multi-split is by "
                  f"construction a non-bundle ordering strategy)")
    else:
        jito_signers = set(ps[ps["jito_bundle"] == True]["signer"].unique())

    # Track 2: Signal Bot
    signal_mask = (
        (sf["sandwich_count"] >= n_min) &
        (sf["win_rate"] >= wr_min) &
        (sf["mean_slippage"] >= slip_min) &
        (sf["fg100_ratio"] >= fg100_min)
    )
    signal_signers = set(sf[signal_mask].index)

    # Track 2b: Oneshot Bot (multi_split only)
    oneshot_signers = set()
    if category == "multi_split":
        ps_priced = ps.copy()
        if token_prices is not None:
            ps_priced["usd_profit"] = (
                ps_priced["profit"] * ps_priced["token_a"].map(token_prices))
        else:
            ps_priced["usd_profit"] = np.nan
        usd_by_sig = ps_priced.groupby("signer")["usd_profit"].sum()
        sf["usd_total"] = usd_by_sig
        oneshot_mask = (
            (sf["sandwich_count"] <= n_min) &
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
            oneshot_signers)


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

def generate_charts(sf, ps, tier_series, bot_signers, token_prices, chart_dir, tag):
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

    # 5. Scatter: slippage vs fg100 for WR>=0.8, CNT>=5 signers
    fig, ax = plt.subplots(figsize=(12, 9))
    ax.set_facecolor("#f5f5f5")

    wr_pool = sf[(sf["win_rate"] >= 0.8) & (sf["sandwich_count"] >= 5) &
                 sf["mean_slippage"].notna() &
                 sf["fg100_ratio"].notna()].copy()
    wr_pool["is_bot"] = wr_pool.index.isin(bot_signers)
    wr_pool["usd"] = usd_by_sig
    wr_pool["log_usd"] = np.log10(wr_pool["usd"].clip(1))

    non_b = wr_pool[~wr_pool["is_bot"]].copy()
    bots = wr_pool[wr_pool["is_bot"]].copy()

    # White background for the bot quadrant (active region)
    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((0.75, 0.6), 0.27, 0.42, facecolor="white",
                            edgecolor="none", zorder=0))

    # Non-bot: steel blue with visible edges
    ax.scatter(non_b["mean_slippage"], non_b["fg100_ratio"],
               s=np.clip(non_b["sandwich_count"] / 5, 8, 40),
               c="#4a90d9", alpha=0.45, edgecolors="#2a5a9a",
               linewidths=0.4, zorder=2,
               label=f"Non-bot (n={len(non_b):,})")

    # Bot: colored by USD, prominent
    sc_bot = ax.scatter(bots["mean_slippage"], bots["fg100_ratio"],
                        s=np.clip(bots["sandwich_count"] / 5, 15, 120),
                        c=bots["log_usd"], cmap="YlOrRd", alpha=0.9,
                        vmin=0, vmax=5, edgecolors="black", linewidths=0.6,
                        zorder=3, label=f"Bot (n={len(bots):,})")

    # Threshold lines
    ax.axvline(0.75, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.8,
               label="Slip threshold = 0.75")
    ax.axhline(0.6, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.8,
               label="FG100 threshold = 0.6")

    ax.set_xlabel("Mean Slippage Consumption", fontsize=13)
    ax.set_ylabel(r"P(FG $\leq$ 100)", fontsize=13)
    ax.set_title(fr"Signer Classification (WR $\geq$ 0.8, CNT $\geq$ 5)\n"
                 f"size = sandwich count, color = log$_{{10}}$(USD profit)",
                 fontsize=13)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.legend(fontsize=10, loc="lower left",
              framealpha=0.9, edgecolor="gray")
    ax.grid(True, alpha=0.15, color="gray")

    cbar = plt.colorbar(sc_bot, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label("log$_{10}$(USD profit)", fontsize=11)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"scatter_slip_fg100_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()

    # 6. Scatter: same layout but colored by avg USD per sandwich
    fig, ax = plt.subplots(figsize=(12, 9))
    ax.set_facecolor("#f5f5f5")

    wr_pool["avg_usd"] = wr_pool["usd"] / wr_pool["sandwich_count"]
    wr_pool["log_avg_usd"] = np.log10(wr_pool["avg_usd"].clip(0.01))
    non_b = wr_pool[~wr_pool["is_bot"]].copy()
    bots = wr_pool[wr_pool["is_bot"]].copy()

    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((0.75, 0.6), 0.27, 0.42, facecolor="white",
                            edgecolor="none", zorder=0))

    ax.scatter(non_b["mean_slippage"], non_b["fg100_ratio"],
               s=np.clip(non_b["sandwich_count"] / 5, 8, 40),
               c="#4a90d9", alpha=0.45, edgecolors="#2a5a9a",
               linewidths=0.4, zorder=2,
               label=f"Non-bot (n={len(non_b):,})")

    sc_bot2 = ax.scatter(bots["mean_slippage"], bots["fg100_ratio"],
                         s=np.clip(bots["sandwich_count"] / 5, 15, 120),
                         c=bots["log_avg_usd"], cmap="YlOrRd", alpha=0.9,
                         vmin=-2, vmax=2, edgecolors="black", linewidths=0.6,
                         zorder=3, label=f"Bot (n={len(bots):,})")

    ax.axvline(0.75, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.8,
               label="Slip threshold = 0.75")
    ax.axhline(0.6, color="#cc0000", linestyle="--", linewidth=1.8, alpha=0.8,
               label="FG100 threshold = 0.6")

    ax.set_xlabel("Mean Slippage Consumption", fontsize=13)
    ax.set_ylabel(r"P(FG $\leq$ 100)", fontsize=13)
    ax.set_title(fr"Signer Classification (WR $\geq$ 0.8, CNT $\geq$ 5)\n"
                 f"size = sandwich count, color = log$_{{10}}$(avg USD/sandwich)",
                 fontsize=13)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.legend(fontsize=10, loc="lower left",
              framealpha=0.9, edgecolor="gray")
    ax.grid(True, alpha=0.15, color="gray")

    cbar2 = plt.colorbar(sc_bot2, ax=ax, shrink=0.75, pad=0.02)
    cbar2.set_label("log$_{10}$(avg USD per sandwich)", fontsize=11)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"scatter_slip_fg100_avgprofit_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


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
    oneshot_usd_min = args.oneshot_usd_min

    print(f"=== Phase 3: Signer Filter ===")
    print(f"Category: {category}" +
          (f" / variant={variant}" if category == "multi_split" else ""))
    print(f"Parameters: N_min={n_min}, WR>={wr_min}, slip>={slip_min}, "
          f"fg100>={fg100_min}")
    if category == "multi_split":
        print(f"Oneshot track: CNT<={n_min} AND WR>={wr_min} "
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
     oneshot_signers) = classify_signers(
        sf, ps, n_min, wr_min, slip_min, fg100_min,
        category=category, token_prices=token_prices,
        oneshot_usd_min=oneshot_usd_min)

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
    generate_charts(sf, ps, tier_series, bot_signers, token_prices, chart_dir, tag)
    print(f"  Charts saved to {chart_dir}/")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
