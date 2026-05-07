"""
Crawl StakeWiz API for validator metadata.
Saves validator data to data/stakewiz/validators.parquet and .csv

Usage:
    python 0_crawl_stakewiz.py
"""

import json
import os

import pandas as pd
import requests

OUT_DIR = "data/stakewiz"
API_URL = "https://api.stakewiz.com/validators"

KEEP_COLS = [
    "identity",
    "vote_identity",
    "name",
    "rank",
    "activated_stake",
    "stake_weight",
    "stake_ratio",
    "commission",
    "is_jito",
    "jito_commission_bps",
    "version",
    "delinquent",
    "skip_rate",
    "wiz_skip_rate",
    "vote_success",
    "uptime",
    "wiz_score",
    "above_halt_line",
    "first_epoch_with_stake",
    "first_epoch_distance",
    "ip_city",
    "ip_country",
    "ip_asn",
    "ip_org",
    "asn_concentration",
    "tpu_ip",
    "tpu_ip_concentration",
    "city_concentration",
    "epoch",
    "apy_estimate",
    "staking_apy",
    "jito_apy",
    "total_apy",
    "description",
    "website",
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Fetching validators from {API_URL}...")
    resp = requests.get(API_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    print(f"  {len(data)} validators fetched")

    df = pd.DataFrame(data)
    # Keep only columns that exist
    cols = [c for c in KEEP_COLS if c in df.columns]
    df = df[cols]

    # identity is the leader key (matches slot_leaders table)
    df = df.set_index("identity")

    df.to_parquet(f"{OUT_DIR}/validators.parquet")
    df.to_csv(f"{OUT_DIR}/validators.csv")
    print(f"Saved to {OUT_DIR}/validators.parquet ({len(df)} rows, {len(df.columns)} cols)")

    # Summary stats
    print(f"\n=== Summary ===")
    print(f"Total validators: {len(df)}")
    print(f"Jito validators: {df['is_jito'].sum()} ({df['is_jito'].mean()*100:.1f}%)")
    print(f"Delinquent: {df['delinquent'].sum()}")
    print(f"Stake: median={df['activated_stake'].median():.0f} SOL, "
          f"top10 total={df.nlargest(10, 'activated_stake')['activated_stake'].sum():.0f} SOL")
    print(f"Commission: median={df['commission'].median()}, "
          f"0%: {(df['commission']==0).sum()}, 100%: {(df['commission']==100).sum()}")


if __name__ == "__main__":
    main()
