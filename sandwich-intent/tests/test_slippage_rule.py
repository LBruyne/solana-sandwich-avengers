"""Pin the slippage-consumption rule on the Python side.

`assert_go_band_matches` guards four CONSTANTS. It does not guard the logic that consumes them: a
one-token edit to `aggregate_sc_per_sandwich` -- dropping the floor from the max, say -- leaves the
startup banner reading `OK (... protection floor 0.01)` while changing the verdict on 18.7 % of
sandwiches. That is the hole these tests fill.

Run:  cd sandwich-intent && python3 -m pytest tests/test_slippage_rule.py -q
"""
import importlib.util
import math
import os
import sys

import numpy as np
import pandas as pd

# Scripts live one level below the package root but address `utils.*`, the numbered
# pipeline modules, and `data/` relative to it. Anchor both to the root so they can be
# run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
_HERE = _ROOT
_SPEC = importlib.util.spec_from_file_location(
    "phase1", os.path.join(_HERE, "1_signer_data_preparation_and_summary.py"))
p1 = importlib.util.module_from_spec(_SPEC)
sys.modules["phase1"] = p1
_SPEC.loader.exec_module(p1)

FLOOR = p1.SC_PROTECTION_FLOOR


def victims(*rows):
    """rows are (sandwichId, limitType, limitAmount, actualAmount)."""
    return pd.DataFrame(rows, columns=["sandwichId", "slippageLimitType",
                                       "slippageLimitAmount", "slippageActualAmount"])


def score(vt):
    """Run the real pipeline functions and return the per-sandwich frame."""
    vsc = p1.compute_victim_sc(vt, {})
    ids = list(dict.fromkeys(vt["sandwichId"]))
    return p1.aggregate_sc_per_sandwich(vsc, ids, {})


def state(row):
    if row.sc_anomaly:
        return "anomaly"
    if row.sc_unprotected:
        return "unprotected"
    return "scored"


# ── per victim ───────────────────────────────────────────────────────────────

def test_victim_states():
    cases = [
        # (limitType, limit, actual, expected sc, expected reason)
        ("output", 0.5, 1.0, 0.5, "ok"),
        ("input", 1.0, 0.5, 0.5, "ok"),
        ("output", 0.0, 1.0, 0.0, "no_protection"),   # min_out = 0
        ("output", 0.0, 0.0, 0.0, "no_protection"),   # legacy row: Go zeroed both
        ("output", 1e-9, 2652.0, 1e-9 / 2652.0, "ok"),  # router placeholder, still a real value
        ("output", 0.99, 1.0, 0.99, "ok"),
        ("output", 1.0, 1.0, 1.0, "ok"),
        # a fill AT the limit rounds a few ULP over 1 and must clamp, not be rejected
        ("output", 86131279.491611, 86131279.49161097, 1.0, "ok"),
        ("output", 1.1, 1.0, None, "above_one"),
        ("input", 0.0, 0.5, None, "limit_nonpositive"),
        ("output", 0.5, 0.0, None, "actual_nonpositive"),
        ("output", -1.0, 1.0, None, "limit_nonpositive"),
        ("", 0.0, 0.0, None, "empty_type"),
    ]
    vt = victims(*[(f"s{i}",) + c[:3] for i, c in enumerate(cases)])
    got = p1.compute_victim_sc(vt, {})
    for i, (_, _, _, want_sc, want_reason) in enumerate(cases):
        assert str(got.sc_reason.iloc[i]) == want_reason, (i, cases[i], got.sc_reason.iloc[i])
        if want_sc is None:
            assert math.isnan(got.sc.iloc[i]), (i, cases[i], got.sc.iloc[i])
        else:
            assert abs(got.sc.iloc[i] - want_sc) < 1e-12, (i, cases[i], got.sc.iloc[i])


# ── per sandwich: the floor ──────────────────────────────────────────────────

def test_single_unprotected_victim_is_not_scored_zero():
    """The whole point of the floor. Scoring this 0 is what dropped four of the five
    highest-earning attackers in the published dataset."""
    df = score(victims(("s", "output", 0.0, 1.0)))
    assert state(df.loc["s"]) == "unprotected"
    assert math.isnan(df.loc["s"].sc), "an unprotected sandwich must carry NaN, never 0.0"
    assert str(df.loc["s"].sc_reason) == "no_protection"


def test_all_victims_unprotected_is_not_scored_zero():
    df = score(victims(("s", "output", 0.0, 1.0), ("s", "output", 1e-9, 5.0)))
    assert state(df.loc["s"]) == "unprotected"
    assert math.isnan(df.loc["s"].sc)


def test_floor_is_inclusive():
    """Exactly on the floor counts; one ULP below does not. Guards a `>` / `>=` slip."""
    on = score(victims(("s", "output", FLOOR, 1.0)))
    assert state(on.loc["s"]) == "scored" and abs(on.loc["s"].sc - FLOOR) < 1e-15
    below = score(victims(("s", "output", np.nextafter(FLOOR, 0), 1.0)))
    assert state(below.loc["s"]) == "unprotected"


def test_unprotected_victim_does_not_lower_a_protected_max():
    """Equivalently: the max is over all victims, and the floor only decides whether the
    sandwich is scored at all. If this ever fails, the rule stopped being a conditional mean."""
    df = score(victims(("s", "output", 0.5, 1.0),      # 0.5
                       ("s", "output", 0.0, 1.0),      # unprotected
                       ("s", "output", 1e-9, 2.0)))    # unprotected
    assert state(df.loc["s"]) == "scored"
    assert abs(df.loc["s"].sc - 0.5) < 1e-12


def test_floor_is_actually_applied():
    """The regression D5 describes: dropping the floor from the max would score this
    sandwich at ~1e-9 instead of leaving it unscored."""
    df = score(victims(("s", "output", 1e-9, 1.0)))
    assert state(df.loc["s"]) == "unprotected", (
        "a sandwich whose only victim is far below the floor must be unscored; "
        "scoring it near 0 is the pre-floor behaviour")


# ── per sandwich: anomalies still win ────────────────────────────────────────

def test_one_anomalous_victim_voids_the_sandwich():
    df = score(victims(("s", "output", 0.5, 1.0), ("s", "output", 1.1, 1.0)))
    assert state(df.loc["s"]) == "anomaly"
    assert math.isnan(df.loc["s"].sc)


def test_anomaly_outranks_unprotected():
    df = score(victims(("s", "output", 0.0, 1.0), ("s", "", 0.0, 0.0)))
    assert state(df.loc["s"]) == "anomaly", "unknown must not be reported as a finding about the victim"


def test_states_are_mutually_exclusive_and_sc_is_nan_off_the_scored_state():
    df = score(victims(("a", "output", 0.5, 1.0),
                       ("b", "output", 0.0, 1.0),
                       ("c", "output", 1.1, 1.0)))
    assert not (df.sc_anomaly & df.sc_unprotected).any()
    assert df.loc[df.sc_anomaly | df.sc_unprotected, "sc"].isna().all()
    assert df.loc[~(df.sc_anomaly | df.sc_unprotected), "sc"].notna().all()


# ── signer level: the denominator ────────────────────────────────────────────

def test_mean_sc_denominator_is_the_scored_count_only():
    """`mean_SC` must divide by the number of SCORED sandwiches -- not by the total, and not
    by total-minus-anomalies. An unprotected sandwich leaves both sums untouched."""
    df = score(victims(("a", "output", 0.9, 1.0),
                       ("b", "output", 0.5, 1.0),
                       ("c", "output", 0.0, 1.0),      # unprotected
                       ("d", "", 0.0, 0.0)))           # anomaly
    scored = df[~df.sc_anomaly & ~df.sc_unprotected]
    assert len(scored) == 2
    assert abs(scored.sc.mean() - 0.7) < 1e-12
    assert abs(scored.sc.sum() / len(scored) - 0.7) < 1e-12


def test_the_three_rates_partition_the_population():
    df = score(victims(("a", "output", 0.9, 1.0),
                       ("b", "output", 0.0, 1.0),
                       ("c", "", 0.0, 0.0)))
    n = len(df)
    scored = int((~df.sc_anomaly & ~df.sc_unprotected).sum())
    unprot = int(df.sc_unprotected.sum())
    anom = int(df.sc_anomaly.sum())
    assert scored + unprot + anom == n
    assert abs((scored / n) + (unprot / n) + (anom / n) - 1.0) < 1e-12


# ── the Go binding ───────────────────────────────────────────────────────────

def test_go_constants_are_bound():
    status = p1.assert_go_band_matches()
    assert status.startswith("OK"), status
    assert f"protection floor {FLOOR:g}" in status
