"""
Phase 2: Parameter Selection via Statistical Hypothesis Testing
================================================================
Determine classification parameters (N_min, alpha) using hypothesis testing
rather than arbitrary thresholds. For each signer, compute p-values for three
signals (WR, slippage, fg) against null hypothesis of random trading, then
combine with Fisher's method.

This script:
  1. Computes per-sandwich baselines from the full population
  2. For each signer, computes individual p-values for WR, slippage, fg
  3. Combines via Fisher's method into a single combined_p
  4. Performs power analysis to determine N_min
  5. Sensitivity analysis across alpha levels
  6. Generates charts and summary tables

Usage:
    python 2_parameter_selection.py --start-epoch 946 --end-epoch 956
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from scipy import stats

SLOTS_PER_EPOCH = 432_000

# ── CLI ──────────────────────────────────────────────────────────────────────

# diff_signer_transfer is excluded: empirically 0 reliable attackers
# (6,920 sandwiches, $-46 net, $1.78 positive-USD total; the only 3
# Signal-Bot matches all hit CNT=5 with $0 USD). See
# docs/evaluator_design.md.
CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]


def parse_args():
    p = argparse.ArgumentParser(description="Phase 2: parameter selection")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=956)
    p.add_argument("--category", type=str, default="standard",
                   choices=CATEGORIES, help="Sandwich category")
    return p.parse_args()


# ── Step 1: Compute Baselines ────────────────────────────────────────────────

def compute_baselines(ps):
    """Compute per-sandwich baselines from full population."""
    baseline_wr = ps["is_profitable"].mean()

    valid_slip = ps[(ps["slippage_consumption"] >= 0) & (ps["slippage_consumption"] <= 1)]
    baseline_slip_mean = valid_slip["slippage_consumption"].mean()
    baseline_slip_std = valid_slip["slippage_consumption"].std()

    baseline_fg50_rate = (ps["proximity"] <= 50).mean()

    return {
        "wr": baseline_wr,
        "slip_mean": baseline_slip_mean,
        "slip_std": baseline_slip_std,
        "fg50_rate": baseline_fg50_rate,
    }


# ── Step 2: Per-Signer P-values ─────────────────────────────────────────────

def compute_signer_pvalues(ps, sf, baselines):
    """Compute three p-values and Fisher combined for each signer (vectorized)."""
    # Pre-aggregate per-signer stats from per_sandwich data
    signer_agg = ps.groupby("signer").agg(
        n_valid_slip=("slippage_consumption",
                      lambda x: ((x >= 0) & (x <= 1)).sum()),
        fg50_count=("proximity", lambda x: (x <= 50).sum()),
    )

    # Build result dataframe from signer features
    df = sf[["sandwich_count", "win_rate", "mean_slippage",
             "median_proximity"]].copy()
    df = df.rename(columns={"sandwich_count": "cnt"})
    df = df.join(signer_agg, how="left")
    df["n_valid_slip"] = df["n_valid_slip"].fillna(0).astype(int)
    df["fg50_count"] = df["fg50_count"].fillna(0).astype(int)
    df["wins"] = (df["win_rate"] * df["cnt"]).round().astype(int)

    # Signal 1: WR — vectorized binomial sf
    df["p_wr"] = stats.binom.sf(df["wins"] - 1, df["cnt"], baselines["wr"])

    # Signal 2: Slippage — vectorized z-test
    se = baselines["slip_std"] / np.sqrt(np.maximum(df["n_valid_slip"], 2))
    z = (df["mean_slippage"] - baselines["slip_mean"]) / se
    df["p_slip"] = 1 - stats.norm.cdf(z)
    df.loc[df["mean_slippage"].isna() | (df["n_valid_slip"] < 1), "p_slip"] = 1.0

    # Signal 3: FG — vectorized binomial sf
    df["p_fg"] = stats.binom.sf(df["fg50_count"] - 1, df["cnt"],
                                baselines["fg50_rate"])

    # Fisher's method: combine p-values
    log_p = (np.log(np.maximum(df["p_wr"], 1e-300)) +
             np.log(np.maximum(df["p_slip"], 1e-300)) +
             np.log(np.maximum(df["p_fg"], 1e-300)))
    df["fisher_chi2"] = -2 * log_p
    df["combined_p"] = 1 - stats.chi2.cdf(df["fisher_chi2"], df=6)

    return df


# ── Step 3: Power Analysis ──────────────────────────────────────────────────

def power_analysis(baselines, alpha_levels, max_n=50):
    """Compute detection power for true attackers at different N and alpha.

    Assumes true attacker has: WR=0.95, mean_slippage=0.93, fg50_rate=0.65
    """
    true_wr = 0.95
    true_slip = 0.93
    true_fg50 = 0.65

    results = []
    for n in range(1, max_n + 1):
        for alpha in alpha_levels:
            # Monte Carlo: simulate N_sim true attackers with n sandwiches each
            N_sim = 10000
            detected = 0
            for _ in range(N_sim):
                # Simulate wins
                wins = np.random.binomial(n, true_wr)
                p_wr = stats.binom.sf(wins - 1, n, baselines["wr"])

                # Simulate slippage (truncated normal around true_slip)
                slips = np.clip(np.random.normal(true_slip, 0.08, n), 0, 1)
                mean_s = slips.mean()
                se = baselines["slip_std"] / np.sqrt(max(n, 2))
                z = (mean_s - baselines["slip_mean"]) / se
                p_slip = 1 - stats.norm.cdf(z)

                # Simulate fg50
                fg50 = np.random.binomial(n, true_fg50)
                p_fg = stats.binom.sf(fg50 - 1, n, baselines["fg50_rate"])

                # Fisher combine
                pvals = [max(p_wr, 1e-300), max(p_slip, 1e-300), max(p_fg, 1e-300)]
                chi2_stat = -2 * sum(np.log(p) for p in pvals)
                combined_p = 1 - stats.chi2.cdf(chi2_stat, df=6)

                if combined_p < alpha:
                    detected += 1

            power = detected / N_sim
            results.append({"n": n, "alpha": alpha, "power": power})

    return pd.DataFrame(results)


def power_analysis_fast(baselines, alpha_levels, max_n=50):
    """Analytical approximation of power (faster than Monte Carlo for display)."""
    true_wr = 0.95
    true_slip = 0.93
    true_fg50 = 0.65

    results = []
    for n in range(1, max_n + 1):
        for alpha in alpha_levels:
            # Approximate: compute expected p-values under H1
            # WR: expected wins = n * true_wr
            expected_wins = int(round(n * true_wr))
            p_wr_expected = stats.binom.sf(expected_wins - 1, n, baselines["wr"])

            # Slippage: expected z under H1
            se = baselines["slip_std"] / np.sqrt(max(n, 2))
            z_expected = (true_slip - baselines["slip_mean"]) / se
            p_slip_expected = 1 - stats.norm.cdf(z_expected)

            # FG: expected fg50 count
            expected_fg50 = int(round(n * true_fg50))
            p_fg_expected = stats.binom.sf(expected_fg50 - 1, n, baselines["fg50_rate"])

            # Fisher at expected p-values
            pvals = [max(p_wr_expected, 1e-300), max(p_slip_expected, 1e-300),
                     max(p_fg_expected, 1e-300)]
            chi2_stat = -2 * sum(np.log(p) for p in pvals)
            combined_p = 1 - stats.chi2.cdf(chi2_stat, df=6)

            # Power approximation: if expected combined_p < alpha, power is high
            # More accurate: use ratio to estimate
            power = 1.0 if combined_p < alpha * 0.01 else (
                0.95 if combined_p < alpha * 0.1 else (
                0.80 if combined_p < alpha else (
                0.50 if combined_p < alpha * 10 else 0.1)))

            results.append({
                "n": n, "alpha": alpha, "power": power,
                "expected_combined_p": combined_p,
            })

    return pd.DataFrame(results)


# ── Step 4: Sensitivity Analysis ─────────────────────────────────────────────

def sensitivity_analysis(pval_df, sf, ps, n_min_values, alpha_values, token_prices):
    """Evaluate classification results across different (N_min, alpha) combinations."""
    # Compute per-sandwich USD profit
    ps_priced = ps.copy()
    ps_priced["token_a_price"] = ps_priced["token_a"].map(token_prices)
    ps_priced["usd_profit"] = ps_priced["profit"] * ps_priced["token_a_price"]

    total_signers = len(sf)
    total_sandwiches = int(sf["sandwich_count"].sum())

    # Jito bots (always included regardless of N_min)
    jito_signers = set(ps[ps["jito_bundle"] == True]["signer"].unique())

    results = []
    for n_min in n_min_values:
        for alpha in alpha_values:
            # Statistical bots: cnt >= n_min AND combined_p < alpha
            stat_mask = (pval_df["cnt"] >= n_min) & (pval_df["combined_p"] < alpha)
            stat_signers = set(pval_df[stat_mask].index)

            # Union with Jito bots
            bot_signers = stat_signers | jito_signers

            # Stats
            bot_sf = sf.loc[sf.index.isin(bot_signers)]
            n_bots = len(bot_sf)
            n_sw = int(bot_sf["sandwich_count"].sum())

            # USD profit
            bot_ps = ps_priced[ps_priced["signer"].isin(bot_signers)]
            usd_profit = bot_ps["usd_profit"].sum()
            sol_profit = bot_ps[bot_ps["token_a"] == "SOL"]["profit"].sum()

            # Also count those who pass individual signal tests
            wr_sig = (pval_df["cnt"] >= n_min) & (pval_df["p_wr"] < 0.05)
            slip_sig = (pval_df["cnt"] >= n_min) & (pval_df["p_slip"] < 0.05)
            fg_sig = (pval_df["cnt"] >= n_min) & (pval_df["p_fg"] < 0.05)
            all3_sig = wr_sig & slip_sig & fg_sig

            results.append({
                "n_min": n_min,
                "alpha": alpha,
                "n_bots": n_bots,
                "n_jito_only": len(jito_signers - stat_signers),
                "n_stat_only": len(stat_signers - jito_signers),
                "n_both": len(stat_signers & jito_signers),
                "n_sandwiches": n_sw,
                "pct_signers": n_bots / total_signers * 100,
                "pct_sandwiches": n_sw / total_sandwiches * 100,
                "sol_profit": sol_profit,
                "usd_profit": usd_profit,
                "n_wr_sig": int(wr_sig.sum()),
                "n_slip_sig": int(slip_sig.sum()),
                "n_fg_sig": int(fg_sig.sum()),
                "n_all3_sig": int(all3_sig.sum()),
            })

    return pd.DataFrame(results)


# ── Step 5: False Positive Rate Estimation ───────────────────────────────────

def estimate_fpr(baselines, n_values, alpha):
    """Estimate false positive rate: P(combined_p < alpha | H0 true)."""
    results = []
    N_sim = 50000
    for n in n_values:
        false_positives = 0
        for _ in range(N_sim):
            # Simulate a random trader with n sandwiches
            wins = np.random.binomial(n, baselines["wr"])
            p_wr = stats.binom.sf(wins - 1, n, baselines["wr"])

            slips = np.clip(np.random.normal(baselines["slip_mean"],
                                             baselines["slip_std"], n), 0, 1)
            mean_s = slips.mean()
            se = baselines["slip_std"] / np.sqrt(max(n, 2))
            z = (mean_s - baselines["slip_mean"]) / se
            p_slip = 1 - stats.norm.cdf(z)

            fg50 = np.random.binomial(n, baselines["fg50_rate"])
            p_fg = stats.binom.sf(fg50 - 1, n, baselines["fg50_rate"])

            pvals = [max(p_wr, 1e-300), max(p_slip, 1e-300), max(p_fg, 1e-300)]
            chi2_stat = -2 * sum(np.log(p) for p in pvals)
            combined_p = 1 - stats.chi2.cdf(chi2_stat, df=6)

            if combined_p < alpha:
                false_positives += 1

        fpr = false_positives / N_sim
        results.append({"n": n, "alpha": alpha, "fpr": fpr,
                         "false_positives": false_positives, "N_sim": N_sim})

    return pd.DataFrame(results)


# ── Charts ───────────────────────────────────────────────────────────────────

def plot_combined_p_distribution(pval_df, chart_dir, tag):
    """Histogram of combined_p values for different CNT buckets."""
    fig, axes = plt.subplots(2, 3, figsize=(16, 10))
    buckets = [(1, 4), (5, 10), (11, 20), (21, 50), (51, 200), (201, None)]
    labels = ["CNT 1-4", "CNT 5-10", "CNT 11-20", "CNT 21-50",
              "CNT 51-200", "CNT 201+"]

    for ax, (lo, hi), label in zip(axes.flat, buckets, labels):
        if hi is not None:
            sub = pval_df[(pval_df["cnt"] >= lo) & (pval_df["cnt"] <= hi)]
        else:
            sub = pval_df[pval_df["cnt"] >= lo]
        cp = sub["combined_p"].clip(1e-20, 1.0)

        ax.hist(cp, bins=50, edgecolor="black", alpha=0.7, log=True)
        ax.axvline(x=0.001, color="red", linestyle="--", linewidth=1.5,
                   label="α=0.001")
        ax.axvline(x=0.01, color="orange", linestyle="--", linewidth=1.5,
                   label="α=0.01")
        ax.set_xlabel("Combined p-value")
        ax.set_ylabel("Count (log scale)")
        ax.set_title(f"{label} (n={len(sub):,})")
        ax.legend(fontsize=8)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"combined_p_distribution_{tag}.png"), dpi=150)
    plt.close()


def plot_power_curve(power_df, chart_dir, tag):
    """Power curve: detection probability vs N for different alpha levels."""
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = {"0.05": "#2ca02c", "0.01": "#ff7f0e", "0.001": "#d62728",
              "0.0001": "#9467bd"}

    for alpha in sorted(power_df["alpha"].unique()):
        sub = power_df[power_df["alpha"] == alpha]
        ax.plot(sub["n"], sub["power"], label=f"α={alpha}", linewidth=2,
                color=colors.get(str(alpha), "gray"))

    ax.axhline(y=0.80, color="black", linestyle=":", linewidth=1, label="Power=0.80")
    ax.axhline(y=0.95, color="black", linestyle="-.", linewidth=1, label="Power=0.95")
    ax.set_xlabel("Number of Sandwiches (N)", fontsize=12)
    ax.set_ylabel("Detection Power", fontsize=12)
    ax.set_title("Power Analysis: Probability of Detecting a True Attacker\n"
                 "(true WR=0.95, slip=0.93, fg50_rate=0.65)", fontsize=13)
    ax.legend(fontsize=10)
    ax.set_xlim(1, 30)
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"power_curve_{tag}.png"), dpi=150)
    plt.close()


def plot_sensitivity_heatmap(sens_df, chart_dir, tag):
    """Heatmap of (N_min, alpha) -> number of detected bots."""
    pivot_bots = sens_df.pivot(index="n_min", columns="alpha", values="n_bots")
    pivot_sw = sens_df.pivot(index="n_min", columns="alpha", values="n_sandwiches")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    im1 = ax1.imshow(pivot_bots.values, aspect="auto", cmap="YlOrRd")
    ax1.set_xticks(range(len(pivot_bots.columns)))
    ax1.set_xticklabels([f"{a}" for a in pivot_bots.columns], rotation=45)
    ax1.set_yticks(range(len(pivot_bots.index)))
    ax1.set_yticklabels(pivot_bots.index)
    ax1.set_xlabel("α (significance level)")
    ax1.set_ylabel("N_min")
    ax1.set_title("Detected Bot Signers")
    for i in range(len(pivot_bots.index)):
        for j in range(len(pivot_bots.columns)):
            ax1.text(j, i, f"{int(pivot_bots.values[i, j])}",
                     ha="center", va="center", fontsize=9)
    fig.colorbar(im1, ax=ax1)

    im2 = ax2.imshow(pivot_sw.values, aspect="auto", cmap="YlOrRd")
    ax2.set_xticks(range(len(pivot_sw.columns)))
    ax2.set_xticklabels([f"{a}" for a in pivot_sw.columns], rotation=45)
    ax2.set_yticks(range(len(pivot_sw.index)))
    ax2.set_yticklabels(pivot_sw.index)
    ax2.set_xlabel("α (significance level)")
    ax2.set_ylabel("N_min")
    ax2.set_title("Detected Sandwiches")
    for i in range(len(pivot_sw.index)):
        for j in range(len(pivot_sw.columns)):
            ax2.text(j, i, f"{int(pivot_sw.values[i, j]):,}",
                     ha="center", va="center", fontsize=8)
    fig.colorbar(im2, ax=ax2)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"sensitivity_heatmap_{tag}.png"), dpi=150)
    plt.close()


def plot_individual_signals(pval_df, chart_dir, tag):
    """Scatter: individual p-values colored by combined classification."""
    classified = pval_df[pval_df["cnt"] >= 5].copy()
    classified["is_bot"] = classified["combined_p"] < 0.001

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    for ax, (px, py, xlabel, ylabel) in zip(axes, [
        ("p_wr", "p_slip", "p_wr (Win Rate)", "p_slip (Slippage)"),
        ("p_wr", "p_fg", "p_wr (Win Rate)", "p_fg (Front Gap)"),
        ("p_slip", "p_fg", "p_slip (Slippage)", "p_fg (Front Gap)"),
    ]):
        bots = classified[classified["is_bot"]]
        non_bots = classified[~classified["is_bot"]]

        ax.scatter(non_bots[px].clip(1e-10, 1), non_bots[py].clip(1e-10, 1),
                   alpha=0.1, s=5, c="gray", label="Non-bot")
        ax.scatter(bots[px].clip(1e-10, 1), bots[py].clip(1e-10, 1),
                   alpha=0.5, s=15, c="red", label="Bot (p<0.001)")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.axvline(x=0.05, color="blue", linestyle=":", alpha=0.5)
        ax.axhline(y=0.05, color="blue", linestyle=":", alpha=0.5)
        ax.legend(fontsize=9)

    plt.suptitle(f"Individual Signal P-values (CNT >= 5, α=0.001)", fontsize=13)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"signal_pvalues_{tag}.png"), dpi=150)
    plt.close()


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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    tag = f"{args.start_epoch}_{args.end_epoch}"
    category = args.category
    in_dir = f"data/1_signer_data_preparation_and_summary/{category}"
    out_dir = f"data/2_parameter_selection/{category}"
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)

    print("=== Phase 2: Parameter Selection via Hypothesis Testing ===")
    print(f"Input: {in_dir}/*_{tag}.*")

    # Load data
    ps = pd.read_parquet(f"{in_dir}/per_sandwich_metrics_{tag}.parquet")
    sf = pd.read_parquet(f"{in_dir}/signer_features_{tag}.parquet")
    token_prices = load_token_prices()
    print(f"Loaded: {len(ps):,} sandwiches, {len(sf):,} signers")
    print(f"Token prices: {len(token_prices)} tokens, SOL=${token_prices.get('SOL', 0):.2f}")

    # ── Step 1: Baselines ─────────────────────────────────────────────────
    print("\n--- Step 1: Population Baselines ---")
    baselines = compute_baselines(ps)
    print(f"  WR baseline (per-sandwich):        {baselines['wr']:.4f}")
    print(f"  Slippage baseline (mean):           {baselines['slip_mean']:.4f}")
    print(f"  Slippage baseline (std):            {baselines['slip_std']:.4f}")
    print(f"  FG<=50 baseline (per-sandwich):     {baselines['fg50_rate']:.4f}")

    # ── Step 2: Per-Signer P-values ───────────────────────────────────────
    print("\n--- Step 2: Computing Per-Signer P-values ---")
    pval_df = compute_signer_pvalues(ps, sf, baselines)
    print(f"  Computed p-values for {len(pval_df):,} signers")

    # Distribution of combined_p
    for threshold in [0.05, 0.01, 0.001, 0.0001]:
        n_sig = (pval_df["combined_p"] < threshold).sum()
        print(f"  combined_p < {threshold}: {n_sig:,} signers")

    # Save p-values
    pval_df.to_csv(f"{out_dir}/signer_pvalues_{tag}.csv")
    pval_df.to_parquet(f"{out_dir}/signer_pvalues_{tag}.parquet")

    # ── Step 3: Power Analysis ────────────────────────────────────────────
    print("\n--- Step 3: Power Analysis (Monte Carlo, N_sim=10000) ---")
    alpha_levels = [0.05, 0.01, 0.001, 0.0001]
    power_df = power_analysis(baselines, alpha_levels, max_n=30)

    print(f"\n  Power at selected (n, alpha) combinations:")
    print(f"  {'n':>4}  {'α=0.05':>8}  {'α=0.01':>8}  {'α=0.001':>8}  {'α=0.0001':>8}")
    print(f"  {'-'*42}")
    for n in [3, 5, 8, 10, 15, 20]:
        row_data = []
        for alpha in alpha_levels:
            p = power_df[(power_df["n"] == n) & (power_df["alpha"] == alpha)]
            row_data.append(f"{p['power'].iloc[0]:.3f}" if len(p) > 0 else "N/A")
        print(f"  {n:>4}  {'  '.join(f'{v:>8}' for v in row_data)}")

    power_df.to_csv(f"{out_dir}/power_analysis_{tag}.csv", index=False)

    # ── Step 4: False Positive Rate ───────────────────────────────────────
    print("\n--- Step 4: False Positive Rate Estimation (Monte Carlo, N_sim=50000) ---")
    fpr_results = []
    for alpha in [0.01, 0.001, 0.0001]:
        fpr_df = estimate_fpr(baselines, [3, 5, 8, 10, 15, 20], alpha)
        fpr_results.append(fpr_df)
        for _, row in fpr_df.iterrows():
            print(f"  n={int(row['n']):>3}, α={alpha}: FPR={row['fpr']:.5f} "
                  f"({int(row['false_positives'])}/{int(row['N_sim'])})")

    fpr_all = pd.concat(fpr_results, ignore_index=True)
    fpr_all.to_csv(f"{out_dir}/fpr_analysis_{tag}.csv", index=False)

    # ── Step 5: Sensitivity Analysis ──────────────────────────────────────
    print("\n--- Step 5: Sensitivity Analysis ---")
    n_min_values = [3, 5, 8, 10, 15, 20, 30, 50]
    alpha_values = [0.05, 0.01, 0.001, 0.0001]

    sens_df = sensitivity_analysis(pval_df, sf, ps, n_min_values, alpha_values,
                                   token_prices)
    sens_df.to_csv(f"{out_dir}/sensitivity_analysis_{tag}.csv", index=False)

    print(f"\n  {'N_min':>5}  {'Alpha':>8}  {'Bots':>6}  {'Sandwiches':>12}  "
          f"{'%Signers':>9}  {'%SW':>7}  {'SOL Profit':>12}  {'USD Profit':>14}")
    print(f"  {'-'*85}")
    for _, row in sens_df.iterrows():
        print(f"  {int(row['n_min']):>5}  {row['alpha']:>8.4f}  "
              f"{int(row['n_bots']):>6}  {int(row['n_sandwiches']):>12,}  "
              f"{row['pct_signers']:>8.2f}%  {row['pct_sandwiches']:>6.1f}%  "
              f"{row['sol_profit']:>12,.1f}  ${row['usd_profit']:>13,.0f}")

    # ── Step 6: Jito Bot Cross-check ──────────────────────────────────────
    print("\n--- Step 6: Jito Bot Cross-validation ---")
    jito_signers = ps[ps["jito_bundle"] == True]["signer"].unique()
    print(f"  Jito same-bundle signers: {len(jito_signers)}")
    for sig in jito_signers:
        if sig in pval_df.index:
            row = pval_df.loc[sig]
            stars = "***" if row["combined_p"] < 0.001 else (
                    "**" if row["combined_p"] < 0.01 else (
                    "*" if row["combined_p"] < 0.05 else ""))
            print(f"  {sig[:10]}...  cnt={int(row['cnt']):>3}  "
                  f"p_wr={row['p_wr']:.2e}  p_slip={row['p_slip']:.2e}  "
                  f"p_fg={row['p_fg']:.2e}  combined={row['combined_p']:.2e} {stars}")

    # ── Step 7: Recommended Parameters ────────────────────────────────────
    print("\n" + "=" * 80)
    print("  PARAMETER SELECTION CONCLUSION")
    print("=" * 80)

    # Find N_min where power >= 0.95 at alpha=0.001
    rec_alpha = 0.001
    power_at_alpha = power_df[power_df["alpha"] == rec_alpha]
    n_for_95 = power_at_alpha[power_at_alpha["power"] >= 0.95]["n"].min()
    n_for_80 = power_at_alpha[power_at_alpha["power"] >= 0.80]["n"].min()

    print(f"\n  Recommended α = {rec_alpha}")
    print(f"    N for power ≥ 0.95: {n_for_95}")
    print(f"    N for power ≥ 0.80: {n_for_80}")

    rec_n = max(n_for_80, 5) if not pd.isna(n_for_80) else 5
    print(f"\n  Recommended N_min = {rec_n}")

    # Results at recommended parameters
    rec_row = sens_df[(sens_df["n_min"] == rec_n) & (sens_df["alpha"] == rec_alpha)]
    if len(rec_row) > 0:
        r = rec_row.iloc[0]
        print(f"\n  At (N_min={rec_n}, α={rec_alpha}):")
        print(f"    Bot signers:     {int(r['n_bots']):>6} ({r['pct_signers']:.2f}% of all)")
        print(f"    Bot sandwiches:  {int(r['n_sandwiches']):>6,} ({r['pct_sandwiches']:.1f}% of all)")
        print(f"    SOL profit:      {r['sol_profit']:>10,.1f}")
        print(f"    USD profit:      ${r['usd_profit']:>10,.0f}")

    # Compare with nearby parameters for robustness
    print(f"\n  Robustness check (nearby parameters):")
    for n_min, alpha in [(rec_n, 0.01), (rec_n, 0.0001),
                         (max(rec_n-2, 3), rec_alpha), (rec_n+5, rec_alpha)]:
        r = sens_df[(sens_df["n_min"] == n_min) & (sens_df["alpha"] == alpha)]
        if len(r) > 0:
            r = r.iloc[0]
            print(f"    N={n_min:>3}, α={alpha:.4f}: "
                  f"{int(r['n_bots']):>5} bots, {int(r['n_sandwiches']):>6,} sw, "
                  f"${r['usd_profit']:>10,.0f}")

    # ── Step 8: Charts ────────────────────────────────────────────────────
    print("\n--- Generating Charts ---")
    plot_combined_p_distribution(pval_df, chart_dir, tag)
    plot_power_curve(power_df, chart_dir, tag)
    plot_sensitivity_heatmap(sens_df, chart_dir, tag)
    plot_individual_signals(pval_df, chart_dir, tag)

    print(f"  Charts saved to {chart_dir}/")
    print("\n=== Done ===")


if __name__ == "__main__":
    main()
