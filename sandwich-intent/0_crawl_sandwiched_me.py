"""Crawl sandwiched.me for external sandwich detection data within an epoch.

Input: epoch number → auto-computes slot range.
Saves chunks of 1000 sandwiches each. Stops when API returns data beyond the epoch.

Usage:
  # Crawl epoch 946 (runs until epoch ends or data goes beyond)
  python 0_crawl_sandwiched_me.py --epoch 946

  # Faster polling
  python 0_crawl_sandwiched_me.py --epoch 946 --interval 30

  # Merge all chunks into one CSV (after crawl completes)
  python 0_crawl_sandwiched_me.py --merge --epoch 946
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"

SANDWICHED_ME_URL = "https://nextgen.mev-hub.snowgenesis.com/api/sandwiches/latest"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": "https://nextgen.mev-hub.snowgenesis.com/",
    "Origin": "https://nextgen.mev-hub.snowgenesis.com",
}

SLOTS_PER_EPOCH = 432_000
CHUNK_SIZE = 1000


def epoch_to_slots(epoch: int) -> tuple:
    """Convert epoch number to (start_slot, end_slot)."""
    start = epoch * SLOTS_PER_EPOCH
    end = (epoch + 1) * SLOTS_PER_EPOCH - 1
    return start, end


def chunk_dir(epoch: int) -> Path:
    d = DATA_DIR / f"site_epoch_{epoch}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def merged_file(epoch: int) -> Path:
    return DATA_DIR / f"sandwiches_site_epoch_{epoch}.csv"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_latest() -> list:
    """Fetch latest sandwich data from sandwiched.me API."""
    import cloudscraper

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    r = scraper.get(SANDWICHED_ME_URL, headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_response(data: list, min_slot: int, max_slot: int) -> tuple:
    """Parse API response. Returns (in_range_rows, saw_beyond_epoch).

    saw_beyond_epoch is True if any sandwich has slot > max_slot,
    meaning the epoch is over and we should stop.
    """
    rows = []
    saw_beyond = False

    for block in data or []:
        slot = int(block.get("slot", 0))

        if slot > max_slot:
            saw_beyond = True
            continue
        if slot < min_slot:
            continue

        for s in block.get("sandwiches") or []:
            fr = s.get("frontrun") or {}
            br = s.get("backrun") or {}
            fr_sig = fr.get("sig", "")
            br_sig = br.get("sig", "")
            if not fr_sig or not br_sig:
                continue

            victims = s.get("victims") or []
            rows.append({
                "slot": slot,
                "front_sig": fr_sig,
                "back_sig": br_sig,
                "front_signer": fr.get("signer", ""),
                "back_signer": br.get("signer", ""),
                "front_program": fr.get("outerProgram", ""),
                "back_program": br.get("outerProgram", ""),
                "front_sell_amount": fr.get("sellAmount", ""),
                "front_buy_amount": fr.get("buyAmount", ""),
                "back_sell_amount": br.get("sellAmount", ""),
                "back_buy_amount": br.get("buyAmount", ""),
                "victim_count": len(victims),
            })

    return rows, saw_beyond


# ---------------------------------------------------------------------------
# Chunk I/O
# ---------------------------------------------------------------------------

def load_existing_chunks(epoch: int) -> dict:
    """Load all existing chunk CSVs into a dedup dict."""
    dedup = {}
    cdir = chunk_dir(epoch)
    for f in sorted(cdir.glob("chunk_*.csv")):
        try:
            df = pd.read_csv(f)
            for _, row in df.iterrows():
                key = (int(row["slot"]), row["front_sig"], row["back_sig"])
                dedup[key] = row.to_dict()
        except Exception:
            continue
    return dedup


def save_chunk(records: list, epoch: int, chunk_idx: int):
    """Save a chunk of records to CSV."""
    cdir = chunk_dir(epoch)
    df = pd.DataFrame(records)
    df.sort_values(["slot", "front_sig", "back_sig"], inplace=True)
    path = cdir / f"chunk_{chunk_idx:04d}.csv"
    df.to_csv(path, index=False)
    print(f"[CHUNK] Saved {path.name} ({len(df):,} rows)")


# ---------------------------------------------------------------------------
# Crawl loop
# ---------------------------------------------------------------------------

def crawl(epoch: int, interval: int = 60, max_rounds: int = 3000):
    """Continuously fetch sandwiched.me data for the given epoch.

    Stops when:
      - API returns sandwiches beyond the epoch's slot range, OR
      - max_rounds reached
    """
    min_slot, max_slot = epoch_to_slots(epoch)
    print(f"Epoch {epoch}: slot {min_slot} - {max_slot}")
    print(f"Duration: {SLOTS_PER_EPOCH * 0.4 / 3600:.0f} hours")
    print(f"Poll interval: {interval}s, chunk size: {CHUNK_SIZE}")
    print()

    # Resume from existing chunks
    dedup = load_existing_chunks(epoch)
    if dedup:
        print(f"[INIT] Resumed {len(dedup):,} existing records")

    cdir = chunk_dir(epoch)
    next_chunk_idx = len(list(cdir.glob("chunk_*.csv")))
    buffer = []

    for rnd in range(max_rounds):
        try:
            data = fetch_latest()
        except Exception as e:
            print(f"[WARN] round {rnd}: {e}; retrying in {interval}s",
                  flush=True)
            time.sleep(interval)
            continue

        rows, saw_beyond = parse_response(data, min_slot, max_slot)

        new = 0
        for row in rows:
            key = (row["slot"], row["front_sig"], row["back_sig"])
            if key not in dedup:
                dedup[key] = row
                buffer.append(row)
                new += 1

        total = len(dedup)
        if dedup:
            slots = [r["slot"] for r in dedup.values()]
            slot_info = f"slots={min(slots)}-{max(slots)}"
        else:
            slot_info = "no data yet"

        print(f"[round {rnd:4d}] new={new:3d} | total={total:,} | "
              f"buffer={len(buffer)} | {slot_info}",
              flush=True)

        # Save chunks
        while len(buffer) >= CHUNK_SIZE:
            chunk_data = buffer[:CHUNK_SIZE]
            buffer = buffer[CHUNK_SIZE:]
            save_chunk(chunk_data, epoch, next_chunk_idx)
            next_chunk_idx += 1

        # Stop if epoch is over
        if saw_beyond:
            print(f"\n[DONE] API returned data beyond epoch {epoch} "
                  f"(slot > {max_slot}). Epoch complete.")
            break

        time.sleep(interval)

    # Save remaining buffer
    if buffer:
        save_chunk(buffer, epoch, next_chunk_idx)

    print(f"\n=== Crawl Summary ===")
    print(f"  Epoch: {epoch}")
    print(f"  Total unique sandwiches: {len(dedup):,}")
    if dedup:
        slots = [r["slot"] for r in dedup.values()]
        print(f"  Slot range: {min(slots)} - {max(slots)}")

    return len(dedup)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_chunks(epoch: int) -> pd.DataFrame:
    """Merge all chunk CSVs for an epoch into one file."""
    cdir = chunk_dir(epoch)
    chunks = sorted(cdir.glob("chunk_*.csv"))
    if not chunks:
        print(f"[MERGE] No chunk files found in {cdir}")
        return pd.DataFrame()

    dfs = []
    for f in chunks:
        try:
            dfs.append(pd.read_csv(f))
        except Exception:
            continue

    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=["slot", "front_sig", "back_sig"])
    df.sort_values(["slot", "front_sig", "back_sig"], inplace=True)

    out = merged_file(epoch)
    df.to_csv(out, index=False)
    print(f"[MERGED] {out} ({len(df):,} rows from {len(chunks)} chunks)")
    if not df.empty:
        print(f"  Slot range: {df['slot'].min()} - {df['slot'].max()}")
        print(f"  Unique front signers: {df['front_signer'].nunique()}")
        print(f"  Same signer (front==back): "
              f"{(df['front_signer'] == df['back_signer']).sum():,}")
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crawl sandwiched.me by epoch")
    parser.add_argument("--epoch", type=int, required=True,
                        help="Epoch number to crawl")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between API polls (default: 60)")
    parser.add_argument("--max-rounds", type=int, default=3000,
                        help="Max polling rounds (default: 3000)")
    parser.add_argument("--merge", action="store_true",
                        help="Merge chunks and exit (no crawling)")
    args = parser.parse_args()

    if args.merge:
        merge_chunks(args.epoch)
        return

    crawl(
        epoch=args.epoch,
        interval=args.interval,
        max_rounds=args.max_rounds,
    )

    # Auto-merge at the end
    merge_chunks(args.epoch)


if __name__ == "__main__":
    main()
