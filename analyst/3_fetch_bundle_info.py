import gc
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
from clickhouse_connect import get_client
from dotenv import load_dotenv

START_SLOT = 370656000  # Start of epoch 858
END_SLOT = 377135999  # End of epoch 872
TX_TYPES = ["frontRun", "backRun", "victim", "transfer", "adverse"]
ATTACKER_TX_TYPES = ["frontRun", "backRun", "transfer"]
EPS_WIN = 1e-5

start = START_SLOT
end = END_SLOT


def load_env():
    load_dotenv(dotenv_path=".env")
    return {
        "host": os.getenv("NEW_CLICKHOUSE_HOST"),
        "port": int(os.getenv("NEW_CLICKHOUSE_PORT")),
        "username": os.getenv("NEW_CLICKHOUSE_USERNAME"),
        "password": os.getenv("NEW_CLICKHOUSE_PASSWORD"),
    }


# Load credentials from .env
config = load_env()
# Initialize ClickHouse client
client = get_client(
    host=config["host"],
    port=config["port"],
    username=config["username"],
    password=config["password"],
)

# 1. Get data
TX_FOLDER = "data/parquet_out"
SANDWICH_STAT_PATH = "data/sandwich_stat.csv"

# load sandwich transaction data
start_time = time.time()

sandwiches_txs = ds.dataset(TX_FOLDER, format="parquet")
sandwiches_txs = sandwiches_txs.to_table().to_pandas()

end_time = time.time()
elapsed = end_time - start_time
print(f"Query took {elapsed:.2f} seconds")

# load sandwich statistics
sandwich_stat = pd.read_csv(SANDWICH_STAT_PATH)
sandwich_stat = sandwich_stat[
    (sandwich_stat["slot"] <= end) & (sandwich_stat["slot"] >= start)
]

# print basic information
print(
    f"Block {sandwiches_txs['tx_slot'].min()} - Block {sandwiches_txs['tx_slot'].max()}"
)
print(f" - Number of transactions: {len(sandwiches_txs)}")
print(f" - Number of sandwichesL: {len(sandwich_stat)}")

# Jito-sandwiches
jito_sandwiches = sandwich_stat[(sandwich_stat["bundle_status"] == "all")]

jito_sandwiches_txs = sandwiches_txs[
    sandwiches_txs["sandwichId"].isin(jito_sandwiches["sandwichId"].unique().tolist())
]

SAVE_INTERVAL = 10_000
BATCH_TIMESTAMP_RANGE_SECONDS = 20
EXTRA_TIME = 10
PARQUET_OUTPUT_DIR = Path("data/sandwich_bundle_parquet")
PARQUET_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# slot -> tx
txs_by_sandwich_slot = jito_sandwiches_txs.groupby("sandwich_slot")

# slot -> timestamp
slot_ts_df = (
    jito_sandwiches_txs[["tx_slot", "timestamp"]]
    .drop_duplicates()
    .rename(columns={"tx_slot": "slot"})
)
slot_ts_df["timestamp"] = pd.to_datetime(slot_ts_df["timestamp"])
slot_ts_df = slot_ts_df.sort_values("timestamp")
min_ts, max_ts = slot_ts_df["timestamp"].min(), slot_ts_df["timestamp"].max()
print(min_ts)
print(max_ts)

sandwich_results = []
slot_processed_count = 0
parquet_file_index = 0

BATCH_SECONDS = 200
time_range = pd.date_range(start=min_ts, end=max_ts, freq=f"{BATCH_SECONDS}s")
total_batches = len(time_range) - 1


# query bundle data from database
def query_bundles_by_timestamp_range(start_ts: datetime, end_ts: datetime):
    start_str = start_ts.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_ts.strftime("%Y-%m-%d %H:%M:%S")
    query = f"""
    SELECT 
        slot,
        bundleId,
        tippers,
        transactions,
        landedTipLamports,
        timestamp
    FROM solwich.jito_bundles
    WHERE timestamp >= toDateTime('{start_str}')
      AND timestamp < toDateTime('{end_str}')
    """
    result = client.query(query)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    return df


sandwich_processed_count = 0
for i in range(total_batches):
    current_ts = time_range[i]
    next_ts = time_range[i + 1]

    bundle_df = query_bundles_by_timestamp_range(
        current_ts, next_ts + pd.Timedelta(seconds=EXTRA_TIME)
    )

    # deduplicate
    bundle_df = bundle_df.drop_duplicates(subset=["slot", "bundleId"])
    if bundle_df.empty:
        current_ts = next_ts
        continue

    bundle_by_slot = {slot: df for slot, df in bundle_df.groupby("slot")}

    # get sandwiches to process
    sandwich_slots_in_batch = (
        slot_ts_df[
            (slot_ts_df["timestamp"] >= current_ts)
            & (slot_ts_df["timestamp"] < next_ts)
        ]["slot"]
        .unique()
        .tolist()
    )

    for sandwich_slot in sandwich_slots_in_batch:
        if sandwich_slot not in txs_by_sandwich_slot.groups:
            continue

        sandwich_txs = txs_by_sandwich_slot.get_group(sandwich_slot)
        sandwiches = sandwich_txs.groupby("sandwichId")

        for sandwich_id, group in sandwiches:
            tx_sigs = set(group["signature"])
            slot_range = group["tx_slot"].unique().tolist()
            matched_bundles = []

            for slot in slot_range:
                if slot not in bundle_by_slot:
                    continue
                for _, row in bundle_by_slot[slot].iterrows():
                    bundle_tx_sigs = set(row["transactions"])
                    if tx_sigs & bundle_tx_sigs:
                        matched_bundles.append(row)
            if not matched_bundles:
                print(
                    f"No bundle matched for sandwich {sandwich_id} (slot {sandwich_slot})"
                )
                continue

            bundle_df_matched = pd.DataFrame(matched_bundles).drop_duplicates(
                subset=["bundleId"]
            )
            tipper_list = bundle_df_matched["tippers"].tolist()
            total_lamports = bundle_df_matched["landedTipLamports"].sum()

            sandwich_results.append(
                {
                    "sandwichId": sandwich_id,
                    "sandwich_slot": sandwich_slot,
                    "bundles": bundle_df_matched["bundleId"].tolist(),
                    "tippers": tipper_list,
                    "totalLandedTipLamports": total_lamports,
                }
            )

        sandwich_processed_count += sandwich_txs["sandwichId"].nunique()
    slot_processed_count += len(sandwich_slots_in_batch)

    # Write to Parquet once SAVE_INTERVAL is reached
    if slot_processed_count >= SAVE_INTERVAL:
        out_df = pd.DataFrame(sandwich_results)
        out_path = (
            PARQUET_OUTPUT_DIR
            / f"new_sandwich_bundles_{parquet_file_index:03d}.parquet"
        )
        out_df.to_parquet(out_path, index=False)
        print(
            f"Saved {out_path} with {len(out_df)} rows (sandwich: {sandwich_processed_count}/{len(jito_sandwiches)} - {sandwich_processed_count / len(jito_sandwiches) * 100:.3f})"
        )

        sandwich_results.clear()
        gc.collect()

        # reset counter
        slot_processed_count = 0
        parquet_file_index += 1

    current_ts = next_ts


# process the remaining data
if sandwich_results:
    out_df = pd.DataFrame(sandwich_results)
    out_path = PARQUET_OUTPUT_DIR / f"sandwich_bundles_{parquet_file_index:03d}.parquet"
    out_df.to_parquet(out_path, index=False)
    print(f"Saved final {out_path} with {len(out_df)} rows")
