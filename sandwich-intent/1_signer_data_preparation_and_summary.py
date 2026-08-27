"""Phase 1: per-sandwich metrics and per-entity features.

Reads sandwiches from ClickHouse by category, computes per-sandwich metrics, aggregates
them to the attacker/entity level, and writes features, a summary and charts to
`data/1_signer_data_preparation_and_summary/<category>/<database>/`.

ORDERING GAPS, in transaction-count units across the slot axis:

    front_gap = distance(first front-run tx, first victim tx)
    back_gap  = distance(first back-run tx,  last  victim tx)

"first"/"last" are by (slot, position). Distances span slots via a prefix sum over
`slot_txs.txCount`.

SLIPPAGE CONSUMPTION (SC) is recomputed from the raw amounts on each victim leg --
`slippageLimitType`, `slippageLimitAmount`, `slippageActualAmount` -- not read from the
detector's `slippageUtilization` column.

    per victim    limitType 'output' -> SC = limit / actual
                  limitType 'input'  -> SC = actual / limit
                  anomaly when the type is empty, the ratio cannot be formed, or SC > 1
    per sandwich  SC = max over the victims at or above SC_PROTECTION_FLOOR; any anomalous
                  victim makes the whole sandwich anomalous
    per attacker  mean_SC = mean over that attacker's SCORED sandwiches

Three outcomes per sandwich, carried on separate columns:

    scored          at least one protected victim; `sc` is their max
    anomalous       `sc_anomaly` set, `sc` is NaN
    unprotected     `sc_unprotected` set, reason `no_protection`, `sc` is NaN

`sc_coverage`, `sc_anomaly_rate` and `unprotected_rate` report the three shares.

CATEGORIES

    standard              signerSame, NOT multi-split (default)
    multi_split           signerSame, multiFrontRun OR multiBackRun
    diff_signer_owner     NOT signerSame, ownerSame,  NOT multi-split
    diff_signer_transfer  NOT signerSame, NOT ownerSame, NOT multi-split

The four do not partition the corpus; see `CATEGORY_SCOPES`. `diff_signer_transfer` names
its attacker by the front-run fee payer.

For `diff_signer_owner`, signers are resolved into entities before aggregation;
`--entity-merge` selects the anchor relation. Owner anchors that are SPL token accounts
rather than wallets are filtered using the cache built by `utils/check_owner_accounts.py`;
without that cache the run says so and proceeds unfiltered.

PROFIT FLOOR. Entities under `--profit-floor-usd` (default $100 net over the whole range)
are dropped after aggregation; entities whose sandwiches are all verified Jito bundle
sandwiches are exempt when positive. Pass `--profit-floor-usd 0` to disable.
See `apply_profit_floor`.

`usd_total_profit` is NaN, not 0.0, when nothing an attacker traded had a price; filter on
`usd_priced_count > 0` before treating it as a profit. Optional input columns are always
present in the output schema, backfilled with NaN when absent.

Usage
    python 1_signer_data_preparation_and_summary.py --database solwich \\
        --category standard --start-epoch 946 --end-epoch 990
    python 1_signer_data_preparation_and_summary.py --database solwich --dry-run

`--cross-leader` selects which sandwich geometries enter the run: `include` (default, all),
`exclude` (single-leader only) or `only` (the cross-leader slice).
"""

import argparse
import json
import os
import time
import warnings
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from utils.db import get_client
from utils.intent import (load_bundle_verdicts, load_token_prices,
                          verified_bundle_sandwiches)

SLOTS_PER_EPOCH = 432_000

# S2 admissibility band, [0, 1]. Only the ceiling rejects: SC above 1 means the decoded
# limit did not bind this swap, so that victim's consumption is unknown and it voids the
# sandwich. SC at or near 0 is a measurement and is kept.
#
# MUST equal dex.SlippageMinAdmissible / dex.SlippageMaxAdmissible in
# sandwich-detector/sol/dex/slippage.go; `assert_go_band_matches()` below checks it.
SC_MIN = 0.0
SC_MAX = 1.0

# Relative slack on the ceiling, then a clamp: the stored limit is one
# float64(uint64)/10**decimals while the stored actual is a SUM of balance deltas, so a
# victim filled at exactly its limit can exceed 1.0 by a few ULP. Must equal
# dex.SlippageCeilingTolerance in slippage.go; `assert_go_band_matches` checks it.
SC_CEIL_TOL = 1e-9

# Protection floor. NOT the bottom of the admissible band, which is still [0, 1]: a
# sandwich's score is the max over its victims AT OR ABOVE this floor, and when no victim
# reaches it the sandwich is not scored rather than scored near 0. `mean_SC` is therefore
# E[SC | SC >= floor]. Must equal dex.SlippageProtectionFloor in slippage.go.
#
# `no_protection` covers both "the victim set no bound" and "the bound is in an outer
# aggregator route, above where the detector decodes"; `unprotected_rate` inherits that.
SC_PROTECTION_FLOOR = 0.01

GO_SLIPPAGE_CONSTANTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "sandwich-detector", "sol", "dex", "slippage.go")


def assert_go_band_matches(path=GO_SLIPPAGE_CONSTANTS):
    """Fail if the Go admissibility band no longer matches the four Python constants.

    Returns a status string for the banner; a missing Go checkout is reported rather than
    treated as agreement.
    """
    import re
    if not os.path.exists(path):
        return f"NOT CHECKED (no {os.path.normpath(path)})"
    src = open(path).read()
    want = {"SlippageMinAdmissible": SC_MIN,
            "SlippageMaxAdmissible": SC_MAX,
            "SlippageCeilingTolerance": SC_CEIL_TOL,
            "SlippageProtectionFloor": SC_PROTECTION_FLOOR}
    found = {}
    for name in want:
        m = re.search(rf"^\s*{name}\s*=\s*([0-9.eE+-]+)\s*$", src, re.M)
        if m is None:
            raise RuntimeError(
                f"{name} not found in {path}; the Go/Python slippage band can no "
                f"longer be verified. Fix the parser or the Go constant block.")
        found[name] = float(m.group(1))
    drift = {k: (found[k], v) for k, v in want.items() if found[k] != v}
    if drift:
        detail = "; ".join(f"{k}: Go {g}, Python {p}" for k, (g, p) in drift.items())
        raise RuntimeError(
            f"S3 VIOLATION: {detail}. The two codebases disagree about which "
            f"victims are anomalous; reconcile before running.")
    return (f"OK (Go and Python both [{SC_MIN}, {SC_MAX}], ceiling slack "
            f"{SC_CEIL_TOL:g}, protection floor {SC_PROTECTION_FLOOR:g})")

# Position is the transaction index inside a slot; Solana blocks hold O(10^3)
# transactions, so 2^20 is a safe radix for a lexicographic (slot, position)
# surrogate key that is injective and monotone.
POS_RADIX = 1 << 20

# Legacy long-gap fallback.  v2's detection window is bounded at 64 blocks so
# this should be structurally unreachable; it is kept, counted and warned about
# rather than deleted so that a silent regression cannot hide in it.
LONG_GAP_SLOTS = 100
LONG_GAP_AVG_TX = 1272

# Entities larger than this are flagged, never split (see S4/g2). Three tripwires:
#   ENTITY_SIZE_FLAG   - anchor keys fused into one component
#   ENTITY_SHARE_FLAG  - share of the analysed population under one label
#   ENTITY_WALLET_FLAG - distinct fee payers folded into one "attacker"
ENTITY_SIZE_FLAG = 50
ENTITY_SHARE_FLAG = 0.10
ENTITY_WALLET_FLAG = 500

# ── Profit floor (S5a) ───────────────────────────────────────────────────────
#
# Entities whose whole net take over the range is under this are dropped before anything
# downstream sees them.
#
# One exception: an entity ALL of whose sandwiches are verified Jito bundle sandwiches is
# kept whenever its take is positive, matching Track 1 in `3_attacker_filter.py`, which
# admits those attackers with no threshold. For such an entity `win_rate_n == 0` (nothing
# priceable) also passes the positivity test, since `profitA > 0` holds for every verified
# bundle sandwich by definition and is asserted below.
PROFIT_FLOOR_USD = 100.0

# Test hook for --shuffle-seed. Set once in main(); every fetched frame is
# permuted with it before anything reads the frame, so an identical result
# proves the pipeline never depends on ClickHouse part-read order.
_SHUFFLE_SEED = None


# ── CLI ──────────────────────────────────────────────────────────────────────

CATEGORY_SCOPES = {
    "standard": """
        signerSame = true
        AND multiFrontRun = false
        AND multiBackRun = false
    """,
    "multi_split": """
        signerSame = true
        AND (multiFrontRun = true OR multiBackRun = true)
    """,
    "diff_signer_owner": """
        signerSame = false
        AND ownerSame = true
        AND multiFrontRun = false
        AND multiBackRun = false
    """,
    # The two legs share no signer AND no owner, so `extract_common_signer` falls back to
    # the front-run fee payer. Report which rule named the attacker wherever this category
    # contributes.
    "diff_signer_transfer": """
        signerSame = false
        AND ownerSame = false
        AND multiFrontRun = false
        AND multiBackRun = false
    """,
}

# The four scopes do NOT partition the corpus: `diff_signer_owner` and `diff_signer_transfer`
# both exclude multi-front/multi-back, so diff-signer x multi-split sandwiches fall into no
# category. `0_report_data_overview.py` section 4 counts the remainder.

ENTITY_MERGE_MODES = ["legacy", "feepayer", "owner", "owner+guard"]


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1: signer data preparation & summary")
    p.add_argument("--database", type=str, default=None,
                   help="ClickHouse database override. "
                        "Defaults to CLICKHOUSE_DATABASE from .env.")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=956)
    p.add_argument("--category", type=str, default="standard",
                   choices=list(CATEGORY_SCOPES.keys()),
                   help="Sandwich category to analyze")
    p.add_argument("--cross-leader", type=str, default="include",
                   choices=["include", "exclude", "only"],
                   help="Which sandwich geometries enter the run. 'include' (default) uses "
                        "all of them; 'exclude' keeps only single-leader sandwiches; 'only' "
                        "isolates the cross-leader slice.")
    p.add_argument("--entity-merge", type=str, default="owner",
                   choices=ENTITY_MERGE_MODES,
                   help="diff_signer_owner entity anchor (S4). 'owner' = token-B owner "
                        "intersection (default), 'feepayer' = signers[0] only, 'legacy' = every "
                        "signer of every front/back leg, 'owner+guard' = owner with high-degree "
                        "bridging keys excluded.")
    p.add_argument("--profit-floor-usd", type=float, default=PROFIT_FLOOR_USD,
                   help=f"S5a noise floor: drop any signer/entity whose 45-epoch NET USD take is "
                        f"below this (default {PROFIT_FLOOR_USD:g}). Signers all of whose "
                        f"sandwiches are verified Jito bundle sandwiches are exempt when positive. "
                        f"Pass 0 for the unfiltered population.")
    p.add_argument("--entity-degree-cap", type=int, default=200,
                   help="owner+guard only: anchor keys touching more than this many distinct "
                        "sandwiches are treated as platform keys and dropped from the union.")
    p.add_argument("--shuffle-seed", type=int, default=None,
                   help="Shuffle every fetched frame with this seed before computing. Output "
                        "must be byte-identical to an unshuffled run; this is the determinism proof.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report row counts and column availability, compute nothing.")
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--out-root", type=str, default="data/1_signer_data_preparation_and_summary")
    return p.parse_args()


def build_scope(category, cross_leader):
    """Compose the SQL predicate that defines the analysed population."""
    parts = [f"({CATEGORY_SCOPES[category].strip()})"]
    if cross_leader == "exclude":
        parts.append("(crossLeader = false)")
    elif cross_leader == "only":
        parts.append("(crossLeader = true)")
    return "\n      AND ".join(parts)


# ── Schema probing ───────────────────────────────────────────────────────────

def probe_columns(client, database):
    """Return {table: set(columns)} so the script can adapt to the source schema."""
    df = client.query_df(
        f"SELECT table, name FROM system.columns WHERE database = '{database}'")
    out = defaultdict(set)
    for t, n in zip(df["table"], df["name"]):
        out[t].add(n)
    return dict(out)


# ── Data Fetching ────────────────────────────────────────────────────────────

def _dedup(df, keys):
    """Deterministic de-duplication.

    A MergeTree read can return duplicate rows for the same key, and a few of
    those duplicates carry genuinely CONFLICTING payloads. Which one wins must
    therefore be decided by a total order on the row contents rather than by
    ClickHouse part-read order — hence the string rendering of the array
    columns, which are otherwise unsortable.
    """
    df = _shuffle(df, _SHUFFLE_SEED, sum(map(ord, "".join(keys))))
    if len(df) == 0 or not df.duplicated(subset=keys).any():
        return df
    df = df.copy()
    sort_cols = []
    for c in df.columns:
        if isinstance(df[c].iloc[0], (list, np.ndarray)):
            tmp = f"__sort__{c}"
            df[tmp] = df[c].map(lambda v: "\x1f".join(map(str, v)))
            sort_cols.append(tmp)
        else:
            sort_cols.append(c)
    df = df.sort_values(sort_cols, kind="mergesort")
    df = df.drop_duplicates(subset=keys, keep="first")
    return df.drop(columns=[c for c in df.columns if c.startswith("__sort__")])


def fetch_timestamp_bounds(client, start_slot, end_slot):
    """Timestamp range of the slot window.

    `sandwich_txs` is sorted by (sandwichTimestamp, sandwichId, ...), so a slot
    predicate prunes nothing while a sandwichTimestamp predicate prunes almost
    everything: the victim-leg fetch for v2 epoch 946 goes from 1.61 s to
    0.16 s with identical row counts (352,164 both ways).
    """
    df = client.query_df(
        f"SELECT min(timestamp) AS lo, max(timestamp) AS hi FROM sandwiches "
        f"WHERE slot >= {start_slot} AND slot < {end_slot}")
    if len(df) == 0 or pd.isna(df["lo"].iloc[0]):
        return None
    lo = pd.Timestamp(df["lo"].iloc[0]) - pd.Timedelta(hours=1)
    hi = pd.Timestamp(df["hi"].iloc[0]) + pd.Timedelta(hours=1)
    return lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")


def fetch_sandwiches(client, scope, start_slot, end_slot):
    query = f"""
    SELECT sandwichId, crossBlock, crossLeader, slot, timestamp, tokenA, tokenB,
           consecutive, multiVictim, victimCount, adverseCount,
           multiFrontRun, multiBackRun, frontCount, backCount,
           perfect, relativeDiffB, profitA
    FROM sandwiches
    WHERE {scope}
      AND slot >= {start_slot} AND slot < {end_slot}
    """
    return _dedup(client.query_df(query), ["sandwichId"])


def _scope_subquery(scope, start_slot, end_slot):
    return (f"SELECT sandwichId FROM sandwiches WHERE {scope} "
            f"AND slot >= {start_slot} AND slot < {end_slot}")


def fetch_leg_txs(client, scope, start_slot, end_slot, ts_bounds, has_pool_dex):
    """Front-run and back-run legs only.

    `adverse` (2,753,415 rows/epoch in v2) and `transfer` (21,697) legs are not
    fetched — nothing in this file has ever read them.  Neither are `fee`,
    `programs`, `fromAmount`, `toAmount`, which were selected but never used.
    """
    dex = ", poolDex" if has_pool_dex else ""
    ts = ""
    if ts_bounds:
        ts = f"AND sandwichTimestamp BETWEEN '{ts_bounds[0]}' AND '{ts_bounds[1]}'"
    query = f"""
    SELECT sandwichId, type, slot, position, signature, signers, ownersOfB, inBundle{dex}
    FROM sandwich_txs
    WHERE type IN ('frontRun', 'backRun')
      {ts}
      AND sandwichId IN ({_scope_subquery(scope, start_slot, end_slot)})
    """
    return _dedup(client.query_df(query), ["sandwichId", "type", "slot", "position"])


def fetch_victim_txs(client, scope, start_slot, end_slot, ts_bounds):
    """Victim legs with the RAW slippage inputs.

    `slippageUtilization` is not selected: S2 recomputes SC from the raw amounts, and that
    column carries the detector's own clamp and sentinel semantics.
    """
    ts = ""
    if ts_bounds:
        ts = f"AND sandwichTimestamp BETWEEN '{ts_bounds[0]}' AND '{ts_bounds[1]}'"
    query = f"""
    SELECT sandwichId, slot, position, signature, inBundle,
           slippageLimitType, slippageLimitAmount, slippageActualAmount
    FROM sandwich_txs
    WHERE type = 'victim'
      {ts}
      AND sandwichId IN ({_scope_subquery(scope, start_slot, end_slot)})
    """
    return _dedup(client.query_df(query), ["sandwichId", "slot", "position"])


def fetch_leg_fees(client, scope, start_slot, end_slot, lo_slot, hi_slot):
    """SOL spent on transaction fees per sandwich, over the ATTACKER's own legs only.

    Front-run + back-run; a victim's fee is never charged. The detector's
    `profitA = backTx.toTotal - frontTx.fromTotal` has no cost side, so this is what makes
    every downstream USD figure net.

    Jito tips are NOT included: they are transfers to a tip account, not a fee, and this
    pipeline does not carry them.
    """
    query = f"""
    SELECT sandwichId, sum(fee) / 1e9 AS fee_sol
    FROM sandwich_txs
    WHERE type IN ('frontRun', 'backRun')
      AND slot >= {lo_slot} AND slot <= {hi_slot}
      AND sandwichId IN ({_scope_subquery(scope, start_slot, end_slot)})
    GROUP BY sandwichId
    """
    d = client.query_df(query)
    if not len(d):
        return pd.Series(dtype="float64", name="fee_sol")
    return d.set_index("sandwichId")["fee_sol"]


def fetch_jito_verdicts(database, epoch):
    """(colocated_ids, verified_ids) for one epoch.

    Ownership of the bundle-sandwich definition:

        0_crawl_jito_bundle_ids  resolves bundle IDs and writes the structural verdicts
        utils.intent             narrows them to the definition (signerSame, profitA > 0)
        here / 2 / 3             read that

    Neither source is `jito_bundles`; see `compute_jito_same_bundle`.
    """
    return (set(load_bundle_verdicts(epoch, epoch)["sandwichId"]),
            verified_bundle_sandwiches(database, epoch, epoch))


def fetch_slot_tx_counts(client, lo_slot, hi_slot):
    query = f"""
    SELECT slot, txCount FROM slot_txs
    WHERE slot >= {lo_slot} AND slot <= {hi_slot}
    """
    df = client.query_df(query)
    # A repeated slot is resolved by max, which is order-free, rather than by arrival order.
    dup = df["slot"].duplicated()
    if dup.any():
        warnings.warn(
            f"slot_txs has {int(dup.sum())} duplicate slot rows in [{lo_slot},{hi_slot}]; "
            f"resolving by max(txCount) so gaps stay run-independent. Investigate — "
            f"the loader should be writing one row per slot.")
        return df.groupby("slot")["txCount"].max()
    return df.set_index("slot")["txCount"]


def _shuffle(df, seed, salt):
    """Row-order shuffle used to prove order-invariance (see --shuffle-seed)."""
    if seed is None or len(df) == 0:
        return df
    rng = np.random.default_rng(seed + salt)
    return df.iloc[rng.permutation(len(df))].reset_index(drop=True)


# ── S1: front_gap / back_gap ─────────────────────────────────────────────────
#
#     front_gap = distance(FIRST front-run tx, FIRST victim tx)
#     back_gap  = distance(FIRST back-run tx,  LAST  victim tx)
#
# "first"/"last" are by (slot, position) as a GLOBAL min/max over the sandwich's legs of
# that type, not "first within the earliest slot".

def _order_key(slot, position):
    """Injective, monotone surrogate for the (slot, position) lexicographic order."""
    return slot.to_numpy(dtype="int64") * POS_RADIX + position.to_numpy(dtype="int64")


def _extreme_leg(df, which):
    """Deterministic first/last leg per sandwich as (slot, position).

    Uses min/max of the surrogate order key instead of `groupby().first()`.
    `groupby().first()` returns the first row in *frame* order — i.e. whatever
    order ClickHouse read its parts in — and additionally skips NaN, which is a
    second silent behaviour we do not want.  min/max of a total order depends
    on nothing but the data.
    """
    if len(df) == 0:
        return pd.DataFrame(columns=["slot", "position"], dtype="int64")
    k = pd.Series(_order_key(df["slot"], df["position"]), index=df.index)
    g = k.groupby(df["sandwichId"].to_numpy(), sort=False)
    ext = g.min() if which == "first" else g.max()
    out = pd.DataFrame({
        "slot": (ext.to_numpy() // POS_RADIX),
        "position": (ext.to_numpy() % POS_RADIX),
    }, index=pd.Index(ext.index, name="sandwichId"))
    return out


def _build_slot_coord(slot_tx_counts, lo, hi, diag):
    """Prefix sum over the slot axis, turning (slot, position) into one ordinal.

        prefix[i]        = sum of txCount over slots lo .. lo+i-1
        coord(slot, pos) = prefix[slot - lo] + pos

    coord(b) - coord(a) is the transaction distance in both cases:
      * same slot  -> the prefix terms cancel, leaving pos_b - pos_a
      * different  -> (txCount(a) - pos_a) + sum_{a<s<b} txCount(s) + pos_b
    """
    idx = np.arange(lo, hi + 1, dtype="int64")
    counts = slot_tx_counts.reindex(idx, fill_value=0).to_numpy(dtype="int64")
    present = int(slot_tx_counts.reindex(idx).notna().sum())
    expected = len(idx)
    coverage = present / expected if expected else 1.0
    diag["slot_coverage"] = round(coverage, 6)
    diag["slot_axis"] = [int(lo), int(hi)]
    if coverage < 0.99:
        warnings.warn(
            f"slot_txs covers only {coverage:.4f} of slots [{lo},{hi}]; every gap "
            f"crossing an absent slot is silently deflated")
    prefix = np.concatenate([[0], counts.cumsum()])
    return prefix


def _coord(prefix, lo, slot, position):
    return prefix[(slot - lo).astype("int64")] + position.astype("int64")


def compute_gaps(front_txs, back_txs, victim_txs, slot_tx_counts, sandwich_ids, diag):
    """Vectorised front_gap / back_gap over the whole epoch."""
    f1 = _extreme_leg(front_txs, "first")
    v1 = _extreme_leg(victim_txs, "first")
    vN = _extreme_leg(victim_txs, "last")
    b1 = _extreme_leg(back_txs, "first")

    all_slots = [d["slot"] for d in (f1, v1, vN, b1) if len(d)]
    if not all_slots:
        empty = pd.Series(pd.array([], dtype="Int64"), index=sandwich_ids)
        return pd.DataFrame({"front_gap": empty, "back_gap": empty})

    lo = int(min(int(s.min()) for s in all_slots))
    hi = int(max(int(s.max()) for s in all_slots))
    prefix = _build_slot_coord(slot_tx_counts, lo, hi, diag)

    def _gap(src, dst, name):
        j = src.join(dst, how="inner", lsuffix="_s", rsuffix="_d")
        if len(j) == 0:
            return pd.Series(pd.array([], dtype="Int64"), index=j.index, name=name)
        cs = _coord(prefix, lo, j["slot_s"], j["position_s"])
        cd = _coord(prefix, lo, j["slot_d"], j["position_d"])
        gap = (cd - cs).to_numpy(dtype="int64")

        # Legacy long-gap fallback, kept for bit-compatibility and instrumented.
        span = (j["slot_d"].to_numpy(dtype="int64") - j["slot_s"].to_numpy(dtype="int64"))
        far = span > LONG_GAP_SLOTS
        n_far = int(far.sum())
        diag[f"{name}_long_gap_fallback"] = n_far
        diag[f"{name}_max_slot_span"] = int(span.max()) if len(span) else 0
        if n_far:
            warnings.warn(
                f"_slot_distance long-gap fallback fired on {n_far} {name} legs "
                f"(max span {int(span.max())} slots); v2's window is bounded at 64 blocks "
                f"so this should be unreachable")
            src_pos = j["position_s"].to_numpy(dtype="int64")
            src_slot = j["slot_s"].to_numpy(dtype="int64")
            dst_pos = j["position_d"].to_numpy(dtype="int64")
            head = prefix[(src_slot - lo + 1)] - prefix[(src_slot - lo)] - src_pos
            gap = np.where(far, head + LONG_GAP_AVG_TX * (span - 1) + dst_pos, gap)
        return pd.Series(gap, index=j.index, name=name)

    front_gap = _gap(f1, v1, "front_gap")
    back_gap = _gap(vN, b1, "back_gap")

    out = pd.DataFrame(index=sandwich_ids)
    out["front_gap"] = front_gap.reindex(sandwich_ids).astype("Int64")
    out["back_gap"] = back_gap.reindex(sandwich_ids).astype("Int64")

    diag["front_gap_null"] = int(out["front_gap"].isna().sum())
    diag["back_gap_null"] = int(out["back_gap"].isna().sum())
    # The detector guarantees front < victim < back, so a non-positive gap is a
    # data bug, not a tight attacker. Surface it instead of averaging it in.
    diag["front_gap_lt1"] = int((out["front_gap"] < 1).sum())
    diag["back_gap_lt1"] = int((out["back_gap"] < 1).sum())
    return out


# ── S2: slippage consumption from raw amounts ────────────────────────────────
#
# The band is stated at SC_MIN/SC_MAX above. An anomaly is NaN plus a boolean and a
# reason, never a sentinel number.

SC_REASONS = ["ok", "no_protection", "empty_type", "limit_nonpositive",
              "actual_nonpositive", "uncomputable", "above_one", "unclassified",
              "no_victim"]
# `ok` and `no_protection` are both measurements; everything else is an anomaly.
SC_MEASURED = ("ok", "no_protection")
# Priority for collapsing several victims into one sandwich-level reason, lowest index
# wins: data unavailability, then decode inconsistencies, then the measured states. `ok`
# beats `no_protection`, so the sandwich reads `no_protection` only when EVERY victim was.
SC_REASON_PRIORITY = {r: i for i, r in enumerate(
    ["no_victim", "unclassified", "empty_type", "limit_nonpositive",
     "actual_nonpositive", "uncomputable", "above_one", "ok", "no_protection"])}


def compute_victim_sc(victim_txs, diag):
    """Per-victim SC and anomaly label.

    A zero output limit is handled before the division rather than after. Both
    amounts can legitimately be 0 on that path, and `0 / 0` here would read a
    decoded fact ("the victim demanded nothing back") as a decode failure and
    void the sandwich.
    """
    n = len(victim_txs)
    if n == 0:
        return pd.DataFrame({"sandwichId": [], "sc": [], "sc_anomaly": [],
                             "sc_reason": []})

    lt = victim_txs["slippageLimitType"].to_numpy()
    la = victim_txs["slippageLimitAmount"].to_numpy(dtype="float64")
    aa = victim_txs["slippageActualAmount"].to_numpy(dtype="float64")

    is_out = lt == "output"
    is_in = lt == "input"
    known = is_out | is_in
    # min_out == 0: the victim set no floor, so it consumed none of its tolerance.
    no_prot = known & is_out & (la == 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sc = np.where(is_out, np.divide(la, aa, out=np.full(n, np.nan), where=aa != 0),
                      np.where(is_in, np.divide(aa, la, out=np.full(n, np.nan), where=la != 0),
                               np.nan))
    sc = np.where(no_prot, 0.0, sc)

    # Every state is assigned positively rather than by falling through to "ok",
    # so a value no mask claims (a NaN amount, say) surfaces as `unclassified`
    # instead of being reported as a measurement of NaN.
    reason = np.full(n, "unclassified", dtype=object)
    reason[~known] = "empty_type"
    # A max-cost ceiling of zero is a bound the swap must have violated, not a
    # loose one; a negative limit cannot come off the wire at all.
    reason[known & (la < 0)] = "limit_nonpositive"
    reason[known & is_in & (la == 0)] = "limit_nonpositive"
    reason[known & (la > 0) & (aa <= 0)] = "actual_nonpositive"
    computable = known & ~no_prot & (la > 0) & (aa > 0)
    reason[computable & (~np.isfinite(sc))] = "uncomputable"
    good = computable & np.isfinite(sc)
    # Clamp a fill at exactly the limit down onto the ceiling before the reject
    # test, so rounding slack never leaves a stored ratio above 1.
    near_ceiling = good & (sc > SC_MAX) & (sc <= SC_MAX * (1 + SC_CEIL_TOL))
    sc = np.where(near_ceiling, SC_MAX, sc)
    reason[good & (sc <= SC_MAX)] = "ok"
    reason[good & (sc > SC_MAX)] = "above_one"
    reason[no_prot] = "no_protection"

    anomaly = ~np.isin(reason, SC_MEASURED)
    sc = np.where(anomaly, np.nan, sc)

    out = pd.DataFrame({
        "sandwichId": victim_txs["sandwichId"].to_numpy(),
        "sc": sc,
        "sc_anomaly": anomaly,
        "sc_reason": pd.Categorical(reason, categories=SC_REASONS),
    })

    vc = pd.Series(reason).value_counts()
    diag["victim_legs"] = n
    diag["victim_sc_reasons"] = {k: int(v) for k, v in vc.items()}
    diag["victim_sc_ok_share"] = round(float((~anomaly).mean()), 6)
    return out


def aggregate_sc_per_sandwich(victim_sc, sandwich_ids, diag):
    """Sandwich SC = max over its victims at or above SC_PROTECTION_FLOOR.

    Any anomalous victim voids the whole sandwich: a max over the known subset is only a
    lower bound on the true max.

    A sub-floor victim is dropped from the max rather than competing in it at 0, and a
    sandwich whose victims are all sub-floor is not scored at all -- `sc_unprotected`, with
    `sc` left NaN.
    """
    empty = pd.DataFrame(index=sandwich_ids)
    if len(victim_sc) == 0:
        empty["sc"] = np.nan
        empty["sc_anomaly"] = True
        empty["sc_reason"] = pd.Categorical(["no_victim"] * len(sandwich_ids),
                                            categories=SC_REASONS)
        empty["sc_victim_n"] = 0
        empty["sc_victim_anomaly_n"] = 0
        empty["sc_victim_protected_n"] = 0
        diag["sandwich_sc"] = {"no_victim": len(sandwich_ids)}
        return empty

    grp = victim_sc.groupby("sandwichId", sort=False)
    n_v = grp.size()
    n_anom = grp["sc_anomaly"].sum()
    # The max runs over PROTECTED victims only.  Victims below the floor had no
    # tolerance to consume, so including them can only understate the attacker.
    protected = victim_sc["sc"] >= SC_PROTECTION_FLOOR
    n_prot = protected.groupby(victim_sc["sandwichId"], sort=False).sum()
    sc_max = (victim_sc["sc"].where(protected)
              .groupby(victim_sc["sandwichId"], sort=False).max())

    # Highest-priority reason among the sandwich's victims.
    prio = victim_sc["sc_reason"].map(SC_REASON_PRIORITY).astype("int16")
    worst = prio.groupby(victim_sc["sandwichId"], sort=False).min()
    inv = {v: k for k, v in SC_REASON_PRIORITY.items()}
    worst_reason = worst.map(inv)

    df = pd.DataFrame({
        "sc": sc_max,
        "sc_victim_n": n_v.astype("int32"),
        "sc_victim_anomaly_n": n_anom.astype("int32"),
        "sc_victim_protected_n": n_prot.astype("int32"),
        "sc_reason": worst_reason,
    })
    df = df.reindex(sandwich_ids)
    for c in ("sc_victim_n", "sc_victim_anomaly_n", "sc_victim_protected_n"):
        df[c] = df[c].fillna(0).astype("int32")
    df["sc_reason"] = df["sc_reason"].fillna("no_victim")

    # Three states, not two.  `sc_anomaly` stays "we could not measure"; being
    # unprotected is a measurement and gets its own flag, because the two carry
    # opposite evidence and lumping them would hide the stronger signal of the
    # pair -- see `unprotected_rate` at signer level.
    df["sc_anomaly"] = (df["sc_victim_anomaly_n"] > 0) | (df["sc_victim_n"] == 0)
    df["sc_unprotected"] = (~df["sc_anomaly"]) & (df["sc_victim_protected_n"] == 0)
    df["sc"] = df["sc"].where(~df["sc_anomaly"] & ~df["sc_unprotected"])
    df.loc[df["sc_unprotected"], "sc_reason"] = "no_protection"
    df["sc_reason"] = pd.Categorical(df["sc_reason"], categories=SC_REASONS)

    vc = df["sc_reason"].value_counts()
    diag["sandwich_sc"] = {k: int(v) for k, v in vc.items() if v}
    diag["sandwich_sc_scoreable"] = int((~df["sc_anomaly"] & ~df["sc_unprotected"]).sum())
    diag["sandwich_sc_unprotected"] = int(df["sc_unprotected"].sum())
    diag["sandwich_sc_no_victim"] = int((df["sc_victim_n"] == 0).sum())
    return df[["sc", "sc_anomaly", "sc_unprotected", "sc_reason",
               "sc_victim_n", "sc_victim_anomaly_n", "sc_victim_protected_n"]]


# ── Jito bundle co-location ──────────────────────────────────────────────────

def compute_jito_same_bundle(sandwich_ids, colocated_ids, verified_ids):
    """The two Jito columns, both read from `0_crawl_jito_bundle_ids.py`'s CSV verdicts.

    NOT computed from the `jito_bundles` table: it is a rolling buffer whose bundle CONTENT
    is deleted per epoch once `inBundle` marking finishes, so a table-based answer depends on
    when the script runs.

    jito_bundle_colocated  one bundleId holds >=1 front, >=1 victim and >=1 back. A structural
                           fact only -- it does not say the legs belong to one attacker.
    jito_bundle            the above AND signerSame AND profitA > 0 -- the bundle-sandwich
                           definition scripts 0_report and 3_attacker_filter use.
    """
    return pd.DataFrame({
        "jito_bundle": pd.Index(sandwich_ids).isin(verified_ids),
        "jito_bundle_colocated": pd.Index(sandwich_ids).isin(colocated_ids),
    }, index=sandwich_ids)


# ── S4: attacker identity & entity merging ───────────────────────────────────

class _UnionFind:
    """Union-Find whose root is always the lexicographically smallest member.

    That makes `find()` a canonical label, so two runs over the same EDGE SET
    always produce the same entity ids regardless of the order the edges were
    added in.  Connected components are order-invariant by definition; pinning
    the label to min(component) makes the *names* order-invariant too.
    """

    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            self.parent[rb] = ra


def extract_common_signer(front_txs, back_txs, diag):
    """Attacker identity for the signerSame categories.

    Common signer between the first front leg and the first back leg; when the
    two legs share nothing (which the category filter should prevent) fall back
    to the front-run fee payer.  Both legs are picked by (slot, position), not
    by frame order.
    """
    if len(front_txs) == 0:
        return pd.Series(dtype=object, name="signer")

    fk = pd.Series(_order_key(front_txs["slot"], front_txs["position"]),
                   index=front_txs.index)
    f_first = front_txs.loc[
        fk.groupby(front_txs["sandwichId"].to_numpy(), sort=False).idxmin().to_numpy()]
    f_first = f_first.set_index("sandwichId")["signers"]

    if len(back_txs) == 0:
        return f_first.map(lambda s: s[0] if len(s) else None).rename("signer")

    bk = pd.Series(_order_key(back_txs["slot"], back_txs["position"]),
                   index=back_txs.index)
    b_first = back_txs.loc[
        bk.groupby(back_txs["sandwichId"].to_numpy(), sort=False).idxmin().to_numpy()]
    b_first = b_first.set_index("sandwichId")["signers"]

    joined = pd.DataFrame({"f": f_first}).join(pd.DataFrame({"b": b_first}), how="left")
    f_arr = joined["f"].to_numpy()
    # A LEFT join fills a missing back leg with float NaN, not None, on an object column,
    # and `len(nan)` / `set(nan)` raise TypeError. Normalise to None once, here, so the
    # fee-payer fallback below is reachable.
    b_arr = np.array([None if (b is None or (isinstance(b, float) and pd.isna(b)))
                      else b for b in joined["b"].to_numpy()], dtype=object)
    diag["signer_missing_back_leg"] = int(sum(1 for b in b_arr if b is None))

    # Fast path: both legs single-signer.  Then the answer is the front signer
    # whether or not the two match (overlap -> that signer; no overlap ->
    # front-run fallback is the same signer), so no set work is needed.
    f_len = np.fromiter((len(x) for x in f_arr), dtype="int32", count=len(f_arr))
    b_len = np.fromiter((0 if b is None else len(b) for b in b_arr),
                        dtype="int32", count=len(b_arr))
    simple = (f_len == 1) & (b_len == 1)
    out = np.empty(len(f_arr), dtype=object)
    out[simple] = [x[0] for x in f_arr[simple]]
    idx = np.flatnonzero(~simple)
    for i in idx:
        f = f_arr[i]
        b = b_arr[i]
        ov = sorted(set(f) & set(b)) if b is not None else []
        out[i] = ov[0] if ov else (f[0] if len(f) else None)
    diag["signer_multi_leg_slowpath"] = int(len(idx))
    return pd.Series(out, index=joined.index, name="signer")


def _key_sets(df, col):
    """sandwichId -> frozenset of the values in `col` across the legs."""
    if len(df) == 0:
        return pd.Series(dtype=object)
    ex = df[["sandwichId", col]].explode(col)
    ex = ex[ex[col].notna()]
    return ex.groupby("sandwichId", sort=False)[col].agg(frozenset)


def extract_entity_inputs(front_txs, back_txs, epoch):
    """Per-sandwich candidate anchor keys for every merge mode.

    Kept as one small frame so that all four `--entity-merge` modes and the
    legacy before/after comparison come out of a single code path rather than a
    git stash.  diff_signer_owner is ~10k sandwiches per epoch, so holding this
    across the whole run costs nothing.
    """
    f_sign = _key_sets(front_txs, "signers")
    b_sign = _key_sets(back_txs, "signers")
    f_own = _key_sets(front_txs, "ownersOfB")
    b_own = _key_sets(back_txs, "ownersOfB")

    ids = f_sign.index.union(b_sign.index)
    if len(ids) == 0:
        return pd.DataFrame(columns=["sandwichId", "epoch", "all_signers",
                                     "feepayers", "owners"])

    def _fp(df):
        if len(df) == 0:
            return pd.Series(dtype=object)
        fp = pd.DataFrame({
            "sandwichId": df["sandwichId"].to_numpy(),
            "fp": [s[0] if len(s) else None for s in df["signers"].to_numpy()],
        })
        fp = fp[fp["fp"].notna()]
        return fp.groupby("sandwichId", sort=False)["fp"].agg(frozenset)

    f_fp = _fp(front_txs)
    b_fp = _fp(back_txs)
    E = frozenset()

    fs = f_sign.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)
    bs = b_sign.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)
    fo = f_own.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)
    bo = b_own.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)
    ff = f_fp.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)
    bf = b_fp.reindex(ids).map(lambda x: x if isinstance(x, frozenset) else E)

    return pd.DataFrame({
        "sandwichId": ids,
        "epoch": epoch,
        "all_signers": [a | b for a, b in zip(fs, bs)],
        "feepayers": [a | b for a, b in zip(ff, bf)],
        # ownerSame is defined as frontOwners superset-of backOwners, so the
        # intersection is non-empty by construction for this category. It is
        # the same relation the diff_signer_owner category is defined by, which
        # is why it is the default anchor rather than a new heuristic.
        "owners": [a & b for a, b in zip(fo, bo)],
        "front_owners": list(fo),
        "back_owners": list(bo),
    }).set_index("sandwichId")


TOKEN_ACCOUNT_CACHE = "data/account_kind/{database}_owners.csv"
_TOKEN_ACCOUNTS = None


def load_token_accounts(database=None, path=None):
    """Owner keys that are SPL token accounts, from `utils/check_owner_accounts.py`'s cache.

    Returns an empty set and says so if the cache is absent, rather than failing: a first-ever run
    on a new database has no cache and must still produce output. It must NOT be silent, though —
    without the cache the anchors are exactly as wrong as they were before S4a existed.
    """
    global _TOKEN_ACCOUNTS
    if _TOKEN_ACCOUNTS is not None:
        return _TOKEN_ACCOUNTS
    p = path or TOKEN_ACCOUNT_CACHE.format(database=database or _DB_FOR_CACHE[0])
    if not os.path.exists(p):
        print(f"  S4a: NO token-account cache at {p} — owner anchors are UNFILTERED. "
              f"Run `python utils/check_owner_accounts.py --database {database or _DB_FOR_CACHE[0]}` "
              f"and re-run phase 1 before publishing diff_signer_owner entities.")
        _TOKEN_ACCOUNTS = frozenset()
        return _TOKEN_ACCOUNTS
    d = pd.read_csv(p, index_col=0)
    _TOKEN_ACCOUNTS = frozenset(d.index[d["is_token_account"].astype(bool)].astype(str))
    print(f"  S4a: {len(_TOKEN_ACCOUNTS):,} token accounts loaded from {p} "
          f"(of {len(d):,} checked owner keys)")
    return _TOKEN_ACCOUNTS


# Set once in main(); `anchor_sets` is called deep in the call stack and threading the database name
# through four frames to reach one cache path is worse than one module-level box.
_DB_FOR_CACHE = [None]


def anchor_sets(entity_inputs, mode, degree_cap, diag):
    """Choose the key set that defines identity, per sandwich."""
    if mode == "legacy":
        anchors = entity_inputs["all_signers"]
    elif mode == "feepayer":
        anchors = entity_inputs["feepayers"]
    else:  # owner / owner+guard
        owners = entity_inputs["owners"]
        fps = entity_inputs["feepayers"]
        # S4a — with `tokenB == SOL` the detector appends every account whose SOL balance
        # moved to `ownersOfB`, so a wSOL associated token account's own address can enter
        # the owner set and win the entity name under `min(members)`. Drop those keys: they
        # are aliases of a wallet already in the same anchor set.
        #
        # Only `is_token_account` disqualifies, not "never signed".
        token_accounts = load_token_accounts()
        if token_accounts:
            cleaned, hits = [], 0
            for o in owners:
                c = frozenset(k for k in o if k not in token_accounts)
                hits += len(o) - len(c)
                cleaned.append(c)
            owners = pd.Series(cleaned, index=entity_inputs.index)
            n_empt = sum(1 for o, c in zip(entity_inputs["owners"], cleaned)
                         if len(o) and not len(c))
            diag["entity_token_account_keys_dropped"] = hits
            diag["entity_token_account_emptied"] = n_empt
            print(f"  S4a: dropped {hits:,} token-account occurrences from the owner anchors "
                  f"({n_empt:,} sandwiches lost their whole owner set and fall back to fee payers)")
        anchors = pd.Series(
            [o if len(o) else f for o, f in zip(owners, fps)],
            index=entity_inputs.index)
        diag["entity_owner_empty_fallback"] = int(sum(1 for o in owners if not len(o)))

    if mode == "owner+guard":
        deg = defaultdict(int)
        for s in anchors:
            for k in s:
                deg[k] += 1
        hot = {k for k, d in deg.items() if d > degree_cap}
        diag["entity_guard_dropped_keys"] = len(hot)
        diag["entity_guard_top_keys"] = sorted(
            ((int(deg[k]), k) for k in hot), reverse=True)[:20]
        # When the guard empties a sandwich's anchor set, fall back to the fee payers, not
        # to the original set. If those are hot too the set goes empty and the sandwich is
        # dropped upstream with a warning.
        fps_guard = entity_inputs["feepayers"]
        guarded, n_fp_fallback, n_empty = [], 0, 0
        for s, f in zip(anchors, fps_guard):
            kept = frozenset(k for k in s if k not in hot)
            if not kept:
                kept = frozenset(k for k in f if k not in hot)
                n_fp_fallback += 1
                if not kept:
                    n_empty += 1
            guarded.append(kept)
        anchors = pd.Series(guarded, index=anchors.index)
        diag["entity_guard_feepayer_fallback"] = n_fp_fallback
        diag["entity_guard_unanchored"] = n_empty
    return anchors


def resolve_entities(anchors):
    """One global pass: edges -> components -> canonical labels.

    The previous code rebuilt a UnionFind per epoch and merged the results with
    `dict.update()`, which keeps only the LAST root for any signer seen in more
    than one epoch and therefore drops every edge established through an
    earlier root.  Accumulating the edges themselves and resolving once is
    order-invariant by construction: connected components do not depend on
    insertion order, and min(component) does not depend on it either.
    """
    uf = _UnionFind()
    nodes = set()
    edges = []
    for s in anchors:
        ks = sorted(s)
        if not ks:
            continue
        nodes.update(ks)
        for k in ks[1:]:
            edges.append((ks[0], k))
    for a, b in edges:
        uf.union(a, b)
    comps = defaultdict(list)
    for k in nodes:
        comps[uf.find(k)].append(k)
    canonical = {root: min(members) for root, members in comps.items()}
    key_to_entity = {k: canonical[uf.find(k)] for k in nodes}
    return key_to_entity, comps, canonical, edges


def legacy_per_epoch_entities(entity_inputs, epoch_order):
    """The `legacy` entity-merge mode: per-epoch UnionFind over all signers, accumulated with
    dict.update(), then re-merged by unioning (signer, last_root). Reported alongside the
    selected mode in the entity diagnostics.
    """
    merged = {}
    for ep in epoch_order:
        sub = entity_inputs[entity_inputs["epoch"] == ep]
        uf = _UnionFind()
        all_sig = set()
        for s in sub["all_signers"]:
            ks = sorted(s)
            all_sig.update(ks)
            for k in ks[1:]:
                uf.union(ks[0], k)
        root_to_members = defaultdict(list)
        for s in sorted(all_sig):
            root_to_members[uf.find(s)].append(s)
        root_to_canon = {r: m[0] for r, m in root_to_members.items()}
        epoch_map = {s: root_to_canon.get(uf.find(s), uf.find(s)) for s in all_sig}
        merged.update(epoch_map)          # <-- the defect
    ufg = _UnionFind()
    for s, r in merged.items():
        ufg.union(s, r)
    return {s: ufg.find(s) for s in merged}


def report_entity_merging(entity_inputs, mode, degree_cap, diag):
    """Print the before/after table S4 asks for and return the new mapping."""
    epochs = sorted(entity_inputs["epoch"].unique())
    signer_epochs = defaultdict(set)
    for ep, sigs in zip(entity_inputs["epoch"], entity_inputs["all_signers"]):
        for s in sigs:
            signer_epochs[s].add(ep)
    multi = sorted(s for s, e in signer_epochs.items() if len(e) > 1)

    fwd = legacy_per_epoch_entities(entity_inputs, epochs)
    rev = legacy_per_epoch_entities(entity_inputs, list(reversed(epochs)))
    stable = sum(1 for s in multi if fwd.get(s) == rev.get(s))
    legacy_entities = len(set(fwd.values()))
    legacy_sizes = pd.Series(list(fwd.values())).value_counts()

    anchors = anchor_sets(entity_inputs, mode, degree_cap, diag)
    key_to_entity, comps, canonical, edges = resolve_entities(anchors)
    # Order-invariance proof for the new path: shuffle the edge list, redo the
    # resolution, require an identical map.
    rng = np.random.default_rng(12345)
    shuffled = [edges[i] for i in rng.permutation(len(edges))] if edges else []
    uf2 = _UnionFind()
    for a, b in shuffled:
        uf2.union(a, b)
    comps2 = defaultdict(list)
    for k in key_to_entity:
        comps2[uf2.find(k)].append(k)
    canon2 = {r: min(m) for r, m in comps2.items()}
    map2 = {k: canon2[uf2.find(k)] for k in key_to_entity}
    edge_shuffle_stable = (map2 == key_to_entity)

    sizes = pd.Series({r: len(m) for r, m in comps.items()})
    sizes.index = [canonical[r] for r in sizes.index]

    def _hist(s):
        b = [(1, 1, "1"), (2, 5, "2-5"), (6, 20, "6-20"),
             (21, 100, "21-100"), (101, 10 ** 9, "101+")]
        return {lbl: int(((s >= lo) & (s <= hi)).sum()) for lo, hi, lbl in b}

    print(f"\n  --- S4 entity merging: before / after ---")
    print(f"  {'':34}{'legacy (per-epoch dict.update)':>32}  {'new (' + mode + ')':>26}")
    # NOTE: the two columns count different things on purpose. Legacy unions
    # every signer of every front/back leg, so its node set is "all signers";
    # the owner modes union token-B owners, so theirs is "all owner keys". The
    # collapse from the first number to the second IS the fix, not a loss.
    print(f"  {'keys entering the union':34}{len(signer_epochs):>32,}  "
          f"{len(key_to_entity):>26,}")
    print(f"  {'entities':34}{legacy_entities:>32,}  {len(comps):>26,}")
    print(f"  {'compression (keys/entity)':34}"
          f"{len(signer_epochs) / max(legacy_entities, 1):>32.3f}  "
          f"{len(key_to_entity) / max(len(comps), 1):>26.3f}")
    print(f"  {'largest cluster':34}{int(legacy_sizes.max()) if len(legacy_sizes) else 0:>32,}  "
          f"{int(sizes.max()) if len(sizes) else 0:>26,}")
    print(f"  {'multi-epoch signers':34}{len(multi):>32,}  {len(multi):>26,}")
    print(f"  {'  order-invariant assignment':34}"
          f"{f'{stable}/{len(multi)}':>32}  {f'{len(multi)}/{len(multi)}':>26}")
    print(f"  {'  consistency rate':34}"
          f"{(stable / len(multi) if multi else 1.0):>32.4f}  {1.0:>26.4f}")
    print(f"  {'edge-shuffle stable':34}{'n/a':>32}  {str(edge_shuffle_stable):>26}")
    print(f"\n  cluster-size histogram   legacy: {_hist(legacy_sizes)}")
    print(f"  cluster-size histogram      new: {_hist(sizes)}")

    anchor_entity = anchors.map(lambda s: key_to_entity[min(s)] if s else None)

    # ---- the comparable pair ----------------------------------------------
    # Everything above counts KEYS per component, in two different key namespaces (legacy =
    # every signer, owner = token-B owners), so the two are not comparable. What decides how
    # many attackers this pipeline reports is entities per SANDWICH population. Print both.
    legacy_sw = entity_inputs["all_signers"].map(
        lambda s: fwd.get(min(s)) if s else None)
    new_sw = anchor_entity
    def _swstats(x):
        v = x.dropna()
        if not len(v):
            return 0, 0, 0.0
        vc = v.value_counts()
        return int(v.nunique()), int(vc.max()), float(vc.max() / len(v))
    l_ent, l_max, l_share = _swstats(legacy_sw)
    n_ent, n_max, n_share = _swstats(new_sw)
    print(f"\n  --- the comparable pair: entities over the SANDWICH population ---")
    print(f"  {'sandwiches':34}{len(entity_inputs):>32,}  {len(entity_inputs):>26,}")
    print(f"  {'distinct entities':34}{l_ent:>32,}  {n_ent:>26,}")
    print(f"  {'sandwiches in largest entity':34}{l_max:>32,}  {n_max:>26,}")
    print(f"  {'  as share of population':34}{l_share:>32.4f}  {n_share:>26.4f}")
    if n_ent and l_ent and n_ent < l_ent:
        print(f"  -> the new anchor merges {l_ent / max(n_ent, 1):.2f}x HARDER at the "
              f"sandwich level than the code it replaces.")

    # o3, report-only: which keys are doing the bridging.
    deg = defaultdict(set)
    for sid, s in zip(entity_inputs.index, entity_inputs["all_signers"]):
        for k in s:
            deg[k].add(sid)
    top = sorted(((len(v), k) for k, v in deg.items()), reverse=True)[:20]
    print(f"\n  top bridging signer keys by distinct-sandwich degree (report only):")
    for d, k in top[:20]:
        ent = key_to_entity.get(k)
        if ent is None:
            note = "NOT AN ANCHOR under " + mode
        else:
            note = f"new-entity-size={int(sizes.get(ent, 1))}"
        print(f"    {k}  degree={d:<7,} {note}")

    # The anchor keys that are actually doing the merging, measured the way the
    # guard measures them. An anchor of degree 2,127 that is a component of size
    # 1 is invisible to `largest cluster` and to ENTITY_SIZE_FLAG, which counts
    # anchor keys per entity, so report anchor degree directly.
    adeg = defaultdict(int)
    for s in anchors:
        for k in s:
            adeg[k] += 1
    atop = sorted(((v, k) for k, v in adeg.items()), reverse=True)[:10]
    print(f"\n  top ANCHOR keys under '{mode}' by distinct-sandwich degree:")
    for d, k in atop:
        print(f"    {k}  degree={d:<7,} entity={key_to_entity.get(k)}")
    diag["entity_top_anchor_degree"] = [[int(d), k] for d, k in atop]

    diag["entity_legacy_entities"] = legacy_entities
    diag["entity_legacy_max_cluster"] = int(legacy_sizes.max()) if len(legacy_sizes) else 0
    diag["entity_legacy_consistency"] = round(stable / len(multi), 6) if multi else 1.0
    diag["entity_multi_epoch_signers"] = len(multi)
    diag["entity_new_entities"] = len(comps)
    diag["entity_new_max_cluster"] = int(sizes.max()) if len(sizes) else 0
    diag["entity_edge_shuffle_stable"] = bool(edge_shuffle_stable)
    diag["entity_hist_legacy"] = _hist(legacy_sizes)
    diag["entity_hist_new"] = _hist(sizes)
    diag["entity_sandwich_level"] = {
        "legacy_entities": l_ent, "legacy_max_sandwiches": l_max,
        "legacy_max_share": round(l_share, 6),
        "new_entities": n_ent, "new_max_sandwiches": n_max,
        "new_max_share": round(n_share, 6),
    }

    return anchor_entity, key_to_entity, sizes, deg


# ── Per-Sandwich Metrics ─────────────────────────────────────────────────────

def compute_per_sandwich_metrics(sandwiches, front_txs, back_txs, victim_txs,
                                 jito_sets, slot_tx_counts, leg_fees, token_prices,
                                 category, diag, has_pool_dex):
    s_idx = sandwiches.set_index("sandwichId")
    ids = s_idx.index

    if category == "diff_signer_owner":
        signer = pd.Series(pd.NA, index=ids, name="signer")  # resolved globally later
    else:
        signer = extract_common_signer(front_txs, back_txs, diag).reindex(ids)

    # Fees, and the two net-profit series built from them. A sandwich with no fee row is charged 0
    # rather than dropped: that means the legs carried no fee at all, which is a real 0, not a gap.
    fee_sol = leg_fees.reindex(ids).fillna(0.0).astype("float64")
    sol_price = float(token_prices.get("SOL", float("nan")))
    # An unpriced tokenA has NO price, so every USD quantity derived from it is NaN. The table is
    # SOL at a pinned $80 plus the 100 most frequent tokens (66 quotable), which prices 56.13 % of
    # the corpus; the other 43.87 % is genuinely unmeasurable in USD, not worth zero.
    price_a = s_idx["tokenA"].map(token_prices).astype("float64")
    usd_net = s_idx["profitA"].astype("float64") * price_a - fee_sol * sol_price
    is_sol = s_idx["tokenA"] == "SOL"
    sol_net = (s_idx["profitA"].astype("float64") - fee_sol).where(is_sol)

    gaps = compute_gaps(front_txs, back_txs, victim_txs, slot_tx_counts, ids, diag)
    victim_sc = compute_victim_sc(victim_txs, diag)
    sc = aggregate_sc_per_sandwich(victim_sc, ids, diag)
    jito = compute_jito_same_bundle(ids, jito_sets[0], jito_sets[1])

    if has_pool_dex and len(front_txs):
        fk = pd.Series(_order_key(front_txs["slot"], front_txs["position"]),
                       index=front_txs.index)
        f_first = front_txs.loc[
            fk.groupby(front_txs["sandwichId"].to_numpy(), sort=False).idxmin().to_numpy()]
        pool_dex = f_first.set_index("sandwichId")["poolDex"].astype(object).reindex(ids)
        # v1 has the column but never populated it. An empty string is
        # "unknown", not a venue: leaving it in would
        # make pool_dex_entropy 0 and pool_dex_top_share 1.0 for every v1
        # signer, i.e. a maximally confident signal derived from no data.
        pool_dex = pool_dex.where(pool_dex.notna() & (pool_dex != ""), other=pd.NA)
    else:
        pool_dex = pd.Series(pd.NA, index=ids, dtype=object)

    metrics = pd.DataFrame({
        "signer": signer,
        "slot": s_idx["slot"].astype("int64"),
        "ts": s_idx["timestamp"],
        "profit": s_idx["profitA"].astype("float64"),
        "fee_sol": fee_sol,
        # Profit is net of the attacker's own transaction fees, in USD: `profitA` is in
        # tokenA and the fee in lamports, so both are priced before subtracting.
        #
        # A tokenA with no price makes the subtraction impossible, so `usd_profit_net` is
        # NaN and the sandwich is neither a win nor a loss. `.where(notna())` is load-
        # bearing: `NaN > 0` is False in pandas, not NaN, so the bare comparison would score
        # every unpriced sandwich a loss. The nullable boolean lets groupby.mean() drop those
        # rows from numerator and denominator alike; `win_rate_n` is the denominator left.
        "usd_profit_net": usd_net,
        "is_profitable": (usd_net > 0).where(usd_net.notna()).astype("boolean"),
        # SOL-denominated sandwiches need no price at all -- profit and fee are both in SOL -- so
        # this one is exact wherever it is defined, and is the win rate to quote when price
        # coverage is in question. NaN outside the SOL subset, for the same reason.
        "is_profitable_sol": (sol_net > 0).where(sol_net.notna()).astype("boolean"),
        "cross_block": s_idx["crossBlock"],
        "cross_leader": s_idx["crossLeader"],
        "token_a": s_idx["tokenA"],
        "token_b": s_idx["tokenB"],
        "victim_count": s_idx["victimCount"].astype("int32"),
        "adverse_count": s_idx["adverseCount"].astype("int32"),
        "consecutive": s_idx["consecutive"],
        "multi_front": s_idx["multiFrontRun"],
        "multi_back": s_idx["multiBackRun"],
        "front_count": s_idx["frontCount"].astype("int32"),
        "back_count": s_idx["backCount"].astype("int32"),
        "pool_dex": pool_dex,
        "front_gap": gaps["front_gap"],
        "back_gap": gaps["back_gap"],
        "sc": sc["sc"],
        "sc_anomaly": sc["sc_anomaly"],
        "sc_unprotected": sc["sc_unprotected"],
        "sc_reason": sc["sc_reason"],
        "sc_victim_n": sc["sc_victim_n"],
        "sc_victim_anomaly_n": sc["sc_victim_anomaly_n"],
        "sc_victim_protected_n": sc["sc_victim_protected_n"],
        "jito_bundle": jito["jito_bundle"],
        "jito_bundle_colocated": jito["jito_bundle_colocated"],
    })
    metrics["token_b"] = metrics["token_b"].astype("category")
    metrics["pool_dex"] = metrics["pool_dex"].astype("category")
    return metrics


# ── S5: signer-level aggregation ─────────────────────────────────────────────
#
# What each family of columns measures:
#   sandwich_count, profit, win rates, active span, sol/non-sol split   volume and outcome
#   gap quantiles + CV + le1 ratio          ordering control: the shape of the gap
#                                           distribution, not just its median
#   interval_cv, burstiness (Goh-Barabasi), timing regularity: burstiness separates
#   duty_cycle, active_slot_count           periodic (-1) from Poisson (0) from bursty (+1)
#   hour_entropy                            24/7 operation is ~1
#   shape_entropy, victim_count_*           stability of the attack shape
#   pool_dex_*, token_b_*                   venue and target concentration
#   sc_std                                  dispersion of slippage consumption
#   sol_profit_top1_share,                  concentration of profit in one hit, and the
#   sol_loss_magnitude_ratio                size of the downside
#   cross_leader_count                      whether the shape spans a validator boundary

# Signer features whose producing block is conditional on the data (no SOL-side
# sandwiches, no populated poolDex, no scoreable sandwich, no anomalous
# sandwich...). They are backfilled with NaN so the emitted schema does not
# depend on which database or epoch range was analysed.
OPTIONAL_SIGNER_COLUMNS = [
    "sol_avg_profit", "sol_profit_median", "sol_win_rate",
    "sol_profit_top1_share", "sol_loss_magnitude_ratio", "nonsol_win_rate",
    "front_gap_p10", "front_gap_p50", "front_gap_p90", "front_gap_cv",
    "front_gap_le1_ratio",
    "back_gap_p10", "back_gap_p50", "back_gap_p90", "back_gap_cv",
    "back_gap_le1_ratio",
    "mean_SC", "sc_median", "sc_p90", "sc_std", "sc_victim_weighted_mean",
    "sc_reason_top",
    "avg_interval_slots", "interval_cv", "burstiness",
    "pool_dex_entropy", "pool_dex_top_share",
]


def _grouped_entropy(keys, values, support=None):
    """Normalised Shannon entropy of `values` within each `keys` group.

    `support` is the size of the variable's FIXED alphabet, when it has one.
    Pass it whenever the alphabet is known a priori (hour-of-day -> 24); leave it
    None only for open alphabets (token mints).

    Why it matters: normalising by log(k_observed) measures "uniform across the
    values this signer happened to touch", not "spread over the alphabet". With
    n observations and k distinct values, k <= n, so any signer with n = 2 and 2
    distinct values scores exactly 1.0 — the maximum. Measured on our
    standard 946-947 that made 2,150 signers score hour_entropy = 1.0 at a median
    of 3 sandwiches each, while the six busiest bots in the dataset (546-2,559
    sandwiches, unmistakably 24/7 automation) scored 0.959-0.990. The feature was
    ranking two-shot noise above every real bot, i.e. the exact inverse of the
    behaviour its docstring claims. log(24) fixes hour-of-day: 2 sandwiches in 2
    hours now scores log(2)/log(24) = 0.218, which is what "we saw 2 of 24 hours"
    should look like.

    Vectorised on purpose: `groupby().apply(lambda)` over millions of rows and
    hundreds of thousands of groups is what made the old fg_le_* block the
    slowest thing in this file.
    """
    vc = pd.DataFrame({"g": keys, "v": values}).groupby(["g", "v"], observed=True).size()
    tot = vc.groupby(level=0).transform("sum")
    p = vc / tot
    ent = (-(p * np.log(p))).groupby(level=0).sum()
    if support is not None and support > 1:
        norm = float(np.log(float(support)))
        vals = ent.to_numpy(dtype="float64") / norm
    else:
        k = vc.groupby(level=0).size()
        denom = np.log(k.to_numpy(dtype="float64"))
        vals = np.where(denom > 0, ent.to_numpy(dtype="float64") / np.where(
            denom > 0, denom, 1.0), 0.0)
    # ent/log(k) can overshoot 1.0 by one ULP on exactly-uniform groups, which a
    # downstream `assert 0 <= x <= 1` would trip on.
    out = pd.Series(np.clip(vals, 0.0, 1.0), index=ent.index)
    top = (vc.groupby(level=0).max() / tot.groupby(level=0).first())
    return out, top


def _interval_stats(metrics):
    """Inter-arrival statistics over UNIQUE active slots, per signer."""
    u = metrics[["signer", "slot"]].drop_duplicates()
    u = u.sort_values(["signer", "slot"], kind="mergesort")
    d = u["slot"].diff()
    same = u["signer"].to_numpy() == np.roll(u["signer"].to_numpy(), 1)
    same[0] = False
    d = d[same]
    grp = u["signer"][same]
    if len(d) == 0:
        empty = pd.Series(dtype="float64")
        return empty, empty, empty, u.groupby("signer").size()
    agg = d.groupby(grp.to_numpy(), sort=False).agg(["mean", "std"])
    mean = agg["mean"]
    std = agg["std"]
    cv = std / mean.replace(0, np.nan)
    burst = (std - mean) / (std + mean).replace(0, np.nan)
    return mean, cv, burst, u.groupby("signer").size()


def aggregate_to_signer(metrics, diag):
    m = metrics
    g = m.groupby("signer", sort=True, observed=True)
    idx_name = "signer"

    out = {}
    out["sandwich_count"] = g.size().rename("sandwich_count")

    # ── lifetime / cadence ───────────────────────────────────────────────
    slot_range = g["slot"].agg(["min", "max"])
    out["first_slot"] = slot_range["min"].rename("first_slot")
    out["last_slot"] = slot_range["max"].rename("last_slot")
    span = (slot_range["max"] - slot_range["min"]).rename("active_slot_span")
    out["active_slot_span"] = span
    out["sandwich_frequency"] = (out["sandwich_count"] / span.replace(0, np.nan)
                                 ).rename("sandwich_frequency")
    ts_range = g["ts"].agg(["min", "max"])
    out["first_ts"] = ts_range["min"].rename("first_ts")
    out["last_ts"] = ts_range["max"].rename("last_ts")

    mean_iv, cv_iv, burst, active_slots = _interval_stats(m)
    out["avg_interval_slots"] = mean_iv.rename("avg_interval_slots")
    out["interval_cv"] = cv_iv.rename("interval_cv")
    out["burstiness"] = burst.rename("burstiness")
    out["active_slot_count"] = active_slots.rename("active_slot_count")
    # span+1 is the number of slots the entity was alive for, so duty_cycle is bounded in
    # (0, 1]. span == 0 (a single active slot) is NaN, not 1.0, matching interval_cv and
    # burstiness.
    out["duty_cycle"] = (active_slots / (span.where(span > 0) + 1)
                         ).rename("duty_cycle")

    # Fixed alphabet: 24 hours. See _grouped_entropy for why self-normalising
    # here inverted the feature.
    hours = pd.to_datetime(m["ts"]).dt.hour
    hour_ent, _ = _grouped_entropy(m["signer"].to_numpy(), hours.to_numpy(),
                                   support=24)
    out["hour_entropy"] = hour_ent.rename("hour_entropy")
    # Non-degenerate companion: hour_entropy alone cannot separate "one sandwich"
    # from "many sandwiches, all in one hour" (both 0.0).
    out["active_hours"] = hours.groupby(m["signer"]).nunique().rename("active_hours")

    # ── outcome ──────────────────────────────────────────────────────────
    # Both win rates are net of the attacker's own fees.
    #   win_rate      over the sandwiches whose tokenA carries a price; NaN rows leave the
    #                 numerator AND denominator via groupby.mean().
    #   sol_win_rate  tokenA == SOL only, where profit and fee share a unit and no price is
    #                 involved. This is the one phase 3 gates on.
    out["win_rate"] = g["is_profitable"].mean().rename("win_rate")
    out["win_rate_n"] = g["is_profitable"].count().rename("win_rate_n")
    out["fee_sol_total"] = g["fee_sol"].sum().rename("fee_sol_total")
    out["usd_net_total"] = g["usd_profit_net"].sum().rename("usd_net_total")

    is_sol = m["token_a"] == "SOL"
    sol_m = m[is_sol]
    nonsol_m = m[~is_sol]
    out["sol_count"] = sol_m.groupby("signer").size().rename("sol_count")
    out["nonsol_count"] = nonsol_m.groupby("signer").size().rename("nonsol_count")
    if len(sol_m):
        sg = sol_m.groupby("signer")
        out["sol_avg_profit"] = sg["profit"].mean().rename("sol_avg_profit")
        out["sol_profit_median"] = sg["profit"].median().rename("sol_profit_median")
        out["sol_win_rate"] = sg["is_profitable_sol"].mean().rename("sol_win_rate")
        out["sol_net_profit"] = (
            (sol_m["profit"] - sol_m["fee_sol"]).groupby(sol_m["signer"]).sum()
            .rename("sol_net_profit"))
        pos = sol_m[sol_m["profit"] > 0].groupby("signer")["profit"]
        neg = sol_m[sol_m["profit"] < 0].groupby("signer")["profit"]
        pos_sum = pos.sum()
        out["sol_profit_top1_share"] = (
            sg["profit"].max() / pos_sum.replace(0, np.nan)
        ).rename("sol_profit_top1_share")
        out["sol_loss_magnitude_ratio"] = (
            neg.mean().abs() / pos.mean().replace(0, np.nan)
        ).rename("sol_loss_magnitude_ratio")
    if len(nonsol_m):
        out["nonsol_win_rate"] = nonsol_m.groupby("signer")["is_profitable"].mean(
        ).rename("nonsol_win_rate")

    # ── ordering control (S1) ────────────────────────────────────────────
    for side in ("front", "back"):
        col = f"{side}_gap"
        f = pd.Series(m[col].to_numpy(dtype="float64", na_value=np.nan),
                      index=m.index, name=col)
        sub = pd.DataFrame({"signer": m["signer"], col: f}).dropna(subset=[col])
        if len(sub) == 0:
            continue
        sg = sub.groupby("signer")[col]
        q = sg.quantile([0.1, 0.5, 0.9]).unstack()
        out[f"{side}_gap_p10"] = q[0.1].rename(f"{side}_gap_p10")
        out[f"{side}_gap_p50"] = q[0.5].rename(f"{side}_gap_p50")
        out[f"{side}_gap_p90"] = q[0.9].rename(f"{side}_gap_p90")
        gm = sg.mean()
        out[f"{side}_gap_cv"] = (sg.std() / gm.replace(0, np.nan)
                                 ).rename(f"{side}_gap_cv")
        sub["_le1"] = sub[col] <= 1
        out[f"{side}_gap_le1_ratio"] = sub.groupby("signer")["_le1"].mean(
        ).rename(f"{side}_gap_le1_ratio")
        out[f"{side}_gap_n"] = sg.size().rename(f"{side}_gap_n")

    # ── slippage consumption (S2) ────────────────────────────────────────
    # Three states, all reported: `sc_anomaly` (could not measure), `sc_unprotected`
    # (measured; no victim had tolerance to consume), and scoreable (the rest).
    scoreable = m[~m["sc_anomaly"] & ~m["sc_unprotected"]]
    out["sc_sandwich_n"] = out["sandwich_count"].rename("sc_sandwich_n")
    if len(scoreable):
        sg = scoreable.groupby("signer")["sc"]
        # groupby.mean() excludes NaN from numerator AND denominator natively,
        # but `scoreable` already holds only non-anomalous rows so the count is
        # explicit rather than implicit. Anomalies are never 0 and never a fail.
        out["mean_SC"] = sg.mean().rename("mean_SC")
        out["sc_median"] = sg.median().rename("sc_median")
        out["sc_p90"] = sg.quantile(0.9).rename("sc_p90")
        out["sc_std"] = sg.std().rename("sc_std")
        out["sc_scoreable_n"] = sg.size().rename("sc_scoreable_n")
        w = scoreable["sc_victim_n"].astype("float64")
        num = (scoreable["sc"] * w).groupby(scoreable["signer"]).sum()
        den = w.groupby(scoreable["signer"]).sum()
        out["sc_victim_weighted_mean"] = (num / den.replace(0, np.nan)
                                          ).rename("sc_victim_weighted_mean")
    out["sc_unprotected_n"] = m["sc_unprotected"].groupby(m["signer"]).sum(
    ).rename("sc_unprotected_n")
    anom = m[m["sc_anomaly"]]
    if len(anom):
        vc = anom.groupby(["signer", "sc_reason"], observed=True).size().sort_index()
        top = vc.groupby(level=0).idxmax()
        out["sc_reason_top"] = pd.Series(
            [t[1] for t in top], index=top.index).rename("sc_reason_top")

    # ── structure ────────────────────────────────────────────────────────
    # `jito_bundle` is the verified definition (bundle + signerSame + profit>0); the colocated
    # count is kept alongside it so the gap between the two stays visible per signer.
    out["jito_rate"] = g["jito_bundle"].mean().rename("jito_rate")
    out["jito_count"] = g["jito_bundle"].sum().rename("jito_count")
    out["jito_colocated_count"] = (
        g["jito_bundle_colocated"].sum().rename("jito_colocated_count"))
    out["in_block_count"] = (~m["cross_block"]).groupby(m["signer"]).sum(
    ).rename("in_block_count")
    out["cross_block_count"] = m["cross_block"].groupby(m["signer"]).sum(
    ).rename("cross_block_count")
    out["cross_leader_count"] = m["cross_leader"].groupby(m["signer"]).sum(
    ).rename("cross_leader_count")

    out["victim_count_mean"] = g["victim_count"].mean().rename("victim_count_mean")
    out["victim_count_p50"] = g["victim_count"].median().rename("victim_count_p50")
    out["victim_count_max"] = g["victim_count"].max().rename("victim_count_max")

    shape = (m["front_count"].to_numpy(dtype="int64") * (1 << 40)
             + m["back_count"].to_numpy(dtype="int64") * (1 << 20)
             + m["victim_count"].to_numpy(dtype="int64"))
    shape_ent, _ = _grouped_entropy(m["signer"].to_numpy(), shape)
    out["shape_entropy"] = shape_ent.rename("shape_entropy")

    if m["pool_dex"].notna().any():
        pd_sub = m[["signer", "pool_dex"]].dropna()
        dex_ent, dex_top = _grouped_entropy(pd_sub["signer"].to_numpy(),
                                            pd_sub["pool_dex"].astype(str).to_numpy())
        out["pool_dex_entropy"] = dex_ent.rename("pool_dex_entropy")
        out["pool_dex_top_share"] = dex_top.rename("pool_dex_top_share")

    tb_ent, tb_top = _grouped_entropy(m["signer"].to_numpy(),
                                      m["token_b"].astype(str).to_numpy())
    out["token_b_entropy"] = tb_ent.rename("token_b_entropy")
    out["token_b_top_share"] = tb_top.rename("token_b_top_share")
    out["token_b_nunique"] = g["token_b"].nunique().rename("token_b_nunique")

    signer_df = pd.concat(list(out.values()), axis=1)
    signer_df.index.name = idx_name

    for col in ["sol_count", "nonsol_count", "jito_count", "jito_colocated_count",
                "in_block_count",
                "cross_block_count", "cross_leader_count", "active_slot_count",
                "front_gap_n", "back_gap_n", "sc_scoreable_n", "sc_unprotected_n",
                "token_b_nunique"]:
        if col in signer_df.columns:
            signer_df[col] = signer_df[col].fillna(0).astype("int64")
        else:
            signer_df[col] = 0
    # sc_coverage      share of the attacker's sandwiches that produced a value
    # unprotected_rate share whose victims had no bound at all
    # sc_anomaly_rate  share that could not be read
    signer_df["sc_coverage"] = (signer_df["sc_scoreable_n"]
                                / signer_df["sc_sandwich_n"].replace(0, np.nan))
    signer_df["unprotected_rate"] = (
        signer_df["sc_unprotected_n"]
        / (signer_df["sc_unprotected_n"] + signer_df["sc_scoreable_n"]).replace(0, np.nan))
    signer_df["sc_anomaly_rate"] = 1 - (
        (signer_df["sc_scoreable_n"] + signer_df["sc_unprotected_n"])
        / signer_df["sc_sandwich_n"].replace(0, np.nan))

    # The optional blocks above are guarded by `if len(...)` / `if ....any()`, so backfill
    # here keeps the output SCHEMA independent of the data. Backfilled columns land at the
    # end, so compare by name, not by position.
    missing = [c for c in OPTIONAL_SIGNER_COLUMNS if c not in signer_df.columns]
    for col in missing:
        signer_df[col] = pd.NA if col.endswith("_top") else np.nan
    if missing:
        warnings.warn(
            f"no input data for {len(missing)} signer feature(s); emitted as NaN "
            f"to keep the output schema stable: {', '.join(missing)}")
    diag["signer_features_all_null"] = missing

    diag["signers"] = int(len(signer_df))
    diag["signers_with_mean_SC"] = int(signer_df["mean_SC"].notna().sum())
    return signer_df.sort_index()


# ── Token Price Loading ──────────────────────────────────────────────────────

def compute_usd_profit(per_sandwich, token_prices):
    """Gross USD. `usd_profit_net` (fee-inclusive) is set in compute_per_sandwich_metrics.

    The price table is `utils.intent.load_token_prices`, i.e. the single snapshot at
    `data/token_prices/prices.csv` that `0_crawl_token_price.py` writes. One table, loaded
    once: a second price source would make two USD figures in this repo incomparable.
    """
    per_sandwich["token_a_price"] = per_sandwich["token_a"].map(token_prices)
    per_sandwich["usd_profit"] = per_sandwich["profit"] * per_sandwich["token_a_price"]
    return per_sandwich


def apply_profit_floor(per_sandwich, signer_df, floor_usd, diag, label="signers"):
    """Drop signers/entities under the net-USD floor; keep bundle-only ones that made money.

    Returns `(per_sandwich, signer_df)` restricted to the survivors, so the two files phase 2 and
    phase 3 load can never describe different populations.

    Applied AFTER entity resolution and AFTER `aggregate_to_signer`, because a total is not knowable
    before aggregation -- "filter first, then aggregate" is not implementable, and filtering
    per-sandwich on per-sandwich profit would be a different (and wrong) rule.
    """
    if floor_usd is None or floor_usd <= 0:
        diag["profit_floor_usd"] = None
        return per_sandwich, signer_df

    n0, s0 = len(signer_df), len(per_sandwich)
    usd = signer_df["usd_net_total"]
    priced = signer_df["win_rate_n"] > 0
    bundle_only = (signer_df["jito_count"] >= signer_df["sandwich_count"]) & \
                  (signer_df["sandwich_count"] > 0)

    # Positivity for the exception: measured where we can measure, and definitional where we
    # cannot. The assert is what stops the second branch from being a wish.
    if bool(bundle_only.any()):
        jb = per_sandwich.loc[per_sandwich["jito_bundle"].astype(bool), "profit"]
        assert len(jb) == 0 or bool((jb > 0).all()), (
            "a verified bundle sandwich with profitA <= 0 exists; the definitional branch of the "
            "profit floor is unsound and must be replaced by a measured test")
    bundle_keep = bundle_only & ((priced & (usd > 0)) | (~priced))

    keep = (priced & (usd >= floor_usd)) | bundle_keep
    dropped_unpriced = int((~priced & ~bundle_keep).sum())

    signer_df = signer_df[keep]
    per_sandwich = per_sandwich[per_sandwich["signer"].isin(signer_df.index)]

    diag["profit_floor_usd"] = float(floor_usd)
    diag["profit_floor_signers_before"] = n0
    diag["profit_floor_signers_after"] = int(len(signer_df))
    diag["profit_floor_sandwiches_before"] = s0
    diag["profit_floor_sandwiches_after"] = int(len(per_sandwich))
    diag["profit_floor_bundle_exemptions"] = int(bundle_keep.sum())
    diag["profit_floor_dropped_unpriced"] = dropped_unpriced

    print(f"\nProfit floor (net USD >= ${floor_usd:,.0f}; bundle-only signers exempt):")
    print(f"  {label:<10} {n0:>10,} -> {len(signer_df):>10,}   "
          f"(-{n0 - len(signer_df):,}, -{(n0 - len(signer_df)) / max(n0, 1):.2%})")
    print(f"  sandwiches {s0:>10,} -> {len(per_sandwich):>10,}   "
          f"(-{s0 - len(per_sandwich):,}, -{(s0 - len(per_sandwich)) / max(s0, 1):.2%})")
    print(f"  bundle-only exemptions kept: {int(bundle_keep.sum()):,}")
    print(f"  dropped with NO priceable sandwich (unmeasurable, not unprofitable): "
          f"{dropped_unpriced:,}")
    return per_sandwich, signer_df


# ── Signer Summary & Statistics ──────────────────────────────────────────────

COUNT_BUCKETS = [
    (1, 10, "1-10"),
    (11, 50, "11-50"),
    (51, 100, "51-100"),
    (101, 500, "101-500"),
    (501, 1000, "501-1000"),
    (1001, None, "1001+"),
]


def _bucket_label(cnt):
    for lo, hi, label in COUNT_BUCKETS:
        if hi is None:
            if cnt >= lo:
                return label
        elif lo <= cnt <= hi:
            return label
    return "unknown"


def build_signer_summary(per_sandwich, signer_df, token_prices):
    # min_count=1 is load-bearing. pandas' groupby sum of an ALL-NaN group is
    # 0.0, not NaN, so a signer none of whose tokens are priced would report
    # "usd_total_profit = 0.00" — indistinguishable from one that traded at a
    # genuine net zero. Callers must filter on usd_priced_count > 0.
    usd_by_signer = per_sandwich.groupby("signer").agg(
        usd_total_profit=("usd_profit", lambda x: x.sum(min_count=1)),
        usd_positive_count=("usd_profit", lambda x: (x > 0).sum()),
        usd_negative_count=("usd_profit", lambda x: (x < 0).sum()),
        usd_priced_count=("usd_profit", lambda x: x.notna().sum()),
    )
    usd_by_signer["usd_price_coverage"] = (
        usd_by_signer["usd_priced_count"] / per_sandwich.groupby("signer").size())

    sol_ps = per_sandwich[per_sandwich["token_a"] == "SOL"]
    sol_by_signer = sol_ps.groupby("signer").agg(
        sol_total_profit=("profit", "sum"),
        sol_sandwich_count=("profit", "count"),
    )

    summary = signer_df.copy()
    summary = summary.join(usd_by_signer, how="left")
    summary = summary.join(sol_by_signer, how="left")
    # sol_total_profit legitimately fills to 0: a signer with no SOL-denominated
    # sandwich earned 0 SOL, which is a fact rather than a missing measurement.
    # usd_total_profit does NOT — NaN there means "not priced", so it stays NaN.
    summary["sol_total_profit"] = summary["sol_total_profit"].fillna(0)
    summary["sol_sandwich_count"] = summary["sol_sandwich_count"].fillna(0).astype("int64")
    summary["usd_priced_count"] = summary["usd_priced_count"].fillna(0).astype("int64")
    summary["count_bucket"] = summary["sandwich_count"].apply(_bucket_label)
    return summary


def _usd(series):
    """Format a USD column sum, keeping 'nothing was priced' distinct from '$0'."""
    v = series.sum(min_count=1)
    return "unpriced" if pd.isna(v) else f"${v:,.2f}"


def print_overall_summary(summary, per_sandwich, label="ALL SIGNERS"):
    n_signers = len(summary)
    n_sandwiches = int(summary["sandwich_count"].sum())
    sol_profit = float(summary["sol_total_profit"].sum())
    usd_profit = float(summary["usd_total_profit"].sum())
    n_priced = int(summary["usd_priced_count"].sum())
    n_unpriced_signers = int(summary["usd_total_profit"].isna().sum())

    print(f"\n{'=' * 80}")
    print(f"  SIGNER SUMMARY: {label}")
    print(f"{'=' * 80}")
    print(f"\n  Signers:          {n_signers:>10,}")
    print(f"  Total sandwiches: {int(n_sandwiches):>10,}")
    print(f"  SOL profit:       {sol_profit:>14,.4f} SOL")
    # Every USD figure below sums only the priced subset. A blank USD cell means
    # "no sandwich in this group had a price", not "zero profit".
    print(f"  USD profit:       ${usd_profit:>13,.2f}   "
          f"(priced: {n_priced:,}/{int(n_sandwiches):,} sandwiches = "
          f"{n_priced / max(n_sandwiches, 1) * 100:.1f}%; "
          f"{n_unpriced_signers:,} signers wholly unpriced)")
    if n_priced < n_sandwiches:
        print(f"  {'':18}NOTE: USD columns are NaN, not 0, where nothing was priced.")

    print(f"\n  --- Win Rate Distribution ---")
    wr_bins = [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
               (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.001)]
    wr_labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
                 "50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
    print(f"  {'WR Range':>10}  {'Signers':>8}  {'%':>7}  {'Sandwiches':>12}  "
          f"{'SOL Profit':>14}  {'USD Profit':>14}")
    print(f"  {'-' * 75}")
    for (lo, hi), lbl in zip(wr_bins, wr_labels):
        mask = (summary["win_rate"] >= lo) & (summary["win_rate"] < hi)
        sub = summary[mask]
        cnt = len(sub)
        sw = int(sub["sandwich_count"].sum())
        print(f"  {lbl:>10}  {cnt:>8,}  {cnt / max(n_signers, 1) * 100:>6.1f}%  {sw:>12,}  "
              f"{sub['sol_total_profit'].sum():>14,.2f}  "
              f"{_usd(sub['usd_total_profit']):>14}")

    print(f"\n  --- Count Bucket Breakdown ---")
    print(f"  {'Bucket':>10}  {'Signers':>8}  {'%':>7}  {'Sandwiches':>12}  {'%':>7}  "
          f"{'Avg WR':>7}  {'Med WR':>7}  {'SOL Profit':>14}  {'USD Profit':>14}")
    print(f"  {'-' * 105}")
    for bucket in [b[2] for b in COUNT_BUCKETS]:
        sub = summary[summary["count_bucket"] == bucket]
        cnt = len(sub)
        if cnt == 0:
            continue
        sw = int(sub["sandwich_count"].sum())
        print(f"  {bucket:>10}  {cnt:>8,}  {cnt / max(n_signers, 1) * 100:>6.1f}%  {sw:>12,}  "
              f"{sw / max(n_sandwiches, 1) * 100:>6.1f}%  {sub['win_rate'].mean():>7.3f}  "
              f"{sub['win_rate'].median():>7.3f}  {sub['sol_total_profit'].sum():>14,.2f}  "
              f"{_usd(sub['usd_total_profit']):>14}")

    return n_signers, int(n_sandwiches), sol_profit, usd_profit


def print_sc_and_gap_report(per_sandwich, signer_df, diag):
    """The parts of the answer that S1/S2 exist to produce."""
    print(f"\n{'=' * 80}")
    print(f"  SLIPPAGE CONSUMPTION (S2) & ORDERING GAPS (S1)")
    print(f"{'=' * 80}")

    n = len(per_sandwich)
    sc_ok = int((~per_sandwich["sc_anomaly"]).sum())
    print(f"\n  Sandwiches:            {n:>12,}")
    print(f"  Scoreable (SC in [{SC_MIN},{SC_MAX}], no anomalous victim): "
          f"{sc_ok:>12,}  ({sc_ok / max(n, 1) * 100:.2f}%)")
    print(f"\n  --- per-sandwich anomaly reason ---")
    vc = per_sandwich["sc_reason"].value_counts()
    for r, c in vc.items():
        if c:
            print(f"    {str(r):22s} {int(c):>12,}  {c / max(n, 1) * 100:>6.2f}%")

    print(f"\n  --- per-victim-leg anomaly reason ---")
    tot = diag.get("victim_legs_total", 0)
    for r, c in sorted(diag.get("victim_sc_reasons_total", {}).items(),
                       key=lambda kv: -kv[1]):
        print(f"    {r:22s} {c:>12,}  {c / max(tot, 1) * 100:>6.2f}%")

    if sc_ok:
        s = per_sandwich.loc[~per_sandwich["sc_anomaly"], "sc"]
        print(f"\n  scoreable SC: mean={s.mean():.4f} p10={s.quantile(.1):.4f} "
              f"p50={s.median():.4f} p90={s.quantile(.9):.4f} max={s.max():.4f}")

    for side in ("front", "back"):
        col = f"{side}_gap"
        v = per_sandwich[col].dropna()
        if len(v) == 0:
            continue
        v = v.astype("int64")
        print(f"\n  {col}: n={len(v):,}  null={int(per_sandwich[col].isna().sum()):,}  "
              f"<1={int((v < 1).sum()):,}")
        print(f"    p10={v.quantile(.1):.0f}  p50={v.median():.0f}  "
              f"p90={v.quantile(.9):.0f}  max={v.max():,}  <=1 share="
              f"{(v <= 1).mean() * 100:.2f}%")
    print(f"\n  long-gap fallback firings: "
          f"front={diag.get('front_gap_long_gap_fallback_total', 0)}  "
          f"back={diag.get('back_gap_long_gap_fallback_total', 0)}  "
          f"(max slot span front={diag.get('front_gap_max_slot_span_max', 0)}, "
          f"back={diag.get('back_gap_max_slot_span_max', 0)})")


# ── Chart Generation ─────────────────────────────────────────────────────────

def generate_charts(summary_all, per_sandwich, out_dir, tag):
    chart_dir = os.path.join(out_dir, "charts")
    os.makedirs(chart_dir, exist_ok=True)
    _plot_wr_distribution(summary_all, chart_dir, tag, "all")
    _plot_bucket_wr_boxplot(summary_all, chart_dir, tag, "all")
    _plot_bucket_profit_bar(summary_all, chart_dir, tag, "all")
    _plot_bucket_signer_sandwich_count(summary_all, chart_dir, tag, "all")
    _plot_sc_and_gaps(per_sandwich, summary_all, chart_dir, tag)
    print(f"\n  Charts saved to {chart_dir}/")


def _plot_wr_distribution(summary, chart_dir, tag, subset):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].hist(summary["win_rate"].dropna(), bins=20, edgecolor="black", alpha=0.7)
    axes[0].set_xlabel("Win Rate")
    axes[0].set_ylabel("Number of Signers")
    axes[0].set_title(f"Win Rate Distribution (by signer count, {subset})")
    axes[1].hist(summary["win_rate"].dropna(), bins=20, edgecolor="black", alpha=0.7,
                 weights=summary.loc[summary["win_rate"].notna(), "sandwich_count"])
    axes[1].set_xlabel("Win Rate")
    axes[1].set_ylabel("Number of Sandwiches")
    axes[1].set_title(f"Win Rate Distribution (by sandwich count, {subset})")
    axes[1].yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"wr_distribution_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_wr_boxplot(summary, chart_dir, tag, subset):
    data, labels = [], []
    for bucket in [b[2] for b in COUNT_BUCKETS]:
        sub = summary[summary["count_bucket"] == bucket]["win_rate"].dropna()
        if len(sub) > 0:
            data.append(sub.values)
            labels.append(bucket)
    if not data:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    for patch in bp["boxes"]:
        patch.set_facecolor("#4C72B0")
        patch.set_alpha(0.7)
    ax.set_xlabel("Sandwich Count Bucket")
    ax.set_ylabel("Win Rate")
    ax.set_title(f"Win Rate by Count Bucket ({subset})")
    ax.set_ylim(-0.05, 1.05)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"wr_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_profit_bar(summary, chart_dir, tag, subset):
    sol_profits, usd_profits, labels = [], [], []
    for bucket in [b[2] for b in COUNT_BUCKETS]:
        sub = summary[summary["count_bucket"] == bucket]
        if len(sub) > 0:
            sol_profits.append(sub["sol_total_profit"].sum())
            usd_profits.append(sub["usd_total_profit"].sum(min_count=1))
            labels.append(bucket)
    if not labels:
        return
    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    ax1.bar(x, sol_profits, color=["#2ca02c" if v >= 0 else "#d62728" for v in sol_profits],
            edgecolor="black", alpha=0.8)
    ax1.set_xticks(x); ax1.set_xticklabels(labels, rotation=30)
    ax1.set_ylabel("SOL Profit"); ax1.set_title(f"SOL Profit by Count Bucket ({subset})")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.axhline(y=0, color="black", linewidth=0.5)
    ax2.bar(x, usd_profits, color=["#2ca02c" if v >= 0 else "#d62728" for v in usd_profits],
            edgecolor="black", alpha=0.8)
    ax2.set_xticks(x); ax2.set_xticklabels(labels, rotation=30)
    ax2.set_ylabel("USD Profit"); ax2.set_title(f"USD Profit by Count Bucket ({subset})")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax2.axhline(y=0, color="black", linewidth=0.5)
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"profit_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_bucket_signer_sandwich_count(summary, chart_dir, tag, subset):
    signer_counts, sandwich_counts, labels = [], [], []
    for bucket in [b[2] for b in COUNT_BUCKETS]:
        sub = summary[summary["count_bucket"] == bucket]
        if len(sub) > 0:
            signer_counts.append(len(sub))
            sandwich_counts.append(int(sub["sandwich_count"].sum()))
            labels.append(bucket)
    if not labels:
        return
    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    ax1.bar(x, signer_counts, color="#4C72B0", edgecolor="black", alpha=0.8)
    ax1.set_xticks(x); ax1.set_xticklabels(labels, rotation=30)
    ax1.set_ylabel("Number of Signers"); ax1.set_title(f"Signers per Count Bucket ({subset})")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax2.bar(x, sandwich_counts, color="#DD8452", edgecolor="black", alpha=0.8)
    ax2.set_xticks(x); ax2.set_xticklabels(labels, rotation=30)
    ax2.set_ylabel("Number of Sandwiches"); ax2.set_title(f"Sandwiches per Count Bucket ({subset})")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"count_by_bucket_{subset}_{tag}.png"), dpi=150)
    plt.close()


def _plot_sc_and_gaps(per_sandwich, summary, chart_dir, tag):
    """New: what S1 and S2 actually produced."""
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    sc = per_sandwich.loc[~per_sandwich["sc_anomaly"], "sc"].dropna()
    if len(sc):
        axes[0].hist(sc, bins=50, range=(0, 1), edgecolor="black", alpha=0.75,
                     color="#4C72B0")
    axes[0].axvline(SC_MIN, color="red", ls="--", lw=1)
    axes[0].axvline(SC_MAX, color="red", ls="--", lw=1)
    axes[0].set_xlabel("SC (scoreable sandwiches only)")
    axes[0].set_ylabel("Sandwiches")
    axes[0].set_title("Slippage consumption, admissible band")
    axes[0].yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    for i, side in enumerate(("front", "back"), start=1):
        v = per_sandwich[f"{side}_gap"].dropna()
        if len(v) == 0:
            continue
        v = v.astype("int64").clip(upper=200)
        axes[i].hist(v, bins=100, edgecolor="none", alpha=0.8, color="#DD8452")
        axes[i].set_xlabel(f"{side}_gap (tx units, clipped at 200)")
        axes[i].set_ylabel("Sandwiches")
        axes[i].set_title(f"{side}_gap distribution")
        axes[i].yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    plt.tight_layout()
    plt.savefig(os.path.join(chart_dir, f"sc_and_gaps_{tag}.png"), dpi=150)
    plt.close()


# ── Epoch Processing ─────────────────────────────────────────────────────────

def process_epoch(client, epoch, scope, category, schema, args, diag, token_prices):
    start_slot = epoch * SLOTS_PER_EPOCH
    end_slot = (epoch + 1) * SLOTS_PER_EPOCH
    tx_cols = schema.get("sandwich_txs", set())
    has_pool_dex = "poolDex" in tx_cols

    ts_bounds = fetch_timestamp_bounds(client, start_slot, end_slot)
    sandwiches = fetch_sandwiches(client, scope, start_slot, end_slot)
    if len(sandwiches) == 0:
        return pd.DataFrame(), None

    leg_txs = fetch_leg_txs(client, scope, start_slot, end_slot, ts_bounds, has_pool_dex)
    victim_txs = fetch_victim_txs(client, scope, start_slot, end_slot, ts_bounds)

    sandwiches = _shuffle(sandwiches, args.shuffle_seed, 1)
    leg_txs = _shuffle(leg_txs, args.shuffle_seed, 2)
    victim_txs = _shuffle(victim_txs, args.shuffle_seed, 3)

    front_txs = leg_txs[leg_txs["type"] == "frontRun"]
    back_txs = leg_txs[leg_txs["type"] == "backRun"]

    leg_slots = [d["slot"] for d in (front_txs, back_txs, victim_txs) if len(d)]
    lo_slot = int(min(int(s.min()) for s in leg_slots)) if leg_slots else start_slot
    hi_slot = int(max(int(s.max()) for s in leg_slots)) if leg_slots else end_slot - 1

    slot_tx_counts = fetch_slot_tx_counts(client, lo_slot, hi_slot)
    jito_sets = fetch_jito_verdicts(args.database, epoch)
    leg_fees = fetch_leg_fees(client, scope, start_slot, end_slot, lo_slot, hi_slot)

    metrics = compute_per_sandwich_metrics(
        sandwiches, front_txs, back_txs, victim_txs, jito_sets,
        slot_tx_counts, leg_fees, token_prices, category, diag, has_pool_dex)

    entity_inputs = None
    if category == "diff_signer_owner":
        entity_inputs = extract_entity_inputs(front_txs, back_txs, epoch)

    diag["fee_sol_total"] = float(leg_fees.sum()) if len(leg_fees) else 0.0
    diag["jito_colocated"] = len(jito_sets[0])
    diag["jito_verified"] = len(jito_sets[1])
    diag["front_legs"] = len(front_txs)
    diag["back_legs"] = len(back_txs)
    return metrics, entity_inputs


# ── Dry run ──────────────────────────────────────────────────────────────────

def dry_run(client, args, scope, schema, database):
    print(f"\n{'=' * 80}")
    print(f"  DRY RUN — row counts and column availability, nothing computed")
    print(f"{'=' * 80}")

    required = {
        "sandwiches": ["sandwichId", "crossBlock", "crossLeader", "slot", "timestamp",
                       "tokenA", "tokenB", "victimCount", "adverseCount",
                       "multiFrontRun", "multiBackRun", "frontCount", "backCount",
                       "profitA", "signerSame", "ownerSame"],
        "sandwich_txs": ["sandwichId", "type", "slot", "position", "signature",
                         "signers", "ownersOfB", "inBundle", "slippageLimitType",
                         "slippageLimitAmount", "slippageActualAmount", "poolDex"],
        "slot_txs": ["slot", "txCount"],
        "jito_bundles": ["bundleId", "slot", "transactions"],
    }
    print(f"\n  --- column availability in `{database}` ---")
    for table, cols in required.items():
        have = schema.get(table, set())
        if not have:
            print(f"    {table:16s} TABLE MISSING")
            continue
        missing = [c for c in cols if c not in have]
        print(f"    {table:16s} {len(have):>3} columns; "
              f"{'all required present' if not missing else 'MISSING: ' + ','.join(missing)}")
    if "poolDex" in schema.get("sandwich_txs", set()):
        n = client.query_df("SELECT countIf(poolDex != '') AS n, count() AS t "
                            "FROM sandwich_txs WHERE type='frontRun' "
                            f"AND slot >= {args.start_epoch * SLOTS_PER_EPOCH} "
                            f"AND slot < {(args.start_epoch + 1) * SLOTS_PER_EPOCH}")
        filled, total = int(n["n"].iloc[0]), int(n["t"].iloc[0])
        print(f"    poolDex populated on frontRun legs (epoch {args.start_epoch}): "
              f"{filled:,}/{total:,}")
        if total and filled == 0:
            print("      -> pool_dex_* features will be emitted as NaN on this database.")

    print(f"\n  --- scope ---")
    print("   " + scope.replace("\n", "\n   "))

    print(f"\n  {'epoch':>6} {'sandwiches':>12} {'front+back':>12} {'victims':>12} "
          f"{'slot_txs':>10} {'jito rows':>12}")
    print(f"  {'-' * 70}")
    tot = defaultdict(int)
    for epoch in range(args.start_epoch, args.end_epoch + 1):
        s0, s1 = epoch * SLOTS_PER_EPOCH, (epoch + 1) * SLOTS_PER_EPOCH
        ts = fetch_timestamp_bounds(client, s0, s1)
        tsp = (f"AND sandwichTimestamp BETWEEN '{ts[0]}' AND '{ts[1]}'" if ts else "")
        sub = _scope_subquery(scope, s0, s1)
        r = client.query_df(f"""
            SELECT
              (SELECT count() FROM sandwiches WHERE {scope} AND slot>={s0} AND slot<{s1}) AS n_sw,
              (SELECT count() FROM sandwich_txs WHERE type IN ('frontRun','backRun') {tsp}
                 AND sandwichId IN ({sub})) AS n_leg,
              (SELECT count() FROM sandwich_txs WHERE type='victim' {tsp}
                 AND sandwichId IN ({sub})) AS n_vic,
              (SELECT count() FROM slot_txs WHERE slot>={s0} AND slot<{s1}) AS n_slot,
              (SELECT count() FROM jito_bundles WHERE slot>={s0} AND slot<{s1}) AS n_jito
        """)
        row = {k: int(r[k].iloc[0]) for k in ["n_sw", "n_leg", "n_vic", "n_slot", "n_jito"]}
        for k, v in row.items():
            tot[k] += v
        cov = row["n_slot"] / SLOTS_PER_EPOCH
        print(f"  {epoch:>6} {row['n_sw']:>12,} {row['n_leg']:>12,} {row['n_vic']:>12,} "
              f"{row['n_slot']:>10,} {row['n_jito']:>12,}   slot coverage {cov:.4f}"
              + ("  <-- BELOW 0.99" if cov < 0.99 else ""))
    print(f"  {'-' * 70}")
    print(f"  {'TOTAL':>6} {tot['n_sw']:>12,} {tot['n_leg']:>12,} {tot['n_vic']:>12,} "
          f"{tot['n_slot']:>10,} {tot['n_jito']:>12,}")
    print("\n  (jito rows are the raw bundle rows in range; the pipeline never downloads "
          "them — the signature join runs server-side.)")


# ── Main ─────────────────────────────────────────────────────────────────────

MODEL_FEATURES = [
    "sandwich_count", "sandwich_frequency", "interval_cv", "burstiness",
    "duty_cycle", "hour_entropy", "win_rate", "sol_avg_profit",
    "sol_profit_top1_share", "jito_rate", "jito_count",
    "front_gap_p50", "front_gap_le1_ratio", "front_gap_cv", "back_gap_p50",
    "mean_SC", "sc_std", "sc_coverage", "unprotected_rate", "sc_anomaly_rate", "shape_entropy", "pool_dex_top_share",
]


def main():
    args = parse_args()
    global _SHUFFLE_SEED
    _SHUFFLE_SEED = args.shuffle_seed
    t_start = time.time()
    category = args.category
    scope = build_scope(category, args.cross_leader)

    client = get_client(args.database)
    database = client.database
    _DB_FOR_CACHE[0] = database          # S4a token-account cache path, see `anchor_sets`

    print(f"=== Phase 1: Signer Data Preparation & Summary ===")
    print(f"Database:      {database}")
    print(f"Category:      {category}")
    print(f"Cross-leader:  {args.cross_leader}")
    print(f"Epoch range:   {args.start_epoch}-{args.end_epoch} "
          f"({args.end_epoch - args.start_epoch + 1} epochs)")
    print(f"SC band (S3):  [{SC_MIN}, {SC_MAX}]  vs Go: {assert_go_band_matches()}")
    if category == "diff_signer_owner":
        print(f"Entity merge:  {args.entity_merge}"
              + (f" (degree cap {args.entity_degree_cap})"
                 if args.entity_merge == "owner+guard" else ""))
    if args.shuffle_seed is not None:
        print(f"Shuffle seed:  {args.shuffle_seed}  (determinism check — output must "
              f"match an unshuffled run)")

    schema = probe_columns(client, database)
    if args.dry_run:
        dry_run(client, args, scope, schema, database)
        return

    if "poolDex" not in schema.get("sandwich_txs", set()):
        warnings.warn(f"{database}.sandwich_txs has no poolDex column; "
                      f"pool_dex_* features will be NaN")

    # Prices are needed INSIDE the epoch loop, not after it. `is_profitable` is now net of fees,
    # which requires pricing tokenA against lamports, and signer features are aggregated per epoch
    # -- so a price table loaded at step 3 would arrive after every win rate had been computed.
    print("\nLoading token prices...")
    token_prices = load_token_prices()
    sol_price = token_prices.get("SOL")
    if not sol_price or not np.isfinite(sol_price):
        raise SystemExit("token price table has no usable SOL price; every net-profit figure "
                         "would be NaN. Fix dataset/aux/token_prices.csv before re-running.")
    print(f"  {len(token_prices):,} tokens loaded, SOL=${sol_price:.2f}")

    # ── Step 1: per-epoch processing ──────────────────────────────────────
    all_metrics, all_entity_inputs = [], []
    diag = {"database": database, "category": category,
            "cross_leader": args.cross_leader, "epochs": {}}
    for epoch in range(args.start_epoch, args.end_epoch + 1):
        t0 = time.time()
        ediag = {}
        em, ei = process_epoch(client, epoch, scope, category, schema, args, ediag,
                               token_prices)
        dt = time.time() - t0
        ediag["seconds"] = round(dt, 2)
        diag["epochs"][epoch] = ediag
        if len(em) > 0:
            n_signers = em["signer"].nunique() if category != "diff_signer_owner" else -1
            print(f"[Epoch {epoch}] {len(em):>9,} sandwiches, "
                  f"{ediag.get('victim_legs', 0):>9,} victim legs, "
                  + (f"{n_signers:>7,} signers, " if n_signers >= 0 else "")
                  + f"{dt:6.2f}s")
            all_metrics.append(em)
            if ei is not None and len(ei):
                all_entity_inputs.append(ei)
        else:
            print(f"[Epoch {epoch}] no sandwiches in scope ({dt:.2f}s)")

    if not all_metrics:
        print("\nNothing to do: no sandwiches in scope.")
        return

    # Canonical row order BEFORE any float aggregation: sandwichId is a unique hash, so
    # sort_index is a total order and float accumulation stops depending on the order
    # ClickHouse read its parts in.
    per_sandwich = pd.concat(all_metrics, ignore_index=False).sort_index(
        kind="mergesort")
    del all_metrics

    # Roll per-epoch diagnostics up.
    vr = defaultdict(int)
    vt = 0
    for e in diag["epochs"].values():
        for k, v in e.get("victim_sc_reasons", {}).items():
            vr[k] += v
        vt += e.get("victim_legs", 0)
    diag["victim_sc_reasons_total"] = dict(vr)
    diag["victim_legs_total"] = vt
    for k in ("front_gap_long_gap_fallback", "back_gap_long_gap_fallback"):
        diag[k + "_total"] = sum(e.get(k, 0) for e in diag["epochs"].values())
    for k in ("front_gap_max_slot_span", "back_gap_max_slot_span"):
        diag[k + "_max"] = max([e.get(k, 0) for e in diag["epochs"].values()] or [0])

    # ── Step 1b: global entity resolution (S4) ────────────────────────────
    entity_sizes = None
    if category == "diff_signer_owner":
        if not all_entity_inputs:
            print("\nNo entity inputs; cannot resolve diff-signer entities.")
            return
        entity_inputs = pd.concat(all_entity_inputs)
        anchor_entity, key_to_entity, entity_sizes, deg = report_entity_merging(
            entity_inputs, args.entity_merge, args.entity_degree_cap, diag)
        per_sandwich["signer"] = anchor_entity.reindex(per_sandwich.index)
        missing = per_sandwich["signer"].isna().sum()
        if missing:
            warnings.warn(f"{missing} diff-signer sandwiches had no anchor key; dropped")
            per_sandwich = per_sandwich[per_sandwich["signer"].notna()]

    print(f"\nTotal: {len(per_sandwich):,} sandwiches, "
          f"{per_sandwich['signer'].nunique():,} "
          f"{'entities' if category == 'diff_signer_owner' else 'signers'}")

    # ── Step 2: signer aggregation (S5) ───────────────────────────────────
    print("\nAggregating to signer level...")
    t0 = time.time()
    signer_df = aggregate_to_signer(per_sandwich, diag)
    print(f"  {len(signer_df):,} rows, {len(signer_df.columns)} columns "
          f"({time.time() - t0:.2f}s)")

    if category == "diff_signer_owner":
        # Wallet-rotation size, measured on FEE PAYERS, not on every co-signer:
        # counting co-signers would inflate every entity that ever touched a
        # shared platform key.
        fp = entity_inputs[["feepayers"]].copy()
        fp["entity"] = anchor_entity.reindex(fp.index)
        ex = fp.explode("feepayers").dropna()
        n_sign = ex.groupby("entity")["feepayers"].nunique()
        cs = entity_inputs[["all_signers"]].copy()
        cs["entity"] = anchor_entity.reindex(cs.index)
        exs = cs.explode("all_signers").dropna()
        n_all = exs.groupby("entity")["all_signers"].nunique()
        signer_df["n_signers"] = n_sign.reindex(signer_df.index).fillna(1).astype("int64")
        signer_df["n_cosigners"] = (n_all - n_sign).reindex(
            signer_df.index).fillna(0).astype("int64")
        signer_df["n_anchor_keys"] = entity_sizes.reindex(
            signer_df.index).fillna(1).astype("int64")
        # A single high-degree anchor key produces the worst over-merge while scoring
        # n_anchor_keys = 1, so flag the population share and the wallet count as well.
        signer_df["entity_sandwich_share"] = (
            signer_df["sandwich_count"] / max(len(per_sandwich), 1))
        signer_df["entity_oversized"] = (
            (signer_df["n_anchor_keys"] > ENTITY_SIZE_FLAG)
            | (signer_df["entity_sandwich_share"] > ENTITY_SHARE_FLAG)
            | (signer_df["n_signers"] > ENTITY_WALLET_FLAG))
        n_flag = int(signer_df["entity_oversized"].sum())
        print(f"  entities flagged oversized (>{ENTITY_SIZE_FLAG} anchor keys OR "
              f">{ENTITY_SHARE_FLAG:.0%} of the population OR >{ENTITY_WALLET_FLAG:,} "
              f"fee payers): {n_flag:,}")
        for ent, row in signer_df[signer_df["entity_oversized"]].sort_values(
                "sandwich_count", ascending=False).head(10).iterrows():
            print(f"    {ent}  sandwiches={int(row['sandwich_count']):,} "
                  f"({row['entity_sandwich_share']:.1%})  fee_payers="
                  f"{int(row['n_signers']):,}  anchor_keys={int(row['n_anchor_keys'])}")
        if n_flag:
            warnings.warn(
                f"{n_flag} diff-signer entities exceed the S4 concentration guard; "
                f"their anchor key merges more wallets than a single operator "
                f"plausibly rotates. Re-run with --entity-merge owner+guard "
                f"--entity-degree-cap <n> and compare.")

    # ── Step 2b: profit floor (S5a) ───────────────────────────────────────
    # After the entity diagnostics above, deliberately: the S4 concentration guard has to see the
    # WHOLE population, or an oversized entity could be hidden by dropping the small entities it
    # was being compared against.
    per_sandwich, signer_df = apply_profit_floor(
        per_sandwich, signer_df, args.profit_floor_usd, diag,
        label="entities" if category == "diff_signer_owner" else "signers")
    if len(signer_df) == 0:
        # Write the empty frames anyway, with their real schema. "No attacker cleared the floor"
        # is a RESULT and the downstream scripts have to be able to read it; returning early
        # instead leaves a hole that phase 3 reports as "run phase 1 first", which is a different
        # and false statement. `diff_signer_transfer` is in exactly this state over 946-990.
        out_dir = os.path.join(args.out_root, category, database)
        os.makedirs(out_dir, exist_ok=True)
        tag = f"{args.start_epoch}_{args.end_epoch}"
        if args.cross_leader != "exclude":
            tag += f"_cl-{args.cross_leader}"
        per_sandwich.to_parquet(f"{out_dir}/per_sandwich_metrics_{tag}.parquet")
        signer_df.to_parquet(f"{out_dir}/signer_features_{tag}.parquet")
        signer_df.to_csv(f"{out_dir}/signer_features_{tag}.csv")
        diag["runtime_seconds"] = round(time.time() - t_start, 2)
        diag["sandwiches"] = 0
        with open(f"{out_dir}/diagnostics_{tag}.json", "w") as fh:
            json.dump(diag, fh, indent=2, default=str)
        print(f"\nProfit floor removed every signer. Wrote EMPTY (0-row) outputs to {out_dir}/ "
              f"so downstream steps read '0 attackers' rather than 'phase 1 not run'.")
        return

    # ── Step 3: USD profit ────────────────────────────────────────────────
    per_sandwich = compute_usd_profit(per_sandwich, token_prices)
    print(f"  Price coverage: {per_sandwich['usd_profit'].notna().mean() * 100:.1f}% "
          f"of sandwiches")

    # ── Step 4: summary ───────────────────────────────────────────────────
    summary_all = build_signer_summary(per_sandwich, signer_df, token_prices)

    # ── Step 5: outputs ───────────────────────────────────────────────────
    out_dir = os.path.join(args.out_root, category, database)
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{args.start_epoch}_{args.end_epoch}"
    if args.cross_leader != "exclude":
        tag += f"_cl-{args.cross_leader}"

    ps_out = per_sandwich
    ps_out.to_parquet(f"{out_dir}/per_sandwich_metrics_{tag}.parquet")
    signer_df.to_parquet(f"{out_dir}/signer_features_{tag}.parquet")
    signer_df.to_csv(f"{out_dir}/signer_features_{tag}.csv")
    summary_all.to_csv(f"{out_dir}/signer_summary_all_{tag}.csv")
    print(f"\nSaved to {out_dir}/:")
    print(f"  per_sandwich_metrics_{tag}.parquet  ({len(per_sandwich):,} rows, "
          f"{len(per_sandwich.columns)} cols)")
    print(f"  signer_features_{tag}.parquet       ({len(signer_df):,} rows, "
          f"{len(signer_df.columns)} cols)")
    print(f"  signer_summary_all_{tag}.csv        ({len(summary_all):,} rows)")

    if category == "diff_signer_owner":
        entity_df = pd.DataFrame(
            sorted(key_to_entity.items()), columns=["anchor_key", "entity"])
        entity_df.to_csv(f"{out_dir}/signer_entity_map_{tag}.csv", index=False)
        es = entity_sizes.rename("anchor_key_count").sort_values(
            ascending=False).rename_axis("entity").reset_index()
        es = es.sort_values(["anchor_key_count", "entity"], ascending=[False, True])
        es.to_csv(f"{out_dir}/entity_sizes_{tag}.csv", index=False)
        print(f"  signer_entity_map_{tag}.csv         ({len(entity_df):,} anchor keys)")
        print(f"  entity_sizes_{tag}.csv              ({len(es):,} entities, "
              f"{int((es['anchor_key_count'] > 1).sum())} multi-key)")

    diag["runtime_seconds"] = round(time.time() - t_start, 2)
    diag["sandwiches"] = int(len(per_sandwich))
    with open(f"{out_dir}/diagnostics_{tag}.json", "w") as fh:
        json.dump(diag, fh, indent=2, default=str)
    print(f"  diagnostics_{tag}.json")

    # ── Step 6: reports ───────────────────────────────────────────────────
    print_overall_summary(summary_all, per_sandwich,
                          label="ALL ENTITIES" if category == "diff_signer_owner"
                          else "ALL SIGNERS")
    print_sc_and_gap_report(per_sandwich, signer_df, diag)

    # ── Step 7: charts ────────────────────────────────────────────────────
    if not args.no_charts:
        print("\nGenerating charts...")
        generate_charts(summary_all, per_sandwich, out_dir, tag)

    # ── Step 8: feature summary ───────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print(f"  MODEL FEATURE SUMMARY")
    print(f"{'=' * 80}")
    for col in MODEL_FEATURES:
        if col in signer_df.columns:
            s = pd.to_numeric(signer_df[col], errors="coerce").dropna()
            if len(s) > 0:
                print(f"  {col:26s}  mean={s.mean():13.4f}  median={s.median():13.4f}  "
                      f"non-null={len(s):>9,}")
            else:
                print(f"  {col:26s}  (all null)")
        else:
            print(f"  {col:26s}  (absent)")

    print(f"\n=== Done in {diag['runtime_seconds']:.1f}s ===")


if __name__ == "__main__":
    main()
