"""Data overview report over an epoch range.

  1. Slot coverage -- theoretical vs fetched vs sandwich-checked, plus Jito bundle collection
  2. Leader coverage and leader-level analysis
  3. Sandwiches overall, split by slot distance
  4. Sandwiches by structural pattern
  5. Pattern x distance
  6. Jito bundle sandwiches, per epoch and per attacker

`--sections` selects which to print.

DISTANCE CLASSES (from `crossBlock` + `crossLeader`, mutually exclusive and exhaustive)
    single-slot            all legs in one block
    same-leader multi-slot spans blocks inside one leader's 4-slot window
    cross-leader multi-slot spans a leader rotation

PATTERN CLASSES (mutually exclusive and exhaustive; the residual is 0)
    standard, multi-front/back, diff-signer (owner same), diff-signer (transfer-linked),
    and diff-signer x multi-split, which matches none of the four phase-1 categories.

PROFIT IS NET OF THE ATTACKER'S OWN FEES. The detector's `profitA` is
`backTx.toTotal - frontTx.fromTotal`, with no cost side. Every USD and win-rate figure here
subtracts the SOL the attacker spent on its own front-run and back-run fees, using the same
definition as phase 1's `fetch_leg_fees` (`sum(fee)/1e9` over `type IN ('frontRun','backRun')`).
A victim's fee is never charged.

TWO THINGS THIS REPORT DOES NOT CLAIM
    - "Attackers". At this stage an entity is the front-run's fee payer; diff-signer
      attackers only become entities after phase 1's union-find merge. Columns are named
      `front_signers` for that reason.
    - That the USD columns cover every sandwich. A net figure needs both sides priced, so a
      sandwich with an unpriced tokenA has an undefined net profit and is excluded from the
      USD sums and the win-rate denominator. The `priced` column is that subset's share.

Usage:
    python3 0_report_data_overview.py --database solwich --start-epoch 946 --end-epoch 990
"""

import argparse
import glob
import os

import pandas as pd

from utils.db import get_client
# The USD floor lives in utils.intent, shared with phases 1/2/3.
from utils.intent import (BUNDLE_USD_MIN, load_token_prices,
                          verified_bundle_sandwiches)

SLOTS_PER_EPOCH = 432_000
BUNDLE_DIR = "data/jito_bundle_ids"

# `tokenA` denominates the attacker's profit. Prices are staged into a ClickHouse table
# rather than inlined, which would blow past max_query_size. A LEFT JOIN leaves `usd_price`
# at its Float64 default of 0 for a token that is not in the table, so every aggregate below
# guards on `PRICED` rather than letting a 0 price read as a $0 profit.
PRICE_TABLE = "default._tokprice"
USD_GROSS = "s.profitA * px.usd_price"
PRICED = f"s.tokenA IN (SELECT token FROM {PRICE_TABLE})"
PRICE_JOIN = f"LEFT JOIN {PRICE_TABLE} px ON s.tokenA = px.token"

# The attacker's own transaction fee, in SOL, over its front-run and back-run legs only.
# Mirrors `fetch_leg_fees` in 1_signer_data_preparation_and_summary.py: same leg types, same
# slot bound, same `sum(fee)/1e9`. A sandwich with no matching leg row gets 0, matching
# phase 1's `.fillna(0.0)`.
FEE_SOL = "fe.fee_sol"
FEE_JOIN = "LEFT JOIN fe ON s.sandwichId = fe.sid"


def fee_cte(lo, hi):
    """The `fe` CTE body, to be spliced into a WITH list."""
    return f"""
        fe AS (
            SELECT sandwichId AS sid, sum(fee) / 1e9 AS fee_sol
            FROM sandwich_txs
            WHERE type IN ('frontRun', 'backRun') AND slot >= {lo} AND slot <= {hi}
            GROUP BY sandwichId
        )
    """


# Profit is in tokenA and the fee is in lamports, so both sides must be priced before they can be
# subtracted. SOL's price is inlined as a literal rather than read from the staged table so that a
# missing SOL row aborts the run instead of silently charging a fee of $0.
SOL_USD = None
FEE_USD = None   # the priced fee alone, so a report can show what the net cost
USD_NET = None   # gross minus the priced fee; DEFINED ONLY where `PRICED` holds


def bind_prices(sol_usd):
    """Fill in the SQL fragments that need SOL's own price. Called once, from main()."""
    global SOL_USD, FEE_USD, USD_NET
    if sol_usd is None:
        raise SystemExit(
            "SOL is missing from the price table, so the attacker's fee cannot be priced and no "
            "net figure in this report would be net. Re-run 0_crawl_token_price.py.")
    SOL_USD = float(sol_usd)
    FEE_USD = f"({FEE_SOL} * {SOL_USD!r})"
    USD_NET = f"({USD_GROSS} - {FEE_USD})"


# Net win: profitable AFTER its own fees, over the subset where that question has an answer. An
# unpriced tokenA is not a loss — it is unmeasurable — so it leaves the numerator and the
# denominator together, the same way phase 1's NaN drops out of `groupby.mean()`.
def win_rate_sql():
    return (f"round(countIf({PRICED} AND {USD_NET} > 0) / "
            f"nullIf(countIf({PRICED}), 0), 4)")


DISTANCE_SQL = """
    multiIf(crossLeader,               'cross-leader multi-slot',
            crossBlock,                'same-leader multi-slot',
                                       'single-slot')
"""

PATTERN_SQL = """
    multiIf(signerSame AND NOT multiFrontRun AND NOT multiBackRun,        '1 standard',
            signerSame,                                                   '2 multi-front/back',
            ownerSame AND NOT multiFrontRun AND NOT multiBackRun,         '3 diff-signer (owner)',
            hasTransfer AND NOT multiFrontRun AND NOT multiBackRun,       '4 diff-signer (transfer)',
            multiFrontRun OR multiBackRun,                                '5 diff-signer x multi',
                                                                          '6 other')
"""


def parse_args():
    p = argparse.ArgumentParser(description="Data overview report")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990, help="inclusive")
    p.add_argument("--database", type=str, default=None)
    p.add_argument("--sections", type=str, default="123456",
                   help="Which sections to print, e.g. '36'")
    return p.parse_args()


def _hdr(n, title):
    print("\n" + "=" * 88)
    print(f"{n}. {title}")
    print("=" * 88)


def load_prices():
    """token -> USD price, from `utils.intent.load_token_prices`.

    A row with an empty price is dropped, so its token never enters `PRICE_TABLE` and every
    sandwich denominated in it fails `PRICED`: excluded from the USD sums and the win-rate
    denominator rather than scored at $0.
    """
    prices = load_token_prices()
    return {t: v for t, v in prices.items() if pd.notna(v)}

def stage_prices(client, prices):
    client.command(f"DROP TABLE IF EXISTS {PRICE_TABLE}")
    client.command(f"CREATE TABLE {PRICE_TABLE} (token String, usd_price Float64) "
                   f"ENGINE = MergeTree ORDER BY token")
    rows = [[t, float(v)] for t, v in prices.items()]
    for i in range(0, len(rows), 50_000):
        client.insert(PRICE_TABLE, rows[i:i + 50_000], column_names=["token", "usd_price"])


# ── 1. Slot coverage & Jito bundle collection ───────────────────────────────

def report_slot_coverage(client, lo, hi, e0, e1):
    _hdr(1, f"SLOT COVERAGE & JITO BUNDLE COLLECTION  (epochs {e0}-{e1}, slots {lo:,}-{hi:,})")
    theoretical = (e1 - e0 + 1) * SLOTS_PER_EPOCH

    r = client.query_df(f"""
        SELECT countIf(txFetched) AS fetched, countIf(sandwichFetched) AS sw,
               countIf(sandwichInBundleChecked) AS bchecked, count() AS rows
        FROM slot_txs WHERE slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    print(f"\n  theoretical slots      {theoretical:>14,}")
    for label, key in [("fetched", "fetched"), ("sandwich-checked", "sw"),
                       ("bundle-mark-checked", "bchecked")]:
        v = int(r[key])
        print(f"  {label:<22} {v:>14,}   {v / theoretical * 100:6.2f}%")

    b = client.query_df(f"""
        SELECT count() AS bundles, uniqExact(slot) AS slots
        FROM jito_bundles WHERE slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    print(f"\n  Jito bundles retained  {int(b['bundles']):>14,}")
    print(f"  slots with bundles     {int(b['slots']):>14,}   "
          f"{int(b['slots']) / theoretical * 100:6.2f}%")
    print("  NOTE: bundle *content* is dropped per epoch after inBundle marking, so a low number "
          "here\n        means the content was reclaimed, NOT that no bundles existed. The "
          "`inBundle` flag survives;\n        section 6 uses it, plus resolved bundle IDs.")

    print(f"\n  {'epoch':>6} {'fetched':>12} {'%':>7} {'sw-checked':>12} {'%':>7} "
          f"{'bundles':>14} {'inBundle legs':>14} {'%legs':>7}")
    print("  " + "-" * 84)
    per = client.query_df(f"""
        SELECT intDiv(slot,{SLOTS_PER_EPOCH}) AS epoch,
               countIf(txFetched) AS fetched, countIf(sandwichFetched) AS sw
        FROM slot_txs WHERE slot >= {lo} AND slot <= {hi} GROUP BY epoch ORDER BY epoch
    """)
    bun = client.query_df(f"""
        SELECT intDiv(slot,{SLOTS_PER_EPOCH}) AS epoch, count() AS bundles
        FROM jito_bundles WHERE slot >= {lo} AND slot <= {hi} GROUP BY epoch
    """)
    legs = client.query_df(f"""
        SELECT intDiv(slot,{SLOTS_PER_EPOCH}) AS epoch, count() AS legs,
               countIf(inBundle) AS in_bundle
        FROM sandwich_txs WHERE slot >= {lo} AND slot <= {hi} GROUP BY epoch
    """)
    bmap = dict(zip(bun["epoch"], bun["bundles"])) if not bun.empty else {}
    lmap = {int(r.epoch): (int(r.legs), int(r.in_bundle)) for r in legs.itertuples()}
    for r in per.itertuples():
        ep = int(r.epoch)
        nlegs, nin = lmap.get(ep, (0, 0))
        print(f"  {ep:>6} {int(r.fetched):>12,} {int(r.fetched)/SLOTS_PER_EPOCH*100:>6.2f}% "
              f"{int(r.sw):>12,} {int(r.sw)/SLOTS_PER_EPOCH*100:>6.2f}% "
              f"{int(bmap.get(ep,0)):>14,} {nin:>14,} "
              f"{(nin/nlegs*100 if nlegs else 0):>6.2f}%")


# ── 2. Leaders ──────────────────────────────────────────────────────────────

def report_leaders(client, lo, hi, e0, e1):
    _hdr(2, "LEADER COVERAGE & ANALYSIS")
    theoretical = (e1 - e0 + 1) * SLOTS_PER_EPOCH
    r = client.query_df(f"""
        SELECT uniqExact(slot) AS slots, uniqExact(leader) AS leaders
        FROM slot_leaders WHERE slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    slots, leaders = int(r["slots"]), int(r["leaders"])
    print(f"\n  slots with a known leader  {slots:>12,}   {slots/theoretical*100:6.2f}%")
    print(f"  missing                    {theoretical-slots:>12,}")
    print(f"  distinct leaders           {leaders:>12,}")

    print(f"\n  Sandwich load per leader (front-run's block):")
    lead = client.query_df(f"""
        WITH f AS (
            SELECT sandwichId AS sid, argMin(slot,(slot,position)) AS fslot
            FROM sandwich_txs WHERE type='frontRun' AND slot >= {lo} AND slot <= {hi}
            GROUP BY sandwichId
        ), l AS (
            SELECT slot, any(leader) AS leader FROM slot_leaders
            WHERE slot >= {lo} AND slot <= {hi} GROUP BY slot
        )
        SELECT l.leader AS leader, count() AS sandwiches
        FROM f INNER JOIN l ON f.fslot = l.slot GROUP BY l.leader
    """)
    if lead.empty:
        print("    (no leader data)")
        return
    tot = lead["sandwiches"].sum()
    top = lead.nlargest(10, "sandwiches")
    print(f"    leaders hosting >=1 sandwich : {len(lead):,} of {leaders:,}")
    print(f"    median sandwiches per leader : {lead['sandwiches'].median():,.0f}")
    print(f"    top-10 share of all sandwiches: {top['sandwiches'].sum()/tot*100:.2f}%")
    print(f"\n    {'leader':<46} {'sandwiches':>12} {'share':>8}")
    for r in top.itertuples():
        print(f"    {r.leader:<46} {int(r.sandwiches):>12,} {r.sandwiches/tot*100:>7.3f}%")

    # Concentration on the attacker side is the interesting direction: an entity whose sandwiches
    # nearly all land in ONE validator's blocks has ordering authority no bidder could buy.
    print(f"\n  Attacker-side leader concentration (front-run fee payers with >=10 sandwiches):")
    conc = client.query_df(f"""
        WITH f AS (
            SELECT sandwichId AS sid, argMin(slot,(slot,position)) AS fslot,
                   argMin(signers[1],(slot,position)) AS ent
            FROM sandwich_txs WHERE type='frontRun' AND slot >= {lo} AND slot <= {hi}
            GROUP BY sandwichId
        ), l AS (
            SELECT slot, any(leader) AS leader FROM slot_leaders
            WHERE slot >= {lo} AND slot <= {hi} GROUP BY slot
        ), g AS (
            SELECT f.ent AS ent, l.leader AS leader, count() AS c
            FROM f INNER JOIN l ON f.fslot = l.slot GROUP BY f.ent, l.leader
        )
        SELECT ent, sum(c) AS n, uniqExact(leader) AS leaders, max(c)/sum(c) AS top_share
        FROM g GROUP BY ent HAVING n >= 10
    """)
    if conc.empty:
        print("    (none)")
        return
    print(f"    entities                     : {len(conc):,}")
    print(f"    median top-leader share      : {conc['top_share'].median():.4f}")
    for thr in (0.5, 0.9, 0.99):
        n = int((conc["top_share"] >= thr).sum())
        print(f"    with top-leader share >= {thr:<4}: {n:>6,}")
    hot = conc[conc["top_share"] >= 0.5].nlargest(10, "n")
    if not hot.empty:
        print(f"\n    {'front signer':<46} {'sandwiches':>11} {'leaders':>8} {'top share':>10}")
        for r in hot.itertuples():
            print(f"    {r.ent:<46} {int(r.n):>11,} {int(r.leaders):>8} {r.top_share:>10.4f}")
        print("    A share near 1.0 over hundreds of sandwiches cannot arise by chance across ~800 "
              "validators;\n    it is direct evidence of validator-integrated ordering.")


# ── 3. Sandwiches overall, by distance ──────────────────────────────────────

def _agg_block(client, lo, hi, group_sql):
    """One aggregate row per group.

    Every money column is net of the attacker's own fees and is computed over the priced
    subset only.
    """
    return client.query_df(f"""
        WITH f AS (
            SELECT sandwichId AS sid, argMin(signers[1],(slot,position)) AS ent
            FROM sandwich_txs WHERE type='frontRun' AND slot >= {lo} AND slot <= {hi}
            GROUP BY sandwichId
        ), {fee_cte(lo, hi)}
        SELECT {group_sql} AS grp,
               count() AS sandwiches,
               uniqExact(f.ent) AS front_signers,
               sum(s.victimCount) AS victims,
               round(sumIf({USD_GROSS}, {PRICED}), 2) AS usd_gross,
               round(sumIf({FEE_USD}, {PRICED}), 2) AS usd_fee,
               round(sumIf({USD_NET}, {PRICED}), 2) AS usd_profit,
               round(avgIf({USD_NET}, {PRICED}), 6) AS usd_avg_priced,
               countIf({PRICED} AND {USD_NET} > 0) AS profitable,
               {win_rate_sql()} AS win_rate,
               countIf(s.profitA > 0) AS profitable_gross,
               round(countIf({PRICED} AND s.profitA > 0) /
                     nullIf(countIf({PRICED}), 0), 4) AS win_rate_gross,
               countIf({PRICED}) AS priced
        FROM sandwiches s INNER JOIN f ON s.sandwichId = f.sid {PRICE_JOIN} {FEE_JOIN}
        WHERE s.slot >= {lo} AND s.slot <= {hi}
        GROUP BY grp ORDER BY grp
    """)


def _print_agg(df, title, key="grp"):
    print(f"\n  {title}")
    print(f"  {'class':<26} {'sandwiches':>12} {'share':>7} {'front sig':>10} {'victims':>11} "
          f"{'USD gross':>14} {'USD fee':>12} {'USD net':>14} {'net/priced':>10} "
          f"{'win':>6} {'win.g':>6} {'priced':>7}")
    print("  " + "-" * 148)
    tot = df["sandwiches"].sum()
    for r in df.itertuples():
        share = r.sandwiches / tot * 100 if tot else 0
        pr = r.priced / r.sandwiches * 100 if r.sandwiches else 0
        print(f"  {getattr(r, key):<26} {int(r.sandwiches):>12,} {share:>6.2f}% "
              f"{int(r.front_signers):>10,} {int(r.victims):>11,} "
              f"{r.usd_gross:>14,.2f} {r.usd_fee:>12,.2f} {r.usd_profit:>14,.2f} "
              f"{r.usd_avg_priced:>10.4f} {r.win_rate:>6.3f} {r.win_rate_gross:>6.3f} {pr:>6.1f}%")
    print("  " + "-" * 148)
    print(f"  {'TOTAL':<26} {int(tot):>12,} {'100.00%':>7}")


def _print_net_legend(with_gross=True):
    rates = ("`win` is the NET win rate and `win.g` the gross one, both" if with_gross
             else "`win` is the NET win rate,")
    print("\n  `USD net` = gross profit minus the attacker's own front-run + back-run fees, both "
          "priced.")
    print(f"  {rates} over the priced subset: the unpriced share")
    print("  (100% - `priced`) has no defined net profit and is excluded from the win-rate")
    print("  denominator rather than scored as a loss.")


def report_distance(client, lo, hi):
    _hdr(3, "SANDWICHES BY SLOT DISTANCE")
    df = _agg_block(client, lo, hi, DISTANCE_SQL)
    _print_agg(df, "single-slot < same-leader multi-slot < cross-leader multi-slot")
    print("\n  `front sig` counts distinct front-run fee payers, NOT attackers — diff-signer "
          "entities are\n  only resolved by the union-find merge in phase 1.")
    _print_net_legend()


# ── 4. Sandwiches by pattern ────────────────────────────────────────────────

def report_pattern(client, lo, hi):
    _hdr(4, "SANDWICHES BY STRUCTURAL PATTERN")
    df = _agg_block(client, lo, hi, PATTERN_SQL)
    _print_agg(df, "mutually exclusive and exhaustive")
    _print_net_legend()

    total = client.query_df(f"SELECT count() AS n FROM sandwiches "
                            f"WHERE slot >= {lo} AND slot <= {hi}")["n"].iloc[0]
    summed = int(df["sandwiches"].sum())
    ok = "OK" if summed == int(total) else "MISMATCH"
    print(f"\n  exhaustiveness check: buckets {summed:,} vs table {int(total):,}   [{ok}]")

    print("\n  Cross-cutting flags (these DO overlap and do not sum to the total):")
    flags = client.query_df(f"""
        SELECT countIf(multiFrontRun OR multiBackRun) AS multi_split,
               countIf(NOT signerSame)                AS diff_signer,
               countIf(hasTransfer)                   AS has_transfer,
               countIf(multiVictim)                   AS multi_victim,
               countIf(perfect)                       AS perfect,
               count()                                AS total
        FROM sandwiches WHERE slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    for k in ("multi_split", "diff_signer", "has_transfer", "multi_victim", "perfect"):
        v = int(flags[k])
        print(f"    {k:<14} {v:>12,}   {v/int(flags['total'])*100:6.3f}%")


# ── 5. Pattern x distance ───────────────────────────────────────────────────

def report_pattern_distance(client, lo, hi):
    _hdr(5, "PATTERN x DISTANCE")
    df = _agg_block(client, lo, hi, f"concat({PATTERN_SQL}, '  |  ', {DISTANCE_SQL})")
    print(f"\n  {'pattern | distance':<52} {'sandwiches':>12} {'front sig':>10} "
          f"{'victims':>12} {'USD net':>14} {'USD fee':>12} {'win':>6} {'priced':>7}")
    print("  " + "-" * 132)
    cur = None
    for r in df.itertuples():
        pat = r.grp.split("  |  ")[0]
        if cur is not None and pat != cur:
            print()
        cur = pat
        pr = r.priced / r.sandwiches * 100 if r.sandwiches else 0
        print(f"  {r.grp:<52} {int(r.sandwiches):>12,} {int(r.front_signers):>10,} "
              f"{int(r.victims):>12,} {r.usd_profit:>14,.2f} {r.usd_fee:>12,.2f} "
              f"{r.win_rate:>6.3f} {pr:>6.1f}%")
    _print_net_legend(with_gross=False)


# ── 6. Jito bundle sandwiches ───────────────────────────────────────────────

def load_bundle_verdicts(e0, e1):
    """Read every bundle_sandwiches_*.csv and keep the rows inside the requested epoch range.

    Produced by 0_crawl_jito_bundle_ids.py, which resolves each leg's bundle ID from retained
    content where it survives and from Jito's API otherwise. The criterion is the exact one: some
    ONE bundleId holds a frontrun, a victim and a backrun leg.
    """
    frames = []
    for path in sorted(glob.glob(f"{BUNDLE_DIR}/bundle_sandwiches_*.csv")):
        df = pd.read_csv(path)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["sandwichId", "slot", "epoch", "bundle_id"])
    allrows = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["sandwichId"])
    return allrows[(allrows["epoch"] >= e0) & (allrows["epoch"] <= e1)]


def report_bundle_sandwiches(client, lo, hi, e0, e1):
    _hdr(6, "JITO BUNDLE SANDWICHES  (one bundle holds front+victim+back, signerSame, profitA>0)")
    v = load_bundle_verdicts(e0, e1)
    if v.empty:
        print(f"\n  No verdicts found under {BUNDLE_DIR}/. Run 0_crawl_jito_bundle_ids.py first.")
        return


    covered = sorted(v["epoch"].unique())
    missing = [e for e in range(e0, e1 + 1) if e not in covered]
    if missing:
        print(f"  epochs with NO verdict file: {missing}")
        print("  Those epochs are missing from the count below, which is therefore a LOWER BOUND.")

    # Stage the IDs rather than inlining them: at whole-dataset scale the `IN ('...')` literal runs
    # past ClickHouse's max_query_size and the whole section dies with a syntax error at 262 kB.
    tmp = "default._bundle_sw_ids"
    client.command(f"DROP TABLE IF EXISTS {tmp}")
    client.command(f"CREATE TABLE {tmp} (sid String) ENGINE = MergeTree ORDER BY sid")
    ids = v["sandwichId"].astype(str).tolist()
    for i in range(0, len(ids), 50_000):
        client.insert(tmp, [[s] for s in ids[i:i + 50_000]], column_names=["sid"])

    df = client.query_df(f"""
        WITH f AS (
            SELECT sandwichId AS sid, argMin(signers[1],(slot,position)) AS ent
            FROM sandwich_txs WHERE type='frontRun' AND slot >= {lo} AND slot <= {hi}
            GROUP BY sandwichId
        ), {fee_cte(lo, hi)}
        SELECT s.sandwichId AS sandwichId, intDiv(s.slot,{SLOTS_PER_EPOCH}) AS epoch,
               f.ent AS front_signer, s.victimCount AS victims, s.profitA AS profit,
               {FEE_SOL} AS fee_sol,
               if({PRICED}, {USD_GROSS}, NULL) AS usd,
               if({PRICED}, {USD_NET}, NULL) AS usd_net,
               {PATTERN_SQL} AS pattern, {DISTANCE_SQL} AS distance
        FROM sandwiches s INNER JOIN f ON s.sandwichId = f.sid {PRICE_JOIN} {FEE_JOIN}
        WHERE s.slot >= {lo} AND s.slot <= {hi}
          AND s.sandwichId IN (SELECT sid FROM {tmp})
    """)
    client.command(f"DROP TABLE IF EXISTS {tmp}")
    if df.empty:
        print("  (verdict IDs did not match any sandwich in this database/range)")
        return

    # Beyond the shared bundleId, a bundle sandwich requires the front and back runs to carry the
    # SAME signer and `profitA > 0`. Those two conditions are the whole definition; the USD
    # figure below is reported, never gated on, since a token missing from the price table
    # scores $0.
    #
    # The definition stays on GROSS `profitA` even though every other section of this report
    # is net, so that membership does not become a function of price coverage. The net figure
    # appears beside it in the `USD net` columns.
    #
    # Applied by calling the shared implementation rather than repeating the predicate here.
    n0 = len(df)
    df = df[df["sandwichId"].isin(verified_bundle_sandwiches(client.database, e0, e1))]
    n_priced = int((df["usd"] > BUNDLE_USD_MIN).sum())
    print(f"\n  verdicts {n0:,} -> {len(df):,} after signerSame & profit>0   "
          f"(of which {n_priced:,} also clear USD > ${BUNDLE_USD_MIN:g}, reported not gated)")
    if df.empty:
        print("  nothing left after filtering.")
        return
    # No GROSS win rate here: `profitA > 0` is part of the definition, so it would print 1.0000
    # always and read as a finding rather than as the tautology it is. The NET rate is not a
    # tautology — a sandwich can clear the definition and still lose money to its own fees — so it
    # is worth printing, over the priced subset for the reason given at the top of the file.
    n_measurable = int(df["usd_net"].notna().sum())
    n_net_pos = int((df["usd_net"] > 0).sum())
    print(f"\n  bundle sandwiches          : {len(df):,}")
    print(f"  distinct front signers     : {df['front_signer'].nunique():,}")
    print(f"  victim legs                : {int(df['victims'].sum()):,}")
    print(f"  attacker fees              : {df['fee_sol'].sum():,.4f} SOL  "
          f"(${df['fee_sol'].sum() * SOL_USD:,.2f})")
    print(f"  USD gross (priced subset)  : {df['usd'].sum():,.2f}")
    print(f"  USD net   (priced subset)  : {df['usd_net'].sum():,.2f}")
    print(f"  still net-positive         : {n_net_pos:,} of {n_measurable:,} priced "
          f"({n_net_pos / n_measurable * 100 if n_measurable else 0:.2f}%);  "
          f"{len(df) - n_measurable:,} unpriced and therefore unmeasurable")

    print("\n  By pattern:")
    print(f"    {'pattern':<26} {'sandwiches':>12} {'front sig':>10} {'victims':>10} "
          f"{'USD gross':>14} {'USD net':>14}")
    g = df.groupby("pattern").agg(n=("sandwichId", "size"), sig=("front_signer", "nunique"),
                                  vic=("victims", "sum"), usd=("usd", "sum"),
                                  net=("usd_net", "sum"))
    for pat, r in g.iterrows():
        print(f"    {pat:<26} {int(r.n):>12,} {int(r.sig):>10,} {int(r.vic):>10,} "
              f"{r.usd:>14,.2f} {r.net:>14,.2f}")
    print("    Only `standard` and `multi-front/back` can appear — diff-signer patterns are excluded "
          "by\n    definition. Multi-split empirically never fits inside a single bundle, so a "
          "non-zero row\n    there would contradict the framework document.")

    print("\n  By distance:")
    g = df.groupby("distance").agg(n=("sandwichId", "size"), usd=("usd", "sum"),
                                   net=("usd_net", "sum"))
    for dist, r in g.iterrows():
        print(f"    {dist:<26} {int(r.n):>12,} {r.usd:>14,.2f} {r.net:>14,.2f}")
    print("    Only single-slot is possible by construction — a bundle never spans slots (verified: "
          "0\n    bundleIds appear at more than one slot). Any other row is a data defect.")

    print("\n  Per epoch:")
    print(f"    {'epoch':>6} {'sandwiches':>12} {'front sig':>10} {'victims':>10} "
          f"{'USD gross':>14} {'USD net':>14}")
    for ep, sub in df.groupby("epoch"):
        print(f"    {int(ep):>6} {len(sub):>12,} {sub['front_signer'].nunique():>10,} "
              f"{int(sub['victims'].sum()):>10,} {sub['usd'].sum():>14,.2f} "
              f"{sub['usd_net'].sum():>14,.2f}")

    print("\n  Per attacker per epoch (top 25 by bundle-sandwich count):")
    per = (df.groupby(["front_signer", "epoch"])
             .agg(n=("sandwichId", "size"), victims=("victims", "sum"), usd=("usd", "sum"),
                  fee_sol=("fee_sol", "sum"), usd_net=("usd_net", "sum"))
             .reset_index().sort_values("n", ascending=False))
    print(f"    {'front signer':<46} {'epoch':>6} {'sw':>6} {'victims':>9} {'USD gross':>12} "
          f"{'USD net':>12}")
    for r in per.head(25).itertuples():
        print(f"    {r.front_signer:<46} {int(r.epoch):>6} {int(r.n):>6} "
              f"{int(r.victims):>9,} {r.usd:>12,.2f} {r.usd_net:>12,.2f}")

    out = f"{BUNDLE_DIR}/bundle_sandwich_attackers_{e0}_{e1}.csv"
    per.to_csv(out, index=False)
    print(f"\n  per-attacker-per-epoch table -> {out}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    client = get_client(args.database)
    db = args.database or os.getenv("CLICKHOUSE_DATABASE", "solwich")
    lo = args.start_epoch * SLOTS_PER_EPOCH
    hi = (args.end_epoch + 1) * SLOTS_PER_EPOCH - 1

    prices = load_prices()
    stage_prices(client, prices)
    bind_prices(prices.get("SOL"))
    print(f"Database : {db}")
    print(f"Epochs   : {args.start_epoch}-{args.end_epoch}")
    print(f"Prices   : {len(prices):,} tokens, SOL pinned at ${SOL_USD:g}")

    cov = client.query_df(f"""
        SELECT count() AS total, countIf(tokenA IN (SELECT token FROM {PRICE_TABLE})) AS priced
        FROM sandwiches WHERE slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    tot, pri = int(cov["total"]), int(cov["priced"])
    print(f"Scope    : all sandwiches; {pri:,} of {tot:,} ({pri/tot*100:.2f}%) have a tokenA price, "
          f"{tot-pri:,} ({(tot-pri)/tot*100:.2f}%) do not.")
    print(f"Profit   : NET of the attacker's own front-run + back-run fees. The {(tot-pri)/tot*100:.2f}%"
          f" without a tokenA price have no\n           defined net profit and are EXCLUDED from "
          f"the USD sums and from every win-rate\n           denominator — they are unmeasurable, "
          f"not losses.")

    fee = client.query_df(f"""
        SELECT sum(fee) / 1e9 AS fee_sol, count() AS legs
        FROM sandwich_txs
        WHERE type IN ('frontRun', 'backRun') AND slot >= {lo} AND slot <= {hi}
    """).iloc[0]
    print(f"Fees     : {float(fee['fee_sol']):,.4f} SOL over {int(fee['legs']):,} attacker legs "
          f"(${float(fee['fee_sol']) * SOL_USD:,.2f})")

    want = set(args.sections)
    if "1" in want:
        report_slot_coverage(client, lo, hi, args.start_epoch, args.end_epoch)
    if "2" in want:
        report_leaders(client, lo, hi, args.start_epoch, args.end_epoch)
    if "3" in want:
        report_distance(client, lo, hi)
    if "4" in want:
        report_pattern(client, lo, hi)
    if "5" in want:
        report_pattern_distance(client, lo, hi)
    if "6" in want:
        report_bundle_sandwiches(client, lo, hi, args.start_epoch, args.end_epoch)
    print()


if __name__ == "__main__":
    main()
