import os
import time
from typing import Dict, List, Tuple

import clickhouse_connect
import numpy as np
import pandas as pd
from dotenv import load_dotenv

SANDWICHED_ME_URL = "ison"


def load_env():
    load_dotenv()
    return {
        "host": os.getenv("NEW_CLICKHOUSE_HOST"),
        "port": int(os.getenv("NEW_CLICKHOUSE_PORT")),
        "username": os.getenv("NEW_CLICKHOUSE_USERNAME"),
        "password": os.getenv("NEW_CLICKHOUSE_PASSWORD"),
    }


config = load_env()
client = clickhouse_connect.get_client(
    host=config["host"],
    port=config["port"],
    username=config["username"],
    password=config["password"],
)

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


def fetch_site_latest() -> List[dict]:
    import cloudscraper

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    r = scraper.get(SANDWICHED_ME_URL, headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_sandwiches_from_site_once(
    site_data: List[dict],
) -> List[Tuple[int, str, str, dict]]:
    rows = []
    for block in site_data or []:
        slot = int(block.get("slot"))
        for s in block.get("sandwiches") or []:
            fr = s.get("frontrun") or {}
            br = s.get("backrun") or {}
            fr_sig = fr.get("sig")
            br_sig = br.get("sig")
            if fr_sig and br_sig:
                rows.append((slot, fr_sig, br_sig, s))
    return rows


def collect_site_sandwiches(
    target_count: int = 1000,
    sleep_sec: int = 60,
) -> pd.DataFrame:
    dedup: Dict[Tuple[int, str, str], dict] = {}

    while True:
        try:
            site_data = fetch_site_latest()
        except Exception as e:
            print(f"[WARN] fetch_site_latest failed: {e} ; retrying in {sleep_sec}s")
            time.sleep(sleep_sec)
            continue

        rows = extract_sandwiches_from_site_once(site_data)
        new_cnt = 0
        for slot, fr_sig, br_sig, s in rows:
            key = (slot, fr_sig, br_sig)
            if key in dedup:
                continue
            fr = s.get("frontrun") or {}
            br = s.get("backrun") or {}
            row = {
                "slot": slot,
                "front_sig": fr_sig,
                "front_signer": (fr.get("signer") or ""),
                "front_sell_amount": (
                    fr.get("sellAmount").replace(",", "").strip() or ""
                ),
                "front_buy_amount": (
                    fr.get("buyAmount").replace(",", "").strip() or ""
                ),
                "back_sig": br_sig,
                "back_signer": (br.get("signer") or ""),
                "back_sell_amount": (
                    br.get("sellAmount").replace(",", "").strip() or ""
                ),
                "back_buy_amount": (br.get("buyAmount").replace(",", "").strip() or ""),
            }
            dedup[key] = row
            new_cnt += 1

        total = len(dedup)
        print(
            f"[SITE] fetched {len(rows)} in this round | new={new_cnt} | total={total}"
        )

        if total >= target_count:
            break
        time.sleep(sleep_sec)

    site_df = pd.DataFrame(list(dedup.values()))
    if not site_df.empty:
        site_df.sort_values(["slot", "front_sig", "back_sig"], inplace=True)
    site_df.to_csv("sandwiches_site.csv", index=False)
    print("[Saved] sandwiches_site.csv")
    return site_df


def db_has_slot_gt(slot: int) -> bool:
    q = f"SELECT 1 FROM solwich.sandwich_txs WHERE slot > {slot} LIMIT 1"
    res = client.query(q)
    return len(res.result_rows) > 0


def wait_until_db_has_slot_gt(
    slot: int, timeout_sec: int = 3000, poll_sec: int = 10
) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if db_has_slot_gt(slot):
            print(f"[OK] DB has slot > {slot}")
            return True
        print(f"[WAIT] DB not beyond slot {slot}; rechecking in {poll_sec}s ...")
        time.sleep(poll_sec)
    print(f"[TIMEOUT] DB still not beyond slot {slot} after {timeout_sec}s.")
    return False


def fetch_db_sandwiches_in_range(min_slot: int, max_slot: int) -> pd.DataFrame:
    q = f"""
        SELECT
            slot,
            sandwichId,
            anyIf(signature, type='frontRun') AS front_sig,
            anyIf(signer,    type='frontRun') AS front_signer,
            anyIf(slot, type='frontRun') AS front_slot,
            toString(anyIf(fromAmount, type='frontRun')) AS front_sell_amount,
            toString(anyIf(toAmount,   type='frontRun')) AS front_buy_amount,
            anyIf(signature, type='backRun')  AS back_sig,
            anyIf(signer,    type='backRun')  AS back_signer,
            anyIf(slot, type='backRun')  AS back_slot,
            toString(anyIf(fromAmount, type='backRun'))  AS back_sell_amount,
            toString(anyIf(toAmount,   type='backRun'))  AS back_buy_amount
        FROM solwich.sandwich_txs
        WHERE slot >= {min_slot} AND slot <= {max_slot}
        GROUP BY slot, sandwichId
        HAVING front_sig != '' AND back_sig != ''
    """
    res = client.query(q)
    df = pd.DataFrame(res.result_rows, columns=res.column_names)

    if df.empty:
        return pd.DataFrame(
            columns=[
                "slot",
                "front_sig",
                "front_sell_amount",
                "front_signer",
                "front_buy_amount",
                "back_sig",
                "back_signer",
                "back_sell_amount",
                "back_buy_amount",
            ]
        )

    df = df[df["front_slot"] == df["back_slot"]].copy()
    df = df[
        [
            "slot",
            "front_sig",
            "front_sell_amount",
            "front_signer",
            "front_buy_amount",
            "back_sig",
            "back_signer",
            "back_sell_amount",
            "back_buy_amount",
        ]
    ].copy()
    df["slot"] = df["slot"].astype(int)
    df.sort_values(["slot", "front_sig", "back_sig"], inplace=True)
    df.to_csv(f"sandwiches_db_{min_slot}_{max_slot}.csv", index=False)
    print(f"[Saved] sandwiches_db_{min_slot}_{max_slot}.csv")
    return df


def merge_and_report(site_df: pd.DataFrame, db_df: pd.DataFrame) -> pd.DataFrame:
    base_cols = [
        "slot",
        "front_sig",
        "front_signer",
        "front_sell_amount",
        "front_buy_amount",
        "back_sig",
        "back_signer",
        "back_sell_amount",
        "back_buy_amount",
    ]
    site_df2 = (
        site_df[base_cols].copy()
        if not site_df.empty
        else pd.DataFrame(columns=base_cols)
    )
    db_df2 = (
        db_df[base_cols].copy() if not db_df.empty else pd.DataFrame(columns=base_cols)
    )

    merged = site_df2.merge(
        db_df2,
        on=["slot", "front_sig", "back_sig"],
        how="outer",
        suffixes=("_site", "_db"),
        indicator=True,
    )

    merged["status"] = merged["_merge"].map(
        {
            "both": "both",
            "left_only": "site_only",
            "right_only": "db_only",
        }
    )

    merged.drop(columns=["_merge"], inplace=True)

    site = merged["status"].isin(["both", "site_only"])
    db = merged["status"].eq("db_only")

    for col in [
        "front_sell_amount_site",
        "front_buy_amount_site",
        "back_sell_amount_site",
        "back_buy_amount_site",
        "front_sell_amount_db",
        "front_buy_amount_db",
        "back_sell_amount_db",
        "back_buy_amount_db",
    ]:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    merged.loc[site, "same_signer"] = (
        merged["front_signer_site"] == merged["back_signer_site"]
    )
    merged.loc[db, "same_signer"] = (
        merged["front_signer_db"] == merged["back_signer_db"]
    )

    merged.loc[site, "profit"] = (
        merged["back_buy_amount_site"] - merged["front_sell_amount_site"]
    )
    merged.loc[db, "profit"] = (
        merged["back_buy_amount_db"] - merged["front_sell_amount_db"]
    )

    merged.loc[site, "relative_amount_diff"] = (
        merged["front_buy_amount_site"] - merged["back_sell_amount_site"]
    ).abs() / np.maximum(
        merged["front_buy_amount_site"], merged["back_sell_amount_site"]
    )
    merged.loc[db, "relative_amount_diff"] = (
        merged["front_buy_amount_db"] - merged["back_sell_amount_db"]
    ).abs() / np.maximum(merged["front_buy_amount_db"], merged["back_sell_amount_db"])

    for col in [
        "front_signer_site",
        "back_signer_site",
        "front_signer_db",
        "back_signer_db",
    ]:
        if col in merged.columns:
            merged[col] = merged[col].astype(str).str.slice(0, 6)

    merged.sort_values(["status", "slot"], inplace=True)
    merged.to_csv(f"sandwiches_site_db_merged_{time.time()}.csv", index=False)
    print(f"[Saved] sandwiches_site_db_merged_{time.time()}.csv")

    total = len(merged)
    both_cnt = (merged["status"] == "both").sum()
    site_only_cnt = (merged["status"] == "site_only").sum()
    db_only_cnt = (merged["status"] == "db_only").sum()

    print("\n===== Stats =====")
    print(f"Total unique sandwiches (by slot+front_sig+back_sig): {total}")
    print(f"Both (site & db): {both_cnt}")
    print(f"Site only:        {site_only_cnt}")
    print(f"DB only:          {db_only_cnt}")

    def _print_examples(tag: str, n: int = 10):
        sub = merged[merged["status"] == tag].head(n)
        if sub.empty:
            print(f"\n-- {tag}: none --")
            return
        print(f"\n-- {tag} (showing up to {n}) --")
        for _, r in sub.iterrows():
            slot = int(r["slot"]) if pd.notna(r["slot"]) else None
            fr = r["front_sig"]
            br = r["back_sig"]
            print(f"[{tag}] slot={slot} | FR={fr} | BR={br}")

    _print_examples("both")
    _print_examples("site_only")
    _print_examples("db_only")

    return merged


def main(
    site_target_count: int = 1000,
    site_sleep_sec: int = 60,
    db_wait_timeout_sec: int = 3000,
    db_wait_poll_sec: int = 10,
):
    # site_df = collect_site_sandwiches(
    #     target_count=site_target_count,
    #     sleep_sec=site_sleep_sec,
    # )

    site_df = pd.read_csv("sandwiches_site.csv")

    if site_df.empty:
        print("[STOP] No site sandwiches collected.")
        return

    max_site_slot = int(site_df["slot"].max())
    min_site_slot = int(site_df["slot"].min())
    print(
        f"[SITE] min_site_slot={min_site_slot}, max_site_slot={max_site_slot}, total_sandwiches={len(site_df)}"
    )

    ok = wait_until_db_has_slot_gt(
        max_site_slot, timeout_sec=db_wait_timeout_sec, poll_sec=db_wait_poll_sec
    )
    if not ok:
        print("[STOP] Timeout waiting DB to surpass max_site_slot. Exiting.")
        return

    db_df = fetch_db_sandwiches_in_range(min_site_slot, max_site_slot)

    merge_and_report(site_df, db_df)


if __name__ == "__main__":
    main()
