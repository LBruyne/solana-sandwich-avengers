"""Phase 3: identify intentional sandwich attackers.

Two tracks, applied uniformly across categories. Every threshold is a CLI default.

  Track 1 (Jito Bot): >=1 verified bundle sandwich, as defined in
    `utils.intent.verified_bundle_sandwiches` -- one bundleId holds the front run, a victim
    and the back run, `signerSame`, `profitA > 0`. No count threshold and no USD floor.
    Pinned across scripts 0/1/3 by `tests/test_bundle_definition.py`.

  Track 2 (Signal Bot): all of
    - cnt >= n_min                        (100)
    - sol_win_rate >= wr_min              (0.85)
    - usd_total >= signal_usd_min         ($10)
    - mean_SC >= slip_min                 (0.90)
    - median front_gap <= fg_median_max   (75)

  Track 2b (Oneshot Bot, multi_split only, off unless --oneshot):
    cnt <= oneshot_cnt_max (5) AND sol_win_rate >= wr_min AND usd_total >= oneshot_usd_min
    ($100).

PROFIT IS NET. Every USD figure here -- the gates' `usd_total`, the tier table, the
geometry table's `*_usd` columns, the charts -- is the attacker's profit minus the fees it
paid on its own front-run and back-run legs. `win_rate` is net for the same reason: it is
the mean of phase 1's `is_profitable`, which is `usd_profit_net > 0`.

`win_rate` is a rate over the PRICEABLE subset and `win_rate_n` is that subset's size, so
`win_rate_n / sandwich_count` is that signer's price coverage. `sol_win_rate` (tokenA ==
SOL only) needs no price table and is the gate.

Tiers emitted: jito_only, jito_and_signal, signal, signal_and_oneshot, oneshot,
unclassified. Each attacker is also annotated with `expert_verdict`
(yes / no / ambiguous / not_audited / no_audit_for_tag) as an annotation, not a filter.

Pre-filter diagnostic charts, auto-generated per run:
  1. WR distribution over all signers in the category (10% buckets).
  2. Avg USD/signer by mean_SC bin, over the pool {USD>=signal_usd_min, CNT>=n_min,
     WR>=wr_min}.
  3. Avg USD/signer by P(fg<=100) bin, same pool.

Usage:
    python 3_attacker_filter.py --start-epoch 946 --end-epoch 990
    python 3_attacker_filter.py --category multi_split
    python 3_attacker_filter.py --category multi_split --multi-variant multi_back
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# `usd_series` is not imported: it is the GROSS figure, and every USD number this script
# reports is net of the attacker's own fees (see `net_usd_series` below).
from utils.intent import (XL_CLASSES, add_geometry_classes,
                          geometry_profile, leader_geometry, leader_rotations,
                          load_phase1, load_token_prices, rotation_index, tag_for,
                          verified_bundle_sandwiches)

SLOTS_PER_EPOCH = 432_000


# `diff_signer_transfer` is measured rather than asserted away: over 946-990 it is 22,587
# single-leg sandwiches, 13.3 % of them with profitA > 0. It is listed LAST because its
# attacker attribution is the weakest of the four -- the legs share no signer and no owner, so
# phase 1 names the front-run fee payer. Anything this category contributes must carry that caveat.
CATEGORIES = ["standard", "multi_split", "diff_signer_owner", "diff_signer_transfer"]


MULTI_VARIANTS = ["all", "multi_front", "multi_back", "both"]


def parse_args():
    p = argparse.ArgumentParser(description="Phase 3: attacker filter")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=960)
    p.add_argument("--database", type=str, default="solwich",
                   help="Which phase-1 output directory to read")
    p.add_argument("--cross-leader", type=str, default="include",
                   choices=["include", "exclude", "only"],
                   help="Which phase-1 cross-leader variant to read. Run it twice — 'include' for "
                        "the whole v2 population and 'exclude' for the slice comparable with v1")
    p.add_argument("--oneshot", action="store_true",
                   help="Enable the multi_split oneshot track. Off by default: it admits attackers "
                        "on 1-5 sandwiches, which the operator excluded from the analysis set")
    p.add_argument("--no-geometry", action="store_true",
                   help="Skip the leader-geometry join (it needs ClickHouse and slot_leaders)")
    p.add_argument("--category", type=str, default="standard",
                   choices=CATEGORIES, help="Sandwich category")
    p.add_argument("--multi-variant", type=str, default="all",
                   choices=MULTI_VARIANTS,
                   help="Structural sub-variant (multi_split only). "
                        "multi_front: multiFrontRun=true AND multiBackRun=false; "
                        "multi_back: multiFrontRun=false AND multiBackRun=true; "
                        "both: both flags true; all: everything in multi_split.")
    p.add_argument("--n-min", type=int, default=100,
                   help="Signal Bot minimum sandwich count.")
    p.add_argument("--wr-min", type=float, default=0.85,
                   help="Minimum SOL win rate, net of the attacker's own fees.")
    p.add_argument("--slip-min", type=float, default=0.90,
                   help="Minimum mean slippage consumption (mean_SC).")
    p.add_argument("--fg-median-max", type=float, default=75.0,
                   help="Maximum MEDIAN front_gap, in transactions. The median rather than the "
                        "mean, since front_gap counts transactions across block boundaries and a "
                        "cross-block gap absorbs whole intervening blocks.")
    p.add_argument("--signal-usd-min", type=float, default=10.0,
                   help="Signal Bot minimum USD total profit, NET of the attacker's own tx fees")
    p.add_argument("--oneshot-cnt-max", type=int, default=5,
                   help="Multi-split Track 2b: maximum sandwich count for "
                        "one-shot attackers (decoupled from Signal Bot's CNT).")
    p.add_argument("--oneshot-usd-min", type=float, default=100.0,
                   help="Multi-split Track 2b: NET USD total threshold for "
                        "one-shot attackers (multi_split only).")
    return p.parse_args()


# ── Net USD profit ───────────────────────────────────────────────────────────
#
# One price table, loaded once, in `utils.intent.load_token_prices`.

def net_usd_series(ps, token_prices):
    """USD profit per sandwich, NET of the attacker's own transaction fees.

    The detector's `profitA` is `backTx.toTotal - frontTx.fromTotal`, with no cost side. Phase
    1 computes the net figure per sandwich as `usd_profit_net` and that column is used when
    present; the recompute branch subtracts the same priced fee for a frame that lacks it.

    An unpriced tokenA yields NaN, not a loss. In the sums this script takes -- per-signer
    `usd_total`, the tier table, the geometry `*_usd` columns -- NaN contributes 0, so an
    unpriced sandwich neither adds nor subtracts.
    """
    if "usd_profit_net" in ps.columns:
        return ps["usd_profit_net"].astype("float64")
    if "fee_sol" not in ps.columns or not token_prices:
        raise SystemExit(
            "per-sandwich metrics carry neither `usd_profit_net` nor (`fee_sol` + a price table): "
            "they were written by a phase 1 that predates net profit. Re-run "
            "1_signer_data_preparation_and_summary.py")
    sol_price = float(token_prices.get("SOL", np.nan))
    return (ps["profit"].astype("float64") * ps["token_a"].map(token_prices).astype("float64")
            - ps["fee_sol"].astype("float64") * sol_price)


# ── Signer feature rebuild (for multi-split sub-variants) ────────────────────

def _rebuild_signer_features(ps):
    """Recompute signer-level features from a filtered per-sandwich slice.

    Mirrors the aggregation in Phase 1 just for the fields used by Phase 3
    (sandwich_count, win_rate, mean_SC, jito_rate, jito_count,
    front_gap_p50, cross_block_count, sol_avg_profit, in_block_count).
    Fields not required by the classifier are left absent to avoid silently
    carrying stale all-multi values.

    Both win rates are NET of the attacker's own fees, because both are read straight off phase 1's
    per-sandwich verdicts and those are already net:
      win_rate      mean of `is_profitable` (usd_profit_net > 0). Phase 1 stores that as a plain
                    bool, so an unpriced tokenA — NaN net profit — lands as False and is charged as
                    a loss rather than excluded; see the module docstring. `win_rate_n` is the
                    denominator and currently equals `sandwich_count`.
      sol_win_rate  mean of `is_profitable_sol`, tokenA == SOL only, where profit and fee share a
                    unit and no price is involved. Exact, and unaffected by the above.
    Both are taken from the columns phase 1 aggregates, so a multi_split sub-variant and the
    `all` variant agree by construction.
    """
    if len(ps) == 0:
        return pd.DataFrame(columns=["sandwich_count", "win_rate"])
    g = ps.groupby("signer")
    sf = pd.DataFrame({
        "sandwich_count": g.size(),
        "win_rate": g["is_profitable"].mean(),
        "win_rate_n": g["is_profitable"].count(),
        "fee_sol_total": g["fee_sol"].sum(),
        "usd_net_total": g["usd_profit_net"].sum(),
        "jito_rate": g["jito_bundle"].mean(),
        "jito_count": g["jito_bundle"].sum(),
        "front_gap_p50": g["front_gap"].median(),
        "in_block_count": (~ps["cross_block"].astype(bool)).groupby(ps["signer"]).sum(),
        "cross_block_count": ps["cross_block"].astype(bool).groupby(ps["signer"]).sum(),
    })

    def _valid_slip(s):
        v = s[(s >= 0) & (s <= 1)]
        return v.mean() if len(v) > 0 else np.nan
    scoreable = ps[~ps["sc_anomaly"].astype(bool) & ~ps["sc_unprotected"].astype(bool)
                   & ps["sc"].notna()]
    sf["mean_SC"] = scoreable.groupby("signer")["sc"].mean()
    sol_mask = ps["token_a"] == "SOL"
    if sol_mask.any():
        sol_ps = ps[sol_mask]
        sg = sol_ps.groupby("signer")
        sf["sol_avg_profit"] = sg["profit"].mean()
        sf["sol_win_rate"] = sg["is_profitable_sol"].mean()
        sf["sol_net_profit"] = (sol_ps["profit"] - sol_ps["fee_sol"]).groupby(
            sol_ps["signer"]).sum()
    else:
        sf["sol_avg_profit"] = np.nan
        sf["sol_win_rate"] = np.nan
        sf["sol_net_profit"] = np.nan
    return sf


# ── Classification ───────────────────────────────────────────────────────────

def classify_signers(sf, ps, n_min, wr_min, slip_min, fg_median_max,
                     category="standard", token_prices=None,
                     signal_usd_min=10.0,
                     oneshot_cnt_max=5, oneshot_usd_min=100.0,
                     enable_oneshot=False,
                     verified_bundle_ids=None):
    """Classify signers into Jito Bot, Signal Bot, Oneshot Bot, or Unclassified.

    Track 1 (Jito Bot): >=1 verified bundle sandwich. No count and no USD gate.
      Independent track — does NOT use the Signal Bot pre-filter pool.

    Track 2 (Signal Bot, two-stage):
      Stage 1 — pre-filter pool:
        usd_total >= signal_usd_min  (NET of the attacker's own tx fees)  AND
        sandwich_count >= n_min      AND
        win_rate >= wr_min
      Stage 2 — final attacker set within the pool:
        mean_SC >= slip_min   AND
        front_gap_p50 <= fg_median_max

    Track 2b (Oneshot Bot, multi_split only): CNT <= oneshot_cnt_max AND
      WR >= wr_min AND net USD >= oneshot_usd_min.

    `usd_total` and `win_rate` are both net of the attacker's own front-run and back-run fees;
    the thresholds are unchanged.

    Returns sf (with usd_total + fg100_ratio + pool/signal masks), tier
    series, and the four signer sets including the intermediate pool.
    """
    # Per-signer fg100 ratio
    fg100_ratio = ps.groupby("signer")["front_gap"].apply(
        lambda g: (g <= 100).mean()
    ).rename("fg100_ratio")
    sf = sf.copy()
    sf["fg100_ratio"] = fg100_ratio

    # Per-signer USD totals, NET of the attacker's own fees. `signal_usd_min` is unchanged at $10;
    # what changed is that the quantity it gates is now profit minus cost rather than a gross swap
    # difference. A signer whose gross total cleared $10 only on fee-financed ordering no longer
    # enters the Stage-1 pool, which is the point.
    usd_by_sig = net_usd_series(ps, token_prices).groupby(ps["signer"]).sum()
    sf["usd_total"] = usd_by_sig

    # Track 1 — Jito Bot, un-thresholded. `ps["jito_bundle"]` carries the same definition
    # (phase 1 reads the same CSV verdicts), so the two branches agree; the explicit id set
    # stays the default because it does not depend on the phase-1 parquet being current.
    if verified_bundle_ids is None:
        jito_sw = ps.index[ps["jito_bundle"].astype(bool)]
    else:
        jito_sw = ps.index[ps.index.isin(verified_bundle_ids)]
        from_col = set(ps.index[ps["jito_bundle"].astype(bool)])
        if from_col != set(jito_sw):
            print(f"  WARNING: phase-1 jito_bundle marks {len(from_col):,} sandwiches but the "
                  f"crawler verdicts give {len(jito_sw):,}. The verdicts win; re-run phase 1 "
                  f"to bring its column back in step.")
    jito_attackers = set(ps.loc[jito_sw, "signer"].unique())
    print(f"  Jito Bot: {len(jito_sw):,} verified bundle sandwiches -> "
          f"{len(jito_attackers):,} attackers (no threshold applied)")

    # Track 2 Stage 1 — Signal Bot pre-filter pool.
    #
    # The gate reads `sol_win_rate`, not `win_rate`: over tokenA == SOL alone, profit and fee
    # are already the same unit and no price table enters. A signer with no SOL-denominated
    # sandwich has sol_win_rate = NaN and fails the comparison. `win_rate` stays in the
    # output as a reported column.
    pool_mask = (
        (sf["usd_total"] >= signal_usd_min) &
        (sf["sandwich_count"] >= n_min) &
        (sf["sol_win_rate"] >= wr_min)
    )
    pool_signers = set(sf[pool_mask].index)

    # Track 2 Stage 2 — Signal Bot final attacker set
    signal_mask = (
        pool_mask &
        (sf["mean_SC"] >= slip_min) &
        (sf["front_gap_p50"] <= fg_median_max)
    )
    signal_attackers = set(sf[signal_mask].index)

    # Track 2b — Oneshot Bot (multi_split only)
    oneshot_attackers = set()
    if category == "multi_split" and enable_oneshot:
        oneshot_mask = (
            (sf["sandwich_count"] <= oneshot_cnt_max) &
            (sf["sol_win_rate"] >= wr_min) &
            (sf["usd_total"] >= oneshot_usd_min)
        )
        oneshot_attackers = set(sf[oneshot_mask].index)

    # Assign tiers
    tiers = {}
    for signer in sf.index:
        is_jito = signer in jito_attackers
        is_signal = signer in signal_attackers
        is_oneshot = signer in oneshot_attackers

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
    bot_attackers = jito_attackers | signal_attackers | oneshot_attackers
    return (sf, tier_series, bot_attackers, jito_attackers, signal_attackers,
            oneshot_attackers, pool_signers)


# ── Summary Report ───────────────────────────────────────────────────────────

def print_report(sf, tier_series, ps, token_prices,
                 bot_attackers, jito_attackers, signal_attackers,
                 n_min, wr_min, slip_min, fg_median_max):
    """Print detailed classification results. Every USD figure is net of the attacker's fees."""
    total_signers = len(sf)
    total_sw = int(sf["sandwich_count"].sum())

    ps_copy = ps.copy()
    ps_copy["usd_profit"] = net_usd_series(ps_copy, token_prices)
    total_usd = ps_copy["usd_profit"].sum()
    pos_usd = ps_copy.groupby("signer")["usd_profit"].sum()
    pos_total = pos_usd[pos_usd > 0].sum()
    # SOL profit is reported net too, so the SOL and USD columns of one row describe the same
    # quantity in two units rather than a net dollar figure beside a gross SOL one.
    ps_copy["sol_net"] = (ps_copy["profit"] - ps_copy["fee_sol"]).where(
        ps_copy["token_a"] == "SOL")

    print(f"\n{'='*80}")
    print(f"  SIGNER CLASSIFICATION RESULTS")
    print(f"  Parameters: N_min={n_min}, WR>={wr_min}, slip>={slip_min}, "
          f"median front_gap<={fg_median_max:g}")
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

    print(f"\n  --- Tier Breakdown (profit NET of the attacker's own tx fees) ---")
    print(f"  {'Tier':<25} {'Signers':>8} {'Sandwiches':>12} "
          f"{'Net SOL':>12} {'Net USD':>14}")
    print(f"  {'-'*75}")

    for tier in tier_order:
        signers_in_tier = set(tier_series[tier_series == tier].index)
        tier_sf = sf.loc[sf.index.isin(signers_in_tier)]
        n_sw = int(tier_sf["sandwich_count"].sum())
        tier_ps = ps_copy[ps_copy["signer"].isin(signers_in_tier)]
        sol_p = tier_ps["sol_net"].sum()
        usd_p = tier_ps["usd_profit"].sum()
        label = tier_labels.get(tier, tier)
        print(f"  {label:<25} {len(signers_in_tier):>8,} {n_sw:>12,} "
              f"{sol_p:>12,.1f} ${usd_p:>13,.0f}")

    # Bot vs Non-bot
    bot_ps = ps_copy[ps_copy["signer"].isin(bot_attackers)]
    non_bot_ps = ps_copy[~ps_copy["signer"].isin(bot_attackers)]
    bot_sf = sf.loc[sf.index.isin(bot_attackers)]
    bot_sw = int(bot_sf["sandwich_count"].sum())
    non_bot_sw = total_sw - bot_sw
    bot_usd = bot_ps["usd_profit"].sum()
    non_bot_usd = non_bot_ps["usd_profit"].sum()
    bot_sol = bot_ps["sol_net"].sum()

    # An empty population is a RESULT, not an error: `diff_signer_transfer` produces zero signers
    # above the phase-1 profit floor over 946-990, and a ZeroDivisionError here would report that
    # as a crash. Percentages of nothing are printed as `n/a` rather than 0.0 %, because 0 % would
    # claim a measurement that was never made.
    pct = lambda n, d: f"{n / d * 100:>6.2f}%" if d else "   n/a"
    pct1 = lambda n, d: f"{n / d * 100:>6.1f}%" if d else "   n/a"
    print(f"\n  --- Bot vs Non-Bot (net) ---")
    print(f"  {'':>20} {'Signers':>10} {'%':>7} {'Sandwiches':>12} {'%':>7} "
          f"{'Net SOL':>12} {'Net USD':>14}")
    print(f"  {'-'*85}")
    print(f"  {'Bot (intentional)':<20} {len(bot_attackers):>10,} "
          f"{pct(len(bot_attackers), total_signers)} {bot_sw:>12,} "
          f"{pct1(bot_sw, total_sw)} {bot_sol:>12,.1f} ${bot_usd:>13,.0f}")
    print(f"  {'Non-bot':<20} {total_signers-len(bot_attackers):>10,} "
          f"{pct(total_signers - len(bot_attackers), total_signers)} {non_bot_sw:>12,} "
          f"{pct1(non_bot_sw, total_sw)} "
          f"{non_bot_ps['sol_net'].sum():>12,.1f} "
          f"${non_bot_usd:>13,.0f}")

    print(f"\n  --- Profit Coverage (net USD) ---")
    print(f"  Total fees paid by all signers: {ps_copy['fee_sol'].sum():,.1f} SOL "
          f"(${ps_copy['fee_sol'].sum() * token_prices.get('SOL', float('nan')):,.0f})")
    print(f"  Bot USD / Total USD:    ${bot_usd:>10,.0f} / ${total_usd:>10,.0f} "
          f"({pct1(bot_usd, total_usd)})")
    print(f"  Bot USD / Positive USD: ${bot_usd:>10,.0f} / ${pos_total:>10,.0f} "
          f"({pct1(bot_usd, pos_total)})")

    # Bot signer profiles
    print(f"\n  --- Bot Attacker Profiles ---")
    if len(bot_sf) > 0:
        print(f"  Count:    median={bot_sf['sandwich_count'].median():.0f}  "
              f"mean={bot_sf['sandwich_count'].mean():.1f}  "
              f"max={bot_sf['sandwich_count'].max():.0f}")
        print(f"  Win rate: median={bot_sf['win_rate'].median():.3f}  "
              f"mean={bot_sf['win_rate'].mean():.3f}  (net, over the priceable subset)")
        if "sol_win_rate" in bot_sf.columns:
            swr = bot_sf["sol_win_rate"].dropna()
            if len(swr) > 0:
                print(f"  SOL WR:   median={swr.median():.3f}  mean={swr.mean():.3f}  "
                      f"(net, tokenA=SOL only — exact, no price involved; n={len(swr):,})")
        slip = bot_sf["mean_SC"].dropna()
        if len(slip) > 0:
            print(f"  Slippage: median={slip.median():.3f}  mean={slip.mean():.3f}")
        fg100 = bot_sf["fg100_ratio"].dropna()
        if len(fg100) > 0:
            print(f"  FG100:    median={fg100.median():.3f}  mean={fg100.mean():.3f}")

    # Top 20 bot signers
    print(f"\n  --- Top 20 Attackers (by sandwich count; USD net of fees) ---")
    bot_usd_map = ps_copy.groupby("signer")["usd_profit"].sum()
    top = bot_sf.nlargest(20, "sandwich_count")
    print(f"  {'Signer':<14} {'Tier':<16} {'CNT':>5} {'WR':>5} {'Slip':>5} "
          f"{'FG100':>5} {'NetUSD':>10}")
    print(f"  {'-'*70}")
    for signer, row in top.iterrows():
        tier = tier_series.get(signer, "?")
        usd = bot_usd_map.get(signer, 0)
        slip_val = row["mean_SC"] if pd.notna(row["mean_SC"]) else 0
        fg100_val = row.get("fg100_ratio", 0)
        print(f"  {signer[:12]:<14} {tier:<16} {int(row['sandwich_count']):>5} "
              f"{row['win_rate']:>5.2f} {slip_val:>5.3f} {fg100_val:>5.2f} "
              f"${usd:>9,.0f}")

    return bot_attackers


# ── Charts ───────────────────────────────────────────────────────────────────

def plot_wr_distribution_pre_filter(sf, chart_dir, tag, n_min, wr_min,
                                    slip_min, fg_median_max, signal_usd_min):
    """Pre-filter win-rate histogram for ALL signers in this category.

    The plot answers: "across all candidate signers (no filter applied yet),
    how do they distribute over win-rate buckets?" Both rates are net of the
    attacker's own front-run and back-run fees.

    TWO series, because only one of them is the gate. `sol_win_rate` (tokenA ==
    SOL, profit and fee in one unit, no price table) is what Stage 1 compares
    against `wr_min`, so the threshold line belongs to it; `win_rate` is drawn
    beside it as the reported all-token rate. Drawing the threshold over
    `win_rate` alone — as this did — showed a cut on a distribution the cut is
    not applied to.
    """
    fig, ax = plt.subplots(figsize=(11, 6))
    bins = np.linspace(0, 1, 11)
    bin_labels = [f"{int(bins[i]*100)}-{int(bins[i+1]*100)}%"
                  for i in range(len(bins) - 1)]
    counts, _ = np.histogram(sf["win_rate"].dropna().values, bins=bins)
    idx = np.arange(len(counts))
    bars = ax.bar(idx - 0.2, counts, width=0.4, color="#4a90d9",
                  edgecolor="black", alpha=0.85,
                  label=f"win_rate, all tokens (n={int(sf['win_rate'].notna().sum()):,})")
    for bar, c in zip(bars, counts):
        if c > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, c, f"{c:,}",
                    ha="center", va="bottom", fontsize=8)
    if "sol_win_rate" in sf.columns:
        swr = sf["sol_win_rate"].dropna()
        s_counts, _ = np.histogram(swr.values, bins=bins)
        ax.bar(idx + 0.2, s_counts, width=0.4, color="#e88a3c",
               edgecolor="black", alpha=0.85,
               label=f"sol_win_rate — THE GATE (n={len(swr):,})")
    ax.axvline(np.searchsorted(bins, wr_min) - 0.5, color="#cc0000",
               linestyle="--", linewidth=1.5,
               label=f"SOL WR threshold = {wr_min}")
    ax.set_xticks(range(len(bin_labels)))
    ax.set_xticklabels(bin_labels, rotation=30, ha="right")
    ax.set_xlabel("Win Rate Bucket (net of the attacker's own tx fees)")
    ax.set_ylabel("Number of Signers")
    ax.set_title(f"Pre-filter Win-Rate Distribution "
                 f"(N={int(sf['win_rate'].notna().sum()):,} signers)\n"
                 f"Signal Bot gates: CNT>={n_min}, WR>={wr_min}, "
                 f"slip>={slip_min}, median front_gap<={fg_median_max:g}, "
                 f"net USD>=${signal_usd_min:.0f}")
    # Upper CENTRE, not upper left: the 0-10% bucket is the tallest bar by an order of magnitude
    # and a left-anchored legend covers its count label.
    ax.legend(loc="upper center", framealpha=0.9)
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
                           threshold, signal_min, n_min, wr_min):
    """Bar plot of avg net USD/signer per signal bin within the pre-filter pool.

    Pool: signers who already pass `usd_total>=signal_min AND CNT>=n_min
    AND sol_win_rate>=wr_min` — `usd_total` net of the attacker's own fees.
    Bin width = 0.05 (20 bins over [0, 1]), matching the Phase-2
    threshold-analysis convention in docs/evaluator_design.md §5.2.

    `n_min` and `wr_min` are passed in so the caption always names the pool the chart was
    drawn from.
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
    ax.set_ylabel("Avg Net USD Profit per Signer")
    ax.set_title(f"Avg Net USD/Signer by {signal_label} "
                 f"(Pool: net USD>=${signal_min:.0f}, CNT>={n_min}, "
                 f"SOL WR>={wr_min} — N={len(sub):,} signers)")
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


def print_pool_signal_stats(pool_sf, slip_min, fg_median_max):
    """Mean/median/distribution stats for slippage and fg100 in the pool."""
    print("\n  --- Stage-1 Pool Signal Stats ---")
    print(f"  Pool size: {len(pool_sf):,} signers "
          f"(passed USD>=signal_usd_min, CNT>=n_min, WR>=wr_min)")

    for col, label, thr in [("mean_SC", "Mean Slippage Consumption", slip_min),
                            ("front_gap_p50", "median front_gap", fg_median_max)]:
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
    ax.set_ylabel("Net USD Total Profit (symlog)")
    ax.set_title(f"Final Attackers: Net USD Profit vs Sandwich Count "
                 f"(N={len(df):,} signers, total ${df['usd'].sum():,.0f})")
    ax.legend()
    ax.grid(True, alpha=0.3, which="both")
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"attacker_profit_cnt_{tag}.png"),
                dpi=150, bbox_inches="tight")
    plt.close()


def generate_pre_filter_charts(sf, chart_dir, tag, n_min, wr_min,
                               slip_min, fg_median_max, signal_usd_min):
    """Pre-filter diagnostic charts (Phase-3 entry, before any track applies).

    Produces:
      1. Pre-filter WR distribution over all signers in the category.
      2. Avg USD/signer by mean_SC bin, restricted to the pool
         {USD>=signal_usd_min, CNT>=n_min, WR>=wr_min}.
      3. Avg USD/signer by P(fg<=100) bin, same pool.
    """
    plot_wr_distribution_pre_filter(sf, chart_dir, tag, n_min, wr_min,
                                    slip_min, fg_median_max, signal_usd_min)

    # Same pool as classify_signers: sol_win_rate, so the charts describe the set the gate
    # actually selects from rather than a differently-defined one.
    pool_mask = (
        (sf["usd_total"] >= signal_usd_min) &
        (sf["sandwich_count"] >= n_min) &
        (sf["sol_win_rate"] >= wr_min)
    )
    pool = sf[pool_mask].copy()
    if len(pool) == 0:
        print(f"  [pre-filter charts] empty pool — skipping slip/fg100 plots")
        return

    plot_avg_usd_by_signal(
        pool, chart_dir, tag,
        signal_col="mean_SC", signal_label="Mean Slippage Consumption",
        threshold=slip_min, signal_min=signal_usd_min,
        n_min=n_min, wr_min=wr_min,
    )
    plot_avg_usd_by_signal(
        pool, chart_dir, tag,
        signal_col="fg100_ratio", signal_label="P(fg <= 100)",
        threshold=fg_median_max, signal_min=signal_usd_min,
        n_min=n_min, wr_min=wr_min,
    )

    # Stage-1 pool signal-distribution histograms (mean/median/threshold lines)
    plot_signal_distribution(
        pool, chart_dir, tag,
        signal_col="mean_SC",
        signal_label="Mean Slippage Consumption",
        threshold=slip_min,
    )
    plot_signal_distribution(
        pool, chart_dir, tag,
        signal_col="fg100_ratio",
        signal_label="P(fg <= 100)",
        threshold=fg_median_max,
    )


def generate_charts(sf, ps, tier_series, bot_attackers, token_prices, chart_dir, tag,
                    pool_signers=None, signal_attackers=None,
                    slip_min=0.90, fg_median_max=75.0):
    """Generate classification summary charts. USD is net of the attacker's own fees."""
    ps_copy = ps.copy()
    ps_copy["usd_profit"] = net_usd_series(ps_copy, token_prices)
    usd_by_sig = ps_copy.groupby("signer")["usd_profit"].sum()

    bot_sf = sf.loc[sf.index.isin(bot_attackers)].copy()
    non_bot_sf = sf.loc[~sf.index.isin(bot_attackers)].copy()

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # 1. WR distribution
    ax = axes[0, 0]
    ax.hist(non_bot_sf["win_rate"].dropna(), bins=20, alpha=0.5,
            label=f"Non-bot ({len(non_bot_sf):,})", color="gray", edgecolor="black")
    ax.hist(bot_sf["win_rate"].dropna(), bins=20, alpha=0.7,
            label=f"Bot ({len(bot_sf):,})", color="red", edgecolor="black")
    ax.set_xlabel("Win Rate (net of attacker fees)")
    ax.set_ylabel("Signers")
    ax.set_title("Win Rate: Bot vs Non-bot")
    ax.legend()

    # 2. Slippage distribution
    ax = axes[0, 1]
    ax.hist(non_bot_sf["mean_SC"].dropna(), bins=20, alpha=0.5,
            label="Non-bot", color="gray", edgecolor="black")
    ax.hist(bot_sf["mean_SC"].dropna(), bins=20, alpha=0.7,
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
    bot_usd_vals = usd_by_sig.loc[usd_by_sig.index.isin(bot_attackers)].dropna()
    non_bot_usd_vals = usd_by_sig.loc[~usd_by_sig.index.isin(bot_attackers)].dropna()
    ax.hist(non_bot_usd_vals.clip(-500, 5000), bins=50, alpha=0.5,
            label=f"Non-bot (${non_bot_usd_vals.sum()/1000:.0f}K)", color="gray")
    ax.hist(bot_usd_vals.clip(-500, 5000), bins=50, alpha=0.7,
            label=f"Bot (${bot_usd_vals.sum()/1000:.0f}K)", color="red")
    ax.set_xlabel("Net USD Profit per Signer (clipped)")
    ax.set_ylabel("Signers")
    ax.set_title("Net USD Profit Distribution: Bot vs Non-bot")
    ax.legend()

    plt.suptitle("Signer Classification Results", fontsize=14)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"classification_summary_{tag}.png"), dpi=150)
    plt.close()

    # 5/6. Scatters over the Stage-1 pool, with Stage-2 (slip+fg100) cuts.
    # Population restricted to the pool so the upper-right region (passed
    # Stage-2) contains exclusively Signal Bots — no non-bot leakage.
    if pool_signers is None or signal_attackers is None:
        return  # backward-compatibility safeguard

    pool_sf = sf.loc[sf.index.isin(pool_signers) &
                     sf["mean_SC"].notna() &
                     sf["fg100_ratio"].notna()].copy()
    if len(pool_sf) == 0:
        return
    pool_sf["usd"] = usd_by_sig
    pool_sf["passed_stage2"] = pool_sf.index.isin(signal_attackers)
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
        ax.add_patch(Rectangle((slip_min, 0),
                               1.02 - slip_min, fg_median_max,
                               facecolor="white", edgecolor="none", zorder=0))

        f_sorted = (failed.sort_values("sandwich_count", ascending=False)
                    if len(failed) > 0 else failed)
        p_sorted = passed.sort_values("sandwich_count", ascending=False)

        if len(f_sorted) > 0:
            ax.scatter(_jitter(f_sorted["mean_SC"]),
                       _jitter(f_sorted["fg100_ratio"]),
                       s=np.clip(f_sorted["sandwich_count"] / 5, 6, 35),
                       c="#4a90d9", alpha=0.35, edgecolors="#2a5a9a",
                       linewidths=0.3, zorder=2,
                       label=f"Failed Stage-2 (n={len(failed):,})")

        sc = ax.scatter(_jitter(p_sorted["mean_SC"]),
                        _jitter(p_sorted["fg100_ratio"]),
                        s=np.clip(p_sorted["sandwich_count"] / 5, 18, 140),
                        c=p_sorted[color_col], cmap="YlOrRd", alpha=0.85,
                        vmin=vmin, vmax=vmax,
                        edgecolors="black", linewidths=0.5, zorder=3,
                        label=f"Signal Bot (n={len(p_sorted):,})")

        ax.axvline(slip_min, color="#cc0000", linestyle="--", linewidth=1.8,
                   alpha=0.8, label=f"Slip threshold = {slip_min}")
        ax.axhline(fg_median_max, color="#cc0000", linestyle="--", linewidth=1.8,
                   alpha=0.8, label=f"median front_gap threshold = {fg_median_max:g}")

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

    # Pool requires net usd_total >= $10, so log10 starts at 1.
    pool_sf["log_usd"] = np.log10(pool_sf["usd"].clip(10))
    passed = pool_sf[pool_sf["passed_stage2"]]
    failed = pool_sf[~pool_sf["passed_stage2"]]
    _draw_scatter("log_usd", "log$_{10}$(net USD profit)", 1, 5,
                  f"scatter_slip_fg100_{tag}.png")

    # Avg net USD per sandwich is positive within the pool. Floor at $1
    # (log10=0) to keep the scale non-negative; cap at $100 (log10=2)
    # because >99% of pool members sit in $1-$100, so a wider range
    # would push the bulk of colours into the pale-yellow end.
    pool_sf["avg_usd"] = pool_sf["usd"] / pool_sf["sandwich_count"]
    pool_sf["log_avg_usd"] = np.log10(pool_sf["avg_usd"].clip(1))
    passed = pool_sf[pool_sf["passed_stage2"]]
    failed = pool_sf[~pool_sf["passed_stage2"]]
    _draw_scatter("log_avg_usd", "log$_{10}$(avg net USD per sandwich)",
                  0, 2, f"scatter_slip_fg100_avgprofit_{tag}.png")



# ── Leader geometry per attacker ─────────────────────────────────────────────

def population_all_front_by_nv(all_ps_geo):
    """P(all victims under the front's leader | victim count), over the WHOLE population.

    The standardization baseline. Without it, `xl_all_victims_front_leader_share_of_xl` reads as a
    behavioural difference when it is mostly a difference in victims per sandwich.
    """
    xl = all_ps_geo[all_ps_geo["geom_class"] == "cross_leader"]
    nv = xl["n_victims"].fillna(0).astype(int).clip(upper=12)
    return xl["xl_all_victims_front_leader"].groupby(nv).mean()


def build_geometry_table(bot_ps, usd_col="usd", pop_all_front_by_nv=None,
                         rot_idx=None, rot=None):
    """One row per attacker: how its sandwiches sit relative to the block producers.

    Three shapes partition every sandwich — `single_slot` (front, victims and back all in one
    block), `same_leader_multi_slot` (spread over blocks, one producer) and `cross_leader` (the
    producer changes between the front and the back).

    Within the cross-leader slice, five sub-shapes are reported because they carry different
    evidence about whether a front-run happened at all. Placing a transaction immediately before a
    victim needs ordering influence inside the block holding that victim, so:

      xl_all_victims_front_leader    every victim is under the front-run's own producer, and only
                                     the back-run landed after the rotation — the shape a genuine
                                     sandwich takes when it straddles a leader boundary
      xl_first_victim_front_leader   at least the EARLIEST victim is; a weaker version of the above
                                     that survives a sandwich sweeping up later trades
      xl_no_victim_front_leader      not one victim shares the front-run's producer, so the attacker
                                     would have had to place its front-run before the next producer
                                     began building — mechanically implausible
      xl_single_victim_same_leader   the unambiguous case: one victim, same producer
      xl_single_victim_diff_leader   one victim, different producer. With a single victim there is
                                     no sweeping-up explanation, so the ratio between these last two
                                     is the sharpest read on whether the pairing is causal

    The first three partition the cross-leader population; the last two are its single-victim slice,
    and `xl_first_victim_front_leader` deliberately overlaps the first.
    """
    rows = {}
    for signer, sub in bot_ps.groupby("signer", sort=False):
        rows[signer] = geometry_profile(sub, usd_col=usd_col,
                                        pop_all_front_by_nv=pop_all_front_by_nv,
                                        rot_idx=rot_idx, rot=rot)
    g = pd.DataFrame.from_dict(rows, orient="index")
    g.index.name = "attacker"
    return g


def print_geometry_report(geom_sf):
    """Population-level view of the same breakdown."""
    n = len(geom_sf)
    if not n:
        return
    tot = geom_sf["sandwich_count"].sum()
    print(f"\n--- Sandwich geometry over {n:,} attackers, {int(tot):,} sandwiches ---")
    print(f"  {'shape':<26}{'count':>12}{'share':>9}{'USD':>14}{'USD/sw':>10}")
    for cls in ("single_slot", "same_leader_multi_slot", "cross_leader"):
        k = int(geom_sf[f"{cls}_n"].sum())
        u = float(geom_sf[f"{cls}_usd"].sum())
        print(f"  {cls:<26}{k:>12,}{k / tot:>9.1%}{u:>14,.0f}{(u / k if k else 0):>10.2f}")
    xl = int(geom_sf["cross_leader_n"].sum())
    if not xl:
        return
    print(f"\n  within the {xl:,} cross-leader sandwiches:")
    print(f"  {'sub-shape':<32}{'count':>12}{'of XL':>9}{'USD':>14}{'USD/sw':>10}")
    for cls in XL_CLASSES:
        k = int(geom_sf[f"{cls}_n"].sum())
        u = float(geom_sf[f"{cls}_usd"].sum())
        print(f"  {cls:<32}{k:>12,}{k / xl:>9.1%}{u:>14,.0f}{(u / k if k else 0):>10.2f}")
    single = (int(geom_sf["xl_single_victim_same_leader_n"].sum())
              + int(geom_sf["xl_single_victim_diff_leader_n"].sum()))
    if single:
        same = int(geom_sf["xl_single_victim_same_leader_n"].sum())
        print(f"\n  single-victim cross-leader sandwiches: {single:,}; the victim shares the "
              f"front-run's producer in {same:,} ({same / single:.1%}).")
        print("  Read that against the null, not against 100%: a detection window is exactly two "
              "leader\n  rotations, so the victim is under the front's or the back's producer by "
              "construction, and\n  uniform placement in a 4+4 window predicts 50.0%. The pooled "
              "rate is uninformative; what\n  carries signal is the two columns below.")
    if "boundary_suppression" in geom_sf.columns:
        obs = float(geom_sf["xl_observed"].sum())
        exp = float(geom_sf["xl_expected_if_arbitrary"].sum())
        vib = (float(geom_sf["victim_in_front_block_n"].sum())
               / max(float(geom_sf["cross_leader_n"].sum()
                           + geom_sf["same_leader_multi_slot_n"].sum()
                           + geom_sf["single_slot_n"].sum()), 1))
        print(f"\n  boundary suppression (cross-block sandwiches): observed {obs:,.0f} "
              f"cross-leader vs {exp:,.0f} expected if the front-run were placed arbitrarily in "
              f"its own\n  rotation — ratio {obs / exp if exp else float('nan'):.3f}. "
              f"Below 1 means the leader boundary acts as a barrier.")
        med = geom_sf["xl_all_front_vs_expected"].median()
        print(f"  all-victims-under-front, standardized on victim count: median {med:.2f}x "
              f"expected across {int(geom_sf['xl_all_front_vs_expected'].notna().sum()):,} attackers")


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


# One label per category for the merged CSV. Kept separate from CATEGORIES so the file a reader
# opens says `multi-split`, not `multi_split`, and so renaming a directory cannot silently rename a
# published column value.
EXPERT_AUDIT_PATH = "data/expert_audit/results/cases_summary.csv"
# The audit that produced these verdicts. Recorded so a run on a different window cannot silently
# inherit labels formed on another population; the tag is checked, not assumed.
EXPERT_AUDIT_TAG = "946_990_cl-include"


def annotate_expert_audit(bot_sf, tag, path=EXPERT_AUDIT_PATH):
    """Add the blinded expert panel's verdict for each attacker, as an ANNOTATION not a filter.

    Reviewer pairs plus an adjudicator judged every Signal Bot of the audited run against a
    codebook that never sees a gate value, mixed blind with entities the gates had rejected.
    Entities labelled `no` stay in the table; applying the verdict is a downstream decision.

    Values:
      yes / no / ambiguous   the adjudicator's final label
      not_audited            Jito Bots, which were excluded by design: they are admitted on
                             deterministic bundle evidence, so there is no statistical claim to test
      no_audit_for_tag       this run's window is not the audited one
    """
    if "expert_verdict" in bot_sf.columns:
        return bot_sf
    if tag != EXPERT_AUDIT_TAG or not os.path.exists(path):
        bot_sf["expert_verdict"] = "no_audit_for_tag"
        bot_sf["expert_case_id"] = pd.NA
        why = (f"tag {tag} != audited {EXPERT_AUDIT_TAG}" if tag != EXPERT_AUDIT_TAG
               else f"missing {path}")
        print(f"\n  Expert audit: not applied ({why})")
        return bot_sf

    a = pd.read_csv(path)
    a = a[a["selected"].astype(bool)].set_index("address")
    idx = bot_sf.index.astype(str)
    bot_sf["expert_verdict"] = pd.Series(
        a["adjudicator"].reindex(idx).to_numpy(), index=bot_sf.index)
    bot_sf["expert_case_id"] = pd.Series(
        a["case_id"].reindex(idx).to_numpy(), index=bot_sf.index)
    # A Jito Bot has no verdict because it was never audited; that is different from having been
    # audited and passed, and the two must not share a label.
    if "bot_type" in bot_sf.columns:
        miss = bot_sf["expert_verdict"].isna()
        jito = bot_sf["bot_type"].astype(str).str.startswith("Jito Bot") & ~bot_sf[
            "bot_type"].astype(str).str.contains("Signal")
        bot_sf.loc[miss & jito, "expert_verdict"] = "not_audited"
    n_no = int((bot_sf["expert_verdict"] == "no").sum())
    n_amb = int((bot_sf["expert_verdict"] == "ambiguous").sum())
    n_yes = int((bot_sf["expert_verdict"] == "yes").sum())
    n_na = int(bot_sf["expert_verdict"].isna().sum())
    print(f"\n  Expert audit ({path}): yes {n_yes:,} · no {n_no:,} · ambiguous {n_amb:,} · "
          f"not audited {int((bot_sf['expert_verdict'] == 'not_audited').sum()):,}")
    if n_no:
        usd = bot_sf.loc[bot_sf["expert_verdict"] == "no", "usd_net_total"].sum() \
            if "usd_net_total" in bot_sf.columns else float("nan")
        sw = bot_sf.loc[bot_sf["expert_verdict"] == "no", "sandwich_count"].sum() \
            if "sandwich_count" in bot_sf.columns else 0
        print(f"     rejected by the panel, RETAINED in this table: {n_no} attackers, "
              f"{int(sw):,} sandwiches, ${usd:,.0f}")
        for x in bot_sf.index[bot_sf["expert_verdict"] == "no"]:
            print(f"       {x}")
    if n_na:
        print(f"     WARNING: {n_na} attacker(s) carry no verdict and are not Jito Bots — the audit "
              f"predates them. Re-run the audit before quoting a precision figure.")
    return bot_sf


ATTACK_TYPE_LABEL = {
    "standard": "standard",
    "multi_split": "multi-split",
    "diff_signer_owner": "diff_signer_owner",
    "diff_signer_transfer": "diff_signer_transfer",
}
# Which bot_type wins when an address is one thing in one category and another elsewhere: neither.
# Both are recorded, because "this address bought bundles for its multi-split sandwiches and cleared
# the statistical gates for its standard ones" is a fact about the attacker, not a conflict.
_BOT_TYPE_ATOMS = {"Jito Bot": "jito", "Signal Bot": "signal",
                   "Jito Bot + Signal Bot": "jito+signal",
                   "Oneshot Bot": "oneshot", "Signal Bot + Oneshot Bot": "signal+oneshot"}


def write_merged_attackers(database, cross_leader, tag, out_root="data/3_attacker_filter"):
    """One row per ADDRESS across all four categories, with bot_type and attack_type.

    Each category filters its own population independently, so the same address can be selected in
    more than one and appears once per category in the per-category files. This collapses them:
    counts and dollars are summed, the behavioural metrics are taken from the category holding most
    of that address's sandwiches (named in `primary_category`), because averaging a win rate across
    populations of different sizes would invent a number that describes none of them.

    Writes whatever categories exist and names the ones it could not find, rather than failing --
    phase 3 runs once per category, so the first three runs necessarily see an incomplete set.
    """
    frames, missing = [], []
    for cat in CATEGORIES:
        p = os.path.join(out_root, cat, database, cross_leader,
                         f"bot_attackers_{tag}.parquet")
        if not os.path.exists(p):
            missing.append(cat)
            continue
        d = pd.read_parquet(p)
        if len(d) == 0:
            continue
        d = d.reset_index().rename(columns={d.index.name or "index": "attacker"})
        d["category"] = cat
        frames.append(d)
    if not frames:
        print(f"\nMerged CSV: no category outputs found for {database}/{cross_leader}/{tag}")
        return None

    A = pd.concat(frames, ignore_index=True)
    num = lambda c: pd.to_numeric(A[c], errors="coerce") if c in A.columns else pd.Series(
        np.nan, index=A.index)
    A["_sw"] = num("sandwich_count").fillna(0)
    A["_usd"] = num("usd_net_total").fillna(0.0)
    A["_sol"] = num("sol_net_profit").fillna(0.0)
    A["_fee"] = num("fee_sol_total").fillna(0.0)

    # Primary = most sandwiches; ties broken by CATEGORIES order so the result is deterministic.
    A["_catrank"] = A["category"].map({c: i for i, c in enumerate(CATEGORIES)})
    prim = A.sort_values(["_sw", "_catrank"], ascending=[False, True]).drop_duplicates("attacker")
    prim = prim.set_index("attacker")

    g = A.groupby("attacker", sort=True)
    atoms = g["bot_type"].agg(
        lambda s: sorted({a for v in s for a in _BOT_TYPE_ATOMS.get(v, str(v)).split("+")}))
    types = g["category"].agg(
        lambda s: "+".join(ATTACK_TYPE_LABEL.get(c, c)
                           for c in sorted(set(s), key=CATEGORIES.index)))

    out = pd.DataFrame({
        "bot_type": atoms.map("+".join),
        # Taken from the primary-category row rather than recombined: the verdict is about the
        # ADDRESS, so it is the same in every category it appears in, and joining it per category
        # would invent a disagreement that cannot exist.
        "expert_verdict": prim["expert_verdict"] if "expert_verdict" in prim.columns else "n/a",
        "expert_case_id": prim["expert_case_id"] if "expert_case_id" in prim.columns else pd.NA,
        "attack_type": types,
        "n_categories": g["category"].nunique(),
        "primary_category": prim["category"],
        "sandwich_count": g["_sw"].sum().astype("int64"),
        "usd_net_total": g["_usd"].sum(),
        "sol_net_profit": g["_sol"].sum(),
        "fee_sol_total": g["_fee"].sum(),
    })
    for c in ("sol_win_rate", "win_rate", "win_rate_n", "mean_SC", "sc_coverage",
              "front_gap_p50", "first_ts", "last_ts", "jito_count"):
        if c in prim.columns:
            out[c] = prim[c]
    for cat in CATEGORIES:
        sub = A[A["category"] == cat].set_index("attacker")
        out[f"sw_{ATTACK_TYPE_LABEL[cat]}"] = sub["_sw"].reindex(out.index).fillna(0).astype("int64")
        out[f"usd_{ATTACK_TYPE_LABEL[cat]}"] = sub["_usd"].reindex(out.index).fillna(0.0)
    out.index.name = "attacker"
    out = out.sort_values("usd_net_total", ascending=False)

    d = os.path.join(out_root, "_merged", database, cross_leader)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"all_attackers_{tag}.csv")
    out.to_csv(path)
    out.to_parquet(os.path.join(d, f"all_attackers_{tag}.parquet"))

    print(f"\nMerged attacker list -> {path}")
    print(f"  {len(out):,} distinct addresses "
          f"({int((out['n_categories'] > 1).sum()):,} appear in more than one category)")
    if missing:
        print(f"  NOT INCLUDED (no phase-3 output yet): {', '.join(missing)}")
    print(f"  by bot_type:    " + ", ".join(
        f"{k}={v:,}" for k, v in out["bot_type"].value_counts().items()))
    print(f"  by attack_type: " + ", ".join(
        f"{k}={v:,}" for k, v in out["attack_type"].value_counts().items()))
    print(f"  net USD total:  ${out['usd_net_total'].sum():,.0f}   "
          f"sandwiches: {int(out['sandwich_count'].sum()):,}")
    return out


def main():
    args = parse_args()
    tag = tag_for(args.start_epoch, args.end_epoch, args.cross_leader)
    category = args.category
    variant = args.multi_variant

    # Output is keyed by database AND cross-leader variant: the two v2 runs answer different
    # questions and must not overwrite each other.
    base = f"data/3_attacker_filter/{category}/{args.database}/{args.cross_leader}"
    out_dir = f"{base}/{variant}" if (category == "multi_split" and variant != "all") else base
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)

    n_min = args.n_min
    wr_min = args.wr_min
    slip_min = args.slip_min
    fg_median_max = args.fg_median_max
    signal_usd_min = args.signal_usd_min
    oneshot_cnt_max = args.oneshot_cnt_max
    oneshot_usd_min = args.oneshot_usd_min

    print(f"=== Phase 3: Attacker Filter ===")
    print(f"Database: {args.database}   cross-leader: {args.cross_leader}   tag: {tag}")
    print(f"Category: {category}" +
          (f" / variant={variant}" if category == "multi_split" else ""))
    print(f"Signal Bot gates: CNT>={n_min}, WR>={wr_min}, slip>={slip_min}, "
          f"median front_gap<={fg_median_max:g}, net USD>=${signal_usd_min:.0f}  (WR and USD are both net of "
          f"the attacker's own front-run + back-run fees)")
    print(f"Jito Bot track: >=1 verified bundle sandwich (one bundleId holds front+victim+back, "
          f"signerSame, profitA>0) — no count and no USD threshold")
    if category == "multi_split":
        print(f"Oneshot track: " + (f"CNT<={oneshot_cnt_max} AND WR>={wr_min} "
              f"AND net USD>=${oneshot_usd_min:.0f}" if args.oneshot else "DISABLED (--oneshot to enable)"))
    print(f"Epoch range: {args.start_epoch}-{args.end_epoch}")

    # Load data
    print(f"\nLoading data...")
    ps, sf = load_phase1(category, args.database, tag)
    token_prices = load_token_prices()
    verified_bundle_ids = verified_bundle_sandwiches(
        args.database, args.start_epoch, args.end_epoch)
    print(f"  Verified bundle sandwiches in range: {len(verified_bundle_ids):,}")
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
    (sf, tier_series, bot_attackers, jito_attackers, signal_attackers,
     oneshot_attackers, pool_signers) = classify_signers(
        sf, ps, n_min, wr_min, slip_min, fg_median_max,
        category=category, token_prices=token_prices,
        signal_usd_min=signal_usd_min,
        oneshot_cnt_max=oneshot_cnt_max, oneshot_usd_min=oneshot_usd_min,
        enable_oneshot=args.oneshot,
        verified_bundle_ids=verified_bundle_ids)

    pool_sf = sf.loc[sf.index.isin(pool_signers)].copy()
    print(f"\nStage-1 pool: {len(pool_sf):,} signers "
          f"(net USD>=${signal_usd_min:.0f} AND CNT>={n_min} AND WR>={wr_min})")
    print_pool_signal_stats(pool_sf, slip_min, fg_median_max)
    pool_sf.to_csv(f"{out_dir}/pool_signers_{tag}.csv")

    # Pre-filter diagnostic charts (WR + per-bin avg-USD + pool distributions)
    print(f"\nGenerating pre-filter diagnostic charts...")
    generate_pre_filter_charts(sf, chart_dir, tag, n_min, wr_min,
                               slip_min, fg_median_max, signal_usd_min)

    # Final attacker scatter (Signal Bot final set after Stage-2 filter)
    final_attacker_sf = sf.loc[sf.index.isin(signal_attackers)]
    plot_attacker_profit_cnt_scatter(final_attacker_sf, chart_dir, tag, n_min)

    # Report
    print_report(sf, tier_series, ps, token_prices,
                 bot_attackers, jito_attackers, signal_attackers,
                 n_min, wr_min, slip_min, fg_median_max)

    if category == "multi_split":
        print(f"\n  --- Multi-split Track Breakdown ---")
        print(f"  Signal Bot only:  {len(signal_attackers - oneshot_attackers):>4}")
        print(f"  Oneshot only:     {len(oneshot_attackers - signal_attackers):>4}")
        print(f"  Both tracks:      {len(signal_attackers & oneshot_attackers):>4}")
        print(f"  Bot total:        {len(bot_attackers):>4}")

    # Save outputs
    print(f"\nSaving outputs to {out_dir}/...")

    bot_sf = sf.loc[sf.index.isin(bot_attackers)].copy()
    bot_sf["tier"] = tier_series.loc[bot_sf.index]
    # `bot_type` names the track that admitted the attacker; `tier` is the same information
    # in the pipeline's internal vocabulary.
    #   Jito Bot    bought atomic inclusion for a front/victim/back triple. No threshold and
    #               no profit floor, so a Jito Bot may be net-negative overall.
    #   Signal Bot  cleared the count, SOL win rate, slippage-consumption and front-gap gates.
    BOT_TYPE = {"jito_only": "Jito Bot",
                "signal": "Signal Bot",
                "jito_and_signal": "Jito Bot + Signal Bot",
                "oneshot": "Oneshot Bot",
                "signal_and_oneshot": "Signal Bot + Oneshot Bot"}
    bot_sf["bot_type"] = bot_sf["tier"].map(BOT_TYPE)
    unmapped = bot_sf["bot_type"].isna()
    if unmapped.any():
        raise SystemExit(f"unmapped tier(s): {sorted(bot_sf.loc[unmapped, 'tier'].unique())}")
    # Front, so it is the first thing seen in the CSV rather than column 113.
    bot_sf = bot_sf[["bot_type"] + [c for c in bot_sf.columns if c != "bot_type"]]
    bot_ps = ps[ps["signer"].isin(bot_attackers)].copy()

    # USD profit columns (total, sol_part, nonsol_part, price coverage), all net of the
    # attacker's own front-run and back-run fees. The SOL / non-SOL split shows where a
    # signer's dollar impact comes from and flags low price coverage.
    ps_priced = ps.copy()
    ps_priced["usd_profit"] = net_usd_series(ps_priced, token_prices)
    ps_priced["is_sol"] = ps_priced["token_a"] == "SOL"
    bot_priced = ps_priced[ps_priced["signer"].isin(bot_attackers)]

    usd_by_signer = bot_priced.groupby("signer").agg(
        usd_total_profit=("usd_profit", "sum"),
        usd_avg_profit=("usd_profit", "mean"),
        fee_sol_paid=("fee_sol", "sum"),
    )
    # Gross SOL is kept beside the net one; the difference between the two columns is the fee.
    sol_grp = bot_priced[bot_priced["is_sol"]].groupby("signer")
    sol_profit = sol_grp["profit"].sum()
    sol_net = (bot_priced.loc[bot_priced["is_sol"], "profit"]
               - bot_priced.loc[bot_priced["is_sol"], "fee_sol"]).groupby(
                   bot_priced.loc[bot_priced["is_sol"], "signer"]).sum()
    usd_by_signer["sol_total_profit"] = sol_profit.reindex(usd_by_signer.index).fillna(0)
    usd_by_signer["sol_total_profit_net"] = sol_net.reindex(usd_by_signer.index).fillna(0)

    # The split is taken from the per-sandwich net series rather than by pricing a SOL subtotal, so
    # the two parts add up to usd_total_profit exactly — a non-SOL leg's fee is a SOL cost, and
    # pricing the SOL subtotal separately would have double-counted it into the SOL part.
    part = bot_priced.groupby(["signer", "is_sol"])["usd_profit"].sum().unstack(fill_value=0.0)
    usd_by_signer["usd_profit_sol_part"] = (
        part[True].reindex(usd_by_signer.index).fillna(0.0) if True in part.columns else 0.0)
    usd_by_signer["usd_profit_nonsol_part"] = (
        part[False].reindex(usd_by_signer.index).fillna(0.0) if False in part.columns else 0.0)

    # NAME COLLISION: phase 1 writes a column of this name covering ALL sandwiches; this one
    # is restricted to the NON-SOL subset. The two are not interchangeable, and this one is
    # NOT the signer's price coverage — for that use `win_rate_n / sandwich_count`.
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
    bot_sf.index.name = "attacker"
    bot_sf = annotate_expert_audit(bot_sf, tag)
    # Front, next to bot_type, so a reader sees the verdict without scrolling past 100 columns.
    lead = [c for c in ("bot_type", "expert_verdict", "expert_case_id") if c in bot_sf.columns]
    bot_sf = bot_sf[lead + [c for c in bot_sf.columns if c not in lead]]


    # `usd_net_total` is the same quantity as `usd_total_profit` above, kept under phase 1's
    # name; `sol_win_rate` is the win rate that needs no price table. Both come from phase 1
    # on the `all` path and from `_rebuild_signer_features` on a multi_split sub-variant, so
    # the fallbacks below only fire for an sf carrying neither.
    if "usd_net_total" not in bot_sf.columns:
        bot_sf["usd_net_total"] = bot_priced.groupby("signer")["usd_profit"].sum()
    if "sol_win_rate" not in bot_sf.columns:
        bot_sf["sol_win_rate"] = bot_priced[bot_priced["is_sol"]].groupby(
            "signer")["is_profitable_sol"].mean()

    # ── Leader geometry ──────────────────────────────────────────────────
    if not args.no_geometry:
        print(f"\nJoining leader geometry ({args.database} {args.start_epoch}-{args.end_epoch})...")
        geom = leader_geometry(args.database, args.start_epoch, args.end_epoch)
        rot = leader_rotations(args.database, args.start_epoch, args.end_epoch)
        rot_idx = rotation_index(rot)
        # The standardization baseline comes from the WHOLE population, not from the attackers.
        all_geo = add_geometry_classes(ps.set_index("sandwichId")
                                       if ps.index.name != "sandwichId" else ps, geom)
        pop_nv = population_all_front_by_nv(all_geo)
        bot_ps_geo = add_geometry_classes(bot_ps.set_index("sandwichId")
                                          if bot_ps.index.name != "sandwichId" else bot_ps, geom)
        # Net, like every other USD column here, so the geometry table's `*_usd` fields and the
        # tier table describe the same quantity.
        bot_ps_geo["usd"] = net_usd_series(bot_ps_geo, token_prices)
        geom_sf = build_geometry_table(bot_ps_geo, pop_all_front_by_nv=pop_nv,
                                       rot_idx=rot_idx, rot=rot)
        # `errors="ignore"`: an empty geometry table has no columns at all, so dropping by name
        # raises rather than yielding an empty frame. Reached by multi_split/exclude over 946-990,
        # where no attacker survives the gates and geom_sf comes back with zero rows.
        bot_sf = bot_sf.join(
            geom_sf.drop(columns=["sandwich_count"], errors="ignore"), how="left")
        print_geometry_report(geom_sf)
        geom_sf.to_csv(f"{out_dir}/attacker_geometry_{tag}.csv")
        bot_ps_geo.to_parquet(f"{out_dir}/bot_sandwiches_geom_{tag}.parquet")

    bot_sf.to_csv(f"{out_dir}/bot_attackers_{tag}.csv")
    bot_sf.to_parquet(f"{out_dir}/bot_attackers_{tag}.parquet")
    bot_ps.to_parquet(f"{out_dir}/bot_sandwiches_{tag}.parquet")

    tier_df = pd.DataFrame({"tier": tier_series})
    tier_df.to_csv(f"{out_dir}/all_signer_tiers_{tag}.csv")

    print(f"  bot_attackers_{tag}.csv/parquet     ({len(bot_sf):,} signers)")
    print(f"  bot_sandwiches_{tag}.parquet       ({len(bot_ps):,} sandwiches)")
    print(f"  all_signer_tiers_{tag}.csv         ({len(tier_df):,} signers)")

    # Charts
    print(f"\nGenerating charts...")
    generate_charts(sf, ps, tier_series, bot_attackers, token_prices, chart_dir, tag,
                    pool_signers=pool_signers, signal_attackers=signal_attackers,
                    slip_min=slip_min, fg_median_max=fg_median_max)
    print(f"  Charts saved to {chart_dir}/")

    # Cross-category merge. Runs after every category so the file exists as soon as the last one
    # lands; the multi_split sub-variants are not merged (they are alternative views
    # of one population, and summing them would double-count).
    if not (category == "multi_split" and variant != "all"):
        write_merged_attackers(args.database, args.cross_leader, tag)

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
