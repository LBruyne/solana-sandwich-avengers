"""Flag `ownersOfB` keys that are SPL token accounts rather than wallets.

`diff_signer_owner` anchors its entity on the token-B owner intersection of the front and
back legs. When `tokenB == SOL` the detector appends every account whose SOL balance moved
to `ownersOfB` (`sol/sandwich_finder.go`), so a wSOL associated token account's own address
can land in the owner set and then win the entity's name, since `resolve_entities`
canonicalises on `min(members)`.

The test is `getAccountInfo(...).owner in {SPL Token programs}`, not "never signed": an
owner is under no obligation to sign.

Scope is `diff_signer_owner`. The other categories label from `extract_common_signer`,
which returns a signer of the front leg.

Candidates come from ClickHouse rather than from a phase-1 run, so this can execute before
phase 1.

Needs an RPC helper; see rpc.sh.example.

Output: data/account_kind/<database>_owners.csv -- address, owner, is_token_account,
checked_at. Consumed by `1_signer_data_preparation_and_summary.anchor_sets`.

Usage:
    python check_owner_accounts.py --database solwich --start-epoch 946 --end-epoch 990
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

import pandas as pd

# Scripts live one level below the package root but address `utils.*`, the numbered
# pipeline modules, and `data/` relative to it. Anchor both to the root so they can be
# run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from utils.db import get_client

# Both on-chain helpers shell out to a small script with the contract
#     rpc.sh <method> <params-json>   ->   the raw JSON-RPC response on stdout
# rather than building the URL here, so the endpoint's API key stays out of this
# process, out of argv and out of every log line. See rpc.sh.example.
RPC = os.environ.get("SOLANA_RPC_CMD", os.path.join(_ROOT, "rpc.sh"))


def _require_rpc():
    if not os.path.exists(RPC):
        raise SystemExit(
            f"No RPC helper at {RPC}. Copy rpc.sh.example to rpc.sh (or point "
            f"SOLANA_RPC_CMD at your own), then make it executable.")
SLOTS_PER_EPOCH = 432_000
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Flag diff_signer_owner anchor candidates that are SPL token accounts")
    p.add_argument("--database", default="solwich")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--out-dir", default="data/account_kind")
    return p.parse_args()


def candidates(client, database, start_epoch, end_epoch):
    """Every address that could become a diff_signer_owner anchor, from the DB.

    The scope mirrors `CATEGORY_SCOPES["diff_signer_owner"]` exactly; if that scope is ever widened,
    widen it here too or the newly-included sandwiches go unchecked.
    """
    lo, hi = start_epoch * SLOTS_PER_EPOCH, (end_epoch + 1) * SLOTS_PER_EPOCH
    df = client.query_df(f"""
        WITH dso AS (
            SELECT sandwichId FROM {database}.sandwiches
            WHERE slot >= {lo} AND slot < {hi}
              AND NOT signerSame AND ownerSame
              AND NOT multiFrontRun AND NOT multiBackRun)
        SELECT DISTINCT arrayJoin(ownersOfB) AS ow
        FROM {database}.sandwich_txs
        WHERE slot >= {lo} AND slot < {hi}
          AND type IN ('frontRun', 'backRun')
          AND sandwichId IN (SELECT sandwichId FROM dso)""")
    out = set(df["ow"].astype(str)) if len(df) else set()
    # Union in whatever previous runs anchored on, so a key that has left the DB scope but is still
    # referenced by an entity map on disk cannot slip through unchecked.
    for p in glob.glob(f"data/1_signer_data_preparation_and_summary/diff_signer_owner/"
                       f"{database}/signer_entity_map_*.csv"):
        d = pd.read_csv(p)
        out |= set(d["anchor_key"].astype(str)) | set(d["entity"].astype(str))
    return sorted(a for a in out if a and a != "nan")


def fetch_owners(addrs):
    """address -> account info (or None), 100 at a time.

    `dataSlice` length 0 keeps the reply to the header; the owner field is the whole question and a
    token account's 165 bytes are not worth transferring 41,000 times.
    """
    _require_rpc()
    out = {}
    t0 = time.time()
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        params = json.dumps([chunk, {"encoding": "base64",
                                     "dataSlice": {"offset": 0, "length": 0}}])
        r = subprocess.run([RPC, "getMultipleAccounts", params],
                           capture_output=True, text=True, timeout=180)
        try:
            j = json.loads(r.stdout)
        except Exception:
            raise SystemExit(f"getMultipleAccounts returned non-JSON: {r.stdout[:200]}")
        if "result" not in j:
            raise SystemExit(f"getMultipleAccounts failed: {r.stdout[:300]}")
        for a, v in zip(chunk, j["result"]["value"]):
            out[a] = v
        if (i // 100) % 50 == 0:
            print(f"    {i + len(chunk):>7,}/{len(addrs):,}  ({time.time() - t0:.0f}s)")
    return out


def main():
    a = parse_args()
    client = get_client(a.database)
    print(f"=== diff_signer_owner anchor kinds — {a.database}, "
          f"epochs {a.start_epoch}-{a.end_epoch} ===")
    addrs = candidates(client, a.database, a.start_epoch, a.end_epoch)
    print(f"{len(addrs):,} candidate owner keys")

    print("\ngetMultipleAccounts:")
    info = fetch_owners(addrs)

    rows = []
    for x in addrs:
        v = info.get(x)
        owner = (v or {}).get("owner")
        rows.append({"address": x, "exists": bool(v), "owner": owner,
                     "is_token_account": owner in TOKEN_PROGRAMS,
                     "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    d = pd.DataFrame(rows).set_index("address").sort_index()
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"{a.database}_owners.csv")
    d.to_csv(path)

    bad = d[d["is_token_account"]]
    print(f"\nWrote {path} ({len(d):,} rows)")
    print(f"  absent at head    : {int((~d['exists']).sum()):,}")
    print(f"  System-program    : {int((d['owner'] == '11111111111111111111111111111111').sum()):,}")
    print(f"  SPL token accounts: {len(bad):,}   <- phase 1 will exclude these from anchors")
    other = d[d["exists"] & ~d["is_token_account"]
              & (d["owner"] != "11111111111111111111111111111111")]
    if len(other):
        print(f"  program-owned, non-token: {len(other):,} across "
              f"{other['owner'].nunique()} programs — these are PDAs and are left alone; they are "
              f"real counterparties, not mislabelled wallets")
    for x in list(bad.index)[:10]:
        print(f"    {x}")
    if len(bad) > 10:
        print(f"    … and {len(bad) - 10:,} more")


if __name__ == "__main__":
    main()
