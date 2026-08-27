"""
Build the token price table used by every USD figure downstream.

Needs MORALIS_API_KEY in `sandwich-intent/.env` (see .env.example); a free
Moralis project key is enough.

Usage:
    python3 0_crawl_token_price.py --database solwich
    python3 0_crawl_token_price.py --database solwich --top-n 5000   # cheap partial run
"""

import argparse
import os
import time

import pandas as pd
import requests

from utils.db import get_client, require_env

BATCH_URL = "https://solana-gateway.moralis.io/token/mainnet/prices"
BATCH_MAX = 100

# The detector stores native SOL as the literal string "SOL" in sandwiches.tokenA; the wrapped mint
# is the same asset and carries the same price.
SOL_WRAPPED_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_OUT = "data/token_prices/prices.csv"
COLUMNS = ["token", "sandwich_count", "usd_price", "symbol", "name", "decimals"]


def parse_args():
    p = argparse.ArgumentParser(description="Crawl token prices")
    p.add_argument("--database", type=str, default=None,
                   help="ClickHouse database to rank tokens by")
    p.add_argument("--top-n", type=int, default=100,
                   help="Only fetch the N most frequent tokens (0 = every token)")
    p.add_argument("--sol-price", type=float, default=80.0,
                   help="Fixed USD price for SOL and wrapped SOL")
    p.add_argument("--out", type=str, default=DEFAULT_OUT)
    p.add_argument("--rps", type=float, default=3.0, help="Batch requests per second")
    p.add_argument("--retry-empty", action="store_true",
                   help="Re-query tokens already on file without a price")
    return p.parse_args()


def get_tokens(client, top_n):
    """Every tokenA seen, most frequent first. The ranking drives fetch order, nothing else."""
    limit = f"LIMIT {top_n}" if top_n > 0 else ""
    return client.query_df(f"""
        SELECT tokenA AS token, count() AS total_cnt FROM sandwiches
        GROUP BY tokenA ORDER BY total_cnt DESC {limit}
    """)


def fetch_batch(session, api_key, addresses):
    """Address -> price record for whichever addresses Moralis can price; others are absent.

    Returns None if the request itself failed. That is not the same as "no price": recording a
    failed batch as unpriced would permanently mark 100 tokens as unquotable on the strength of one
    transient 500, and they would never be retried.
    """
    for attempt in range(5):
        try:
            r = session.post(BATCH_URL,
                             headers={"X-API-Key": api_key,
                                      "Content-Type": "application/json"},
                             json={"addresses": addresses}, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            out = {}
            for row in r.json():
                addr = row.get("tokenAddress")
                if addr and row.get("usdPrice") is not None:
                    out[addr] = {
                        "usd_price": row.get("usdPrice"),
                        "symbol": row.get("tokenSymbol") or row.get("symbol") or "",
                        "name": row.get("tokenName") or row.get("name") or "",
                        "decimals": row.get("tokenDecimals") or row.get("decimals"),
                    }
            return out
        if r.status_code in (429, 503):
            time.sleep(5 * (attempt + 1))
            continue
        print(f"    HTTP {r.status_code}: {r.text[:120]}")
        return None
    return None


def load_existing(path):
    """Whatever the table already holds. A freshly quoted price replaces the row it lands on."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    df["usd_price"] = pd.to_numeric(df["usd_price"], errors="coerce")
    return df[COLUMNS]


def main():
    args = parse_args()
    # Before anything else: a missing key must not surface after the ranking query.
    api_key = require_env("MORALIS_API_KEY",
                          "a free Moralis project key is what prices the tokens")
    client = get_client(args.database)
    db = args.database or os.getenv("CLICKHOUSE_DATABASE", "solwich")
    out_path = args.out
    if os.path.dirname(out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

    tokens = get_tokens(client, args.top_n)
    counts = {str(t): int(c) for t, c in zip(tokens["token"], tokens["total_cnt"])}
    total_sw = int(tokens["total_cnt"].sum())
    print(f"Database : {db}")
    print(f"Tokens   : {len(tokens):,} distinct tokenA over {total_sw:,} sandwiches")

    existing = load_existing(out_path)
    if args.retry_empty:
        skip = set(existing.loc[existing["usd_price"].notna(), "token"].astype(str))
    else:
        skip = set(existing["token"].astype(str))
    print(f"On file  : {len(existing):,} rows ({int(existing['usd_price'].notna().sum()):,} priced)")

    pending = [t for t in tokens["token"].astype(str)
               if t not in skip and t not in ("SOL", SOL_WRAPPED_MINT)]
    nbatch = (len(pending) + BATCH_MAX - 1) // BATCH_MAX
    print(f"To fetch : {len(pending):,}  ({nbatch:,} batches at {args.rps} req/s, "
          f"~{nbatch / args.rps / 60:.0f} min)")

    session = requests.Session()
    fetched, found, failed = {}, 0, 0
    interval = 1.0 / args.rps
    t_next = time.monotonic()
    for i in range(0, len(pending), BATCH_MAX):
        chunk = pending[i:i + BATCH_MAX]
        wait = t_next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        t_next = time.monotonic() + interval
        got = fetch_batch(session, api_key, chunk)
        if got is None:
            failed += len(chunk)          # left pending, not recorded as unpriced
        else:
            found += len(got)
            for t in chunk:
                fetched[t] = got.get(t)
        done = i + len(chunk)
        if done % (BATCH_MAX * 50) == 0 or done >= len(pending):
            print(f"    {done:,}/{len(pending):,} queried, {found:,} priced"
                  + (f", {failed:,} in failed batches (left pending)" if failed else ""),
                  flush=True)

    new_df = pd.DataFrame(
        [{"token": t,
          "sandwich_count": counts.get(t),
          "usd_price": (v or {}).get("usd_price"),
          "symbol": (v or {}).get("symbol", ""),
          "name": (v or {}).get("name", ""),
          "decimals": (v or {}).get("decimals")}
         for t, v in fetched.items()],
        columns=COLUMNS)

    df = pd.concat([existing, new_df], ignore_index=True)
    df = df.drop_duplicates(subset=["token"], keep="last")

    for tok in ("SOL", SOL_WRAPPED_MINT):
        df = df[df["token"] != tok]
        df = pd.concat([df, pd.DataFrame([{
            "token": tok, "sandwich_count": counts.get(tok), "usd_price": args.sol_price,
            "symbol": "SOL", "name": "Solana", "decimals": 9}])], ignore_index=True)

    df["sandwich_count"] = df["token"].astype(str).map(counts).fillna(df["sandwich_count"])
    df["usd_price"] = pd.to_numeric(df["usd_price"], errors="coerce")
    df = df.sort_values("sandwich_count", ascending=False, na_position="last")
    df[COLUMNS].to_csv(out_path, index=False)

    priced = df[df["usd_price"].notna()]
    covered = float(priced["sandwich_count"].fillna(0).sum())
    print(f"\nSaved {len(df):,} rows to {out_path}")
    print(f"  priced            : {len(priced):,}")
    print(f"  sandwich coverage : {covered:,.0f} / {total_sw:,} = {covered / total_sw * 100:.2f}%")
    print(f"  SOL pinned at     : ${args.sol_price}")
    if failed:
        print(f"  WARNING: {failed:,} tokens were in batches that errored and remain unfetched. "
              f"Re-run to pick them up.")


if __name__ == "__main__":
    main()
