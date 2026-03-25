"""Extract 17 dynamic entity features (attacker + leader + program profiles).

Reads instance_features.parquet + ClickHouse (sandwich_txs signers, slot_leaders).
Outputs entity_features.parquet and features_combined.parquet.

Feature categories:
  Cat 6 — Attacker Profile (12)
  Cat 7 — Leader Distribution (3)
  Cat 8 — Program Profile (3)
"""

import gc
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

from utils.db import get_client
from utils.programs import get_representative_program, classify_program
from utils.union_find import build_attacker_mapping

OUTPUT_DIR = Path(__file__).resolve().parent / "data"


# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------

def load_instance_features() -> pd.DataFrame:
    path = OUTPUT_DIR / "instance_features.parquet"
    print(f"[1/4] Loading {path} ...")
    df = pd.read_parquet(path)
    print(f"  -> {len(df):,} sandwiches")
    return df


def load_sandwich_txs_signers(client) -> pd.DataFrame:
    """Load minimal sandwich_txs for Union-Find + program extraction."""
    print("[2/4] Loading sandwich_txs (signers + programs) ...")
    q = """
    SELECT
        sandwichId, type, slot,
        arrayElement(signers, 1) AS primarySigner,
        programs
    FROM solwich.sandwich_txs
    LIMIT 1 BY sandwichId, type, signature
    """
    res = client.query(q)
    df = pd.DataFrame(res.result_rows, columns=res.column_names)
    print(f"  -> {len(df):,} rows")
    return df


def load_slot_leaders(client) -> dict:
    print("[3/4] Loading slot_leaders ...")
    q = "SELECT slot, leader FROM solwich.slot_leaders"
    res = client.query(q)
    leader_map = dict(res.result_rows)
    print(f"  -> {len(leader_map):,} slots with leaders")
    return leader_map


# ---------------------------------------------------------------------------
# 2. Empirical Bayes shrinkage
# ---------------------------------------------------------------------------

def estimate_beta_prior(raw_rates: np.ndarray, counts: np.ndarray, min_count: int = 2):
    """Estimate Beta prior (alpha0, beta0) via method of moments.

    Only uses attackers with count >= min_count for stable estimation.
    """
    mask = counts >= min_count
    if mask.sum() < 10:
        return 1.0, 1.0  # uninformative prior

    rates = raw_rates[mask]
    mu = rates.mean()
    var = rates.var()

    if var <= 0 or mu <= 0 or mu >= 1:
        return 1.0, 1.0

    # Method of moments for Beta distribution
    common = mu * (1 - mu) / var - 1
    if common <= 0:
        return 1.0, 1.0

    alpha0 = mu * common
    beta0 = (1 - mu) * common

    # Clamp to reasonable range
    alpha0 = max(0.1, min(alpha0, 100))
    beta0 = max(0.1, min(beta0, 100))
    return alpha0, beta0


def beta_shrink(successes: int, trials: int, alpha0: float, beta0: float) -> float:
    """Compute Beta-shrunk rate."""
    return (alpha0 + successes) / (alpha0 + beta0 + trials)


def estimate_normal_prior(values: np.ndarray, counts: np.ndarray, min_count: int = 2):
    """Estimate Normal prior (mu0, sigma0^2) for shrinkage."""
    mask = counts >= min_count
    if mask.sum() < 10:
        return 0.0, 1.0
    v = values[mask]
    return v.mean(), max(v.var(), 1e-10)


def normal_shrink(sample_mean: float, sample_count: int,
                  mu0: float, sigma0_sq: float, sigma_sq: float = None) -> float:
    """Compute Normal-shrunk mean (James-Stein style)."""
    if sample_count == 0:
        return mu0
    if sigma_sq is None:
        sigma_sq = sigma0_sq
    w = sigma_sq / (sigma_sq + sample_count * sigma0_sq) if sigma0_sq > 0 else 0
    return w * mu0 + (1 - w) * sample_mean


# ---------------------------------------------------------------------------
# 3. Attacker profile computation
# ---------------------------------------------------------------------------

def compute_attacker_profiles(df_inst: pd.DataFrame,
                              sandwich_to_attacker: dict,
                              attacker_members: dict) -> dict:
    """Compute raw per-attacker statistics."""
    profiles = {}

    for atk_key, members in attacker_members.items():
        profiles[atk_key] = {
            "count": 0,
            "wins": 0,
            "per_token": defaultdict(lambda: {"income": 0.0, "loss": 0.0, "count": 0}),
            "pool_dist": Counter(),
            "active_slots": set(),
            "bundle_count": 0,
            "inblock_count": 0,
            "consecutive_count": 0,
            "address_count": len(members),
        }

    for _, row in tqdm(df_inst.iterrows(), total=len(df_inst), desc="Attacker profiles"):
        sw_id = row["sandwichId"]
        atk_key = sandwich_to_attacker.get(sw_id)
        if atk_key is None:
            continue

        p = profiles[atk_key]
        p["count"] += 1

        profit = row["profit_a"]
        if profit > 0:
            p["wins"] += 1

        token_a = row["tokenA"]
        tok_stats = p["per_token"][token_a]
        tok_stats["count"] += 1
        if profit > 0:
            tok_stats["income"] += profit
        else:
            tok_stats["loss"] += abs(profit)

        p["pool_dist"][row["tokenB"]] += 1
        p["active_slots"].add(int(row["slot"]))

        if row.get("all_in_bundle", False):
            p["bundle_count"] += 1
        if not row["cross_block"]:
            p["inblock_count"] += 1
        if row["consecutive"]:
            p["consecutive_count"] += 1

    return profiles


def map_attacker_features(df_inst: pd.DataFrame,
                          sandwich_to_attacker: dict,
                          profiles: dict) -> pd.DataFrame:
    """Map attacker profile features back to each sandwich."""

    # Estimate global priors for shrinkage
    counts = np.array([p["count"] for p in profiles.values()])
    raw_win_rates = np.array([
        p["wins"] / p["count"] if p["count"] > 0 else 0
        for p in profiles.values()
    ])
    raw_bundle_rates = np.array([
        p["bundle_count"] / p["count"] if p["count"] > 0 else 0
        for p in profiles.values()
    ])
    raw_inblock_rates = np.array([
        p["inblock_count"] / p["count"] if p["count"] > 0 else 0
        for p in profiles.values()
    ])
    raw_consec_rates = np.array([
        p["consecutive_count"] / p["count"] if p["count"] > 0 else 0
        for p in profiles.values()
    ])

    # Collect per-token avg profits for Normal prior
    all_token_avgs = []
    all_token_counts = []
    for p in profiles.values():
        for tok, stats in p["per_token"].items():
            if stats["count"] > 0:
                avg = (stats["income"] - stats["loss"]) / stats["count"]
                all_token_avgs.append(avg)
                all_token_counts.append(stats["count"])
    all_token_avgs = np.array(all_token_avgs) if all_token_avgs else np.array([0.0])
    all_token_counts = np.array(all_token_counts) if all_token_counts else np.array([1])

    # Estimate priors
    a_wr, b_wr = estimate_beta_prior(raw_win_rates, counts)
    a_br, b_br = estimate_beta_prior(raw_bundle_rates, counts)
    a_ir, b_ir = estimate_beta_prior(raw_inblock_rates, counts)
    a_cr, b_cr = estimate_beta_prior(raw_consec_rates, counts)
    mu_profit, sigma_sq_profit = estimate_normal_prior(all_token_avgs, all_token_counts)

    print(f"  Beta priors — win_rate: α={a_wr:.2f} β={b_wr:.2f}, "
          f"bundle: α={a_br:.2f} β={b_br:.2f}")
    print(f"  Normal prior — profit: μ={mu_profit:.6f}, σ²={sigma_sq_profit:.6f}")

    records = []
    for _, row in tqdm(df_inst.iterrows(), total=len(df_inst), desc="Mapping attacker features"):
        sw_id = row["sandwichId"]
        atk_key = sandwich_to_attacker.get(sw_id, "")
        p = profiles.get(atk_key)

        if p is None or p["count"] == 0:
            records.append({
                "sandwichId": sw_id,
                "attacker_key": atk_key,
                "attacker_sandwich_count": 0,
                "attacker_win_rate": beta_shrink(0, 0, a_wr, b_wr),
                "attacker_token_income": 0.0,
                "attacker_token_loss": 0.0,
                "attacker_token_avg_profit": mu_profit,
                "attacker_pool_entropy": 0.0,
                "attacker_avg_slot_interval": float("nan"),
                "attacker_avg_sandwiches_per_slot": 0.0,
                "attacker_bundle_rate": beta_shrink(0, 0, a_br, b_br),
                "attacker_inblock_rate": beta_shrink(0, 0, a_ir, b_ir),
                "attacker_consecutive_rate": beta_shrink(0, 0, a_cr, b_cr),
                "attacker_address_count": 0,
            })
            continue

        n = p["count"]

        # Per-token profit for THIS sandwich's tokenA
        token_a = row["tokenA"]
        tok_stats = p["per_token"].get(token_a, {"income": 0.0, "loss": 0.0, "count": 0})
        tok_avg = (tok_stats["income"] - tok_stats["loss"]) / tok_stats["count"] if tok_stats["count"] > 0 else 0

        # Pool entropy
        pool_counts = list(p["pool_dist"].values())
        total_pool = sum(pool_counts)
        if total_pool > 0:
            probs = np.array(pool_counts) / total_pool
            pool_entropy = float(-np.sum(probs * np.log2(probs + 1e-15)))
        else:
            pool_entropy = 0.0

        # Temporal density
        active_slots = sorted(p["active_slots"])
        n_active = len(active_slots)
        if n_active > 1:
            slot_span = active_slots[-1] - active_slots[0]
            avg_interval = slot_span / (n_active - 1)
        else:
            avg_interval = float("nan")
        avg_per_slot = n / n_active if n_active > 0 else 0.0

        records.append({
            "sandwichId": sw_id,
            "attacker_key": atk_key,
            "attacker_sandwich_count": n,
            "attacker_win_rate": beta_shrink(p["wins"], n, a_wr, b_wr),
            "attacker_token_income": tok_stats["income"],
            "attacker_token_loss": tok_stats["loss"],
            "attacker_token_avg_profit": normal_shrink(
                tok_avg, tok_stats["count"], mu_profit, sigma_sq_profit
            ),
            "attacker_pool_entropy": pool_entropy,
            "attacker_avg_slot_interval": avg_interval,
            "attacker_avg_sandwiches_per_slot": avg_per_slot,
            "attacker_bundle_rate": beta_shrink(p["bundle_count"], n, a_br, b_br),
            "attacker_inblock_rate": beta_shrink(p["inblock_count"], n, a_ir, b_ir),
            "attacker_consecutive_rate": beta_shrink(p["consecutive_count"], n, a_cr, b_cr),
            "attacker_address_count": p["address_count"],
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 4. Leader distribution
# ---------------------------------------------------------------------------

def compute_leader_features(df_inst: pd.DataFrame,
                            sandwich_to_attacker: dict,
                            leader_map: dict) -> pd.DataFrame:
    """Compute per-attacker leader distribution and map to sandwiches."""
    # Build attacker -> leader distribution
    attacker_leaders = defaultdict(Counter)

    for _, row in df_inst.iterrows():
        sw_id = row["sandwichId"]
        atk_key = sandwich_to_attacker.get(sw_id)
        if atk_key is None:
            continue
        slot = int(row["slot"])
        leader = leader_map.get(slot, "")
        if leader:
            attacker_leaders[atk_key][leader] += 1

    records = []
    for _, row in df_inst.iterrows():
        sw_id = row["sandwichId"]
        atk_key = sandwich_to_attacker.get(sw_id, "")
        dist = attacker_leaders.get(atk_key, Counter())

        if not dist:
            records.append({
                "sandwichId": sw_id,
                "attacker_leader_count": 0,
                "attacker_leader_concentration": 0.0,
                "attacker_leader_entropy": 0.0,
            })
            continue

        total = sum(dist.values())
        max_count = max(dist.values())
        probs = np.array(list(dist.values())) / total
        entropy = float(-np.sum(probs * np.log2(probs + 1e-15)))

        records.append({
            "sandwichId": sw_id,
            "attacker_leader_count": len(dist),
            "attacker_leader_concentration": max_count / total,
            "attacker_leader_entropy": entropy,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 5. Program profile
# ---------------------------------------------------------------------------

def compute_program_features(df_inst: pd.DataFrame,
                             df_txs: pd.DataFrame,
                             sandwich_to_attacker: dict) -> pd.DataFrame:
    """Compute per-program profile and map to sandwiches."""
    # For each sandwich, find the representative program from front/back txs
    atk_types = {"frontRun", "backRun"}
    atk_txs = df_txs[df_txs["type"].isin(atk_types)]
    grouped = atk_txs.groupby("sandwichId")

    sandwich_rep_program = {}
    for sw_id, txs in grouped:
        programs_lists = [
            row["programs"] if isinstance(row["programs"], list) else []
            for _, row in txs.iterrows()
        ]
        rep = get_representative_program(programs_lists)
        sandwich_rep_program[sw_id] = rep

    # Build program -> {attacker: count} mapping
    program_attacker_usage = defaultdict(Counter)
    for sw_id, rep in sandwich_rep_program.items():
        if not rep:
            continue
        atk_key = sandwich_to_attacker.get(sw_id, "unknown")
        program_attacker_usage[rep][atk_key] += 1

    records = []
    for _, row in df_inst.iterrows():
        sw_id = row["sandwichId"]
        rep = sandwich_rep_program.get(sw_id, "")

        if not rep:
            records.append({
                "sandwichId": sw_id,
                "representative_program": "",
                "program_is_custom": False,
                "program_total_usage": 0,
                "program_attacker_count": 0,
            })
            continue

        usage = program_attacker_usage.get(rep, Counter())
        records.append({
            "sandwichId": sw_id,
            "representative_program": rep,
            "program_is_custom": classify_program(rep) == "custom",
            "program_total_usage": sum(usage.values()),
            "program_attacker_count": len(usage),
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main():
    client = get_client()

    df_inst = load_instance_features()
    df_txs = load_sandwich_txs_signers(client)
    leader_map = load_slot_leaders(client)

    # Build attacker mapping
    print("[4/4] Building attacker mapping (Union-Find) ...")
    sandwich_to_attacker, attacker_members = build_attacker_mapping(df_txs)
    n_attackers = len(attacker_members)
    print(f"  -> {n_attackers:,} unique attacker entities")

    # Attacker profile features (Cat 6)
    print("\n== Computing attacker profiles ==")
    profiles = compute_attacker_profiles(df_inst, sandwich_to_attacker, attacker_members)
    df_atk = map_attacker_features(df_inst, sandwich_to_attacker, profiles)

    # Leader features (Cat 7)
    print("\n== Computing leader distribution ==")
    df_leader = compute_leader_features(df_inst, sandwich_to_attacker, leader_map)

    # Program features (Cat 8)
    print("\n== Computing program profiles ==")
    df_prog = compute_program_features(df_inst, df_txs, sandwich_to_attacker)

    # Merge entity features
    df_entity = df_atk.merge(df_leader, on="sandwichId", how="left")
    df_entity = df_entity.merge(
        df_prog[["sandwichId", "program_is_custom", "program_total_usage", "program_attacker_count"]],
        on="sandwichId", how="left"
    )

    # Fill NaN
    df_entity["attacker_avg_slot_interval"] = df_entity["attacker_avg_slot_interval"].fillna(
        df_entity["attacker_avg_slot_interval"].median()
    )
    df_entity = df_entity.fillna(0)

    # Export entity features
    out_entity = OUTPUT_DIR / "entity_features.parquet"
    df_entity.to_parquet(out_entity, index=False)
    print(f"\n[SAVED] Entity features: {out_entity} ({len(df_entity):,} rows)")

    # Merge with instance features
    df_combined = df_inst.merge(df_entity, on="sandwichId", how="left")
    out_combined = OUTPUT_DIR / "features_combined.parquet"
    df_combined.to_parquet(out_combined, index=False)
    df_combined.to_csv(OUTPUT_DIR / "features_combined.csv", index=False)
    print(f"[SAVED] Combined features: {out_combined} ({len(df_combined):,} rows)")

    # Summary
    feature_cols = [c for c in df_combined.columns
                    if c not in {"sandwichId", "slot", "timestamp", "tokenA", "tokenB",
                                 "primary_signer", "attacker_key", "representative_program"}]
    print(f"\n=== Total feature columns: {len(feature_cols)} ===")
    print(f"  Instance (static):  37")
    print(f"  Entity (dynamic):   {len(feature_cols) - 37}")

    # Shrinkage validation
    print("\n=== Shrinkage Validation ===")
    low_count = df_combined[df_combined["attacker_sandwich_count"] <= 3]
    high_count = df_combined[df_combined["attacker_sandwich_count"] >= 50]
    for col in ["attacker_win_rate", "attacker_bundle_rate", "attacker_inblock_rate"]:
        if col in df_combined.columns:
            low_std = low_count[col].std()
            high_std = high_count[col].std()
            print(f"  {col}: low-count std={low_std:.4f}, high-count std={high_std:.4f}")

    del df_inst, df_txs, df_entity
    gc.collect()


if __name__ == "__main__":
    main()
