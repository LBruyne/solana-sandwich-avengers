"""Definitions shared by scripts 0, 1, 2 and 3.

`tag_for` / `phase1_paths` -- phase 1 writes under `<cat>/<database>/` and encodes the
cross-leader variant in the filename. `946_960` is a prefix of `946_960_cl-include`, so
tags are matched exactly and the file must exist.

`load_token_prices` -- the price table, `data/token_prices/prices.csv`, written by
`0_crawl_token_price.py` and read from nowhere else. A token outside it has no price, and
every quantity derived from it is NaN, not 0.

`load_bundle_verdicts` / `verified_bundle_sandwiches` -- the bundle-sandwich definition,
read from `0_crawl_jito_bundle_ids.py`'s CSVs.

`leader_geometry` -- where each sandwich's legs sit relative to the block producers.
Computed from `slot_leaders` and cached.
"""
import glob
import os

import numpy as np
import pandas as pd

from .db import get_client

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHASE1_ROOT = os.path.join(
    REPO, "sandwich-intent", "data", "1_signer_data_preparation_and_summary")
PRICE_CSV = os.path.join(REPO, "sandwich-intent", "data", "token_prices", "prices.csv")
GEOM_DIR = os.path.join(REPO, "sandwich-intent", "data", "geometry")

SLOTS_PER_EPOCH = 432_000
# A Solana leader holds four consecutive slots; a detection window spans two rotations.
SLOTS_PER_LEADER = 4
WINDOW_SLACK = 2 * SLOTS_PER_LEADER


def tag_for(start_epoch, end_epoch, cross_leader):
    """Phase-1 filename tag. `exclude` carries no suffix — that is phase 1's own convention."""
    t = f"{start_epoch}_{end_epoch}"
    return t if cross_leader == "exclude" else f"{t}_cl-{cross_leader}"


def phase1_paths(category, database, tag):
    d = os.path.join(PHASE1_ROOT, category, database)
    ps = os.path.join(d, f"per_sandwich_metrics_{tag}.parquet")
    sf = os.path.join(d, f"signer_features_{tag}.parquet")
    for p in (ps, sf):
        if not os.path.exists(p):
            raise SystemExit(
                f"missing {p}\nRun phase 1 for {database} / {category} / tag {tag} first.")
    return ps, sf


def load_phase1(category, database, tag):
    ps_path, sf_path = phase1_paths(category, database, tag)
    ps = pd.read_parquet(ps_path)
    sf = pd.read_parquet(sf_path)
    missing = [c for c in ("front_gap", "sc", "sc_anomaly", "sc_unprotected", "jito_bundle")
               if c not in ps.columns]
    if missing:
        raise SystemExit(
            f"{ps_path} is missing {missing}; it predates the current phase-1 schema. "
            f"Re-run phase 1 for this range and cross-leader variant.")
    return ps, sf


def load_token_prices(path=PRICE_CSV):
    """token -> USD, from the ONE table `0_crawl_token_price.py` writes.

    A token absent from the table, or present with an empty price, is simply ABSENT from the
    returned dict -- an unparseable price is dropped here so it takes the same path as a missing
    row. Absent means UNKNOWN, not 0: every consumer looks the token up with `.map()`, which
    yields NaN, and `usd_series` below depends on that. Do not "helpfully" turn this into a
    defaultdict or a `.get(tok, 0.0)` lookup; that would convert a coverage limit into measured
    zeros and make every mean a statement about the price table (see `usd_series`).
    """
    p = pd.read_csv(path)
    p["usd_price"] = pd.to_numeric(p["usd_price"], errors="coerce")
    p = p[p["usd_price"].notna()]
    return dict(zip(p["token"].astype(str), p["usd_price"].astype(float)))


def usd_series(ps, token_prices):
    """Gross USD per sandwich; NaN where tokenA has no price.

    NaN rather than 0, because the two say different things and only one of them is true. A
    sandwich in an unpriced token did not earn nothing -- its earnings are UNKNOWN, and the
    top-N price table leaves a large minority of the corpus in that state. Scoring them 0 would
    silently convert a coverage limit into millions of measured zeros, and any mean taken over
    the result would be a statement about the price table rather than about attackers. Consumers
    must aggregate with skipna (pandas default) so those rows leave numerator and denominator
    together.
    """
    return ps["profit"] * ps["token_a"].map(token_prices)


# ── Jito bundle verdicts ─────────────────────────────────────────────────────

BUNDLE_DIR = os.path.join(REPO, "sandwich-intent", "data", "jito_bundle_ids")

# Epochs dropped from every bundle-sandwich figure, applied here so no consumer can
# disagree about whether they are in. Epoch 961 holds a single coordinated pump.fun
# launch-sniping event: 12.3 % of the bundle sandwiches and 93.1 % of the SOL, from 47
# signers of which 46 never appear again in the window.
EXCLUDED_EPOCHS = (961,)

# A REPORTING threshold, not part of the bundle-sandwich definition: a token missing from
# the price table scores $0, so gating on this would make membership a function of price
# coverage.
BUNDLE_USD_MIN = 10.0


def load_bundle_verdicts(start_epoch, end_epoch, bundle_dir=BUNDLE_DIR):
    """Sandwich IDs where ONE bundleId holds a front-run, a victim and a back-run.

    Epochs in `EXCLUDED_EPOCHS` are dropped here, so every downstream count is already net of them.

    Read from the CSVs `0_crawl_jito_bundle_ids.py` produced, NOT from `jito_bundles`: bundle
    CONTENT is reclaimed per epoch once `inBundle` marking is done, so the table only still holds
    946-960 and a table-based Track 1 silently loses 86 % of the verdicts. This is the same source
    `0_report_data_overview.py` reads.
    """
    parts = []
    for path in sorted(glob.glob(os.path.join(bundle_dir, "bundle_sandwiches_*.csv"))):
        d = pd.read_csv(path)
        parts.append(d[(d["epoch"] >= start_epoch) & (d["epoch"] <= end_epoch)])
    if not parts:
        # Loud, because the silent version is worse: Track 1 would report zero Jito Bots and
        # look like a finding rather than a missing input.
        raise SystemExit(
            f"No bundle verdicts under {bundle_dir}/. Run 0_crawl_jito_bundle_ids.py for this "
            f"epoch range first -- without it Track 1 has no evidence and admits nobody.")
    v = pd.concat(parts, ignore_index=True).drop_duplicates("sandwichId")
    # Applied HERE, at the single point every consumer reads, so no script can quietly disagree
    # about whether 961 is in. See EXCLUDED_EPOCHS for why it is out.
    return v[~v["epoch"].isin(EXCLUDED_EPOCHS)]


def verified_bundle_sandwiches(database, start_epoch, end_epoch, bundle_dir=BUNDLE_DIR):
    """Verdict IDs narrowed by the two conditions that complete the definition:

      signerSame   the front and back runs carry the same signer
      profitA > 0  the attack made money in tokenA

    Epochs in EXCLUDED_EPOCHS are already dropped by `load_bundle_verdicts`.
    """
    v = load_bundle_verdicts(start_epoch, end_epoch, bundle_dir)
    if v.empty:
        return set()
    client = get_client(database)
    tmp = "default._verified_bundle_ids"
    client.command(f"DROP TABLE IF EXISTS {tmp}")
    client.command(f"CREATE TABLE {tmp} (sandwichId String) ENGINE=Memory")
    # Staged rather than inlined: 5,743 ids blow past max_query_size.
    client.insert(tmp, v[["sandwichId"]].values.tolist(), column_names=["sandwichId"])
    try:
        d = client.query_df(
            f"SELECT sandwichId FROM sandwiches "
            f"WHERE sandwichId IN (SELECT sandwichId FROM {tmp}) "
            f"AND signerSame AND profitA > 0")
    finally:
        client.command(f"DROP TABLE IF EXISTS {tmp}")
    # An empty ClickHouse result is a DataFrame with NO columns, so index by name only when there
    # are rows -- otherwise this raises KeyError instead of returning the empty set.
    return set(d["sandwichId"]) if len(d) else set()


# ── leader geometry ──────────────────────────────────────────────────────────

GEOM_COLS = ["sandwichId", "cross_block", "cross_leader", "n_victims",
             "v_front_leader", "v_back_leader", "first_v_front_leader", "v_same_block_as_front",
             "front_slot", "back_slot"]


def leader_geometry(database, start_epoch, end_epoch, refresh=False, verbose=True):
    """Per-sandwich leg positions relative to the block producers.

    Returns one row per sandwich with:
      cross_block, cross_leader   as the detector recorded them
      n_victims                   victim legs
      v_front_leader              victims produced by the FRONT-run's leader
      v_back_leader               victims produced by the BACK-run's leader
      first_v_front_leader        is the earliest victim (slot, position) under the front's leader
      v_same_block_as_front       victims in the front-run's own slot

    `frontLeader` / `backLeader` come from the detector; victim leaders are joined from
    `slot_leaders`. The two agreeing is checked once here rather than assumed — if the detector wrote
    its leader fields from a different source than this table, every downstream class is comparing
    two different things.
    """
    os.makedirs(GEOM_DIR, exist_ok=True)
    cache = os.path.join(GEOM_DIR, f"{database}_{start_epoch}_{end_epoch}.parquet")
    if os.path.exists(cache) and not refresh:
        return pd.read_parquet(cache)

    client = get_client(database)
    parts = []
    for ep in range(start_epoch, end_epoch + 1):
        s0, s1 = ep * SLOTS_PER_EPOCH, (ep + 1) * SLOTS_PER_EPOCH
        q = f"""
        WITH sw AS (
          -- max() collapses any duplicate sandwichId rows. Both flags are re-derived from the
          -- legs below; the stored ones are kept only to cross-check.
          SELECT sandwichId AS sid, max(frontLeader) AS stored_fl,
                 max(crossBlock) AS stored_xb, max(crossLeader) AS stored_xl
          FROM sandwiches WHERE slot >= {s0} AND slot < {s1} GROUP BY sandwichId
        ),
        legs AS (
          SELECT t.sandwichId AS sid, t.type AS ty, t.slot AS sl, t.position AS pos,
                 l.leader AS ld
          FROM sandwich_txs t
          LEFT JOIN slot_leaders l ON l.slot = t.slot
          WHERE t.slot >= {s0 - WINDOW_SLACK} AND t.slot < {s1 + WINDOW_SLACK}
            AND t.type IN ('frontRun', 'backRun', 'victim')
            AND t.sandwichId IN (SELECT sid FROM sw)
        ),
        ends AS (
          SELECT sid,
                 argMinIf(sl, (sl, pos), ty = 'frontRun') AS fslot,
                 argMaxIf(sl, (sl, pos), ty = 'backRun')  AS bslot,
                 argMinIf(ld, (sl, pos), ty = 'frontRun') AS fld,
                 argMaxIf(ld, (sl, pos), ty = 'backRun')  AS bld,
                 argMinIf(ld, (sl, pos), ty = 'victim')   AS v1ld,
                 countIf(ty = 'victim')                   AS nv
          FROM legs GROUP BY sid
        )
        SELECT sw.sid AS sandwichId,
               ends.fslot != ends.bslot AS cross_block,
               ends.fld != ends.bld AS cross_leader,
               ends.nv AS n_victims,
               countIf(g.ty = 'victim' AND g.ld = ends.fld) AS v_front_leader,
               countIf(g.ty = 'victim' AND g.ld = ends.bld) AS v_back_leader,
               any(ends.v1ld = ends.fld) AS first_v_front_leader,
               countIf(g.ty = 'victim' AND g.sl = ends.fslot) AS v_same_block_as_front,
               ends.fslot AS front_slot, ends.bslot AS back_slot,
               any(sw.stored_fl = '' OR sw.stored_fl = ends.fld) AS front_leader_agrees,
               any(sw.stored_fl = '') AS leader_derived,
               any(sw.stored_xl != (ends.fld != ends.bld)) AS xl_flag_disagrees,
               any(sw.stored_xb != (ends.fslot != ends.bslot)) AS xb_flag_disagrees
        FROM legs g
        INNER JOIN sw   ON sw.sid = g.sid
        INNER JOIN ends ON ends.sid = g.sid
        GROUP BY sw.sid, ends.fslot, ends.bslot, ends.fld, ends.bld, ends.nv
        """
        d = client.query_df(q)
        parts.append(d)
        if verbose:
            print(f"    epoch {ep}: {len(d):,} sandwiches", flush=True)
    geom = pd.concat(parts, ignore_index=True)

    # Leaders are DERIVED from slot_leaders, not read from the detector's frontLeader/backLeader
    # columns, so the answer does not depend on whether the detector that produced a given row
    # populated them. Where it did, the two must agree; where it did not, say so rather than let
    # an empty string read as a leader.
    bad = int((~geom["front_leader_agrees"].astype(bool)).sum())
    if bad:
        raise SystemExit(
            f"{bad:,} of {len(geom):,} sandwiches have a non-empty sandwiches.frontLeader that "
            f"disagrees with slot_leaders[front leg slot]. Two sources, two answers — resolve "
            f"before using the geometry.")
    derived = int(geom["leader_derived"].astype(bool).sum())
    flag_bad = int(geom["xl_flag_disagrees"].astype(bool).sum())
    if verbose and derived:
        print(f"    {derived:,} sandwiches had no stored leader; derived from slot_leaders")
    if verbose and flag_bad:
        print(f"    NOTE {flag_bad:,} sandwiches ({flag_bad / len(geom):.2%}) have a stored "
              f"crossLeader flag that disagrees with the derived leaders — the derived value is "
              f"used here")
    xb_bad = int(geom["xb_flag_disagrees"].astype(bool).sum())
    if verbose and xb_bad:
        print(f"    NOTE {xb_bad:,} sandwiches ({xb_bad / len(geom):.2%}) have a stored crossBlock "
              f"flag that disagrees with the derived leg slots — derived is used")
    geom = geom.drop(columns=["front_leader_agrees", "leader_derived", "xl_flag_disagrees",
                              "xb_flag_disagrees"])
    geom.to_parquet(cache, index=False)
    if verbose:
        print(f"    -> {cache}")
    return geom


# ── leader rotations, for the boundary-suppression null ──────────────────────

def leader_rotations(database, start_epoch, end_epoch, refresh=False, verbose=True):
    """(slot -> rotation start/end) over the produced slots one leader held consecutively.

    A leader normally holds four consecutive slots, but a validator scheduled twice in a row holds
    eight or twelve, and slots can be skipped. The rotation is therefore the maximal run of produced
    slots with the same leader, not `intDiv(slot, 4)`.
    """
    os.makedirs(GEOM_DIR, exist_ok=True)
    cache = os.path.join(GEOM_DIR, f"{database}_rotations_{start_epoch}_{end_epoch}.parquet")
    if os.path.exists(cache) and not refresh:
        return pd.read_parquet(cache)
    client = get_client(database)
    s0, s1 = start_epoch * SLOTS_PER_EPOCH, (end_epoch + 1) * SLOTS_PER_EPOCH
    d = client.query_df(
        f"SELECT slot, leader FROM slot_leaders WHERE slot >= {s0} AND slot < {s1} ORDER BY slot")
    # A new run starts when the leader changes or the slot sequence breaks.
    new = ((d["leader"] != d["leader"].shift())
           | (d["slot"] != d["slot"].shift() + 1)).to_numpy(dtype=bool)
    d["run"] = np.cumsum(new)
    ext = d.groupby("run")["slot"].agg(["min", "max", "size"])
    d["rot_start"] = d["run"].map(ext["min"])
    d["rot_end"] = d["run"].map(ext["max"])
    d["rot_n"] = d["run"].map(ext["size"])
    out = d[["slot", "leader", "rot_start", "rot_end", "rot_n"]]
    out.to_parquet(cache, index=False)
    if verbose:
        print(f"    {len(ext):,} rotations, produced-size median {ext['size'].median():.0f}"
              f"  -> {cache}")
    return out


def rotation_index(rot):
    """Per-slot rotation extent, keyed by slot, for `boundary_suppression`.

    Built once and passed in: the naive version rebuilt a 1.6 M-group lookup inside the per-attacker
    loop, which is the same work several hundred times over.
    """
    r = rot.set_index("slot")[["rot_start", "rot_end", "rot_n"]]
    r["contiguous"] = (r["rot_end"] - r["rot_start"] + 1) == r["rot_n"]
    return r


def boundary_suppression(sub, rot_idx, rot=None):
    """Does this attacker avoid letting its back-run cross into the next validator's block?

    Hold the front-to-back slot span fixed and slide the front-run uniformly over the produced slots
    of its own rotation; count how often the back-run would then land past the rotation's end. That
    expectation is what arbitrary placement predicts. A ratio below 1 means the attacker crosses less often than
    arbitrary placement predicts.

    For a contiguous rotation [s, e] the expectation is closed-form: placing the front at c crosses
    iff c + span > e, and the candidates are exactly [s, e], so the probability is
    min(span, k) / k. Rotations with a skipped slot inside are rare and fall back to the explicit
    candidate list, which is why `rot` is still accepted.

    Restricted to cross-block sandwiches — an in-block sandwich cannot cross by construction, and
    including them would dilute both sides by the same factor.

    Returns (observed cross-leader count, expected count, n cross-block sandwiches).
    """
    xb = sub[sub["cross_block"].fillna(False).astype(bool)]
    xb = xb[xb["front_slot"].notna() & xb["back_slot"].notna()]
    if xb.empty:
        return np.nan, np.nan, 0
    fs = xb["front_slot"].to_numpy()
    span = (xb["back_slot"] - xb["front_slot"]).to_numpy()
    r = rot_idx.reindex(fs)
    k = r["rot_n"].to_numpy(dtype="float64")
    end = r["rot_end"].to_numpy(dtype="float64")
    start = r["rot_start"].to_numpy(dtype="float64")
    contig = r["contiguous"].to_numpy(dtype=bool)

    exp = np.full(len(xb), np.nan)
    ok = contig & np.isfinite(k) & (k > 0)
    exp[ok] = np.minimum(span[ok], k[ok]) / k[ok]

    # Gapped rotations: enumerate the produced slots that actually exist.
    gap = (~contig) & np.isfinite(k)
    if gap.any() and rot is not None:
        by = rot.groupby("rot_start")["slot"].apply(list)
        for i2 in np.flatnonzero(gap):
            cand = by.get(start[i2])
            exp[i2] = (float(np.mean([(c + span[i2]) > end[i2] for c in cand]))
                       if cand else np.nan)

    obs = xb["cross_leader"].fillna(False).astype(bool).to_numpy()
    good = np.isfinite(exp)
    return float(obs[good].sum()), float(exp[good].sum()), int(good.sum())


# Three mutually exclusive shapes by how far the sandwich reaches.
GEOM_CLASSES = ["single_slot", "same_leader_multi_slot", "cross_leader"]
# Sub-shapes of a cross-leader sandwich, by where its victims sit. The first three partition the
# cross-leader population; the last two are the single-victim slice, reported separately because
# that is the case where "did the attacker actually front-run this victim" has no ambiguity.
XL_CLASSES = ["xl_all_victims_front_leader", "xl_first_victim_front_leader",
              "xl_no_victim_front_leader", "xl_single_victim_same_leader",
              "xl_single_victim_diff_leader"]


def add_geometry_classes(ps, geom):
    """Join geometry onto per-sandwich metrics and add the class flags.

    `ps` is indexed by sandwichId (phase 1's convention).
    """
    g = geom.set_index("sandwichId")
    # Phase 1 carries cross_block/cross_leader from the stored detector columns; the geometry
    # table re-derives them from the legs' own slots and producers. Join the geometry's copies
    # under a suffix, assert they agree, then drop them.
    take = [c for c in GEOM_COLS if c != "sandwichId"]
    out = ps.drop(columns=[c for c in ("cross_block", "cross_leader") if c in ps.columns])
    out = out.join(g[take], how="left")

    xb = out["cross_block"].fillna(False).astype(bool)
    xl = out["cross_leader"].fillna(False).astype(bool)
    out["geom_class"] = np.where(xl, "cross_leader",
                                 np.where(xb, "same_leader_multi_slot", "single_slot"))

    nv = out["n_victims"].fillna(0)
    vf = out["v_front_leader"].fillna(0)
    single = xl & (nv == 1)
    out["xl_all_victims_front_leader"] = xl & (nv > 0) & (vf == nv)
    out["xl_first_victim_front_leader"] = xl & out["first_v_front_leader"].fillna(False).astype(bool)
    out["xl_no_victim_front_leader"] = xl & (nv > 0) & (vf == 0)
    out["xl_single_victim_same_leader"] = single & (vf == 1)
    out["xl_single_victim_diff_leader"] = single & (vf == 0)
    return out


def geometry_profile(sub, usd_col="usd", pop_all_front_by_nv=None, rot_idx=None, rot=None):
    """Counts, shares and profit for one attacker's sandwiches, by geometry class.

    `xl_all_victims_front_leader_share_of_xl` falls MECHANICALLY with victim count: all k
    victims have to land on one side of the boundary, so an attacker sandwiching one victim at
    a time scores high on it for arithmetic reasons. Compare attackers on
    `..._vs_expected`, which divides by the direct standardization onto the attacker's own
    victim-count mix.

    `victim_in_front_block_share` asks whether the front-run is in the victim's own block; the
    boundary-suppression columns ask whether the attacker's back-run crosses into the next
    validator's block less often than its own span and rotation would produce by chance.

    Returned as a flat dict so a caller can build one row per attacker.
    """
    n = len(sub)
    row = {"sandwich_count": n}
    for cls in GEOM_CLASSES:
        m = sub["geom_class"] == cls
        k = int(m.sum())
        row[f"{cls}_n"] = k
        row[f"{cls}_share"] = k / n if n else np.nan
        row[f"{cls}_usd"] = float(sub.loc[m, usd_col].sum())
        row[f"{cls}_usd_avg"] = float(sub.loc[m, usd_col].mean()) if k else np.nan
    xl_n = int((sub["geom_class"] == "cross_leader").sum())
    xl = sub[sub["geom_class"] == "cross_leader"]

    # Leg-level: is the front-run in the victim's own block? Not a leader statistic, and the one
    # that survives standardization.
    nvsum = float(sub["n_victims"].fillna(0).sum())
    row["victim_in_front_block_n"] = float(sub["v_same_block_as_front"].fillna(0).sum())
    row["victim_in_front_block_share"] = (row["victim_in_front_block_n"] / nvsum
                                          if nvsum else np.nan)
    row["victims_per_xl_sandwich"] = (float(xl["n_victims"].fillna(0).mean())
                                      if xl_n else np.nan)

    for cls in XL_CLASSES:
        m = sub[cls].fillna(False).astype(bool)
        k = int(m.sum())
        row[f"{cls}_n"] = k
        # Denominator is the cross-leader population, so the share reads as "of this attacker's
        # cross-leader sandwiches, how many look like this".
        row[f"{cls}_share_of_xl"] = k / xl_n if xl_n else np.nan
        row[f"{cls}_usd"] = float(sub.loc[m, usd_col].sum())
        row[f"{cls}_usd_avg"] = float(sub.loc[m, usd_col].mean()) if k else np.nan

    # Direct standardization of the all-victims-under-front share onto this attacker's own
    # victim-count mix, so the comparison stops being a comparison of victim counts.
    if pop_all_front_by_nv is not None and xl_n:
        nv = xl["n_victims"].fillna(0).astype(int).clip(upper=int(pop_all_front_by_nv.index.max()))
        exp = nv.map(pop_all_front_by_nv).astype(float)
        e = float(exp.mean())
        row["xl_all_front_expected"] = e
        row["xl_all_front_vs_expected"] = (
            row["xl_all_victims_front_leader_share_of_xl"] / e if e else np.nan)
    else:
        row["xl_all_front_expected"] = np.nan
        row["xl_all_front_vs_expected"] = np.nan

    if rot_idx is not None:
        obs, exp, n = boundary_suppression(sub, rot_idx, rot)
        row["xb_sandwiches"] = n
        row["xl_observed"] = obs
        row["xl_expected_if_arbitrary"] = exp
        # Below 1 means the attacker crosses the leader boundary LESS than chance -- the boundary
        # behaves like a barrier, which is what ordering control looks like.
        row["boundary_suppression"] = (obs / exp) if (exp and exp == exp and exp > 0) else np.nan
    return row
