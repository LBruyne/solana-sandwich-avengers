"""Build the public attacker and sandwich dataset.

Reads the phase-3 output of the sandwich-intent pipeline, pulls the matching transaction
rows from ClickHouse, and writes:

    attackers_<start>_<end>.{parquet,csv}          one row per attacker
    sandwiches_<start>_<end>.parquet               one row per sandwich
    sandwiches_<start>_<end>.csv                   the SELECTED attackers only
    sandwich_txs_<start>_<end>_<a>_<b>.parquet     one row per leg, in epoch parts
    sandwich_txs_csv_<start>_<end>_<a>_<b>.csv     the SELECTED attackers' own legs, in epoch parts

SELECTED = the top `--csv-top-n` attackers by net USD profit, plus every attacker named in
the paper (`PAPER_ATTACKERS`).

`sandwich_txs` is split into contiguous epoch ranges, each under `--max-part-mb`, so no
file passes GitHub's 100 MB limit. The CSV carries only the SELECTED attackers' own
frontRun and backRun legs and is packed separately, since a CSV row costs about five times
a parquet row.

Attackers carrying `expert_verdict == "no"` are excluded.

Usage:
    python dataset/build_dataset.py
    python dataset/build_dataset.py --database solwich --start-epoch 946 --end-epoch 990
    python dataset/build_dataset.py --no-tx-detail
    python dataset/build_dataset.py --copy-aux
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
INTENT = REPO / "sandwich-intent"
DATASET = REPO / "dataset"
SLOTS_PER_EPOCH = 432_000

CATEGORIES = ["standard", "multi_split", "diff_signer_owner", "diff_signer_transfer"]
CATEGORY_LABEL = {
    "standard": "standard",
    "multi_split": "multi-split",
    "diff_signer_owner": "diff-signer-owner",
    "diff_signer_transfer": "diff-signer-transfer",
}
LEG_TYPES = ("frontRun", "backRun", "victim", "adverse")
EXPERT_REJECT = "no"

# Five-character address prefixes of every attacker the paper names. Resolved against the
# dataset at build time; a prefix that matches no attacker, or more than one, is reported.
PAPER_ATTACKERS = [
    "2YMM3", "2fNPN", "4hASK", "6m4bq", "7CtW3", "7Fovy", "7nCRb", "7q15W", "8qAsH",
    "93kgx", "AYo5y", "C7y4i", "DDm1B", "DPwH9", "E45YL", "F4qeu", "F8Loq", "FSSFn",
    "HqXf7", "HwGqF", "KKKzQ", "Meskx", "RTYiz", "hnu5i", "hnu69", "iK7Bm", "undwZ",
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database", default="solwich_v2")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--cross-leader", default="include", choices=["include", "exclude"])
    p.add_argument("--csv-top-n", type=int, default=20,
                   help="Attackers by net USD profit in the CSV slice, before the union "
                        "with PAPER_ATTACKERS")
    p.add_argument("--max-part-mb", type=float, default=90.0,
                   help="Upper bound on one sandwich_txs parquet part")
    p.add_argument("--max-csv-part-mb", type=float, default=45.0,
                   help="Upper bound on one sandwich_txs CSV part")
    p.add_argument("--no-tx-detail", action="store_true",
                   help="Skip sandwich_txs and the ClickHouse query")
    p.add_argument("--copy-aux", action="store_true",
                   help="Refresh dataset/aux/token_prices.csv from the pipeline")
    return p.parse_args()


def tag(a):
    t = f"{a.start_epoch}_{a.end_epoch}"
    return t if a.cross_leader == "exclude" else f"{t}_cl-include"


# ── phase-3 input ────────────────────────────────────────────────────────────

def phase3_path(a, cat, kind):
    return (INTENT / "data" / "3_attacker_filter" / cat / a.database / a.cross_leader
            / f"{kind}_{tag(a)}.parquet")


def load_phase3(a):
    """(attackers, sandwiches), pooled over categories, minus the expert-rejected."""
    att, sw = [], []
    for cat in CATEGORIES:
        pa, ps = phase3_path(a, cat, "bot_attackers"), phase3_path(a, cat, "bot_sandwiches")
        if not pa.exists():
            continue
        d = pd.read_parquet(pa)
        if not len(d):
            continue
        d = d.reset_index().rename(columns={d.index.name or "index": "attacker"})
        d["_category"] = cat
        att.append(d)
        s = pd.read_parquet(ps).reset_index().rename(columns={"signer": "attacker"})
        s["_category"] = cat
        sw.append(s)
    if not att:
        sys.exit(f"no phase-3 output under {phase3_path(a, CATEGORIES[0], 'bot_attackers').parent}")
    A = pd.concat(att, ignore_index=True)
    S = pd.concat(sw, ignore_index=True)
    A["attacker"] = A["attacker"].astype(str)
    S["attacker"] = S["attacker"].astype(str)

    if "expert_verdict" not in A.columns:
        sys.exit("phase-3 output has no `expert_verdict` column -- re-run 3_attacker_filter.py")
    rejected = set(A.loc[A["expert_verdict"].astype(str) == EXPERT_REJECT, "attacker"])
    A = A[~A["attacker"].isin(rejected)]
    S = S[~S["attacker"].isin(rejected)]
    print(f"  attackers {A['attacker'].nunique():,} ({len(rejected)} expert-rejected removed) · "
          f"sandwiches {S['sandwichId'].nunique():,}")
    return A, S


# ── output tables ────────────────────────────────────────────────────────────

def build_sandwiches(S):
    out = pd.DataFrame({
        "sandwich_id": S["sandwichId"].astype(str),
        "attacker": S["attacker"],
        "slot": S["slot"].astype("int64"),
        "timestamp": pd.to_datetime(S["ts"], utc=True),
        "token_a": S["token_a"],
        "token_b": S["token_b"],
        "profit_token_a": S["profit"],
        "fee_sol": S["fee_sol"],
        "usd_profit_net": S["usd_profit_net"],
        "is_profitable": S["is_profitable"],
        "victim_count": S["victim_count"].astype("int32"),
        "adverse_count": S["adverse_count"].astype("int32"),
        "front_count": S["front_count"].astype("int32"),
        "back_count": S["back_count"].astype("int32"),
        "cross_block": S["cross_block"].astype(bool),
        "cross_leader": S["cross_leader"].astype(bool),
        "multi_split": (S["multi_front"] | S["multi_back"]).astype(bool),
        "front_gap": S["front_gap"],
        "back_gap": S["back_gap"],
        "slippage_consumption": S["sc"],
        "slippage_state": np.where(S["sc_anomaly"].astype(bool), "anomalous",
                            np.where(S["sc_unprotected"].astype(bool), "unprotected", "scored")),
        "slippage_reason": S["sc_reason"].astype(str),
        "jito_bundle": S["jito_bundle"].astype(bool),
        "pool_dex": S["pool_dex"].astype(str),
        "category": S["_category"].map(CATEGORY_LABEL),
    })
    return out.drop_duplicates("sandwich_id").sort_values(["slot", "sandwich_id"]) \
              .reset_index(drop=True)


def build_attackers(A, sandwiches):
    cats = A.groupby("attacker")["_category"].agg(
        lambda v: ",".join(sorted({CATEGORY_LABEL[c] for c in v})))
    types = A.groupby("attacker")["bot_type"].agg(lambda v: ",".join(sorted(set(v.astype(str)))))
    first = A.sort_values("sandwich_count", ascending=False).drop_duplicates("attacker") \
             .set_index("attacker")

    g = sandwiches.groupby("attacker")
    out = pd.DataFrame(index=g.size().index)
    out.index.name = "attacker"
    out["bot_type"] = types
    out["categories"] = cats
    out["sandwich_count"] = g.size()
    out["usd_profit_net"] = g["usd_profit_net"].sum()
    out["usd_avg_profit"] = out["usd_profit_net"] / out["sandwich_count"]
    out["sol_profit_net"] = first["sol_net_profit"].reindex(out.index)
    out["fee_sol_total"] = g["fee_sol"].sum()
    out["win_rate"] = first["win_rate"].reindex(out.index)
    out["sol_win_rate"] = first["sol_win_rate"].reindex(out.index)
    out["mean_slippage_consumption"] = first["mean_SC"].reindex(out.index)
    out["slippage_coverage"] = first["sc_coverage"].reindex(out.index)
    out["front_gap_median"] = first["front_gap_p50"].reindex(out.index)
    out["front_gap_eq1_ratio"] = first["front_gap_le1_ratio"].reindex(out.index)
    out["in_block_count"] = g.apply(lambda d: int((~d["cross_block"]).sum()), include_groups=False)
    out["cross_block_count"] = g["cross_block"].sum().astype(int)
    out["cross_leader_count"] = g["cross_leader"].sum().astype(int)
    out["multi_split_count"] = g["multi_split"].sum().astype(int)
    out["jito_bundle_count"] = g["jito_bundle"].sum().astype(int)
    out["first_slot"] = g["slot"].min()
    out["last_slot"] = g["slot"].max()
    out["expert_verdict"] = first["expert_verdict"].reindex(out.index).astype(str)
    if "n_signers" in first.columns:
        out["n_signing_keys"] = first["n_signers"].reindex(out.index).fillna(1).astype(int)
    return out.reset_index().sort_values("usd_profit_net", ascending=False).reset_index(drop=True)


def select_csv_attackers(a, attackers):
    top = set(attackers.head(a.csv_top_n)["attacker"])
    named, unresolved = set(), []
    known = set(attackers["attacker"])
    for pre in PAPER_ATTACKERS:
        hit = [x for x in known if x.startswith(pre)]
        if len(hit) == 1:
            named.add(hit[0])
        else:
            unresolved.append((pre, len(hit)))
    if unresolved:
        print("  prefixes not resolved to exactly one attacker: "
              + ", ".join(f"{p} ({n})" for p, n in unresolved))
    sel = top | named
    print(f"  CSV slice: {len(sel)} attackers (top {a.csv_top_n} by profit ∪ "
          f"{len(named)} named in the paper)")
    return sel


# ── ClickHouse: transaction legs ─────────────────────────────────────────────

def fetch_legs(client, a, epoch, ids_table):
    lo, hi = epoch * SLOTS_PER_EPOCH, (epoch + 1) * SLOTS_PER_EPOCH
    d = client.query_df(f"""
        SELECT sandwichId, type, slot, position, signature, signers, inBundle, programs,
               fromToken, toToken, fromAmount, toAmount, fee,
               slippageLimitType, slippageLimitAmount, slippageActualAmount
        FROM sandwich_txs
        WHERE slot >= {lo} AND slot < {hi} AND type IN {LEG_TYPES}
          AND sandwichId IN (SELECT sandwichId FROM {ids_table})""")
    if not len(d):
        return d
    d["tx_signer"] = d["signers"].apply(
        lambda xs: xs[0] if isinstance(xs, (list, tuple, np.ndarray)) and len(xs) else None)
    is_victim = d["type"] == "victim"
    for c in ("slippageLimitAmount", "slippageActualAmount"):
        d[c] = d[c].where(is_victim, np.nan)
    d["slippageLimitType"] = d["slippageLimitType"].where(is_victim, "")
    out = d.rename(columns={
        "sandwichId": "sandwich_id", "inBundle": "in_bundle",
        "fromToken": "from_token", "toToken": "to_token",
        "fromAmount": "from_amount", "toAmount": "to_amount",
        "slippageLimitType": "slippage_limit_type",
        "slippageLimitAmount": "slippage_limit_amount",
        "slippageActualAmount": "slippage_actual_amount",
    })
    cols = ["sandwich_id", "type", "slot", "position", "signature", "tx_signer",
            "from_token", "to_token", "from_amount", "to_amount", "fee", "in_bundle",
            "programs", "slippage_limit_type", "slippage_limit_amount",
            "slippage_actual_amount"]
    return out[cols].sort_values(["slot", "position"]).reset_index(drop=True)


def pack_epochs(sizes_mb, limit):
    """Contiguous epoch runs, each at or under `limit` MB. Returns [(lo, hi), ...]."""
    parts, lo, acc = [], None, 0.0
    for epoch, mb in sizes_mb:
        if lo is None:
            lo, acc, prev = epoch, mb, epoch
        elif acc + mb > limit:
            parts.append((lo, prev))
            lo, acc = epoch, mb
        else:
            acc += mb
        prev = epoch
    if lo is not None:
        parts.append((lo, prev))
    return parts


# ── aux ──────────────────────────────────────────────────────────────────────

def copy_aux():
    aux = DATASET / "aux"
    aux.mkdir(parents=True, exist_ok=True)
    src = INTENT / "data" / "token_prices" / "prices.csv"
    if src.exists():
        shutil.copyfile(src, aux / "token_prices.csv")
        print(f"  token_prices.csv <- {src.relative_to(REPO)}")
    else:
        print(f"  skip token_prices.csv: {src} missing")


# ── main ─────────────────────────────────────────────────────────────────────

def csv_slice(legs, csv_ids):
    """The CSV attackers' own legs: front-run and back-run, no victim or adverse rows."""
    return legs[legs["sandwich_id"].isin(csv_ids)
                & legs["type"].isin(("frontRun", "backRun"))]


def write(df, stem, csv_df=None):
    p = DATASET / f"{stem}.parquet"
    df.to_parquet(p, compression="zstd", index=False)
    print(f"  {p.name:<48} {len(df):>9,} rows  {p.stat().st_size/1e6:>7.1f} MB")
    if csv_df is not None:
        c = DATASET / f"{stem}.csv"
        csv_df.to_csv(c, index=False)
        print(f"  {c.name:<48} {len(csv_df):>9,} rows  {c.stat().st_size/1e6:>7.1f} MB")


def main():
    a = parse_args()
    DATASET.mkdir(exist_ok=True)
    t = f"{a.start_epoch}_{a.end_epoch}"
    print(f"=== {a.database} · epochs {a.start_epoch}-{a.end_epoch} · cross-leader {a.cross_leader} ===")

    if a.copy_aux:
        print("\n=== aux ===")
        copy_aux()

    print("\n=== phase-3 input ===")
    A, S = load_phase3(a)

    print("\n=== tables ===")
    sandwiches = build_sandwiches(S)
    attackers = build_attackers(A, sandwiches)
    sel = select_csv_attackers(a, attackers)
    sw_csv = sandwiches[sandwiches["attacker"].isin(sel)]

    write(attackers, f"attackers_{t}", attackers)
    write(sandwiches, f"sandwiches_{t}", sw_csv)

    if a.no_tx_detail:
        return

    print("\n=== transaction legs ===")
    sys.path.insert(0, str(INTENT))
    from utils.db import get_client
    client = get_client(a.database)
    ids_table = "default._dataset_ids"
    client.command(f"DROP TABLE IF EXISTS {ids_table}")
    client.command(f"CREATE TABLE {ids_table} (sandwichId String) ENGINE=Memory")
    client.insert(ids_table, [[x] for x in sandwiches["sandwich_id"]],
                  column_names=["sandwichId"])

    csv_ids = set(sw_csv["sandwich_id"])
    frames, pq_sizes, csv_sizes = {}, [], []
    for e in range(a.start_epoch, a.end_epoch + 1):
        d = fetch_legs(client, a, e, ids_table)
        if not len(d):
            continue
        frames[e] = d
        buf = io.BytesIO()
        d.to_parquet(buf, compression="zstd", index=False)
        pq_sizes.append((e, buf.tell() / 1e6))
        sub = csv_slice(d, csv_ids)
        txt = io.StringIO()
        sub.to_csv(txt, index=False)
        csv_sizes.append((e, len(txt.getvalue().encode()) / 1e6))
        print(f"    epoch {e}: {len(d):>8,} legs", end="\r")
    client.command(f"DROP TABLE IF EXISTS {ids_table}")
    print(f"{'':<70}\r", end="")

    total = 0
    for lo, hi in pack_epochs(pq_sizes, a.max_part_mb):
        part = pd.concat([frames[e] for e in range(lo, hi + 1) if e in frames],
                         ignore_index=True)
        total += len(part)
        write(part, f"sandwich_txs_{t}_{lo}_{hi}")
    print(f"  sandwich_txs total: {total:,} legs\n")

    total = 0
    for lo, hi in pack_epochs(csv_sizes, a.max_csv_part_mb):
        part = pd.concat([csv_slice(frames[e], csv_ids)
                          for e in range(lo, hi + 1) if e in frames], ignore_index=True)
        total += len(part)
        f = DATASET / f"sandwich_txs_csv_{t}_{lo}_{hi}.csv"
        part.to_csv(f, index=False)
        print(f"  {f.name:<48} {len(part):>9,} rows  {f.stat().st_size/1e6:>7.1f} MB")
    print(f"  sandwich_txs CSV total: {total:,} legs")


if __name__ == "__main__":
    main()
