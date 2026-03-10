import time
from collections import defaultdict

import pandas as pd
import pyarrow.dataset as ds


class UnionFind:
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.rank.setdefault(ra, 0)
        self.rank.setdefault(rb, 0)
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def identify_attackers(df, profit_df, name, attacker_tx_types):
    uf = UnionFind()

    for sid, txs in df[df["type"].isin(attacker_tx_types)].groupby("sandwichId"):
        attacker_txs = txs[txs["type"].isin(attacker_tx_types)]
        attacker_signers = sorted(
            set(attacker_txs["signer"].dropna().astype(str).tolist())
        )

        signer_same = (
            bool(txs["signerSame"].dropna().iloc[0])
            if not txs["signerSame"].empty
            else False
        )
        if signer_same and len(attacker_signers) != 1:
            print(
                f"[{name}] Warning: sandwichId {sid} has signerSame={signer_same} but multiple signers: {attacker_signers}"
            )
            continue

        if len(attacker_signers) <= 1:
            if len(attacker_signers) == 1:
                _ = uf.find(attacker_signers[0])
            continue
        else:
            base = attacker_signers[0]
            for other in attacker_signers[1:]:
                uf.union(base, other)

    comp_members = defaultdict(set)
    for s in sorted(set(df["signer"].dropna().astype(str))):
        comp_members[uf.find(s)].add(s)

    root_to_attacker_key = {
        root: hex(abs(hash(",".join(sorted(members)))))
        for root, members in comp_members.items()
    }
    signer_to_attacker_key = {
        s: root_to_attacker_key[uf.find(s)]
        for s in set(df["signer"].dropna().astype(str))
    }

    def choose_attacker_for_sandwich(group):
        ss = (
            group[group["type"].isin(attacker_tx_types)]["signer"]
            .dropna()
            .astype(str)
            .tolist()
        )
        keys = sorted(
            {signer_to_attacker_key.get(s) for s in ss if s in signer_to_attacker_key}
        )
        if len(keys) == 0:
            return "UNKNOWN"
        if len(keys) > 1:
            print(
                f"[{name}] Warning: sandwichId {group['sandwichId'].iloc[0]} has multiple attackers: {keys}"
            )
        return keys[0] if len(keys) == 1 else " + ".join(sorted(set(keys)))

    print(df.columns.tolist())
    sandwich_to_attacker = df.groupby("sandwichId", as_index=False).apply(
        lambda g: pd.Series({"attacker_key": choose_attacker_for_sandwich(g)})
    )

    attacker_stat = sandwich_to_attacker.merge(
        profit_df[
            [
                "sandwichId",
                "profitA",
                "profit_SOL",
                "is_SOL",
                "profit_in_usd",
                "distance_type",
                "signerSame",
                "fr_count",
                "br_count",
                "bundle_status",
            ]
        ],
        on="sandwichId",
        how="left",
    )
    print("profit_df: ", profit_df.columns.tolist())
    print("attacker_stat: ", attacker_stat.columns.tolist())
    attacker_stat["win"] = (attacker_stat["profitA"] > 0).astype(int)

    attacker_summary = (
        attacker_stat.groupby("attacker_key", dropna=False)
        .apply(
            lambda r: pd.Series(
                {
                    "sandwich_count": r["sandwichId"].nunique(),
                    "total_profit_SOL": r.loc[r["is_SOL"], "profit_SOL"].sum(),
                    "total_profit_in_usd": r["profit_in_usd"].sum(),
                    "sol_sandwich_count": r["is_SOL"].sum(),
                    "avg_profit_SOL": r.loc[r["is_SOL"], "profit_SOL"].mean(),
                    "avg_profit_in_usd": r["profit_in_usd"].mean(),
                    "win_count": r["win"].sum(),
                    "win_rate": r["win"].mean(),
                    "signer_diff_count": r[r["signerSame"] == False][
                        "sandwichId"
                    ].nunique(),
                    "signer_diff_profit": r[r["signerSame"] == False][
                        "profit_in_usd"
                    ].sum(),
                    "inblock_consec_count": r[r["distance_type"] == "inblock_consec"][
                        "sandwichId"
                    ].nunique(),
                    "inblock_consec_profit": r[r["distance_type"] == "inblock_consec"][
                        "profit_in_usd"
                    ].sum(),
                    "non_consec_count": r[r["distance_type"] != "inblock_consec"][
                        "sandwichId"
                    ].nunique(),
                    "non_consec_profit": r[r["distance_type"] != "inblock_consec"][
                        "profit_in_usd"
                    ].sum(),
                    "inbundle_count": r[(r["bundle_status"] == "all")][
                        "sandwichId"
                    ].nunique(),
                    "inbundle_profit": r[(r["bundle_status"] == "all")][
                        "profit_in_usd"
                    ].sum(),
                    "multi_fb_count": r[(r["fr_count"] > 1) | (r["br_count"] > 1)][
                        "sandwichId"
                    ].nunique(),
                    "multi_fb_profit": r[(r["fr_count"] > 1) | (r["br_count"] > 1)][
                        "profit_in_usd"
                    ].sum(),
                    "inblock_non_consec_count": r[
                        r["distance_type"] == "inblock_non_consec"
                    ]["sandwichId"].nunique(),
                    "inblock_non_consec_profit": r[
                        r["distance_type"] == "inblock_non_consec"
                    ]["profit_in_usd"].sum(),
                    "cross_block_count": r[r["distance_type"] == "cross_block"][
                        "sandwichId"
                    ].nunique(),
                    "cross_block_profit": r[r["distance_type"] == "cross_block"][
                        "profit_in_usd"
                    ].sum(),
                }
            )
        )
        .reset_index()
    )

    attacker_addresses = (
        pd.Series(
            {
                root_to_attacker_key[root]: sorted(list(members))
                for root, members in comp_members.items()
            },
            name="signer_addresses",
        )
        .reset_index()
        .rename(columns={"index": "attacker_key"})
    )
    attacker_addresses["signer_address_count"] = attacker_addresses[
        "signer_addresses"
    ].apply(len)
    attacker_addresses["signer_addresses_str"] = attacker_addresses[
        "signer_addresses"
    ].apply(lambda xs: ", ".join(xs))

    attacker_summary = attacker_summary.merge(
        attacker_addresses, on="attacker_key", how="left"
    )
    attacker_summary["sandwichType"] = name
    attacker_summary = attacker_summary.sort_values(
        by=["win_count", "win_rate"], ascending=False
    )

    return attacker_summary


START_SLOT = 370656000  # Start of epoch 858
END_SLOT = 377135999  # End of epoch 872
TX_TYPES = ["frontRun", "backRun", "victim", "transfer", "adverse"]
ATTACKER_TX_TYPES = ["frontRun", "backRun", "transfer"]
EPS_WIN = 1e-5

TX_FOLDER = "data/parquet_out"
SANDWICH_STAT_PATH = "data/sandwich_stat.csv"

# 1. load sandwich transaction data
start_time = time.time()

sandwiches_txs = ds.dataset(TX_FOLDER, format="parquet")
sandwiches_txs = sandwiches_txs.to_table().to_pandas()

end_time = time.time()
elapsed = end_time - start_time
print(f"Query took {elapsed:.2f} seconds")

# 2. load sandwich statistics
sandwich_stat = pd.read_csv(SANDWICH_STAT_PATH)

# 3. print basic information
print(
    f"Block {sandwiches_txs['tx_slot'].min()} - Block {sandwiches_txs['tx_slot'].max()}"
)
print(f" - Number of transactions: {len(sandwiches_txs)}")
print(f" - Number of sandwiches: {sandwiches_txs['sandwichId'].nunique()}")
print(
    f" - Number of victims: {sandwiches_txs[sandwiches_txs['type'] == 'victim']['signer'].nunique()}"
)
print(
    f" - Number of unique victim transactions: {sandwiches_txs[sandwiches_txs['type'] == 'victim']['signature'].nunique()}"
)
print(
    f" - Number of victim transactions: {len(sandwiches_txs[sandwiches_txs['type'] == 'victim']['signature'])}"
)
start = time.time()
attacker_summary = identify_attackers(
    sandwiches_txs, sandwich_stat, "All sandwiches", ATTACKER_TX_TYPES
)
end_time = time.time()
elapsed = end_time - start_time
print(f"Analysis took {elapsed:.2f} seconds")

attacker_summary.to_csv("data/attacker_summary.csv", index=False)
