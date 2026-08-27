"""Pins one bundle-sandwich definition across scripts 0, 1, 2 and 3.

    colocated   one bundleId holds >=1 frontRun, >=1 victim, >=1 backRun
    verified    colocated AND signerSame AND profitA > 0

"Bundle sandwich" means `verified` everywhere; `colocated` survives only as a named
diagnostic column. Verdicts are read from `0_crawl_jito_bundle_ids.py`'s CSVs, never from
`jito_bundles`, which is a rolling buffer deleted per epoch after `inBundle` marking.

Epochs in `utils.intent.EXCLUDED_EPOCHS` are dropped at the single point every consumer
reads.

    python -m pytest tests/test_bundle_definition.py -q
"""

import ast
import importlib.util
import os
import sys
from collections import defaultdict

import pandas as pd
import pytest

# Scripts live one level below the package root but address `utils.*`, the numbered
# pipeline modules, and `data/` relative to it. Anchor both to the root so they can be
# run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
from utils.db import get_client                                     # noqa: E402
from utils.intent import load_bundle_verdicts, verified_bundle_sandwiches  # noqa: E402

# Defaults to the database a fresh detector run writes. Point it at another one to check a
# specific dataset: INTENT_DB=<name> python -m pytest tests/ -q
DB = os.environ.get("INTENT_DB", "solwich")
SLOTS_PER_EPOCH = 432000
# 946 is the epoch to probe against the live table: bundle CONTENT for it has not been reclaimed
# yet (961-989 are already empty, which is the whole reason the table cannot be the source).
TABLE_EPOCH = 946
TABLE_RANGE = (946, 960)
RANGE = (946, 990)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HERE = _ROOT
crawl = _load("crawl0", os.path.join(HERE, "0_crawl_jito_bundle_ids.py"))
phase1 = _load("phase1", os.path.join(HERE, "1_signer_data_preparation_and_summary.py"))


@pytest.fixture(scope="module")
def client():
    return get_client(DB)


# ── 0_crawl: the CSV verdicts really are decide()'s output ───────────────────

CORE_TYPES = ("frontRun", "victim", "backRun")


def _recompute_colocated(client, epoch):
    """decide() re-run from the retained table, with NO candidate pre-filter.

    The `type IN CORE_TYPES` restriction is NOT part of the pre-filter and must stay. `decide`
    tests `len(types) == len(CORE_TYPES)`, which only means "front and victim and back" when its
    input holds nothing else -- and `sandwich_txs` also carries `adverse` (2.4M legs over one
    epoch) and `transfer`. Feeding it the unrestricted leg set makes {frontRun, victim, adverse}
    score as a hit; that mistake inflated an earlier run of this measurement from 837 to 3,814 and
    produced a false report that the crawler drops 78 % of colocations. `0_crawl` applies the same
    restriction at its own query (`WHERE type IN {CORE_TYPES}`), so dropping it here is not
    "unfiltered", it is wrong.
    """
    lo = epoch * SLOTS_PER_EPOCH
    hi = lo + SLOTS_PER_EPOCH - 1
    scope = (f"SELECT signature FROM sandwich_txs WHERE type IN {CORE_TYPES} "
             f"AND slot BETWEEN {lo} AND {hi}")
    legs = client.query_df(
        f"SELECT sandwichId, type, signature FROM sandwich_txs "
        f"WHERE type IN {CORE_TYPES} AND slot BETWEEN {lo} AND {hi}")
    pairs = client.query_df(f"""
        SELECT sig, bundleId FROM (
            SELECT arrayJoin(transactions) AS sig, bundleId
            FROM jito_bundles WHERE slot BETWEEN {lo} AND {hi})
        WHERE sig IN ({scope})""")
    if not len(pairs):
        return set()
    sig_to_bundle = dict(zip(pairs["sig"], pairs["bundleId"]))
    return {sid for sid, bid in crawl.decide(legs, sig_to_bundle).items() if bid}


def test_crawler_candidate_filter_loses_nothing(client):
    """The crawler's candidate pre-filter reproduces the definition exactly.

    `0_crawl_jito_bundle_ids` only asks the bundle source about sandwiches that sit in ONE slot
    with EVERY core leg flagged `inBundle`. That is narrower than decide()'s own rule (one bundle
    holding >=1 front, >=1 victim, >=1 back), so it COULD drop real colocations. Measured: it does
    not. Re-running decide() over 946-960 without the pre-filter reproduces the verdict CSVs
    epoch for epoch -- 837 vs 837, zero either way, on all 15 epochs.

    The mechanism: a bundle never spans slots, so a bundle holding all three leg types forces
    those three legs into one slot; and the filter's other clause only removes sandwiches with an
    unbundled core leg, which cannot be the leg that completes the triple.

    This test previously asserted the opposite, on a measurement that fed `decide` every leg type
    instead of only the core three -- see `_recompute_colocated`. Epochs 961-989 cannot be
    re-checked here (bundle content reclaimed) and 991+ has too little detection data yet.
    """
    lo, hi = TABLE_RANGE[0] * SLOTS_PER_EPOCH, (TABLE_RANGE[1] + 1) * SLOTS_PER_EPOCH - 1
    have = client.query_df(
        f"SELECT count() c FROM jito_bundles WHERE slot BETWEEN {lo} AND {hi}")["c"][0]
    if int(have) == 0:
        pytest.skip(f"epochs {TABLE_RANGE} bundle content already reclaimed")

    # One epoch is too thin -- 946 alone has no signerSame colocation at all. Takes ~2 min.
    colocated = set()
    for e in range(TABLE_RANGE[0], TABLE_RANGE[1] + 1):
        colocated |= _recompute_colocated(client, e)
    from_csv = set(load_bundle_verdicts(*TABLE_RANGE)["sandwichId"])

    assert colocated == from_csv, (
        f"{TABLE_RANGE}: unfiltered decide() gives {len(colocated)} colocated sandwiches, the "
        f"verdict CSVs hold {len(from_csv)}; "
        f"filter-dropped={len(colocated - from_csv)} csv-only={len(from_csv - colocated)}")



def test_decide_requires_all_three_leg_types():
    """front+back without a victim is a bundle, not a bundle SANDWICH."""
    legs = pd.DataFrame({
        "sandwichId": ["all3", "all3", "all3", "no_victim", "no_victim", "split", "split"],
        "type":       ["frontRun", "victim", "backRun", "frontRun", "backRun",
                       "frontRun", "victim"],
        "signature":  ["a1", "a2", "a3", "b1", "b2", "c1", "c2"],
    })
    # `split` has all three legs bundled but across TWO bundle ids -> not a sandwich.
    legs = pd.concat([legs, pd.DataFrame(
        {"sandwichId": ["split"], "type": ["backRun"], "signature": ["c3"]})])
    sig_to_bundle = {"a1": "B", "a2": "B", "a3": "B",
                     "b1": "B", "b2": "B",
                     "c1": "X", "c2": "X", "c3": "Y"}
    out = crawl.decide(legs, sig_to_bundle)
    assert out["all3"] == "B"
    assert out["no_victim"] is None
    assert out["split"] is None


# ── 1_signer == utils.intent == 0_report ─────────────────────────────────────

def test_phase1_verified_set_matches_utils_intent(client):
    """Per-epoch reads must union to the same set as one range-wide read.

    1_signer no longer implements the predicate -- it calls `utils.intent` -- so this is no longer
    a two-implementations check. It is now a scoping check: phase 1 asks epoch by epoch while
    Track 1 asks for the whole range at once, and a verdict whose sandwich straddles an epoch
    boundary could fall out of one and not the other.
    """
    per_epoch_col, per_epoch_ver = set(), set()
    for e in range(RANGE[0], RANGE[1] + 1):
        c, v = phase1.fetch_jito_verdicts(DB, e)
        assert v <= c, f"epoch {e}: verified set is not a subset of colocated"
        per_epoch_col |= c
        per_epoch_ver |= v

    assert per_epoch_col == set(load_bundle_verdicts(*RANGE)["sandwichId"])
    assert per_epoch_ver == verified_bundle_sandwiches(DB, *RANGE)


def test_phase1_no_longer_reads_the_rolling_table():
    """The reclaimable table must not be reachable from the live jito path.

    `jito_bundles` content is deleted per epoch after marking; any read of it makes the column a
    function of wall-clock time.
    """
    import ast
    import inspect
    import textwrap
    for fn in (phase1.fetch_jito_verdicts, phase1.compute_jito_same_bundle):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        if ast.get_docstring(tree) is not None:
            tree.body = tree.body[1:]          # the docstrings explain the ban; only code counts
        assert "jito_bundles" not in ast.unparse(tree), fn.__name__


def test_only_utils_intent_implements_the_definition():
    """Scripts 0_report/1/2/3 must READ the definition, never re-derive it.

    Three copies of `signerSame AND profitA > 0` existed at once (utils.intent, 0_report's pandas
    filter, 1_signer's own SQL). Pinning them equal in a test is weaker than not having copies:
    a test proves they agree today, ownership stops them from diverging tomorrow.

    `signerSame` on its own is fine and appears legitimately -- it is also the standard /
    multi-split / diff-signer taxonomy. What must not reappear is the CONJUNCTION.

    Two lists, because "must read the definition" and "must not re-derive it" are different
    obligations. Phase 2 was rewritten as `2_parameter_selection_and_sensitivity_analysis.py` and
    no longer touches bundles at all -- requiring it to import a definition it has no use for
    would push bundle logic back into a script that was deliberately cleared of it. It still has
    to obey the ban.
    """
    MUST_NOT_REDERIVE = ("0_report_data_overview.py",
                         "1_signer_data_preparation_and_summary.py",
                         "2_parameter_selection_and_sensitivity_analysis.py",
                         "3_attacker_filter.py")
    MUST_READ_DEFINITION = ("0_report_data_overview.py",
                            "1_signer_data_preparation_and_summary.py",
                            "3_attacker_filter.py")
    for name in MUST_NOT_REDERIVE:
        src = open(os.path.join(HERE, name)).read()
        if name in MUST_READ_DEFINITION:
            assert "verified_bundle_sandwiches" in src, \
                f"{name} does not read the shared definition"
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue                                   # docstrings describe the rule
            if not isinstance(node, (ast.Compare, ast.BoolOp, ast.BinOp)):
                continue
            seg = ast.get_source_segment(src, node) or ""
            assert not ("signerSame" in seg and "profitA" in seg), (
                f"{name} re-implements the bundle-sandwich predicate:\n  {seg[:200]}")


def test_report_predicate_is_the_same_two_conditions(client):
    """0_report's SQL narrows the verdicts by exactly signerSame AND profitA > 0."""
    v = load_bundle_verdicts(*RANGE)
    tmp = "default._test_bundle_ids"
    client.command(f"DROP TABLE IF EXISTS {tmp}")
    client.command(f"CREATE TABLE {tmp} (sandwichId String) ENGINE=Memory")
    client.insert(tmp, v[["sandwichId"]].values.tolist(), column_names=["sandwichId"])
    try:
        d = client.query_df(
            f"SELECT countIf(signerSame) AS same, "
            f"       countIf(signerSame AND profitA > 0) AS verified, count() AS total "
            f"FROM sandwiches WHERE sandwichId IN (SELECT sandwichId FROM {tmp})")
    finally:
        client.command(f"DROP TABLE IF EXISTS {tmp}")
    total, same, verified = int(d["total"][0]), int(d["same"][0]), int(d["verified"][0])
    assert total == len(v), "some verdict ids are missing from `sandwiches`"
    assert verified == len(verified_bundle_sandwiches(DB, *RANGE))
    assert verified <= same <= total


# ── the emitted columns carry the settled meaning ────────────────────────────

def test_phase1_columns_mean_what_downstream_assumes(client):
    """`jito_bundle` == verified, `jito_bundle_colocated` == structural."""
    from utils.intent import load_phase1
    ps, sf = load_phase1("standard", DB, "946_990_cl-include")
    for col in ("jito_bundle", "jito_bundle_colocated"):
        assert col in ps.columns, f"{col} missing -- rerun or patch phase 1"

    verified = verified_bundle_sandwiches(DB, *RANGE)
    colocated = set(load_bundle_verdicts(*RANGE)["sandwichId"])
    marked_v = set(ps.index[ps["jito_bundle"].to_numpy(dtype=bool)])
    marked_c = set(ps.index[ps["jito_bundle_colocated"].to_numpy(dtype=bool)])

    # `standard` is one category of three, so the marks are a SUBSET of the range-wide sets;
    # what must never happen is a mark that is not in the authoritative set at all.
    assert marked_v <= verified, f"{len(marked_v - verified)} marks are not verified sandwiches"
    assert marked_c <= colocated
    assert marked_v == marked_c & verified

    agg = ps.groupby("signer")["jito_bundle"].sum()
    common = sf.index.intersection(agg.index)
    assert (sf.loc[common, "jito_count"].fillna(0).to_numpy()
            == agg.loc[common].to_numpy()).all(), "signer jito_count is stale"
