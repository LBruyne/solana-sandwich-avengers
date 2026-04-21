"""
Phase 1: Signer-level Data Preparation & Summary
=================================================
Extract sandwiches from ClickHouse by category, compute per-sandwich metrics
(recomputed slippage consumption, unified proximity, Jito bundle co-location),
aggregate to signer-level features, and produce comprehensive summary reports
with charts.

Categories:
  standard           - signerSame=true, NOT multi-split (default)
  multi_split         - signerSame=true, multiFrontRun OR multiBackRun
  diff_signer_owner   - signerSame=false, ownerSame=true

Note: diff_signer_transfer (signerSame=false, ownerSame=false,
hasTransfer=true) is detected by the Watcher but has been removed from
the intent-classification pipeline: empirically it contains zero reliable
attackers (6,920 sandwiches, $-46 net USD, $1.78 positive-USD total; the
only 3 Signal-Bot matches all hit CNT exactly 5 with $0 USD profit — pure
structural coincidence). Keeping it in the framework would only dilute
the classifier. See docs/definitions.md and docs/evaluator_design.md.

Usage:
  python 1_signer_data_preparation_and_summary.py --start-epoch 946 --end-epoch 956
  python 1_signer_data_preparation_and_summary.py --category multi_split
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from utils.db import get_client
from utils.programs import classify_program

SLOTS_PER_EPOCH = 432_000

# ── CLI ──────────────────────────────────────────────────────────────────────

CATEGORY_SCOPES = {
    "standard": """
        signerSame = true
        AND multiFrontRun = false
        AND multiBackRun = false
    """,
    "multi_split": """
        signerSame = true
        AND (multiFrontRun = true OR multiBackRun = true)
    """,
    "diff_signer_owner": """
        signerSame = false
        AND ownerSame = true
        AND multiFrontRun = false
        AND multiBackRun = false
    """,
    # diff_signer_transfer is intentionally removed from the pipeline; see
    # the module docstring above.
}


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1: signer data preparation & summary")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=956)
    p.add_argument("--category", type=str, default="standard",
                   choices=list(CATEGORY_SCOPES.keys()),
                   help="Sandwich category to analyze")
    return p.parse_args()


# ── Data Fetching ────────────────────────────────────────────────────────────

# Default scope (overridden by --category in main)
SANDWICH_SCOPE = CATEGORY_SCOPES["standard"]


def fetch_sandwiches(client, start_slot, end_slot):
    """Fetch target sandwiches, deduplicated.

    multiFrontRun/multiBackRun flags are included so downstream analysis
    can split multi_split into structural sub-variants (multi_front,
    multi_back, both). These flags are always all-false for non-multi_split
    categories per the CATEGORY_SCOPES filter, so they are harmless there.
    """
    query = f"""
    SELECT sandwichId, crossBlock, slot, timestamp, tokenA, tokenB,
           consecutive, multiVictim, victimCount, adverseCount,
           multiFrontRun, multiBackRun, frontCount, backCount,
           perfect, relativeDiffB, profitA
    FROM sandwiches
    WHERE {SANDWICH_SCOPE}
      AND slot >= {start_slot} AND slot < {end_slot}
    """
    df = client.query_df(query)
    return df.drop_duplicates(subset=["sandwichId"])


def fetch_sandwich_txs(client, start_slot, end_slot):
    """Fetch all txs belonging to target sandwiches, deduplicated."""
    query = f"""
    SELECT st.sandwichId, st.type, st.slot, st.position, st.fee,
           st.signature, st.signers, st.inBundle, st.programs,
           st.fromAmount, st.toAmount,
           st.slippageUtilization
    FROM sandwich_txs st
    WHERE st.sandwichId IN (
        SELECT sandwichId FROM sandwiches
        WHERE {SANDWICH_SCOPE}
          AND slot >= {start_slot} AND slot < {end_slot}
    )
    """
    df = client.query_df(query)
    return df.drop_duplicates(subset=["sandwichId", "type", "slot", "position"])


def fetch_jito_bundle_mapping(client, txs):
    """Build signature -> bundleId mapping for all inBundle txs."""
    in_bundle_txs = txs[txs["inBundle"] == True]
    if len(in_bundle_txs) == 0:
        return {}

    sig_set = set(in_bundle_txs["signature"].values)
    unique_slots = sorted(in_bundle_txs["slot"].unique())

    sig_to_bundle = {}
    batch_size = 200
    for i in range(0, len(unique_slots), batch_size):
        batch = unique_slots[i:i + batch_size]
        slot_list = ",".join(str(int(s)) for s in batch)
        bundles = client.query_df(
            f"SELECT bundleId, transactions FROM jito_bundles WHERE slot IN ({slot_list})"
        )
        for _, brow in bundles.iterrows():
            bid = brow["bundleId"]
            for sig in brow["transactions"]:
                if sig in sig_set:
                    sig_to_bundle[sig] = bid

    return sig_to_bundle


def fetch_slot_tx_counts(client, start_slot, end_slot):
    """Fetch per-slot transaction counts for proximity calculation."""
    query = f"""
    SELECT slot, txCount
    FROM slot_txs
    WHERE slot >= {start_slot} AND slot < {end_slot}
    """
    df = client.query_df(query)
    return df.drop_duplicates(subset=["slot"]).set_index("slot")["txCount"]


# ── Per-Sandwich Metrics ─────────────────────────────────────────────────────

def compute_per_sandwich_metrics(sandwiches, txs, slot_tx_counts, client,
                                  category="standard"):
    """Compute per-sandwich metrics from tx-level data."""
    front_txs = txs[txs["type"] == "frontRun"].copy()
    back_txs = txs[txs["type"] == "backRun"].copy()
    victim_txs = txs[txs["type"] == "victim"].copy()

    if category == "diff_signer_owner":
        signer_map, entity_map, uf = _extract_signer_with_entity_merging(
            front_txs, back_txs)
    else:
        signer_map = _extract_common_signer(front_txs, back_txs)
        entity_map = None

    sig_to_bundle = fetch_jito_bundle_mapping(client, txs)
    jito_map = _compute_jito_same_bundle(front_txs, back_txs, victim_txs, sig_to_bundle)

    slippage_map = victim_txs.groupby("sandwichId")["slippageUtilization"].apply(_compute_slippage)

    proximity_map = _compute_unified_proximity(front_txs, back_txs, victim_txs, slot_tx_counts)
    back_gap_map = _compute_back_gap(back_txs, victim_txs, slot_tx_counts)

    s_idx = sandwiches.set_index("sandwichId")
    metrics = pd.DataFrame({
        "signer": signer_map,
        "is_profitable": s_idx["profitA"] > 0,
        "profit": s_idx["profitA"],
        "cross_block": s_idx["crossBlock"],
        "slot": s_idx["slot"],
        "token_a": s_idx["tokenA"],
        "token_b": s_idx["tokenB"],
        "victim_count": s_idx["victimCount"],
        "adverse_count": s_idx["adverseCount"],
        "consecutive": s_idx["consecutive"],
        "multi_front": s_idx["multiFrontRun"],
        "multi_back": s_idx["multiBackRun"],
        "front_count": s_idx["frontCount"],
        "back_count": s_idx["backCount"],
        "slippage_consumption": slippage_map,
        "jito_bundle": jito_map,
        "proximity": proximity_map,
        "back_gap": back_gap_map,
    })
    return metrics, entity_map


class _UnionFind:
    """Simple Union-Find for merging signers into attacker entities."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic: smaller string becomes root
            if ra > rb:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _extract_common_signer(front_txs, back_txs):
    """Extract the signer for each sandwich.

    For signerSame=true: use the common signer between front and back.
    For signerSame=false: use the front-run signer (the entity initiating the attack).
    """
    front_signers = front_txs.groupby("sandwichId")["signers"].first()
    back_signers = back_txs.groupby("sandwichId")["signers"].first()
    common_ids = front_signers.index.intersection(back_signers.index)

    result = {}
    for sid in common_ids:
        overlap = set(front_signers[sid]) & set(back_signers[sid])
        if overlap:
            result[sid] = sorted(overlap)[0]
        else:
            # diff-signer: use front-run signer as the attacker identity
            result[sid] = front_signers[sid][0]

    return pd.Series(result, name="signer")


def _extract_signer_with_entity_merging(front_txs, back_txs):
    """Extract attacker entity for diff-signer sandwiches using UnionFind.

    Merges all signers appearing in the same sandwich's front/back txs into
    one attacker entity. This links signer keys that co-appear across
    sandwiches via transitive closure.

    Returns (signer_map, entity_map, uf):
      - signer_map: sandwichId -> entity root (canonical signer)
      - entity_map: original signer -> entity root
      - uf: the UnionFind instance
    """
    uf = _UnionFind()

    # Collect ALL signers per sandwich from front and back txs
    sandwich_all_signers = {}
    for df in [front_txs, back_txs]:
        for _, row in df.iterrows():
            sid = row["sandwichId"]
            if sid not in sandwich_all_signers:
                sandwich_all_signers[sid] = set()
            sandwich_all_signers[sid].update(row["signers"])

    # Union all signers within each sandwich
    for sid, signers in sandwich_all_signers.items():
        signers_list = sorted(signers)
        for i in range(1, len(signers_list)):
            uf.union(signers_list[0], signers_list[i])

    # Build canonical mapping: root -> smallest signer
    all_signers = set()
    for signers in sandwich_all_signers.values():
        all_signers.update(signers)

    from collections import defaultdict
    root_to_signers = defaultdict(list)
    for s in sorted(all_signers):
        root_to_signers[uf.find(s)].append(s)

    root_to_canonical = {}
    for root, members in root_to_signers.items():
        root_to_canonical[root] = members[0]

    # Build signer map: sandwichId -> canonical signer
    result = {}
    for sid, signers in sandwich_all_signers.items():
        root = uf.find(sorted(signers)[0])
        result[sid] = root_to_canonical.get(root, root)

    # Build entity map: original signer -> canonical signer
    entity_map = {}
    for s in all_signers:
        root = uf.find(s)
        entity_map[s] = root_to_canonical.get(root, root)

    return pd.Series(result, name="signer"), entity_map, uf


def _compute_jito_same_bundle(front_txs, back_txs, victim_txs, sig_to_bundle):
    """Determine if front+victim+back are ALL in the same Jito bundle."""
    from collections import defaultdict
    sandwich_bundle_types = defaultdict(lambda: defaultdict(set))

    for df, typ in [(front_txs, "front"), (back_txs, "back"), (victim_txs, "victim")]:
        for _, row in df.iterrows():
            bid = sig_to_bundle.get(row["signature"])
            if bid is not None:
                sandwich_bundle_types[row["sandwichId"]][bid].add(typ)

    results = {}
    all_sids = set(front_txs["sandwichId"]) | set(back_txs["sandwichId"])
    for sid in all_sids:
        if sid in sandwich_bundle_types:
            for bid, types in sandwich_bundle_types[sid].items():
                if "front" in types and "victim" in types and "back" in types:
                    results[sid] = True
                    break
            else:
                results[sid] = False
        else:
            results[sid] = False

    return pd.Series(results, name="jito_bundle")


def _compute_unified_proximity(front_txs, back_txs, victim_txs, slot_tx_counts):
    """Compute proximity as front-run -> first victim distance in tx-count units."""
    f = front_txs.groupby("sandwichId")[["slot", "position"]].first()

    v_sorted = victim_txs.sort_values(["slot", "position"])
    v_first = v_sorted.groupby("sandwichId")[["slot", "position"]].first()
    v_first.columns = ["v1_slot", "v1_pos"]

    common = f.index.intersection(v_first.index)
    results = {}

    for sid in common:
        f_slot, f_pos = int(f.loc[sid, "slot"]), int(f.loc[sid, "position"])
        v1_slot, v1_pos = int(v_first.loc[sid, "v1_slot"]), int(v_first.loc[sid, "v1_pos"])
        results[sid] = _slot_distance(f_slot, f_pos, v1_slot, v1_pos, slot_tx_counts)

    return pd.Series(results, name="proximity")


def _compute_back_gap(back_txs, victim_txs, slot_tx_counts):
    """Back gap: distance from last victim to the (first) back-run, tx-count units.

    Together with proximity (front gap), this characterises how tightly the
    attacker wraps the victim on both sides — a key ordering-control signal.
    """
    v_sorted = victim_txs.sort_values(["slot", "position"])
    v_last = v_sorted.groupby("sandwichId")[["slot", "position"]].last()
    v_last.columns = ["vN_slot", "vN_pos"]

    b_sorted = back_txs.sort_values(["slot", "position"])
    b_first = b_sorted.groupby("sandwichId")[["slot", "position"]].first()
    b_first.columns = ["b1_slot", "b1_pos"]

    common = v_last.index.intersection(b_first.index)
    results = {}
    for sid in common:
        vN_slot, vN_pos = int(v_last.loc[sid, "vN_slot"]), int(v_last.loc[sid, "vN_pos"])
        b1_slot, b1_pos = int(b_first.loc[sid, "b1_slot"]), int(b_first.loc[sid, "b1_pos"])
        results[sid] = _slot_distance(vN_slot, vN_pos, b1_slot, b1_pos, slot_tx_counts)

    return pd.Series(results, name="back_gap")


def _slot_distance(slot_a, pos_a, slot_b, pos_b, slot_tx_counts):
    """Compute distance in tx-count units between two positions."""
    if slot_a == slot_b:
        return int(pos_b) - int(pos_a)

    dist = int(slot_tx_counts.get(slot_a, 0)) - int(pos_a)

    gap = slot_b - slot_a
    if gap <= 100:
        for s in range(slot_a + 1, slot_b):
            dist += int(slot_tx_counts.get(s, 0))
    else:
        avg_tx = 1272
        dist += avg_tx * (gap - 1)

    dist += int(pos_b)
    return dist


def _compute_slippage(utils):
    """Recompute sandwich-level slippage consumption from victim utilizations."""
    values = utils.values
    if np.any((values == -2) | (values == -3)):
        return -2.0
    return float(np.max(values))


# ── Signer-level Aggregation ─────────────────────────────────────────────────

def aggregate_to_signer(metrics):
    """Aggregate per-sandwich metrics to signer-level features."""
    g = metrics.groupby("signer")
    is_sol = metrics["token_a"] == "SOL"

    # Model Features
    sandwich_count = g.size().rename("sandwich_count")
    slot_range = g["slot"].agg(["min", "max"])
    active_slot_span = (slot_range["max"] - slot_range["min"]).rename("active_slot_span")
    sandwich_frequency = (sandwich_count / active_slot_span.replace(0, np.nan)).rename("sandwich_frequency")
    avg_interval = g["slot"].apply(_avg_interval).rename("avg_interval_slots")

    win_rate = g["is_profitable"].mean().rename("win_rate")
    sol_metrics = metrics[is_sol]
    if len(sol_metrics) > 0:
        sol_avg_profit = sol_metrics.groupby("signer")["profit"].mean().rename("sol_avg_profit")
    else:
        sol_avg_profit = pd.Series(dtype=float, name="sol_avg_profit")

    jito_rate = g["jito_bundle"].mean().rename("jito_rate")
    jito_count = g["jito_bundle"].sum().rename("jito_count").astype(int)
    median_proximity = g["proximity"].median().rename("median_proximity")

    # Front/back gap distribution at canonical thresholds. Gives a fuller
    # picture of ordering tightness than any single summary statistic and
    # matches the legacy layer2_classification columns.
    FG_BG_THRESHOLDS = [1, 2, 5, 10, 20, 50, 100]
    fg_median = g["proximity"].median().rename("fg_median")
    bg_median = g["back_gap"].median().rename("bg_median")
    fg_ratios = {}
    bg_ratios = {}
    for t in FG_BG_THRESHOLDS:
        fg_ratios[f"fg_le_{t}_ratio"] = g["proximity"].apply(
            lambda s, t=t: (s <= t).mean()).rename(f"fg_le_{t}_ratio")
        bg_ratios[f"bg_le_{t}_ratio"] = g["back_gap"].apply(
            lambda s, t=t: (s <= t).mean()).rename(f"bg_le_{t}_ratio")

    # Slippage: average only over valid samples (0-1 range).
    # Signers with NO valid slippage data get NaN, which correctly excludes
    # them from the Signal Bot gate (NaN >= 0.75 → False) in step 3.
    valid_slippage = metrics[(metrics["slippage_consumption"] >= 0) & (metrics["slippage_consumption"] <= 1)]
    if len(valid_slippage) > 0:
        vg = valid_slippage.groupby("signer")
        mean_slippage = vg["slippage_consumption"].mean().rename("mean_slippage")
    else:
        mean_slippage = pd.Series(dtype=float, name="mean_slippage")

    # Info Columns
    in_block_count = g["cross_block"].apply(lambda x: (~x).sum()).rename("in_block_count")
    cross_block_count = g["cross_block"].sum().rename("cross_block_count").astype(int)

    sol_count = metrics[is_sol].groupby("signer").size().rename("sol_count")
    nonsol_count = metrics[~is_sol].groupby("signer").size().rename("nonsol_count")

    sol_win_rate = (
        sol_metrics.groupby("signer")["is_profitable"].mean().rename("sol_win_rate")
        if len(sol_metrics) > 0
        else pd.Series(dtype=float, name="sol_win_rate")
    )
    nonsol_metrics = metrics[~is_sol]
    nonsol_win_rate = (
        nonsol_metrics.groupby("signer")["is_profitable"].mean().rename("nonsol_win_rate")
        if len(nonsol_metrics) > 0
        else pd.Series(dtype=float, name="nonsol_win_rate")
    )

    slippage_valid_ratio = g.apply(
        lambda df: (df["slippage_consumption"] >= 0).mean()
    ).rename("slippage_valid_ratio")
    slippage_no_protection_ratio = g.apply(
        lambda df: (df["slippage_consumption"] == -1).mean()
    ).rename("slippage_no_protection_ratio")

    signer_df = pd.concat([
        sandwich_count, active_slot_span, sandwich_frequency, avg_interval,
        win_rate, sol_avg_profit,
        jito_rate, jito_count, median_proximity,
        mean_slippage,
        in_block_count, cross_block_count,
        sol_count, nonsol_count, sol_win_rate, nonsol_win_rate,
        slippage_valid_ratio, slippage_no_protection_ratio,
        fg_median, bg_median,
        *fg_ratios.values(),
        *bg_ratios.values(),
    ], axis=1)

    for col in ["sol_count", "nonsol_count", "jito_count"]:
        signer_df[col] = signer_df[col].fillna(0).astype(int)

    return signer_df


def _avg_interval(slots):
    """Compute average interval between unique active slots."""
    unique_slots = np.unique(slots.values)
    if len(unique_slots) < 2:
        return np.nan
    intervals = np.diff(unique_slots)
    return float(np.mean(intervals))


# ── Token Price Loading ──────────────────────────────────────────────────────

def load_token_prices():
    """Load token prices for USD conversion."""
    price_path = os.path.join(os.path.dirname(__file__), "data", "token_prices", "prices.csv")
    prices = {}
    if os.path.exists(price_path):
        pdf = pd.read_csv(price_path)
        for _, row in pdf.iterrows():
            if pd.notna(row.get("usd_price")):
                prices[row["token"]] = row["usd_price"]

    if "SOL" not in prices:
        try:
            import requests
            resp = requests.get(
                "https://solana-gateway.moralis.io/token/mainnet/So11111111111111111111111111111111111111112/price",
                headers={"X-API-Key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6IjJhYTc1MGZlLWY0YTMtNGNkOC1iOTdkLWE2YzE5ZmMyNmE4OSIsIm9yZ0lkIjoiNTA5NTI2IiwidXNlcklkIjoiNTI0MjQ2IiwidHlwZUlkIjoiMmQ1M2E0M2YtMzExOC00Y2IxLWJiN2ItN2YyZTMyOWQ3MmQ3IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NzYxNTA3MzEsImV4cCI6NDkzMTkxMDczMX0.AGEZYIgBc_q9O1maVv0siYOrZm2HnNFflcPJdpGjQSI"},
                timeout=10,
            )
            if resp.ok:
                prices["SOL"] = resp.json().get("usdPrice", 86.0)
        except Exception:
            pass
        if "SOL" not in prices:
            prices["SOL"] = 86.0
    return prices


def compute_usd_profit(per_sandwich, token_prices):
    """Compute per-sandwich USD profit using token prices."""
    per_sandwich["token_a_price"] = per_sandwich["token_a"].map(token_prices)
    per_sandwich["usd_profit"] = per_sandwich["profit"] * per_sandwich["token_a_price"]
    return per_sandwich


# ── Signer Summary & Statistics ──────────────────────────────────────────────

COUNT_BUCKETS = [
    (1, 10, "1-10"),
    (11, 50, "11-50"),
    (51, 100, "51-100"),
    (101, 500, "101-500"),
    (501, 1000, "501-1000"),
    (1001, None, "1001+"),
]


def _bucket_label(cnt):
    for lo, hi, label in COUNT_BUCKETS:
        if hi is None:
            if cnt >= lo:
                return label
        elif lo <= cnt <= hi:
            return label
    return "unknown"


def build_signer_summary(per_sandwich, signer_df, token_prices):
    """Build a signer summary dataframe with USD profit and bucket labels."""
    # Per-signer USD profit aggregation
    # Compute on all sandwiches: usd_profit is NaN for unpriced tokens
    usd_by_signer = per_sandwich.groupby("signer").agg(
        usd_total_profit=("usd_profit", "sum"),
        usd_positive_count=("usd_profit", lambda x: (x > 0).sum()),
        usd_negative_count=("usd_profit", lambda x: (x < 0).sum()),
        usd_priced_count=("usd_profit", lambda x: x.notna().sum()),
    )
    # Coverage = fraction of sandwiches with price data
    usd_by_signer["usd_price_coverage"] = (
        usd_by_signer["usd_priced_count"] / per_sandwich.groupby("signer").size()
    )

    # SOL-only profit
    sol_ps = per_sandwich[per_sandwich["token_a"] == "SOL"]
    sol_by_signer = sol_ps.groupby("signer").agg(
        sol_total_profit=("profit", "sum"),
        sol_sandwich_count=("profit", "count"),
    )

    summary = signer_df.copy()
    summary = summary.join(usd_by_signer, how="left")
    summary = summary.join(sol_by_signer, how="left")
    summary["usd_total_profit"] = summary["usd_total_profit"].fillna(0)
    summary["sol_total_profit"] = summary["sol_total_profit"].fillna(0)
    summary["sol_sandwich_count"] = summary["sol_sandwich_count"].fillna(0).astype(int)
    summary["count_bucket"] = summary["sandwich_count"].apply(_bucket_label)

    return summary


def print_overall_summary(summary, per_sandwich, label="ALL SIGNERS"):
    """Print comprehensive signer-level statistics."""
    n_signers = len(summary)
    n_sandwiches = int(summary["sandwich_count"].sum())
    sol_profit = float(summary["sol_total_profit"].sum())
    usd_profit = float(summary["usd_total_profit"].sum())

    print(f"\n{'='*80}")
    print(f"  SIGNER SUMMARY: {label}")
    print(f"{'='*80}")

    print(f"\n  Signers:          {n_signers:>10,}")
    print(f"  Total sandwiches: {int(n_sandwiches):>10,}")
    print(f"  SOL profit:       {sol_profit:>14,.4f} SOL")
    print(f"  USD profit:       ${usd_profit:>13,.2f}")

    # WR distribution
    print(f"\n  --- Win Rate Distribution ---")
    wr_bins = [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
               (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.001)]
    wr_labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
                 "50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
    print(f"  {'WR Range':>10}  {'Signers':>8}  {'%':>7}  {'Sandwiches':>12}  "
          f"{'SOL Profit':>14}  {'USD Profit':>14}")
    print(f"  {'-'*75}")
    for (lo, hi), lbl in zip(wr_bins, wr_labels):
        mask = (summary["win_rate"] >= lo) & (summary["win_rate"] < hi)
        sub = summary[mask]
        cnt = len(sub)
        sw = int(sub["sandwich_count"].sum())
        sol_p = sub["sol_total_profit"].sum()
        usd_p = sub["usd_total_profit"].sum()
        print(f"  {lbl:>10}  {cnt:>8,}  {cnt/n_signers*100:>6.1f}%  {sw:>12,}  "
              f"{sol_p:>14,.2f}  ${usd_p:>13,.2f}")

    # Count bucket breakdown
    print(f"\n  --- Count Bucket Breakdown ---")
    print(f"  {'Bucket':>10}  {'Signers':>8}  {'%':>7}  {'Sandwiches':>12}  {'%':>7}  "
          f"{'Avg WR':>7}  {'Med WR':>7}  {'SOL Profit':>14}  {'USD Profit':>14}")
    print(f"  {'-'*105}")
    bucket_order = [b[2] for b in COUNT_BUCKETS]
    for bucket in bucket_order:
        sub = summary[summary["count_bucket"] == bucket]
        cnt = len(sub)
        if cnt == 0:
            continue
        sw = int(sub["sandwich_count"].sum())
        avg_wr = sub["win_rate"].mean()
        med_wr = sub["win_rate"].median()
        sol_p = sub["sol_total_profit"].sum()
        usd_p = sub["usd_total_profit"].sum()
        sw_pct = sw / n_sandwiches * 100 if n_sandwiches > 0 else 0
        print(f"  {bucket:>10}  {cnt:>8,}  {cnt/n_signers*100:>6.1f}%  {sw:>12,}  "
              f"{sw_pct:>6.1f}%  {avg_wr:>7.3f}  {med_wr:>7.3f}  "
              f"{sol_p:>14,.2f}  ${usd_p:>13,.2f}")

    return n_signers, int(n_sandwiches), sol_profit, usd_profit


def print_filter_comparison(all_stats, filt_stats):
    """Print comparison between all and filtered signers."""
    a_sig, a_sw, a_sol, a_usd = all_stats
    f_sig, f_sw, f_sol, f_usd = filt_stats

    print(f"\n{'='*80}")
    print(f"  FILTER COMPARISON")
    print(f"{'='*80}")
    print(f"  {'Metric':>20}  {'All':>14}  {'Filtered':>14}  {'Retained %':>12}")
    print(f"  {'-'*65}")
    print(f"  {'Signers':>20}  {a_sig:>14,}  {f_sig:>14,}  {f_sig/a_sig*100:>11.2f}%")
    print(f"  {'Sandwiches':>20}  {a_sw:>14,}  {f_sw:>14,}  {f_sw/a_sw*100:>11.2f}%")
    print(f"  {'SOL Profit':>20}  {a_sol:>14,.2f}  {f_sol:>14,.2f}  "
          f"{'N/A' if a_sol == 0 else f'{f_sol/a_sol*100:.2f}%':>12}")
    print(f"  {'USD Profit':>20}  ${a_usd:>13,.2f}  ${f_usd:>13,.2f}  "
          f"{'N/A' if a_usd == 0 else f'{f_usd/a_usd*100:.2f}%':>12}")


# ── Chart Generation ─────────────────────────────────────────────────────────

def generate_charts(summary_all, summary_filtered, out_dir, tag):
    """Generate all summary charts."""
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)

    _plot_wr_distribution(summary_all, chart_dir, tag, "all")
    _plot_wr_distribution(summary_filtered, chart_dir, tag, "filtered")
    _plot_bucket_wr_boxplot(summary_all, chart_dir, tag, "all")
    _plot_bucket_wr_boxplot(summary_filtered, chart_dir, tag, "filtered")
    _plot_bucket_profit_bar(summary_all, chart_dir, tag, "all")
    _plot_bucket_profit_bar(summary_filtered, chart_dir, tag, "filtered")
    _plot_bucket_signer_sandwich_count(summary_all, chart_dir, tag, "all")
    _plot_bucket_signer_sandwich_count(summary_filtered, chart_dir, tag, "filtered")

    print(f"\n  Charts saved to {chart_dir}/")


def _plot_wr_distribution(summary, chart_dir, tag, subset):
    """Histogram of win rate distribution."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # By signer count
    axes[0].hist(summary["win_rate"].dropna(), bins=20, edgecolor="black", alpha=0.7)
    axes[0].set_xlabel("Win Rate")
    axes[0].set_ylabel("Number of Signers")
    axes[0].set_title(f"Win Rate Distribution (by signer count, {subset})")

    # By sandwich count (weighted)
    axes[1].hist(summary["win_rate"].dropna(), bins=20, edgecolor="black", alpha=0.7,
                 weights=summary.loc[summary["win_rate"].notna(), "sandwich_count"])
    axes[1].set_xlabel("Win Rate")
    axes[1].set_ylabel("Number of Sandwiches")
    axes[1].set_title(f"Win Rate Distribution (by sandwich count, {subset})")
    axes[1].yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"wr_distribution_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_wr_boxplot(summary, chart_dir, tag, subset):
    """Box plot of win rate by count bucket."""
    bucket_order = [b[2] for b in COUNT_BUCKETS]
    data = []
    labels = []
    for bucket in bucket_order:
        sub = summary[summary["count_bucket"] == bucket]["win_rate"].dropna()
        if len(sub) > 0:
            data.append(sub.values)
            labels.append(bucket)

    if not data:
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    bp = ax.boxplot(data, labels=labels, patch_artist=True)
    for patch in bp["boxes"]:
        patch.set_facecolor("#4C72B0")
        patch.set_alpha(0.7)
    ax.set_xlabel("Sandwich Count Bucket")
    ax.set_ylabel("Win Rate")
    ax.set_title(f"Win Rate by Count Bucket ({subset})")
    ax.set_ylim(-0.05, 1.05)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"wr_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_profit_bar(summary, chart_dir, tag, subset):
    """Bar chart of SOL and USD profit by count bucket."""
    bucket_order = [b[2] for b in COUNT_BUCKETS]
    sol_profits = []
    usd_profits = []
    labels = []
    for bucket in bucket_order:
        sub = summary[summary["count_bucket"] == bucket]
        if len(sub) > 0:
            sol_profits.append(sub["sol_total_profit"].sum())
            usd_profits.append(sub["usd_total_profit"].sum())
            labels.append(bucket)

    if not labels:
        return

    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in sol_profits]
    ax1.bar(x, sol_profits, color=colors, edgecolor="black", alpha=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=30)
    ax1.set_ylabel("SOL Profit")
    ax1.set_title(f"SOL Profit by Count Bucket ({subset})")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.axhline(y=0, color="black", linewidth=0.5)

    colors2 = ["#2ca02c" if v >= 0 else "#d62728" for v in usd_profits]
    ax2.bar(x, usd_profits, color=colors2, edgecolor="black", alpha=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, rotation=30)
    ax2.set_ylabel("USD Profit")
    ax2.set_title(f"USD Profit by Count Bucket ({subset})")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax2.axhline(y=0, color="black", linewidth=0.5)

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"profit_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_signer_sandwich_count(summary, chart_dir, tag, subset):
    """Stacked bar: signers and sandwiches per bucket."""
    bucket_order = [b[2] for b in COUNT_BUCKETS]
    signer_counts = []
    sandwich_counts = []
    labels = []
    for bucket in bucket_order:
        sub = summary[summary["count_bucket"] == bucket]
        if len(sub) > 0:
            signer_counts.append(len(sub))
            sandwich_counts.append(int(sub["sandwich_count"].sum()))
            labels.append(bucket)

    if not labels:
        return

    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    ax1.bar(x, signer_counts, color="#4C72B0", edgecolor="black", alpha=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=30)
    ax1.set_ylabel("Number of Signers")
    ax1.set_title(f"Signers per Count Bucket ({subset})")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    ax2.bar(x, sandwich_counts, color="#DD8452", edgecolor="black", alpha=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, rotation=30)
    ax2.set_ylabel("Number of Sandwiches")
    ax2.set_title(f"Sandwiches per Count Bucket ({subset})")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"count_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


# ── Epoch Processing ─────────────────────────────────────────────────────────

def process_epoch(client, epoch, category="standard"):
    """Process a single epoch: fetch data, compute per-sandwich metrics."""
    start_slot = epoch * SLOTS_PER_EPOCH
    end_slot = (epoch + 1) * SLOTS_PER_EPOCH

    sandwiches = fetch_sandwiches(client, start_slot, end_slot)
    if len(sandwiches) == 0:
        return pd.DataFrame(), None

    txs = fetch_sandwich_txs(client, start_slot, end_slot)
    slot_tx_counts = fetch_slot_tx_counts(client, start_slot, end_slot)
    metrics, entity_map = compute_per_sandwich_metrics(
        sandwiches, txs, slot_tx_counts, client, category=category)

    del txs, sandwiches, slot_tx_counts
    return metrics, entity_map


# ── Main ─────────────────────────────────────────────────────────────────────

MODEL_FEATURES = [
    "sandwich_count", "sandwich_frequency", "avg_interval_slots",
    "win_rate", "sol_avg_profit",
    "jito_rate", "jito_count", "median_proximity",
    "mean_slippage",
]


def main():
    args = parse_args()
    start_epoch = args.start_epoch
    end_epoch = args.end_epoch
    category = args.category
    num_epochs = end_epoch - start_epoch + 1

    # Set the global SANDWICH_SCOPE based on category
    global SANDWICH_SCOPE
    SANDWICH_SCOPE = CATEGORY_SCOPES[category]

    print(f"=== Phase 1: Signer Data Preparation & Summary ===")
    print(f"Category: {category}")
    print(f"Epoch range: {start_epoch}-{end_epoch} ({num_epochs} epochs)")

    client = get_client()

    # ── Step 1: Process each epoch ────────────────────────────────────────
    all_metrics = []
    merged_entity_map = {}
    for epoch in range(start_epoch, end_epoch + 1):
        print(f"\n[Epoch {epoch}] Processing...")
        epoch_metrics, epoch_entity_map = process_epoch(
            client, epoch, category=category)
        if len(epoch_metrics) > 0:
            print(f"  {len(epoch_metrics):,} sandwiches, "
                  f"{epoch_metrics['signer'].nunique():,} signers")
            all_metrics.append(epoch_metrics)
            if epoch_entity_map:
                merged_entity_map.update(epoch_entity_map)

    per_sandwich = pd.concat(all_metrics, ignore_index=False)
    del all_metrics

    # For diff-signer categories: re-merge entities across epochs.
    # Each epoch builds its own UnionFind independently using signer
    # co-occurrence + owner bridging. Cross-epoch chaining: union each
    # signer with its per-epoch entity root to propagate links.
    if category == "diff_signer_owner" and merged_entity_map:
        uf_global = _UnionFind()
        for signer, root in merged_entity_map.items():
            uf_global.union(signer, root)
        # Re-map per_sandwich signer to global entity root
        global_map = {s: uf_global.find(s) for s in uf_global.parent}
        per_sandwich["signer"] = per_sandwich["signer"].map(
            lambda s: global_map.get(s, s))
        n_original = len(merged_entity_map)
        n_entities = len(set(global_map.values()))
        print(f"\nEntity merging: {n_original} signers -> {n_entities} entities")

    print(f"\nTotal: {len(per_sandwich):,} sandwiches, "
          f"{per_sandwich['signer'].nunique():,} signers (entities)")

    # ── Step 2: Signer-level aggregation ──────────────────────────────────
    print("\nAggregating to signer level...")
    signer_df = aggregate_to_signer(per_sandwich)

    # For diff-signer categories: add n_signers (number of signing keys per entity)
    if category == "diff_signer_owner" and merged_entity_map:
        entity_sizes = pd.Series(merged_entity_map).groupby(
            lambda s: merged_entity_map.get(s, s)).size()
        # Re-index by canonical entity root (the same as signer_df index)
        uf_g = _UnionFind()
        for signer, root in merged_entity_map.items():
            uf_g.union(signer, root)
        canonical_sizes = {}
        for ent, sz in entity_sizes.items():
            canonical_sizes[uf_g.find(ent)] = sz
        signer_df["n_signers"] = pd.Series(canonical_sizes)
        signer_df["n_signers"] = signer_df["n_signers"].fillna(1).astype(int)

    print(f"  {len(signer_df):,} entities" if category.startswith("diff_signer")
          else f"  {len(signer_df):,} signers")

    # ── Step 3: USD profit computation ────────────────────────────────────
    print("\nLoading token prices...")
    token_prices = load_token_prices()
    print(f"  {len(token_prices)} tokens loaded, SOL=${token_prices.get('SOL', 0):.2f}")
    per_sandwich = compute_usd_profit(per_sandwich, token_prices)

    priced_pct = per_sandwich["usd_profit"].notna().mean() * 100
    print(f"  Price coverage: {priced_pct:.1f}% of sandwiches")

    # ── Step 4: Build signer summary with USD profit ──────────────────────
    summary_all = build_signer_summary(per_sandwich, signer_df, token_prices)

    # ── Step 5: Output data files ─────────────────────────────────────────
    out_dir = f"data/1_signer_data_preparation_and_summary/{category}"
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{start_epoch}_{end_epoch}"

    per_sandwich.to_parquet(f"{out_dir}/per_sandwich_metrics_{tag}.parquet")
    signer_df.to_parquet(f"{out_dir}/signer_features_{tag}.parquet")
    signer_df.to_csv(f"{out_dir}/signer_features_{tag}.csv")
    summary_all.to_csv(f"{out_dir}/signer_summary_all_{tag}.csv")
    print(f"\nSaved to {out_dir}/:")
    print(f"  per_sandwich_metrics_{tag}.parquet  ({len(per_sandwich):,} rows)")
    print(f"  signer_features_{tag}.parquet       ({len(signer_df):,} rows)")
    print(f"  signer_summary_all_{tag}.csv        ({len(summary_all):,} rows)")

    # For diff-signer categories: save entity mapping
    if category == "diff_signer_owner" and merged_entity_map:
        uf_global_final = _UnionFind()
        for signer, root in merged_entity_map.items():
            uf_global_final.union(signer, root)
        global_map_final = {s: uf_global_final.find(s) for s in uf_global_final.parent}
        entity_df = pd.DataFrame([
            {"signer": s, "entity": e}
            for s, e in global_map_final.items()
        ])
        entity_df.to_csv(f"{out_dir}/signer_entity_map_{tag}.csv", index=False)
        # Entity summary: how many signers per entity
        entity_sizes = entity_df.groupby("entity").size().reset_index(name="signer_count")
        entity_sizes = entity_sizes.sort_values("signer_count", ascending=False)
        entity_sizes.to_csv(f"{out_dir}/entity_sizes_{tag}.csv", index=False)
        multi_signer = entity_sizes[entity_sizes["signer_count"] > 1]
        print(f"  signer_entity_map_{tag}.csv         ({len(entity_df):,} mappings)")
        print(f"  entity_sizes_{tag}.csv              "
              f"({len(entity_sizes):,} entities, {len(multi_signer)} multi-signer)")

    # ── Step 6: All-signer summary report ─────────────────────────────────
    all_stats = print_overall_summary(summary_all, per_sandwich, label="ALL SIGNERS")

    # ── Step 7: Filtered signers (WR >= 0.5 AND USD total_profit > 0) ────
    # Signers with no price coverage (usd_total_profit=0 from NaN sum) are excluded
    # because we cannot confirm positive profit.
    mask_wr = summary_all["win_rate"] >= 0.5
    mask_profit = summary_all["usd_total_profit"] > 0
    summary_filtered = summary_all[mask_wr & mask_profit].copy()

    # Also filter per_sandwich to only include filtered signers
    filtered_signers = set(summary_filtered.index)
    per_sandwich_filtered = per_sandwich[per_sandwich["signer"].isin(filtered_signers)]

    summary_filtered.to_csv(f"{out_dir}/signer_summary_filtered_{tag}.csv")
    print(f"\n  signer_summary_filtered_{tag}.csv         ({len(summary_filtered):,} rows)")

    filt_stats = print_overall_summary(
        summary_filtered, per_sandwich_filtered,
        label="FILTERED SIGNERS (WR >= 0.5 AND USD profit > 0)"
    )

    print_filter_comparison(all_stats, filt_stats)

    # ── Step 8: Generate charts ───────────────────────────────────────────
    print("\nGenerating charts...")
    generate_charts(summary_all, summary_filtered, out_dir, tag)

    # ── Step 9: Feature summary ───────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  MODEL FEATURE SUMMARY (all signers)")
    print(f"{'='*80}")
    for col in MODEL_FEATURES:
        if col in signer_df.columns:
            s = signer_df[col].dropna()
            if len(s) > 0:
                print(f"  {col:30s}  mean={s.mean():12.4f}  "
                      f"median={s.median():12.4f}  non-null={len(s):,}")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
