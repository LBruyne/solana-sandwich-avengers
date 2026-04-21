"""
Crawl current token prices from Moralis API for top tokenB in our database.
Saves to data/token_prices/prices.csv

Usage:
    python 0_crawl_token_price.py [--top-n 100]
"""

import argparse
import os
import time

import pandas as pd
import requests

from utils.db import get_client

API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6IjJhYTc1MGZlLWY0YTMtNGNkOC1iOTdkLWE2YzE5ZmMyNmE4OSIsIm9yZ0lkIjoiNTA5NTI2IiwidXNlcklkIjoiNTI0MjQ2IiwidHlwZUlkIjoiMmQ1M2E0M2YtMzExOC00Y2IxLWJiN2ItN2YyZTMyOWQ3MmQ3IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NzYxNTA3MzEsImV4cCI6NDkzMTkxMDczMX0.AGEZYIgBc_q9O1maVv0siYOrZm2HnNFflcPJdpGjQSI"
BASE_URL = "https://solana-gateway.moralis.io/token/mainnet"
OUT_DIR = "data/token_prices"


def get_top_tokens(client, top_n):
    """Get top tokenA by occurrence in sandwiches (profit is in tokenA units)."""
    df = client.query_df(f"""
        SELECT tokenA as token, count() as total_cnt FROM sandwiches
        WHERE signerSame = true AND hasTransfer = false
          AND multiFrontRun = false AND multiBackRun = false
        GROUP BY tokenA ORDER BY total_cnt DESC LIMIT {top_n}
    """)
    return df


def fetch_price(token_address):
    """Fetch current price from Moralis."""
    url = f"{BASE_URL}/{token_address}/price"
    headers = {"X-API-Key": API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "usd_price": data.get("usdPrice", None),
                "symbol": data.get("symbol", ""),
                "name": data.get("name", ""),
                "decimals": data.get("nativePrice", {}).get("decimals", None),
            }
        elif resp.status_code == 404:
            return {"usd_price": None, "symbol": "", "name": "", "decimals": None}
        else:
            print(f"    HTTP {resp.status_code}: {resp.text[:100]}")
            return None
    except Exception as e:
        print(f"    Error: {e}")
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--top-n", type=int, default=100)
    args = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    client = get_client()
    tokens = get_top_tokens(client, args.top_n)
    print(f"Top {len(tokens)} tokens to fetch prices for")

    # Load existing prices to avoid re-fetching
    out_path = f"{OUT_DIR}/prices.csv"
    if os.path.exists(out_path):
        existing = pd.read_csv(out_path)
        existing_tokens = set(existing["token"])
        print(f"Existing prices: {len(existing)} ({existing['usd_price'].notna().sum()} valid)")
    else:
        existing = pd.DataFrame()
        existing_tokens = set()

    # Only fetch tokens we don't have yet
    to_fetch = tokens[~tokens["token"].isin(existing_tokens)]
    print(f"New tokens to fetch: {len(to_fetch)}")

    results = []
    for i, row in to_fetch.iterrows():
        token = row["token"]
        cnt = int(row["total_cnt"])
        price_data = fetch_price(token)

        if price_data is None:
            time.sleep(1)
            price_data = fetch_price(token)

        if price_data:
            results.append({
                "token": token,
                "sandwich_count": cnt,
                "usd_price": price_data["usd_price"],
                "symbol": price_data["symbol"],
                "name": price_data["name"],
                "decimals": price_data["decimals"],
            })
            status = f"${price_data['usd_price']:.6f}" if price_data["usd_price"] else "N/A"
            fetched = len(results)
            if fetched % 50 == 0 or fetched <= 5:
                print(f"  [{fetched}/{len(to_fetch)}] {price_data.get('symbol', '?'):>8s} {status}")
        else:
            results.append({
                "token": token,
                "sandwich_count": cnt,
                "usd_price": None,
                "symbol": "",
                "name": "",
                "decimals": None,
            })

        time.sleep(0.05)

    new_df = pd.DataFrame(results)
    # Merge with existing
    if len(existing) > 0:
        # Update counts for existing tokens
        token_counts = dict(zip(tokens["token"], tokens["total_cnt"]))
        existing["sandwich_count"] = existing["token"].map(token_counts).fillna(existing["sandwich_count"])
        df = pd.concat([existing, new_df], ignore_index=True).drop_duplicates(subset=["token"], keep="last")
    else:
        df = new_df
    df.to_csv(out_path, index=False)
    print(f"\nSaved to {out_path} ({len(df)} tokens)")

    # Summary
    valid = df[df["usd_price"].notna()]
    print(f"Valid prices: {len(valid)}/{len(df)}")
    print(f"\nTop 10 by price:")
    for _, row in valid.nlargest(10, "usd_price").iterrows():
        print(f"  {row['symbol']:>8s} ${row['usd_price']:>12.4f}  (cnt={row['sandwich_count']:,})")


if __name__ == "__main__":
    main()
