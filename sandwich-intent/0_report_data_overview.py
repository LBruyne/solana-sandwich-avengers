"""
Data Overview Report
====================
Generate a comprehensive data report for a given epoch range.

Covers:
  1. Slot coverage (theoretical vs fetched vs sandwich-checked, Jito bundles)
  2. Per-epoch slot coverage breakdown
  3. Sandwich counts by category with in/cross-block, victim, profit stats
  4. Jito bundle sandwiches (all front+victim+back in same bundle)

Usage:
    python report_data_overview.py --start-epoch 946 --end-epoch 955
"""

import argparse
import os

import numpy as np
import pandas as pd

from utils.db import get_client

SLOTS_PER_EPOCH = 432_000


def parse_args():
    p = argparse.ArgumentParser(description="Data overview report")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=955)
    return p.parse_args()


# ── 1. Slot Coverage ────────────────────────────────────────────────────────

def report_slot_coverage(client, start_slot, end_slot, start_epoch, end_epoch):
    num_epochs = end_epoch - start_epoch + 1
    theoretical = num_epochs * SLOTS_PER_EPOCH

    print("=" * 80)
    print(f"1. SLOT COVERAGE  (Epoch {start_epoch}-{end_epoch}, "
          f"Slot {start_slot}-{end_slot})")
    print("=" * 80)

    # Overall
    row = client.query_df(f"""
        SELECT
            countIf(txFetched = true) AS fetched,
            countIf(sandwichFetched = true) AS sw_checked,
            countIf(sandwichInBundleChecked = true) AS bundle_checked
        FROM slot_txs
        WHERE slot >= {start_slot} AND slot <= {end_slot}
    """).iloc[0]

    fetched = int(row["fetched"])
    sw_checked = int(row["sw_checked"])
    bundle_checked = int(row["bundle_checked"])

    print(f"\n  Theoretical slots:       {theoretical:>12,}")
    print(f"  Fetched slots:           {fetched:>12,}  ({fetched/theoretical*100:.2f}%)")
    print(f"  Sandwich-checked slots:  {sw_checked:>12,}  ({sw_checked/theoretical*100:.2f}%)")
    print(f"  Bundle-checked slots:    {bundle_checked:>12,}  ({bundle_checked/theoretical*100:.2f}%)")

    # Jito bundles
    brow = client.query_df(f"""
        SELECT count() AS total_bundles, uniq(slot) AS slots_with_bundles
        FROM jito_bundles
        WHERE slot >= {start_slot} AND slot <= {end_slot}
    """).iloc[0]

    total_bundles = int(brow["total_bundles"])
    slots_with_bundles = int(brow["slots_with_bundles"])

    print(f"\n  Jito bundles fetched:    {total_bundles:>12,}")
    print(f"  Slots with bundles:      {slots_with_bundles:>12,}  "
          f"({slots_with_bundles/theoretical*100:.2f}% of theoretical)")

    # Per-epoch
    print(f"\n  {'Epoch':>5}  {'Theoretical':>12}  {'Fetched':>12}  {'%':>7}  "
          f"{'SW Checked':>12}  {'%':>7}")
    print("  " + "-" * 68)

    epoch_df = client.query_df(f"""
        SELECT
            intDiv(slot, {SLOTS_PER_EPOCH}) AS epoch,
            countIf(txFetched = true) AS fetched,
            countIf(sandwichFetched = true) AS sw_checked
        FROM slot_txs
        WHERE slot >= {start_slot} AND slot <= {end_slot}
        GROUP BY epoch ORDER BY epoch
    """)

    for _, r in epoch_df.iterrows():
        ep = int(r["epoch"])
        f = int(r["fetched"])
        s = int(r["sw_checked"])
        print(f"  {ep:>5}  {SLOTS_PER_EPOCH:>12,}  {f:>12,}  {f/SLOTS_PER_EPOCH*100:>6.2f}%  "
              f"{s:>12,}  {s/SLOTS_PER_EPOCH*100:>6.2f}%")

    print()


# ── 2. Sandwich Category Breakdown ──────────────────────────────────────────

def _category_sql():
    """SQL CASE expression for mutually exclusive categories."""
    return """
        CASE
            WHEN multiFrontRun = true OR multiBackRun = true THEN 'B_multi_split'
            WHEN signerSame = true THEN 'A_standard'
            WHEN signerSame = false AND ownerSame = true THEN 'C_signer_change_owner'
            WHEN signerSame = false AND ownerSame = false AND hasTransfer = true THEN 'D_signer_change_transfer'
            ELSE 'E_other'
        END
    """


def report_sandwich_categories(client, start_slot, end_slot, token_prices):
    print("=" * 80)
    print("2. SANDWICH CATEGORY BREAKDOWN")
    print("=" * 80)

    # Fetch all sandwiches with category
    df = client.query_df(f"""
        SELECT
            sandwichId, crossBlock, tokenA, profitA, victimCount,
            multiFrontRun, multiBackRun,
            {_category_sql()} AS category
        FROM (
            SELECT * FROM sandwiches
            WHERE slot >= {start_slot} AND slot <= {end_slot}
            LIMIT 1 BY sandwichId
        )
    """)

    total = len(df)
    print(f"\n  Total sandwiches: {total:,}")

    # Multi-split sub-breakdown
    mf = df["multiFrontRun"].sum()
    mb = df["multiBackRun"].sum()
    both = ((df["multiFrontRun"]) & (df["multiBackRun"])).sum()
    print(f"\n  Multi-split detail: multi_front={mf:,}, multi_back={mb:,}, "
          f"both={both:,}")

    # Compute USD profit
    df["token_price"] = df["tokenA"].map(token_prices)
    df["usd_profit"] = df["profitA"] * df["token_price"]

    # Per-category stats
    categories = [
        ("A_standard", "Standard (signerSame, no multi)"),
        ("B_multi_split", "Multi-split (multiFront/Back)"),
        ("C_signer_change_owner", "Signer-change (ownerSame)"),
        ("D_signer_change_transfer", "Signer-change (transfer)"),
        ("E_other", "Other (signerDiff, ownerDiff, no transfer)"),
    ]

    for cat_key, cat_label in categories:
        sub = df[df["category"] == cat_key]
        n = len(sub)
        if n == 0:
            continue

        inblock = (sub["crossBlock"] == False).sum()
        crossblock = (sub["crossBlock"] == True).sum()
        victims = sub["victimCount"].sum()

        # SOL profit (tokenA = SOL only)
        sol_sub = sub[sub["tokenA"] == "SOL"]
        sol_profit = sol_sub["profitA"].sum()
        sol_positive = (sol_sub["profitA"] > 0).sum()
        sol_negative = (sol_sub["profitA"] < 0).sum()

        # USD profit (all tokens with price)
        priced = sub[sub["usd_profit"].notna()]
        usd_profit = priced["usd_profit"].sum()
        usd_positive = (priced["usd_profit"] > 0).sum()
        usd_negative = (priced["usd_profit"] < 0).sum()
        price_coverage = len(priced) / n * 100

        # Overall profit direction (using profitA sign, not just SOL)
        positive_all = (sub["profitA"] > 0).sum()
        negative_all = (sub["profitA"] < 0).sum()
        zero_all = (sub["profitA"] == 0).sum()

        max_usd = priced["usd_profit"].max() if len(priced) > 0 else 0
        min_usd = priced["usd_profit"].min() if len(priced) > 0 else 0

        print(f"\n  ── {cat_label} ──")
        print(f"     Count:         {n:>10,}  ({n/total*100:.2f}% of total)")
        print(f"     In-block:      {inblock:>10,}  ({inblock/n*100:.2f}%)")
        print(f"     Cross-block:   {crossblock:>10,}  ({crossblock/n*100:.2f}%)")
        print(f"     Total victims: {victims:>10,}")
        print()
        print(f"     Profitable:    {positive_all:>10,}  ({positive_all/n*100:.2f}%)")
        print(f"     Unprofitable:  {negative_all:>10,}  ({negative_all/n*100:.2f}%)")
        print(f"     Zero profit:   {zero_all:>10,}  ({zero_all/n*100:.2f}%)")
        print()
        print(f"     SOL profit (tokenA=SOL, n={len(sol_sub):,}):")
        print(f"       Total:       {sol_profit:>14,.4f} SOL")
        print(f"       Positive:    {sol_positive:>10,}  ({sol_positive/len(sol_sub)*100:.2f}%)" if len(sol_sub) > 0 else "")
        print(f"       Negative:    {sol_negative:>10,}  ({sol_negative/len(sol_sub)*100:.2f}%)" if len(sol_sub) > 0 else "")
        print(f"       Max:         {sol_sub['profitA'].max():>14,.4f} SOL" if len(sol_sub) > 0 else "")
        print(f"       Min:         {sol_sub['profitA'].min():>14,.4f} SOL" if len(sol_sub) > 0 else "")
        print()
        print(f"     USD profit (price coverage: {price_coverage:.1f}%):")
        print(f"       Total:       ${usd_profit:>14,.2f}")
        print(f"       Positive:    {usd_positive:>10,}  ({usd_positive/len(priced)*100:.2f}%)" if len(priced) > 0 else "")
        print(f"       Negative:    {usd_negative:>10,}  ({usd_negative/len(priced)*100:.2f}%)" if len(priced) > 0 else "")
        print(f"       Max:         ${max_usd:>14,.2f}")
        print(f"       Min:         ${min_usd:>14,.2f}")

    print()


# ── 3. Jito Bundle Sandwiches ───────────────────────────────────────────────

def report_jito_bundle_sandwiches(client, start_slot, end_slot, start_epoch, end_epoch):
    print("=" * 80)
    print("3. JITO BUNDLE SANDWICHES (front+victim+back in same bundle)")
    print("=" * 80)

    # Use pure SQL: arrayJoin to expand jito_bundles.transactions,
    # join with inBundle sandwich_txs, group by (sandwichId, bundleId),
    # check if all three roles are present.
    jito_base_query = f"""
        SELECT DISTINCT sandwichId, any(slot) AS slot
        FROM (
            SELECT sandwichId, bundleId, any(slot) AS slot,
                   groupUniqArray(type) AS types
            FROM (
                SELECT st.sandwichId, st.type, st.slot, b.bundleId
                FROM (
                    SELECT sandwichId, type, signature, slot
                    FROM sandwich_txs
                    WHERE slot >= {start_slot} AND slot <= {end_slot}
                      AND inBundle = true
                      AND type IN ('frontRun', 'backRun', 'victim')
                ) AS st
                INNER JOIN (
                    SELECT bundleId, arrayJoin(transactions) AS sig
                    FROM jito_bundles
                    WHERE slot >= {start_slot} AND slot <= {end_slot}
                ) AS b ON st.signature = b.sig
            )
            GROUP BY sandwichId, bundleId
            HAVING hasAll(types, ['frontRun', 'backRun', 'victim'])
        )
        GROUP BY sandwichId
    """

    # Total count
    total_df = client.query_df(f"SELECT count() AS cnt FROM ({jito_base_query})")
    total_jito = int(total_df.iloc[0]["cnt"])
    print(f"\n  Jito bundle sandwiches: {total_jito:,}")

    if total_jito == 0:
        return

    # Per-epoch breakdown
    epoch_df = client.query_df(f"""
        SELECT intDiv(slot, {SLOTS_PER_EPOCH}) AS epoch, count() AS cnt
        FROM ({jito_base_query})
        GROUP BY epoch ORDER BY epoch
    """)

    print(f"\n  {'Epoch':>5}  {'Jito Bundle SW':>15}")
    print("  " + "-" * 25)

    for ep in range(start_epoch, end_epoch + 1):
        row = epoch_df[epoch_df["epoch"] == ep]
        cnt = int(row["cnt"].iloc[0]) if len(row) > 0 else 0
        print(f"  {ep:>5}  {cnt:>15,}")

    print()


# ── Token Prices ────────────────────────────────────────────────────────────

def load_token_prices():
    """Load token prices from crawled data, return dict token -> usd_price."""
    price_path = os.path.join(os.path.dirname(__file__), "data", "token_prices", "prices.csv")
    prices = {}
    if os.path.exists(price_path):
        pdf = pd.read_csv(price_path)
        for _, row in pdf.iterrows():
            if pd.notna(row.get("usd_price")):
                prices[row["token"]] = row["usd_price"]

    # SOL price: use Moralis or fallback
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
            prices["SOL"] = 86.0  # fallback

    print(f"  Token prices loaded: {len(prices)} tokens, SOL=${prices.get('SOL', 0):.2f}")
    return prices


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    start_epoch = args.start_epoch
    end_epoch = args.end_epoch
    start_slot = start_epoch * SLOTS_PER_EPOCH
    end_slot = (end_epoch + 1) * SLOTS_PER_EPOCH - 1

    print(f"\nData Overview Report: Epoch {start_epoch}-{end_epoch}")
    print(f"Slot range: {start_slot:,} - {end_slot:,}")
    print()

    client = get_client()
    token_prices = load_token_prices()
    print()

    report_slot_coverage(client, start_slot, end_slot, start_epoch, end_epoch)
    report_sandwich_categories(client, start_slot, end_slot, token_prices)
    report_jito_bundle_sandwiches(client, start_slot, end_slot, start_epoch, end_epoch)

    print("Report complete.")


if __name__ == "__main__":
    main()
