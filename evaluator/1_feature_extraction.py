"""Extract 37 static instance features from sandwich data.

Reads from ClickHouse (sandwiches, sandwich_txs, slot_txs) and outputs
data/instance_features.parquet with 37 features + metadata columns.

Feature categories:
  Cat 1 — Identity & Technique (12)
  Cat 2 — Proximity & Ordering (7)
  Cat 3 — Economics (10)
  Cat 4 — Slippage Exploitation (5)
  Cat 5 — Bundle Evidence (3)
"""

import gc
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

from utils.db import get_client

OUTPUT_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

SOL_MINT = "SOL"
SLIPPAGE_DEPLOY_SLOT = 408285759  # slot where slippage feature was deployed


# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------

def load_sandwiches(client) -> pd.DataFrame:
    print("[1/3] Loading sandwiches ...")
    q = """
    SELECT
        sandwichId, slot, timestamp, crossBlock, consecutive,
        frontConsecutive, backConsecutive, victimConsecutive,
        signerSame, ownerSame, hasTransfer,
        hasFrontInlineTransfer, hasDirectTransfer, hasBackInlineTransfer,
        multiFrontRun, multiBackRun, frontCount, backCount,
        victimCount, adverseCount, perfect, relativeDiffB, profitA,
        tokenA, tokenB, maxSlippageUtilization
    FROM solwich.sandwiches
    LIMIT 1 BY sandwichId
    """
    res = client.query(q)
    df = pd.DataFrame(res.result_rows, columns=res.column_names)
    print(f"  -> {len(df):,} sandwiches loaded")
    return df


def load_sandwich_txs(client) -> pd.DataFrame:
    print("[2/3] Loading sandwich_txs ...")
    q = """
    SELECT
        sandwichId, type, slot, position, fee, signature,
        arrayElement(signers, 1) AS primarySigner,
        inBundle, programs,
        fromAmount, toAmount, fromTotalAmount, toTotalAmount,
        attackerPreBalanceB, attackerPostBalanceB,
        slippageUtilization, slippageDexName, slippageLimitType
    FROM solwich.sandwich_txs
    LIMIT 1 BY sandwichId, type, signature
    """
    res = client.query(q)
    df = pd.DataFrame(res.result_rows, columns=res.column_names)
    print(f"  -> {len(df):,} sandwich_txs loaded")
    return df


def load_slot_txs(client) -> dict:
    print("[3/3] Loading slot_txs (for cross-slot gap) ...")
    q = "SELECT slot, txCount FROM solwich.slot_txs"
    res = client.query(q)
    slot_tx_map = dict(res.result_rows)
    print(f"  -> {len(slot_tx_map):,} slots loaded")
    return slot_tx_map


# ---------------------------------------------------------------------------
# 2. Feature computation
# ---------------------------------------------------------------------------

def compute_features(df_s: pd.DataFrame, df_t: pd.DataFrame, slot_tx_map: dict) -> pd.DataFrame:
    """Compute 37 static features for each sandwich."""
    print("Computing features ...")

    # Pre-group txs by sandwichId for efficient lookup
    grouped = df_t.groupby("sandwichId")

    records = []
    for _, sw in tqdm(df_s.iterrows(), total=len(df_s), desc="Features"):
        sw_id = sw["sandwichId"]

        if sw_id not in grouped.groups:
            continue

        txs = grouped.get_group(sw_id)
        fronts = txs[txs["type"] == "frontRun"].sort_values("position")
        backs = txs[txs["type"] == "backRun"].sort_values("position")
        victims = txs[txs["type"] == "victim"].sort_values("position")
        transfers = txs[txs["type"] == "transfer"]
        adverses = txs[txs["type"] == "adverse"]

        rec = {"sandwichId": sw_id}

        # -- Metadata (not features) --
        rec["slot"] = sw["slot"]
        rec["timestamp"] = sw["timestamp"]
        rec["tokenA"] = sw["tokenA"]
        rec["tokenB"] = sw["tokenB"]
        rec["primary_signer"] = fronts.iloc[0]["primarySigner"] if len(fronts) > 0 else ""

        # == Cat 1: Identity & Technique (12) ==
        rec["signer_same"] = bool(sw["signerSame"])
        rec["owner_same"] = bool(sw["ownerSame"])
        rec["has_transfer"] = bool(sw["hasTransfer"])
        rec["has_front_inline_transfer"] = bool(sw["hasFrontInlineTransfer"])
        rec["has_direct_transfer"] = bool(sw["hasDirectTransfer"])
        rec["has_back_inline_transfer"] = bool(sw["hasBackInlineTransfer"])
        rec["multi_front"] = bool(sw["multiFrontRun"])
        rec["multi_back"] = bool(sw["multiBackRun"])
        rec["front_count"] = int(sw["frontCount"])
        rec["back_count"] = int(sw["backCount"])

        # front/back internal gap
        front_positions = fronts["position"].values
        if len(front_positions) > 1:
            front_gaps = np.diff(np.sort(front_positions)) - 1
            rec["front_internal_gap"] = int(front_gaps.max())
        else:
            rec["front_internal_gap"] = 0

        back_positions = backs["position"].values
        if len(back_positions) > 1:
            back_gaps = np.diff(np.sort(back_positions)) - 1
            rec["back_internal_gap"] = int(back_gaps.max())
        else:
            rec["back_internal_gap"] = 0

        # == Cat 2: Proximity & Ordering (7) ==
        rec["cross_block"] = bool(sw["crossBlock"])
        rec["consecutive"] = bool(sw["consecutive"])
        rec["front_consecutive"] = bool(sw["frontConsecutive"])
        rec["back_consecutive"] = bool(sw["backConsecutive"])
        rec["victim_consecutive"] = bool(sw["victimConsecutive"])

        # position_gap: total distance from last front to first back
        if len(fronts) > 0 and len(backs) > 0:
            front_slot = int(fronts.iloc[-1]["slot"])
            front_pos = int(fronts.iloc[-1]["position"])
            back_slot = int(backs.iloc[0]["slot"])
            back_pos = int(backs.iloc[0]["position"])

            if front_slot == back_slot:
                rec["position_gap"] = back_pos - front_pos
            else:
                gap = slot_tx_map.get(front_slot, 0) - front_pos
                for s in range(front_slot + 1, back_slot):
                    gap += slot_tx_map.get(s, 0)
                gap += back_pos
                rec["position_gap"] = gap

            rec["slot_gap"] = back_slot - front_slot
        else:
            rec["position_gap"] = 0
            rec["slot_gap"] = 0

        # == Cat 3: Economics (10) ==
        rec["profit_a"] = float(sw["profitA"])
        rec["profit_positive"] = float(sw["profitA"]) > 0
        rec["relative_diff_b"] = float(sw["relativeDiffB"])
        rec["victim_count"] = int(sw["victimCount"])
        rec["victim_total_amount"] = float(victims["toAmount"].sum()) if len(victims) > 0 else 0.0
        rec["adverse_count"] = int(sw["adverseCount"])

        # attacker_no_b_before: first front's pre-balance ≈ 0
        if len(fronts) > 0:
            rec["attacker_no_b_before"] = float(fronts.iloc[0]["attackerPreBalanceB"]) < 1e-6
        else:
            rec["attacker_no_b_before"] = True

        # attacker_no_b_after: last back's post-balance ≈ 0
        if len(backs) > 0:
            rec["attacker_no_b_after"] = float(backs.iloc[-1]["attackerPostBalanceB"]) < 1e-6
        else:
            rec["attacker_no_b_after"] = True

        # total_attacker_fee (lamports -> SOL)
        attacker_txs = pd.concat([fronts, backs, transfers])
        rec["total_attacker_fee"] = float(attacker_txs["fee"].sum()) / 1e9

        rec["base_is_sol"] = sw["tokenA"] == SOL_MINT

        # == Cat 4: Slippage Exploitation (5) ==
        valid_slips = []
        has_unprotected = False
        slippage_num = 0  # numerator: valid + unprotected
        slippage_denom = 0  # denominator: all fetched victims
        all_not_fetched = True

        for _, v in victims.iterrows():
            v_slot = int(v["slot"])
            v_dex = v["slippageDexName"] if v["slippageDexName"] else ""
            v_slip = float(v["slippageUtilization"])

            # Classify this victim's slippage status
            if v_slot < SLIPPAGE_DEPLOY_SLOT and v_dex == "":
                continue  # not_fetched: skip entirely
            all_not_fetched = False

            if v_slip == -3:  # missing_inner
                slippage_denom += 1
            elif v_slip == -2:  # unsupported_dex
                slippage_denom += 1
            elif v_slip == -1:  # unprotected
                has_unprotected = True
                slippage_num += 1
                slippage_denom += 1
            elif v_slip >= 0:  # valid
                valid_slips.append(v_slip)
                slippage_num += 1
                slippage_denom += 1

        rec["max_slippage_utilization"] = max(valid_slips) if valid_slips else -1.0
        rec["avg_slippage_utilization"] = float(np.mean(valid_slips)) if valid_slips else -1.0
        rec["has_unprotected_victim"] = has_unprotected
        rec["slippage_coverage"] = (slippage_num / slippage_denom) if slippage_denom > 0 else -1.0
        rec["slippage_data_missing"] = all_not_fetched

        # == Cat 5: Bundle Evidence (3) ==
        fbv_txs = pd.concat([fronts, backs, victims])
        all_in_bundle = bool(fbv_txs["inBundle"].all()) if len(fbv_txs) > 0 else False
        rec["all_in_bundle"] = all_in_bundle
        rec["likely_same_bundle"] = all_in_bundle and bool(sw["consecutive"])

        if len(attacker_txs) > 0:
            rec["attacker_bundle_ratio"] = float(attacker_txs["inBundle"].sum()) / len(attacker_txs)
        else:
            rec["attacker_bundle_ratio"] = 0.0

        records.append(rec)

    df_features = pd.DataFrame(records)
    print(f"  -> {len(df_features):,} sandwiches with features")
    return df_features


# ---------------------------------------------------------------------------
# 3. Main
# ---------------------------------------------------------------------------

def main():
    client = get_client()

    df_s = load_sandwiches(client)
    df_t = load_sandwich_txs(client)
    slot_tx_map = load_slot_txs(client)

    df_features = compute_features(df_s, df_t, slot_tx_map)

    # Export
    out_parquet = OUTPUT_DIR / "instance_features.parquet"
    out_csv = OUTPUT_DIR / "instance_features.csv"
    df_features.to_parquet(out_parquet, index=False)
    df_features.to_csv(out_csv, index=False)
    print(f"\n[DONE] Exported {len(df_features):,} rows")
    print(f"  parquet: {out_parquet}")
    print(f"  csv:     {out_csv}")

    # Quick distribution summary
    print("\n=== Distribution Summary ===")
    bool_cols = [c for c in df_features.columns if df_features[c].dtype == bool]
    for c in bool_cols:
        pct = df_features[c].mean() * 100
        print(f"  {c}: {pct:.1f}% True")

    float_cols = ["profit_a", "relative_diff_b", "total_attacker_fee",
                  "max_slippage_utilization", "avg_slippage_utilization",
                  "slippage_coverage", "attacker_bundle_ratio"]
    for c in float_cols:
        if c in df_features.columns:
            print(f"  {c}: mean={df_features[c].mean():.4f}, median={df_features[c].median():.4f}")

    del df_s, df_t
    gc.collect()


if __name__ == "__main__":
    main()
