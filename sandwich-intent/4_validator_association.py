"""
Phase 4: Validator Association Analysis (Pooled Mode)
=====================================================
Detect validator-signer collusion among the unified 302 attackers identified
in Step 3 (jito ∪ signal across the three sandwich categories, excluding
oneshot tier), restricted by default to those with sandwich count >= 10
(the 282 attackers reported in the paper §7.1).

Pipeline:
  1. Pool bot_attackers and bot_sandwiches across {standard, multi_split,
     diff_signer_owner}, restrict to tier in {jito_only, jito_and_signal,
     signal} (excludes oneshot/signal_and_oneshot), then to CNT >= cnt_min.
  2. For each (attacker, validator) pair, count how often the validator
     appears in ±k leader rotations around the attacker's sandwich slots.
  3. Compute the enrichment ratio:

        eta_{a,v} = N_{a,v} * N / ((2k+1) * N_v * |S_a|)

     where N_{a,v} = total appearances of v in ±k window, N_v = v's leader
     rotation share (slot-weighted), N = total leader slots, |S_a| = a's
     sandwich count. The code's slot-weighted form `(N_{a,v}/|S_a|) /
     sum_off slot_freq[off][v]` is mathematically equivalent to the paper
     formula under the standard 4-slot leader rotation assumption; the
     emitted CSV includes both forms.
  4. Flag pairs with eta >= min_enrichment (default 5x) AND near_cnt >=
     min_near_cnt (default 5).
  5. Two cluster mechanisms identify collusion structure:
       - shared-validator clustering (>=cohort_min_shared shared validators)
       - single-validator groups (one validator flagged by >=2 attackers)
     Both feed into the same Union-Find; clusters of >=2 attackers form a
     reported cohort.

Outputs (all under data/4_validator_association/all_attackers/):
  flagged_pairs_<tag>.csv      every (attacker, validator) pair with eta>=5
  untrusted_pairs_<tag>.csv    same but excludes the trusted-top-N stake set
  signer_leader_summary_<tag>.csv  per-attacker windowed top-validator
  cohort_members_<tag>.csv     attackers grouped into cohorts
  charts/*.png

Usage:
    python 4_validator_association.py --start-epoch 946 --end-epoch 960
"""

import argparse
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from utils.db import get_client

SLOTS_PER_EPOCH = 432_000
OFFSET_MAX = 4
OFFSETS = list(range(-OFFSET_MAX, OFFSET_MAX + 1))


# diff_signer_transfer is excluded: empirically 0 reliable attackers
# (6,920 sandwiches, $-46 net, $1.78 positive-USD total; the only 3
# Signal-Bot matches all hit CNT=5 with $0 USD). See
# docs/evaluator_design.md.
CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]


def parse_args():
    p = argparse.ArgumentParser(description="Phase 4: validator association")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=960)
    p.add_argument("--cnt-min", type=int, default=10,
                   help="Minimum sandwich count to include an attacker in the "
                        "pooled set (paper §7.1 uses 10).")
    p.add_argument("--min-enrichment", type=float, default=10.0,
                   help="Min enrichment to flag a (signer, validator) pair "
                        "(paper §7.1 strong-association threshold)")
    p.add_argument("--min-near-cnt", type=int, default=5,
                   help="Min nearby count to consider a pair")
    p.add_argument("--signer-peak-pct", type=float, default=0.0,
                   help="Signer retention: at least one pair must have near_pct >= this. "
                        "Default 0 keeps all enriched signers.")
    p.add_argument("--cohort-min-shared", type=int, default=3,
                   help="Min shared flagged validators to link two signers")
    p.add_argument("--trusted-top-n", type=int, default=15,
                   help="Top-N named validators by stake treated as trusted")
    p.add_argument("--exclude-tiers", type=str, default="oneshot,signal_and_oneshot",
                   help="Comma-separated tier names to exclude from pooled set. "
                        "Default excludes Oneshot Bots; pass empty string to keep them.")
    return p.parse_args()


# ── Leader Block Construction ────────────────────────────────────────────────

def load_leader_blocks(client, start_slot, end_slot):
    """Build leader blocks: consecutive slots by the same leader."""
    df = client.query_df(f"""
        SELECT slot, leader FROM slot_leaders
        WHERE slot >= {start_slot} AND slot < {end_slot}
        ORDER BY slot
    """)
    slots = df["slot"].values.astype(int)
    ldrs = df["leader"].values

    blocks = []
    cur_leader = ldrs[0]
    cur_start = slots[0]
    for i in range(1, len(slots)):
        if ldrs[i] != cur_leader or slots[i] != slots[i - 1] + 1:
            blocks.append((cur_start, slots[i - 1], cur_leader))
            cur_leader = ldrs[i]
            cur_start = slots[i]
    blocks.append((cur_start, slots[-1], cur_leader))
    return blocks


def build_slot_to_block_idx(blocks):
    """Map each slot to its leader-block index."""
    mapping = {}
    for idx, (s, e, _) in enumerate(blocks):
        for sl in range(s, e + 1):
            mapping[sl] = idx
    return mapping


def compute_global_offset_freq(blocks):
    """For each (offset, validator), compute baseline fraction of slots."""
    freq = {off: defaultdict(float) for off in OFFSETS}
    total_slots = sum(e - s + 1 for s, e, _ in blocks)
    for idx, (s, e, _) in enumerate(blocks):
        size = e - s + 1
        for off in OFFSETS:
            ni = idx + off
            if 0 <= ni < len(blocks):
                freq[off][blocks[ni][2]] += size
    for off in OFFSETS:
        for l in freq[off]:
            freq[off][l] /= total_slots
    return freq


# ── Per-Signer Offset Counts ────────────────────────────────────────────────

def compute_signer_validator_counts(bot_ps, slot_to_block, blocks):
    """For each (signer, validator, offset), count sandwich occurrences."""
    per_signer = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    signers = bot_ps["signer"].values
    slots = bot_ps["slot"].astype(int).values

    for i in range(len(bot_ps)):
        bidx = slot_to_block.get(int(slots[i]))
        if bidx is None:
            continue
        for off in OFFSETS:
            ni = bidx + off
            if 0 <= ni < len(blocks):
                per_signer[signers[i]][blocks[ni][2]][off] += 1
    return per_signer


# ── Scoring & Flagging ───────────────────────────────────────────────────────

def score_pairs(per_signer, signer_totals, global_freq, min_enrichment, min_near_cnt):
    """Score each (signer, validator) pair by enrichment."""
    rows = []
    for signer, vmap in per_signer.items():
        total = signer_totals.get(signer, 0)
        if total == 0:
            continue
        for validator, offset_cnts in vmap.items():
            near_cnt = sum(offset_cnts.values())
            near_pct = near_cnt / total
            expected_pct = sum(global_freq[off].get(validator, 0) for off in OFFSETS)
            enrichment = near_pct / expected_pct if expected_pct > 0 else 0
            if enrichment < min_enrichment or near_cnt < min_near_cnt:
                continue

            offset_dist = {off: offset_cnts.get(off, 0) for off in OFFSETS}
            peak_off = max(offset_dist, key=offset_dist.get)

            rows.append({
                "signer": signer,
                "validator": validator,
                "signer_total": total,
                "near_cnt": near_cnt,
                "near_pct": near_pct,
                "expected_pct": expected_pct,
                "enrichment": enrichment,
                "peak_offset": peak_off,
                "peak_count": offset_dist[peak_off],
                **{f"off_{off}": offset_dist[off] for off in OFFSETS},
            })
    # Emit a fixed-schema empty frame when nothing qualifies so downstream code
    # can safely index expected columns (near_pct, enrichment, ...).
    base_cols = [
        "signer", "validator", "signer_total", "near_cnt", "near_pct",
        "expected_pct", "enrichment", "peak_offset", "peak_count",
    ] + [f"off_{off}" for off in OFFSETS]
    if not rows:
        return pd.DataFrame(columns=base_cols)
    return pd.DataFrame(rows)


# ── Cohort Clustering ────────────────────────────────────────────────────────

def cluster_cohorts(flagged_df, cohort_min_shared):
    """Cluster signers into cohorts via two mechanisms:

    1. Shared-validator clustering (Union-Find): two signers are linked if they
       share >= cohort_min_shared enriched validators. Discovers multi-signer
       entities that rotate across multiple validators.

    2. Single-validator groups: a validator with >= 2 enriched signers forms a
       cohort even if those signers don't share other validators. Discovers
       validator-controlled signer pools.

    Both mechanisms are merged: if signer A is linked to B via shared validators,
    and B is linked to C via a single-validator group, all three form one cohort.
    """
    signer_to_vals = defaultdict(set)
    val_to_signers = defaultdict(set)
    for _, r in flagged_df.iterrows():
        signer_to_vals[r["signer"]].add(r["validator"])
        val_to_signers[r["validator"]].add(r["signer"])

    signers_list = sorted(signer_to_vals.keys())
    parent = {s: s for s in signers_list}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # Mechanism 1: shared-validator linking
    for i, s1 in enumerate(signers_list):
        for s2 in signers_list[i + 1:]:
            shared = signer_to_vals[s1] & signer_to_vals[s2]
            if len(shared) >= cohort_min_shared:
                union(s1, s2)

    # Mechanism 2: single-validator groups (>= 2 signers on same validator)
    for val, sigs in val_to_signers.items():
        if len(sigs) >= 2:
            sigs_list = list(sigs)
            for i in range(1, len(sigs_list)):
                union(sigs_list[0], sigs_list[i])

    clusters = defaultdict(list)
    for s in signers_list:
        clusters[find(s)].append(s)

    cohorts = [m for m in clusters.values() if len(m) >= 2]
    cohorts.sort(key=lambda c: -len(c))
    return cohorts, signer_to_vals, val_to_signers


# ── Charts ───────────────────────────────────────────────────────────────────

def plot_enrichment_overview(signer_summary, chart_dir, tag):
    """Leader concentration scatter and enrichment distribution."""
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))

    # Left: top-1 share vs count
    ax = axes[0]
    ax.scatter(signer_summary["sandwich_count"],
               signer_summary["w0_top_pct"] * 100,
               s=20, alpha=0.6, c="steelblue")
    ax.set_xlabel("Sandwich Count", fontsize=12)
    ax.set_ylabel("Top-1 Leader Share at Slot (%)", fontsize=12)
    ax.set_title("Leader Concentration at Sandwich Slot (offset=0)", fontsize=13)
    ax.set_xscale("log")
    ax.axhline(5, color="red", linestyle="--", alpha=0.5, label="5%")
    ax.legend()

    # Right: enrichment at ±4 window
    ax = axes[1]
    enr = signer_summary["w4_top_enrich"].clip(0, 30)
    ax.hist(enr, bins=40, edgecolor="black", alpha=0.7, color="steelblue")
    ax.set_xlabel("Top-1 Enrichment (±4 window)", fontsize=12)
    ax.set_ylabel("Number of Signers", fontsize=12)
    ax.set_title("Distribution of Top-1 Leader Enrichment (±4)", fontsize=13)
    ax.axvline(5, color="red", linestyle="--", alpha=0.7, label="Threshold=5x")
    ax.legend()

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"leader_concentration_{tag}.png"), dpi=150)
    plt.close()


def plot_cohort_detail(cohorts, signer_to_vals, bot_sf, per_signer, signer_totals,
                       global_freq, v_info, chart_dir, tag):
    """Heatmap for top cohorts showing signer × validator enrichment."""
    if not cohorts:
        return

    # Take top 3 cohorts (or fewer)
    top_cohorts = cohorts[:3]
    n_plots = len(top_cohorts)
    fig, axes = plt.subplots(1, n_plots, figsize=(7 * n_plots, max(6, max(len(c) for c in top_cohorts) * 0.5 + 2)))

    if n_plots == 1:
        axes = [axes]

    for ax, (cidx, members) in zip(axes, enumerate(top_cohorts, 1)):
        # Get shared validators (union)
        shared_vals = set()
        for s in members:
            shared_vals.update(signer_to_vals.get(s, set()))

        # Sort validators by how many members flag them
        val_member_count = {}
        for v in shared_vals:
            val_member_count[v] = sum(1 for s in members if v in signer_to_vals.get(s, set()))
        sorted_vals = sorted(shared_vals, key=lambda v: -val_member_count[v])[:15]

        # Build enrichment matrix
        matrix = np.zeros((len(members), len(sorted_vals)))
        for i, sig in enumerate(members):
            for j, val in enumerate(sorted_vals):
                near_cnt = sum(per_signer.get(sig, {}).get(val, {}).values())
                total = signer_totals.get(sig, 1)
                pct = near_cnt / total
                exp = sum(global_freq[off].get(val, 0) for off in OFFSETS)
                matrix[i, j] = pct / exp if exp > 0 else 0

        im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd", vmin=0, vmax=20)
        ax.set_xticks(range(len(sorted_vals)))
        v_labels = []
        for v in sorted_vals:
            name = str(v_info.get(v, {}).get("name", ""))
            name = name.strip() if name and name != "nan" else ""
            v_labels.append(f"{v[:8]}({name[:10]})" if name else v[:10])
        ax.set_xticklabels(v_labels, rotation=90, fontsize=7)
        ax.set_yticks(range(len(members)))
        sig_labels = [f"{s[:10]}({signer_totals.get(s,0)})" for s in members]
        ax.set_yticklabels(sig_labels, fontsize=8)
        ax.set_title(f"Cohort {cidx}: {len(members)} signers, {len(shared_vals)} validators",
                     fontsize=11)

    fig.suptitle("Signer-Validator Enrichment Heatmaps (top cohorts)", fontsize=13, y=1.02)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"cohort_heatmaps_{tag}.png"), dpi=150,
                bbox_inches="tight")
    plt.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def _load_pooled_attackers(tag, exclude_tiers, cnt_min):
    """Pool bot_attackers and bot_sandwiches across the three classifier
    categories, exclude the specified tiers, and restrict to attackers
    with sandwich_count >= cnt_min.

    Returns (bot_sf, bot_ps, breakdown_string).
    """
    bot_frames = []
    sw_frames = []
    breakdown = []
    for cat in CATEGORIES:
        sf_path = f"data/3_attacker_filter/{cat}/bot_attackers_{tag}.parquet"
        sw_path = f"data/3_attacker_filter/{cat}/bot_sandwiches_{tag}.parquet"
        if not (os.path.exists(sf_path) and os.path.exists(sw_path)):
            print(f"  WARN: missing {cat} outputs, skipping")
            continue
        sf = pd.read_parquet(sf_path)
        sw = pd.read_parquet(sw_path)
        if exclude_tiers:
            sf = sf[~sf["tier"].isin(exclude_tiers)]
            sw = sw[sw["signer"].isin(sf.index)]
        sf = sf.assign(origin_category=cat).reset_index()
        sw = sw.assign(origin_category=cat)
        bot_frames.append(sf)
        sw_frames.append(sw)
        breakdown.append(f"{cat}={len(sf)}")
    bot_sf_all = pd.concat(bot_frames, ignore_index=True)
    bot_ps_all = pd.concat(sw_frames, ignore_index=True)

    # Apply CNT >= cnt_min on POOLED sandwich count per signer (since a
    # signer's sandwiches may straddle categories — though by design the 302
    # tier-filtered set has no signer-level overlap, the pooled groupby is
    # the safest aggregation).
    cnt_per_signer = bot_ps_all.groupby("signer").size()
    keep = set(cnt_per_signer[cnt_per_signer >= cnt_min].index)
    bot_sf = bot_sf_all[bot_sf_all["signer"].isin(keep)].copy()
    # Dedup signer-level rows: a signer should only appear once. Keep the
    # row from whichever category it was originally classified in.
    bot_sf = bot_sf.drop_duplicates("signer", keep="first").set_index("signer")
    bot_ps = bot_ps_all[bot_ps_all["signer"].isin(keep)].reset_index(drop=True)

    return bot_sf, bot_ps, breakdown


def main():
    args = parse_args()
    tag = f"{args.start_epoch}_{args.end_epoch}"
    start_slot = args.start_epoch * SLOTS_PER_EPOCH
    end_slot = (args.end_epoch + 1) * SLOTS_PER_EPOCH

    out_dir = "data/4_validator_association/all_attackers"
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)

    exclude_tiers = [t.strip() for t in args.exclude_tiers.split(",") if t.strip()]
    print("=== Phase 4: Validator Association Analysis (Pooled) ===")
    print(f"Epoch range: {args.start_epoch}-{args.end_epoch}")
    print(f"Pooling 3 categories, excluding tiers: {exclude_tiers}")
    print(f"Parameters: cnt_min={args.cnt_min}, min_enrichment={args.min_enrichment}, "
          f"min_near_cnt={args.min_near_cnt}, "
          f"signer_peak_pct={args.signer_peak_pct}, "
          f"cohort_min_shared={args.cohort_min_shared}")

    # Pool attackers across categories
    print("\nLoading data...")
    bot_sf, bot_ps, breakdown = _load_pooled_attackers(
        tag, exclude_tiers, args.cnt_min)
    print(f"  Per-category contribution (after tier filter): {', '.join(breakdown)}")
    print(f"  Pooled attackers (CNT>={args.cnt_min}): {len(bot_sf):,}")
    print(f"  Pooled sandwiches: {len(bot_ps):,}")

    # Load validator info
    v_info = {}
    vpath = os.path.join(os.path.dirname(__file__), "data", "stakewiz", "validators.csv")
    if os.path.exists(vpath):
        vdf = pd.read_csv(vpath)
        v_info = {r["identity"]: r.to_dict() for _, r in vdf.iterrows()}
        # Trusted set
        named = vdf[vdf["name"].notna() & (vdf["name"].str.strip() != "")]
        trusted = set(named.nlargest(args.trusted_top_n, "activated_stake")["identity"])
        print(f"  Validator info: {len(v_info)} validators, "
              f"{args.trusted_top_n} trusted")
    else:
        trusted = set()
        print("  No validator info file found")

    # Load leader schedule
    print("\nLoading leader schedule...")
    client = get_client()
    blocks = load_leader_blocks(client, start_slot, end_slot)
    slot_to_block = build_slot_to_block_idx(blocks)
    print(f"  {len(blocks):,} leader blocks")

    global_freq = compute_global_offset_freq(blocks)

    # Per-signer counts
    print("\nComputing per-signer per-validator offset counts...")
    per_signer = compute_signer_validator_counts(bot_ps, slot_to_block, blocks)
    signer_totals = bot_ps.groupby("signer").size().to_dict()

    # Compute signer profiles for enrichment output
    token_prices = {}
    tp_path = os.path.join(os.path.dirname(__file__), "data", "token_prices", "prices.csv")
    if os.path.exists(tp_path):
        tpdf = pd.read_csv(tp_path)
        for _, r in tpdf.iterrows():
            if pd.notna(r.get("usd_price")):
                token_prices[r["token"]] = r["usd_price"]
    if "SOL" not in token_prices:
        token_prices["SOL"] = 86.0

    bot_ps_copy = bot_ps.copy()
    bot_ps_copy["usd_profit"] = bot_ps_copy["profit"] * bot_ps_copy["token_a"].map(token_prices)
    signer_usd = bot_ps_copy.groupby("signer")["usd_profit"].sum()

    # Score pairs
    print(f"\nScoring (signer, validator) pairs...")
    pairs = score_pairs(per_signer, signer_totals, global_freq,
                        args.min_enrichment, args.min_near_cnt)
    print(f"  Flagged pairs: {len(pairs):,}")

    no_flagged = (len(pairs) == 0)
    if no_flagged:
        print("No suspicious pairs found — will still emit empty output files "
              "so downstream analyst scripts see an explicit 'ran, zero enriched' "
              "signal rather than a missing directory.")

    # Enrich with validator info
    def _vi(v, key, default=""):
        return v_info[v].get(key, default) if v in v_info else default

    def vfmt(v, max_addr=10):
        """Format validator as 'addr(name)' or 'addr' if no name."""
        name = str(_vi(v, "name", ""))
        name = name.strip() if name and name != "nan" else ""
        addr = v[:max_addr]
        return f"{addr}({name})" if name else addr

    # Validator-level baselines: N_v (slot count led by v) and N (total slots).
    # These let the CSV reproduce the paper formula
    #   eta_paper = N_av * N / ((2k+1) * N_v * |S_a|)
    # alongside the code's slot-weighted form.
    N_total = sum(e - s + 1 for s, e, _ in blocks)
    Nv_by_validator = defaultdict(int)
    for s_, e_, ld_ in blocks:
        Nv_by_validator[ld_] += (e_ - s_ + 1)
    window_size = 2 * OFFSET_MAX + 1   # = 9

    if not no_flagged:
        # Validator metadata
        pairs["v_name"] = pairs["validator"].map(lambda v: str(_vi(v, "name", "")))
        pairs["v_stake"] = pairs["validator"].map(lambda v: _vi(v, "activated_stake", 0))
        pairs["v_ip_org"] = pairs["validator"].map(lambda v: str(_vi(v, "ip_org", "")))
        pairs["v_ip_city"] = pairs["validator"].map(lambda v: str(_vi(v, "ip_city", "")))
        pairs["v_ip_asn"] = pairs["validator"].map(lambda v: str(_vi(v, "ip_asn", "")))
        pairs["trusted"] = pairs["validator"].isin(trusted)

        # Signer profile (from pooled bot_sf)
        def _sig(col, default=np.nan):
            ser = bot_sf[col] if col in bot_sf.columns else pd.Series(dtype=float)
            return pairs["signer"].map(ser).fillna(default)
        pairs["signer_origin_category"] = _sig("origin_category", "")
        pairs["signer_tier"] = _sig("tier", "")
        pairs["signer_wr"] = _sig("win_rate")
        pairs["signer_slip"] = _sig("mean_slippage")
        pairs["signer_fg100"] = _sig("fg100_ratio")
        pairs["signer_jito_count"] = _sig("jito_count", 0).astype(int)
        pairs["signer_sol_total_profit"] = _sig("sol_total_profit", 0.0)
        pairs["signer_usd_total_profit"] = _sig("usd_total_profit", 0.0)
        pairs["signer_usd_pooled"] = pairs["signer"].map(signer_usd)

        # Formula variables (explicit, for paper/code cross-check)
        pairs["N_av_near_cnt"] = pairs["near_cnt"]
        pairs["N_sa_signer_total"] = pairs["signer_total"]
        pairs["N_v_validator_slots"] = pairs["validator"].map(
            lambda v: Nv_by_validator.get(v, 0))
        pairs["N_total_slots"] = N_total
        pairs["window_size_2kp1"] = window_size
        # Paper denominator: (2k+1) * N_v / N
        pairs["expected_per_window_paper"] = (
            window_size * pairs["N_v_validator_slots"] / N_total)
        pairs["expected_per_window_code"] = pairs["expected_pct"]
        # Paper closed-form
        denom = (window_size * pairs["N_v_validator_slots"] *
                 pairs["N_sa_signer_total"]).replace(0, np.nan)
        pairs["enrichment_paper"] = (
            pairs["N_av_near_cnt"] * N_total / denom)
        pairs["enrichment_code"] = pairs["enrichment"]

        # Optional signer peak filter (default 0 keeps everything)
        if args.signer_peak_pct > 0:
            peak_per_signer = pairs.groupby("signer")["near_pct"].max()
            qualified = set(peak_per_signer[peak_per_signer >=
                                            args.signer_peak_pct].index)
            before = len(pairs)
            pairs = pairs[pairs["signer"].isin(qualified)]
            print(f"  After signer peak filter (>={args.signer_peak_pct:.0%}): "
                  f"{len(qualified)} signers, {len(pairs)} pairs "
                  f"(dropped {before - len(pairs)})")

        # Exclude trusted for cohort analysis
        untrusted_pairs = pairs[~pairs["trusted"]]
        print(f"  Non-trusted pairs: {len(untrusted_pairs)}")
    else:
        # No pairs passed the enrichment threshold. Keep pairs/untrusted_pairs as
        # empty frames with the full expected schema so the on-disk CSVs have
        # stable headers and the downstream analyst sees "ran, zero enriched".
        extra = ["signer_origin_category","signer_tier","signer_wr","signer_slip",
                 "signer_fg100","signer_jito_count","signer_sol_total_profit",
                 "signer_usd_total_profit","signer_usd_pooled",
                 "N_av_near_cnt","N_sa_signer_total","N_v_validator_slots",
                 "N_total_slots","window_size_2kp1","expected_per_window_paper",
                 "expected_per_window_code","enrichment_paper","enrichment_code"]
        empty_cols = list(pairs.columns) + [
            "v_name", "v_stake", "v_ip_org", "v_ip_city", "v_ip_asn",
            "trusted",
        ] + extra
        pairs = pd.DataFrame(columns=empty_cols)
        untrusted_pairs = pairs.copy()

    # ── Per-signer windowed summary ──────────────────────────────────────
    print("\nBuilding per-signer windowed summaries...")
    summary_rows = []
    for signer in sorted(per_signer.keys()):
        total = signer_totals.get(signer, 0)
        if total == 0:
            continue
        vmap = per_signer[signer]
        row = {"signer": signer, "sandwich_count": total}

        for k in range(OFFSET_MAX + 1):
            offs = [o for o in OFFSETS if abs(o) <= k]
            win_counts = defaultdict(int)
            for val, offset_cnts in vmap.items():
                c = sum(offset_cnts.get(o, 0) for o in offs)
                if c > 0:
                    win_counts[val] = c

            if not win_counts:
                row[f"w{k}_top_val"] = ""
                row[f"w{k}_top_pct"] = 0
                row[f"w{k}_top_enrich"] = 0
                continue

            ranked = sorted(win_counts.items(), key=lambda x: -x[1])
            top_v, top_c = ranked[0]
            top_pct = top_c / total
            top_exp = sum(global_freq[o].get(top_v, 0) for o in offs)
            top_enrich = top_pct / top_exp if top_exp > 0 else 0

            row[f"w{k}_top_val"] = top_v
            row[f"w{k}_top_pct"] = top_pct
            row[f"w{k}_top_enrich"] = top_enrich

        summary_rows.append(row)

    signer_summary = pd.DataFrame(summary_rows)

    # ── Cohort clustering ────────────────────────────────────────────────
    print(f"\nClustering cohorts (min_shared={args.cohort_min_shared})...")
    cohorts, signer_to_vals, val_to_signers = cluster_cohorts(
        untrusted_pairs, args.cohort_min_shared)
    print(f"  Cohorts with >= 2 signers: {len(cohorts)}")

    # ── Reports ──────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  RESULTS")
    print(f"{'='*80}")

    # Highlight: KKK + HwGq type (self-operated validator)
    self_op = pairs[pairs["near_pct"] >= 0.5].sort_values("near_pct", ascending=False)
    if len(self_op) > 0:
        print(f"\n  --- Self-operated validator candidates (near_pct >= 50%) ---")
        for _, r in self_op.iterrows():
            print(f"    {r['signer'][:14]} [wr={r['signer_wr']:.2f} slip={r['signer_slip']:.3f} "
                  f"USD=${r['signer_usd_pooled']:,.0f}]")
            print(f"      -> {vfmt(r['validator'], 14)} "
                  f"near={int(r['near_cnt'])}/{int(r['signer_total'])} "
                  f"({r['near_pct']*100:.1f}%) enrich={r['enrichment']:.0f}x")

    # Multi-signer validators
    multi_val = untrusted_pairs.groupby("validator").agg(
        n_signers=("signer", "nunique"),
        sum_near=("near_cnt", "sum"),
        max_enrich=("enrichment", "max"),
        v_name=("v_name", "first"),
        v_ip_org=("v_ip_org", "first"),
    ).sort_values("n_signers", ascending=False)
    multi_val = multi_val[multi_val["n_signers"] >= 2]

    if len(multi_val) > 0:
        print(f"\n  --- Validators controlling >= 2 signers (non-trusted) ---")
        for val, r in multi_val.head(20).iterrows():
            sigs = untrusted_pairs[untrusted_pairs["validator"] == val]["signer"].unique()
            sig_str = ", ".join(s[:10] for s in sorted(sigs)[:6])
            if len(sigs) > 6:
                sig_str += f" +{len(sigs)-6}"
            org = r['v_ip_org'][:20] if pd.notna(r['v_ip_org']) else ""
            print(f"    {vfmt(val, 14)} ({org}) "
                  f"-> {int(r['n_signers'])} signers: {sig_str}")

    # Cohort details
    if cohorts:
        print(f"\n  --- Signer Cohorts ---")
        cohort_rows = []
        for cidx, members in enumerate(cohorts, 1):
            shared_union = set()
            for s in members:
                shared_union.update(signer_to_vals.get(s, set()))
            total_sw = sum(signer_totals.get(s, 0) for s in members)

            # Validator ASN fingerprint
            orgs = defaultdict(int)
            for v in shared_union:
                org = str(_vi(v, "ip_org", ""))
                if org:
                    orgs[org] += 1
            top_org = ", ".join(f"{o}({c})" for o, c in sorted(orgs.items(), key=lambda x: -x[1])[:3])

            print(f"\n    Cohort {cidx}: {len(members)} signers, "
                  f"{len(shared_union)} validators, {total_sw:,} sandwiches")
            print(f"      Top orgs: {top_org}")

            for sig in sorted(members, key=lambda s: -signer_totals.get(s, 0)):
                n = signer_totals.get(sig, 0)
                n_vals = len(signer_to_vals.get(sig, set()))
                wr = bot_sf.loc[sig, "win_rate"] if sig in bot_sf.index else float("nan")
                slip = bot_sf.loc[sig, "mean_slippage"] if sig in bot_sf.index else float("nan")
                fg100 = bot_sf.loc[sig, "fg100_ratio"] if sig in bot_sf.index and "fg100_ratio" in bot_sf.columns else float("nan")
                usd = signer_usd.get(sig, 0)
                # Top 3 enriched validators for this signer
                sig_pairs = untrusted_pairs[untrusted_pairs["signer"] == sig].nlargest(3, "enrichment")
                top_strs = []
                for _, pr in sig_pairs.iterrows():
                    top_strs.append(f"{vfmt(pr['validator'])}"
                                   f"={pr['near_pct']*100:.1f}%({pr['enrichment']:.0f}x)")
                print(f"      {sig[:16]}... cnt={n:>5} wr={wr:.2f} slip={slip:.3f} "
                      f"fg100={fg100:.2f} USD=${usd:>8,.0f}  {' '.join(top_strs)}")

                cohort_rows.append({
                    "cohort": cidx,
                    "signer": sig,
                    "sandwich_count": n,
                    "win_rate": wr,
                    "mean_slippage": slip,
                    "fg100_ratio": fg100,
                    "usd_profit": usd,
                    "n_enriched_validators": n_vals,
                })

        cohort_df = pd.DataFrame(cohort_rows)
    else:
        cohort_df = pd.DataFrame()

    # ── Summary stats ────────────────────────────────────────────────────
    n_total = len(signer_summary)
    n_baseline = len(signer_summary[signer_summary["w4_top_enrich"] < 2])
    n_moderate = len(signer_summary[(signer_summary["w4_top_enrich"] >= 2) &
                                    (signer_summary["w4_top_enrich"] < 5)])
    n_high = len(signer_summary[signer_summary["w4_top_enrich"] >= 5])
    print(f"\n  --- Enrichment Summary (±4 window) ---")
    print(f"    Baseline-like (<2x):  {n_baseline} signers ({n_baseline/n_total*100:.0f}%)")
    print(f"    Moderate (2-5x):      {n_moderate} signers ({n_moderate/n_total*100:.0f}%)")
    print(f"    High (>=5x):          {n_high} signers ({n_high/n_total*100:.0f}%)")

    # ── Save outputs ─────────────────────────────────────────────────────
    print(f"\nSaving outputs to {out_dir}/...")

    # Reorder columns: attacker info → validator info → formula vars → offsets
    preferred = [
        "signer", "signer_origin_category", "signer_tier",
        "N_sa_signer_total",
        "signer_wr", "signer_slip", "signer_fg100", "signer_jito_count",
        "signer_sol_total_profit", "signer_usd_total_profit", "signer_usd_pooled",
        "validator", "v_name", "v_ip_org", "v_ip_city", "v_ip_asn",
        "v_stake", "trusted",
        "N_av_near_cnt", "N_v_validator_slots", "N_total_slots",
        "window_size_2kp1",
        "expected_per_window_paper", "expected_per_window_code",
        "enrichment_paper", "enrichment_code",
        "near_pct", "peak_offset", "peak_count",
    ] + [f"off_{o}" for o in OFFSETS]
    final_cols = [c for c in preferred if c in pairs.columns] + [
        c for c in pairs.columns if c not in preferred
    ]
    pairs_out = pairs[final_cols] if len(pairs) > 0 else pairs
    untrusted_out = untrusted_pairs[final_cols] if len(untrusted_pairs) > 0 else untrusted_pairs

    pairs_out.to_csv(f"{out_dir}/flagged_pairs_{tag}.csv", index=False)
    untrusted_out.to_csv(f"{out_dir}/untrusted_pairs_{tag}.csv", index=False)
    signer_summary.to_csv(f"{out_dir}/signer_leader_summary_{tag}.csv", index=False)
    if len(cohort_df) > 0:
        cohort_df.to_csv(f"{out_dir}/cohort_members_{tag}.csv", index=False)

    print(f"  flagged_pairs_{tag}.csv ({len(pairs):,} pairs)")
    print(f"  untrusted_pairs_{tag}.csv ({len(untrusted_pairs):,} pairs)")
    print(f"  signer_leader_summary_{tag}.csv ({len(signer_summary):,} signers)")
    if len(cohort_df) > 0:
        print(f"  cohort_members_{tag}.csv ({len(cohort_df):,} rows)")

    # ── Charts ───────────────────────────────────────────────────────────
    print(f"\nGenerating charts...")
    plot_enrichment_overview(signer_summary, chart_dir, tag)
    plot_cohort_detail(cohorts, signer_to_vals, bot_sf, per_signer,
                       signer_totals, global_freq, v_info, chart_dir, tag)
    print(f"  Charts saved to {chart_dir}/")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
