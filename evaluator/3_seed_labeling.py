"""Generate seed labels for EM bootstrap.

Positive seeds: intersection with sandwiched.me (from 0_crawl_sandwiched_me.py output)
Negative seeds: rule-based (adverse-dominant + poor-match-loss), balanced to match positive count
Gap analysis: sandwiches detected by sandwiched.me but not by us

Prerequisites:
  - Run 0_crawl_sandwiched_me.py first to generate data/sandwiches_site.csv
  - Run 1_feature_extraction.py + 2_entity_features.py to generate data/features_combined.parquet

Outputs:
  data/seed_labels.parquet — (sandwichId, label, source)
  data/site_only_analysis.csv — gap analysis for missed sandwiches
"""

from pathlib import Path

import pandas as pd
from tqdm.auto import tqdm

from utils.db import get_client

OUTPUT_DIR = Path(__file__).resolve().parent / "data"


# ---------------------------------------------------------------------------
# 1. Match sandwiched.me data with our DB
# ---------------------------------------------------------------------------

def load_site_data() -> pd.DataFrame:
    """Load crawled sandwiched.me data from CSV."""
    path = OUTPUT_DIR / "sandwiches_site.csv"
    if not path.exists():
        print(f"[ERROR] {path} not found. Run 0_crawl_sandwiched_me.py first.")
        return pd.DataFrame()
    df = pd.read_csv(path)
    print(f"[SITE] Loaded {len(df):,} sandwiches from {path}")
    print(f"  Slot range: {df['slot'].min()} - {df['slot'].max()}")
    return df


def match_with_db(site_df: pd.DataFrame, client) -> tuple:
    """Match sandwiched.me data with our sandwich_txs.

    Returns (both_df, site_only_df) where:
      both_df: matched sandwiches with our sandwichId
      site_only_df: sandwiches they detected but we didn't
    """
    if site_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    min_slot = int(site_df["slot"].min())
    max_slot = int(site_df["slot"].max())
    print(f"\nMatching with DB for slot range [{min_slot}, {max_slot}] ...")

    # Check which slots have completed detection + bundle marking
    q_checked = f"""
    SELECT slot FROM solwich.slot_txs
    WHERE slot >= {min_slot} AND slot <= {max_slot}
      AND sandwichInBundleChecked = true AND sandwichFetched = true
    """
    res = client.query(q_checked)
    checked_slots = set(row[0] for row in res.result_rows)
    print(f"  {len(checked_slots):,} slots with completed detection + bundle marking")

    # Filter site data to only checked slots
    site_df = site_df[site_df["slot"].isin(checked_slots)].copy()
    print(f"  {len(site_df):,} site sandwiches in checked slots")

    if site_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Query our sandwiches in this range (in-block only, matching sandwiched.me)
    q_db = f"""
    SELECT
        sandwichId,
        anyIf(signature, type='frontRun') AS front_sig,
        anyIf(signature, type='backRun') AS back_sig,
        anyIf(slot, type='frontRun') AS front_slot,
        anyIf(slot, type='backRun') AS back_slot
    FROM solwich.sandwich_txs
    WHERE slot >= {min_slot} AND slot <= {max_slot}
    GROUP BY sandwichId
    HAVING front_sig != '' AND back_sig != ''
    """
    res = client.query(q_db)
    db_df = pd.DataFrame(res.result_rows, columns=res.column_names)
    # In-block only (sandwiched.me only detects in-block)
    db_df = db_df[db_df["front_slot"] == db_df["back_slot"]].copy()
    db_df["slot"] = db_df["front_slot"].astype(int)
    print(f"  {len(db_df):,} in-block DB sandwiches in range")

    # Merge on (slot, front_sig, back_sig)
    merged = site_df.merge(
        db_df[["sandwichId", "slot", "front_sig", "back_sig"]],
        on=["slot", "front_sig", "back_sig"],
        how="outer",
        indicator=True,
    )

    both_df = merged[merged["_merge"] == "both"][
        ["sandwichId", "slot", "front_sig", "back_sig"]
    ].copy()
    site_only_df = merged[merged["_merge"] == "left_only"][
        ["slot", "front_sig", "back_sig"]
    ].copy()

    print(f"\n  Matched (both): {len(both_df):,}")
    print(f"  Site only:      {len(site_only_df):,}")
    print(f"  DB only:        {(merged['_merge'] == 'right_only').sum():,}")

    return both_df, site_only_df


# ---------------------------------------------------------------------------
# 2. Gap analysis for site_only
# ---------------------------------------------------------------------------

def analyze_site_only(site_only_df: pd.DataFrame, client) -> pd.DataFrame:
    """Analyze why sandwiched.me detected sandwiches that we missed.

    Possible reasons:
      - slot_not_scanned: we didn't scan this slot
      - matched_differently: tx exists in our DB under a different sandwich
      - threshold_filtered_or_not_detected: tx in our scanned slot but not
        flagged (our relativeDiffB<=10% threshold, or they have false positives)
    """
    if site_only_df.empty:
        print("[GAP] No site-only sandwiches to analyze")
        return pd.DataFrame()

    print(f"\n[GAP] Analyzing {len(site_only_df):,} site-only sandwiches ...")
    records = []

    for _, row in tqdm(site_only_df.iterrows(), total=len(site_only_df), desc="Gap analysis"):
        slot = int(row["slot"])
        fr_sig = row["front_sig"]
        br_sig = row["back_sig"]

        rec = {"slot": slot, "front_sig": fr_sig, "back_sig": br_sig}

        # Check if slot was scanned
        q_slot = f"""
        SELECT txFetched, sandwichFetched
        FROM solwich.slot_txs WHERE slot = {slot} LIMIT 1
        """
        res = client.query(q_slot)
        if not res.result_rows:
            rec["in_our_db"] = False
            rec["likely_reason"] = "slot_not_scanned"
            records.append(rec)
            continue

        rec["in_our_db"] = True

        # Check if these signatures exist in any sandwich
        q_sig = f"""
        SELECT sandwichId, type
        FROM solwich.sandwich_txs
        WHERE signature IN ('{fr_sig}', '{br_sig}')
        """
        res = client.query(q_sig)
        if res.result_rows:
            rec["likely_reason"] = "matched_differently"
            records.append(rec)
            continue

        rec["likely_reason"] = "threshold_filtered_or_not_detected"
        records.append(rec)

    df = pd.DataFrame(records)
    out = OUTPUT_DIR / "site_only_analysis.csv"
    df.to_csv(out, index=False)
    print(f"[SAVED] {out}")

    if not df.empty:
        print("\n  Reason distribution:")
        for reason, cnt in df["likely_reason"].value_counts().items():
            print(f"    {reason}: {cnt}")

    return df


# ---------------------------------------------------------------------------
# 3. Negative seed generation
# ---------------------------------------------------------------------------

def generate_negative_seeds(df_features: pd.DataFrame,
                            max_count: int = 0) -> pd.DataFrame:
    """Generate negative seeds using rule-based strategies.

    Strategy A: adverse_count >= victim_count AND profit_a < 0
    Strategy B: relative_diff_b > 0.05 AND profit_a < 0
      (our detection threshold is 10%, so >5% is the upper half)

    If max_count > 0, randomly sample to balance with positive seeds.
    """
    strat_a = df_features[
        (df_features["adverse_count"] >= df_features["victim_count"]) &
        (df_features["profit_a"] < 0)
    ]["sandwichId"].tolist()

    strat_b = df_features[
        (df_features["relative_diff_b"] > 0.05) &
        (df_features["profit_a"] < 0)
    ]["sandwichId"].tolist()

    neg_ids = {}
    for sw_id in strat_a:
        neg_ids[sw_id] = "adverse_dominant"
    for sw_id in strat_b:
        if sw_id not in neg_ids:
            neg_ids[sw_id] = "poor_match_loss"

    print(f"\n[NEG] Strategy A (adverse >= victim & loss): {len(strat_a):,}")
    print(f"[NEG] Strategy B (relative_diff_b > 0.05 & loss): {len(strat_b):,}")
    print(f"[NEG] Total candidates: {len(neg_ids):,}")

    df_neg = pd.DataFrame([
        {"sandwichId": sw_id, "label": 0, "source": source}
        for sw_id, source in neg_ids.items()
    ])

    if max_count > 0 and len(df_neg) > max_count:
        df_neg = df_neg.sample(n=max_count, random_state=42)
        print(f"[NEG] Sampled to {max_count:,} to balance with positive seeds")

    print(f"[NEG] Final negative seeds: {len(df_neg):,}")
    return df_neg


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------

def main():
    client = get_client()

    # Load features
    features_path = OUTPUT_DIR / "features_combined.parquet"
    print(f"Loading {features_path} ...")
    df_features = pd.read_parquet(features_path)
    print(f"  {len(df_features):,} sandwiches")

    # Load sandwiched.me data (from 0_crawl_sandwiched_me.py)
    print("\n== Loading sandwiched.me data ==")
    site_df = load_site_data()

    # Match with our DB
    print("\n== Matching with DB ==")
    both_df, site_only_df = match_with_db(site_df, client)

    # Gap analysis
    if not site_only_df.empty:
        analyze_site_only(site_only_df, client)

    # Positive seeds
    pos_labels = pd.DataFrame({
        "sandwichId": both_df["sandwichId"],
        "label": 1,
        "source": "sandwiched_me",
    }) if not both_df.empty else pd.DataFrame(columns=["sandwichId", "label", "source"])

    # Negative seeds — independent criteria, no forced balancing
    print("\n== Generating negative seeds ==")
    neg_labels = generate_negative_seeds(df_features)

    # Remove overlap
    pos_ids = set(pos_labels["sandwichId"])
    neg_labels = neg_labels[~neg_labels["sandwichId"].isin(pos_ids)]

    # Unlabeled
    labeled_ids = set(pos_labels["sandwichId"]) | set(neg_labels["sandwichId"])
    unlabeled_ids = set(df_features["sandwichId"]) - labeled_ids
    unlabeled_labels = pd.DataFrame({
        "sandwichId": list(unlabeled_ids),
        "label": -1,
        "source": "unlabeled",
    })

    df_labels = pd.concat([pos_labels, neg_labels, unlabeled_labels], ignore_index=True)

    # Export
    out = OUTPUT_DIR / "seed_labels.parquet"
    df_labels.to_parquet(out, index=False)
    df_labels.to_csv(OUTPUT_DIR / "seed_labels.csv", index=False)
    print(f"\n[SAVED] {out} ({len(df_labels):,} rows)")

    # Summary
    print("\n=== Seed Label Summary ===")
    for label_val, name in [(1, "Positive"), (0, "Negative"), (-1, "Unlabeled")]:
        cnt = (df_labels["label"] == label_val).sum()
        pct = cnt / len(df_labels) * 100
        print(f"  {name}: {cnt:,} ({pct:.1f}%)")

    if not pos_labels.empty:
        print(f"\n  Positive source: sandwiched_me ({len(pos_labels):,})")
    if not neg_labels.empty:
        print(f"  Negative sources:")
        for src, cnt in neg_labels["source"].value_counts().items():
            print(f"    {src}: {cnt:,}")


if __name__ == "__main__":
    main()
