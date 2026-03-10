import gc
import math
import os
import time
from typing import Callable, Optional, Sequence

import pandas as pd
import pyarrow.dataset as ds
import requests
from clickhouse_connect import get_client
from clickhouse_connect.driver import exceptions as chx
from dotenv import load_dotenv
from tqdm import tqdm

START_SLOT = 370656000  # Start of epoch 858
END_SLOT = 377135999  # End of epoch 872
TX_TYPES = ["frontRun", "backRun", "victim", "transfer", "adverse"]
ATTACKER_TX_TYPES = ["frontRun", "backRun", "transfer"]
EPS_WIN = 1e-5


def load_env():
    load_dotenv(dotenv_path=".env")
    return {
        "host": os.getenv("NEW_CLICKHOUSE_HOST"),
        "port": int(os.getenv("NEW_CLICKHOUSE_PORT")),
        "username": os.getenv("NEW_CLICKHOUSE_USERNAME"),
        "password": os.getenv("NEW_CLICKHOUSE_PASSWORD"),
    }


def quert_slot_info_in_DB(start_slot=0, end_slot=500000000):
    """
    Query the ClickHouse DB to get slot info stored
    """
    if start_slot < START_SLOT:
        print("Warning: start_slot is before the earliest valid slot in DB.")

    query = f"""
    SELECT
        slot,
        txCount
    FROM solwich.slot_txs
    WHERE slot BETWEEN {start_slot} AND {end_slot}
    ORDER BY slot ASC
    """
    result = client.query(query)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    return df


def query_current_slots_in_DB(start_slot=0, end_slot=500000000):
    """
    Query the ClickHouse DB to find the min and max slot stored
    """
    if start_slot < START_SLOT:
        print("Warning: start_slot is before the earliest valid slot in DB.")

    query = f"""
    SELECT
        min(slot) AS min_slot,
        max(slot) AS max_slot,
        countDistinct(slot) AS total_slots
    FROM solwich.slot_txs
    WHERE slot BETWEEN {start_slot} AND {end_slot} AND txFetched = true
    """
    result = client.query(query)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    min_slot = df["min_slot"].iloc[0]
    max_slot = df["max_slot"].iloc[0]
    total_slots = df["total_slots"].iloc[0]
    return min_slot, max_slot, total_slots


def query_sandwiches_with_txs_and_leader_to_parquet(
    start_slot: int,
    end_slot: int,
    *,
    out_dir: str,
    num_parts: int = 55,
    client=None,
    on_chunk: Optional[Callable[[pd.DataFrame], None]] = None,
    parquet_compression: str = "zstd",
    parquet_use_timestamp_ns: bool = True,
) -> Sequence[str]:
    """
    Split the data into chunks and write each chunk directly to a Parquet file.
    """
    if client is None:
        client = get_client()

    os.makedirs(out_dir, exist_ok=True)

    total_slots = end_slot - start_slot + 1
    if total_slots <= 0:
        return []

    step = math.ceil(total_slots / num_parts)
    slots = []
    s = start_slot
    while s <= end_slot:
        e = min(s + step - 1, end_slot)
        slots.append((s, e))
        if e == end_slot:
            break
        s = e + 1

    written_files = []

    for chunk_start, chunk_end in slots:
        retries = 0
        while True:
            try:
                query = f"""
                WITH dedup_sandwiches AS (
                    SELECT
                        sandwichId, slot AS sandwich_slot, crossBlock, tokenA, tokenB,
                        signerSame, ownerSame, profitA, relativeDiffB
                    FROM solwich.sandwiches
                    WHERE slot BETWEEN {chunk_start} AND {chunk_end}
                    LIMIT 1 BY sandwichId
                ),
                dedup_txs AS (
                    SELECT
                        *
                    FROM solwich.sandwich_txs
                    WHERE sandwichId IN (SELECT sandwichId FROM dedup_sandwiches)
                      AND slot BETWEEN {chunk_start} AND {chunk_end}
                    LIMIT 1 BY sandwichId, type, signature
                ),
                s_leaders AS (
                    SELECT
                        slot, any(leader) AS leader
                    FROM solwich.slot_leaders
                    WHERE slot BETWEEN {chunk_start} AND {chunk_end}
                    GROUP BY slot
                )
                SELECT
                    s.sandwichId AS sandwichId,
                    s.sandwich_slot,
                    l.leader AS validator,
                    s.crossBlock,
                    s.tokenA,
                    s.tokenB,
                    s.signerSame,
                    s.ownerSame,
                    s.profitA,
                    s.relativeDiffB,
                    t.type,
                    t.slot AS tx_slot,
                    t.position,
                    t.timestamp,
                    t.fee,
                    t.signature,
                    t.signer,
                    t.inBundle,
                    t.programs,
                    t.fromToken,
                    t.toToken,
                    t.fromAmount,
                    t.toAmount,
                    t.attackerPreBalanceB,
                    t.attackerPostBalanceB,
                    t.poolPreBalanceB,
                    t.poolPostBalanceB,
                    t.ownersOfB,
                    t.fromTotalAmount,
                    t.toTotalAmount,
                    t.diffA,
                    t.diffB
                FROM dedup_sandwiches AS s
                INNER JOIN dedup_txs AS t ON s.sandwichId = t.sandwichId
                LEFT JOIN s_leaders AS l ON l.slot = t.slot
                """

                result = client.query(query)
                if not result.result_rows:
                    break

                df_chunk = pd.DataFrame(result.result_rows, columns=result.column_names)

                for col in ("signerSame", "ownerSame", "inBundle"):
                    if col in df_chunk.columns:
                        df_chunk[col] = df_chunk[col].astype("uint8")

                fname = os.path.join(out_dir, f"{chunk_start}_{chunk_end}.parquet")
                df_chunk.to_parquet(
                    fname,
                    index=False,
                    compression=parquet_compression,
                    engine="pyarrow",
                )
                written_files.append(fname)

                if on_chunk:
                    on_chunk(df_chunk)

                del df_chunk
                break

            except (
                chx.OperationalError,
                chx.DatabaseError,
                chx.InternalError,
                chx.Error,
            ) as e:
                retries += 1
                if retries > 3:
                    print(
                        f"[Fatal] {chunk_start}-{chunk_end} failed after retries: {e}"
                    )
                    break
                wait = 2 * retries
                print(
                    f"[Retry {retries}] {chunk_start}-{chunk_end}: {e} → sleep {wait}s"
                )
                time.sleep(wait)

            except Exception as e:
                print(f"[Fatal] Unexpected on {chunk_start}-{chunk_end}: {e}")
                break

    print(f"\nFinished. Wrote {len(written_files)} parquet parts into {out_dir}")
    return written_files


WSOL = "So11111111111111111111111111111111111111112"


def to_mint(tokenA):
    return WSOL if tokenA.upper() == "SOL" else tokenA


def fetch_token_prices(tokens):
    """
    Fetch the prices of the specified tokens (in SOL and USD).
    """
    load_dotenv(dotenv_path=".env")
    MORALIS_KEY = os.getenv("MORALIS_API_KEY")

    session = requests.Session()
    price_rows = []
    print(f"Fetching {len(tokens)} token prices from Moralis...")

    for t in tokens:
        url = f"https://solana-gateway.moralis.io/token/mainnet/{to_mint(t)}/price"
        headers = {"X-API-Key": MORALIS_KEY, "accept": "application/json"}
        try:
            r = session.get(url, headers=headers, timeout=10)
            d = r.json()
            val = float(d["nativePrice"]["value"])
            dec = int(d["nativePrice"]["decimals"])
            price_sol = val / (10**dec)
            price_usd = float(d["usdPrice"])
        except Exception as e:
            print(f"Failed to fetch price for {t}: {e}")
            price_sol = price_usd = None
        price_rows.append((t, price_sol, price_usd))
        time.sleep(0.05)

    price_df = pd.DataFrame(price_rows, columns=["tokenA", "price_in_sol", "usd_price"])
    return price_df


def _build_profit_view(
    df: pd.DataFrame,
    fetch_token_prices_func: Callable[[list[str]], pd.DataFrame] = fetch_token_prices,
    top_n_tokens: int = 20,
) -> pd.DataFrame:
    base = df.drop_duplicates(subset=["sandwichId"])[
        ["sandwichId", "tokenA", "profitA"]
    ].copy()
    base["tokenA"] = base["tokenA"].fillna("UNKNOWN")
    base["profitA"] = base["profitA"].fillna(0.0)

    token_counts = base["tokenA"].value_counts()
    top_tokens = token_counts.head(top_n_tokens).index.tolist()

    price_df = fetch_token_prices_func(top_tokens)  # ['tokenA', 'usd_price']
    base = base.merge(price_df, on="tokenA", how="left")
    base["usd_price"] = base["usd_price"].fillna(0.0)
    base["profit_in_usd"] = base["profitA"] * base["usd_price"]

    base["is_SOL"] = base["tokenA"] == "SOL"
    base["profit_SOL"] = base["profitA"].where(base["is_SOL"], 0.0)
    return base


def _analyze_one_chunk(
    chunk_df: pd.DataFrame,
    slot_txcount_map: dict,
) -> pd.DataFrame:
    dist_rows = []

    for sid, txs in chunk_df.groupby("sandwichId", sort=False):
        cross_block = bool(txs["crossBlock"].iloc[0])
        tx_count = int(len(txs))
        inbundle_count = int(txs["inBundle"].sum())
        tokenB = txs["tokenB"].iloc[0] if "tokenB" in txs.columns else None

        signer_same = txs["signerSame"].iloc[0] if "signerSame" in txs.columns else None
        owner_same = txs["ownerSame"].iloc[0] if "ownerSame" in txs.columns else None

        fr = txs[txs["type"] == "frontRun"]
        br = txs[txs["type"] == "backRun"]
        victim = txs[txs["type"] == "victim"]

        fr_count = int(len(fr))
        br_count = int(len(br))
        transfer_count = int((txs["type"] == "transfer").sum())
        adverse_count = int((txs["type"] == "adverse").sum())
        victim_count = int(len(victim))

        pos_unique = sorted(txs["position"].dropna().unique().tolist())
        consecutive = (
            (not cross_block)
            and len(pos_unique) > 0
            and (pos_unique[-1] - pos_unique[0] + 1 == len(pos_unique))
        )

        last_front_pos = fr["position"].max() if not fr.empty else None
        first_back_pos = br["position"].min() if not br.empty else None
        last_front_slot = fr["tx_slot"].max() if not fr.empty else None
        first_back_slot = br["tx_slot"].min() if not br.empty else None
        first_victim_slot = victim["tx_slot"].min() if not victim.empty else None
        last_victim_slot = victim["tx_slot"].max() if not victim.empty else None

        inblock_distance = None
        crossblock_gap_slots = None
        crossblock_fr_victimslot_gap = None
        crossblock_br_victimslot_gap = None
        crossblock_distance = None

        # fee
        fr_fee = fr["fee"].sum() if not fr.empty and "fee" in fr.columns else 0.0
        br_fee = br["fee"].sum() if not br.empty and "fee" in br.columns else 0.0
        tx_fee = fr_fee + br_fee

        # inblock distance
        if (
            (not cross_block)
            and last_front_pos is not None
            and first_back_pos is not None
        ):
            inblock_distance = int(first_back_pos - last_front_pos)

        # cross block distance
        if cross_block and last_front_slot is not None and first_back_slot is not None:
            crossblock_gap_slots = int(first_back_slot - last_front_slot)

            if first_victim_slot is not None:
                crossblock_fr_victimslot_gap = int(first_victim_slot - last_front_slot)
            if last_victim_slot is not None:
                crossblock_br_victimslot_gap = int(first_back_slot - last_victim_slot)

            if slot_txcount_map:
                crossblock_distance = sum(
                    slot_txcount_map.get(slot, 0)
                    for slot in range(last_front_slot, first_back_slot)
                )
                if last_front_pos is not None and first_back_pos is not None:
                    crossblock_distance += first_back_pos - last_front_pos

        # bundle status
        if tx_count == 0:
            bundle_status = "no_tx"
        elif inbundle_count == tx_count:
            bundle_status = "all"
        elif 0 < inbundle_count < tx_count:
            bundle_status = "partial"
        else:
            bundle_status = "none"

        timestamp = txs["timestamp"].min()

        dist_rows.append(
            {
                "sandwichId": sid,
                "slot": txs["tx_slot"].min(),
                "timestamp": timestamp,
                "cross_block": cross_block,
                "consecutive": consecutive,
                "bundle_status": bundle_status,
                "tx_count": tx_count,
                "inbundle_count": inbundle_count,
                "fr_count": fr_count,
                "br_count": br_count,
                "transfer_count": transfer_count,
                "adverse_count": adverse_count,
                "victim_count": victim_count,
                "inblock_distance": inblock_distance,
                "crossblock_gap_slots": crossblock_gap_slots,
                "crossblock_fr_victimslot_gap": crossblock_fr_victimslot_gap,
                "crossblock_br_victimslot_gap": crossblock_br_victimslot_gap,
                "crossblock_distance": crossblock_distance,
                "signerSame": signer_same,
                "ownerSame": owner_same,
                "tokenB": tokenB,
                "fr_fee": fr_fee,
                "br_fee": br_fee,
                "tx_fee": tx_fee,
            }
        )

    return pd.DataFrame(dist_rows)


def analyze_sandwiches(
    sandwiches_txs: pd.DataFrame,
    fetch_token_prices_func: Callable[[list[str]], pd.DataFrame] = fetch_token_prices,
    top_n_tokens: int = 20,
    analyze_bundle_status: bool = True,
    verbose: bool = True,
    slot_info: Optional[pd.DataFrame] = None,
    chunk_size: int = 200_000,
    out_path: Optional[str] = None,
) -> pd.DataFrame:
    df = sandwiches_txs.copy()

    # build profit view
    if "tokenA" not in df.columns:
        df["tokenA"] = "UNKNOWN"
    if "profitA" not in df.columns:
        df["profitA"] = 0.0

    profit_view = _build_profit_view(df, fetch_token_prices_func, top_n_tokens)

    slot_txcount_map = {}
    if slot_info is not None:
        slot_txcount_map = slot_info.set_index("slot")["txCount"].to_dict()

    all_ids = df["sandwichId"].drop_duplicates().values
    n = len(all_ids)

    pieces = []
    first_write = True

    for i in tqdm(range(0, n, chunk_size), desc="analyze sandwiches", ncols=100):
        batch_ids = all_ids[i : i + chunk_size]
        batch_df = df[df["sandwichId"].isin(batch_ids)].copy()

        dist_df = _analyze_one_chunk(batch_df, slot_txcount_map)

        if out_path is not None:
            if first_write:
                dist_df.to_csv(out_path, index=False, mode="w")
                first_write = False
            else:
                dist_df.to_csv(out_path, index=False, mode="a", header=False)
        else:
            pieces.append(dist_df)

        del batch_df, dist_df
        gc.collect()
        time.sleep(0)

    if out_path is None:
        dist_all = pd.concat(pieces, ignore_index=True)
    else:
        dist_all = pd.read_csv(out_path)

    merged = dist_all.merge(profit_view, on="sandwichId", how="left")

    def classify_type(row):
        if row["cross_block"]:
            return "cross_block"
        elif row["consecutive"]:
            return "inblock_consec"
        else:
            return "inblock_non_consec"

    merged["distance_type"] = merged.apply(classify_type, axis=1)
    merged.drop(["cross_block", "consecutive"], axis=1, inplace=True)
    return merged


os.makedirs("data", exist_ok=True)

# Query Sandwich Txs Data
# Load credentials from .env
config = load_env()
# Initialize ClickHouse client
client = get_client(
    host=config["host"],
    port=config["port"],
    username=config["username"],
    password=config["password"],
)

print("Querying data from ClickHouse DB...")

# query sandwiches
start = START_SLOT
end = END_SLOT

start_time = time.time()
files = query_sandwiches_with_txs_and_leader_to_parquet(
    start, end, out_dir="data/parquet_out", num_parts=20, client=client
)
end_time = time.time()
elapsed = end_time - start_time
print(f"Query took {elapsed:.2f} seconds")

start_time = time.time()
sandwiches_txs = ds.dataset("data/parquet_out", format="parquet")
sandwiches_txs = sandwiches_txs.to_table().to_pandas()
end_time = time.time()
elapsed = end_time - start_time
print(f"Query took {elapsed:.2f} seconds")

start = sandwiches_txs["sandwich_slot"].min()
end = sandwiches_txs["sandwich_slot"].max()
print(
    f"Total rows of sandwiches with txs and leader in slots from {sandwiches_txs['sandwich_slot'].min()} to {sandwiches_txs['sandwich_slot'].max()}: sandwiches - {sandwiches_txs['sandwichId'].nunique()} sandwich_txs - {len(sandwiches_txs)}"
)

# Analysis sandwich data

print("Analyzing sandwiches...")
# Get info of each sandwich
start_time = time.time()
sandwich_stat = analyze_sandwiches(
    sandwiches_txs,
    slot_info=quert_slot_info_in_DB(start, end),
    out_path="data/sandwich_info.csv",
)
end_time = time.time()
elapsed = end_time - start_time
print(f"Analysis took {elapsed:.2f} seconds")

sandwich_stat.to_csv("data/sandwich_stat.csv", index=False)
