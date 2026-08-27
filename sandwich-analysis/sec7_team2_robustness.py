"""Appendix E.1 permutation test for the Team 2 validator association.

Null: the attacker's whole leader-rotation sequence is shifted by one random offset,
wrapped inside its own active span, preserving sandwich count, burst structure and active
window. The permutation unit is the rotation, not the sandwich.

Statistic: the largest co-occurrence count over all (validator, offset) pairs, so the
search is absorbed into the null distribution; `q` is Bonferroni over the attackers only.

Two counting modes are reported: one row per sandwich, and one row per rotation.

Population: single-leader sandwiches of the `--top-n` most profitable cohort members, the
cohort being derived by `sec7_cohort_heatmap.core_members`.

Input: the association cache from `sec7_build_assoc.py`. Rotations come from
`4_validator_association.load_leader_blocks` and are cached under ./data.

Output, under results/sec7_team2_robustness/:
    test1_per_attacker.csv, test1_per_attacker_unique.csv, summary.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import figcfg
from sec7_cohort_heatmap import core_members, ASSOC, BASE, PEAK_OFFSET

OFFSETS = [-2, -1, 0, 1, 2]
SEED = 20260824
B = 10_000
HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "sec7_team2_robustness"


def load_rotations(a):
    """(leader code per rotation, validator names, first slot of each rotation).

    Rotations come from `4_validator_association.load_leader_blocks`. Cached under ./data;
    delete the file to rebuild.
    """
    cache = HERE / "data" / f"rotations_{a.database}_{a.start_epoch}_{a.end_epoch}.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return z["lead"], z["vals"], z["starts"]

    ph4, _ = figcfg.phase_module(4)
    lo = a.start_epoch * ph4.SLOTS_PER_EPOCH
    hi = (a.end_epoch + 1) * ph4.SLOTS_PER_EPOCH
    blocks = ph4.load_leader_blocks(ph4.get_client(a.database), lo, hi)
    vals = sorted({b[2] for b in blocks})
    code = {v: i for i, v in enumerate(vals)}
    lead = np.array([code[b[2]] for b in blocks], dtype=np.int32)
    starts = np.array([b[0] for b in blocks], dtype=np.int64)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, lead=lead, vals=np.array(vals, dtype=object), starts=starts)
    print(f"  rotations: {len(lead):,} over {len(vals):,} validators -> {cache.name}")
    return lead, np.array(vals, dtype=object), starts


def team2_rotations(a, starts, top_n):
    """The top-N cohort members by net profit, as rotation indices over their single-leader
    sandwiches."""
    att = figcfg.load_attackers(a, verbose=False)
    sw = figcfg.load_sandwiches(a, att, verbose=False)
    sw = sw[~sw["cross_leader"].astype(bool)]
    sw["rot"] = np.searchsorted(starts, sw["slot"].to_numpy(), side="right") - 1

    Bo = pd.read_parquet(BASE)
    core = core_members(pd.read_parquet(ASSOC), Bo[Bo["offset"] == PEAK_OFFSET]
                        .set_index("leader")["freq"])
    profit = (att[att["attacker"].isin(core)]
              .groupby("attacker")["usd_net_total"].sum().sort_values(ascending=False))
    return {x: sw.loc[sw["signer"].astype(str) == x, "rot"].to_numpy()
            for x in profit.index[:top_n]}


def shift(r, delta):
    """Circular shift inside the attacker's own active span, not the whole window."""
    lo, hi = r.min(), r.max()
    span = hi - lo + 1
    return lo + (r - lo + delta) % span


def max_cooccurrence(r, lead, nv, unique_rotations):
    """Largest count over all (validator, offset) pairs, and its argmax."""
    if unique_rotations:
        r = np.unique(r)
    best, arg = 0, None
    for d in OFFSETS:
        c = np.bincount(lead[(r + d) % len(lead)], minlength=nv)
        i = int(c.argmax())
        if c[i] > best:
            best, arg = int(c[i]), (i, d)
    return best, arg


def permutation_test(R, lead, vals, rng, unique_rotations):
    rows = []
    for a, r in R.items():
        obs, arg = max_cooccurrence(r, lead, len(vals), unique_rotations)
        null = np.empty(B, dtype=np.int64)
        for b in range(B):
            null[b] = max_cooccurrence(shift(r, rng.integers(r.max() - r.min() + 1)),
                                       lead, len(vals), unique_rotations)[0]
        rows.append({
            "attacker": a[:5], "n_sandwich": len(r), "n_rotation": len(np.unique(r)),
            "observed": obs, "validator": vals[arg[0]][:5], "offset": arg[1],
            "null_p50": float(np.percentile(null, 50)),
            "null_p99": float(np.percentile(null, 99)),
            "null_max": int(null.max()),
            "p": (int((null >= obs).sum()) + 1) / (B + 1),
        })
    df = pd.DataFrame(rows)
    # Only the attackers remain to correct for: the max statistic already absorbed the
    # search over validators and offsets. Bonferroni over so few tests costs nothing.
    df["q_bonferroni"] = (df["p"] * len(df)).clip(upper=1.0)
    return df


def main():
    p = figcfg.add_args(argparse.ArgumentParser(
        description="Team 2 robustness: permutation test"))
    p.add_argument("--top-n", type=int, default=3,
                   help="Test the N most profitable Team 2 attackers (default 3)")
    p.add_argument("--permutations", type=int, default=B)
    p.add_argument("--seed", type=int, default=SEED)
    a = p.parse_args()
    globals()["B"] = a.permutations

    lead, vals, starts = load_rotations(a)
    R = team2_rotations(a, starts, a.top_n)

    print(f"Team 2, top {len(R)} by profit: {sum(len(r) for r in R.values()):,} "
          f"single-leader sandwiches; B={B}, seed={a.seed}")

    rng = np.random.default_rng(a.seed)
    t1 = permutation_test(R, lead, vals, rng, unique_rotations=False)
    rng = np.random.default_rng(a.seed)
    t1u = permutation_test(R, lead, vals, rng, unique_rotations=True)

    OUT.mkdir(parents=True, exist_ok=True)
    t1.assign(counting="sandwiches").to_csv(OUT / "test1_per_attacker.csv", index=False)
    t1u.assign(counting="unique_rotations").to_csv(OUT / "test1_per_attacker_unique.csv", index=False)
    (OUT / "summary.json").write_text(json.dumps(
        {"seed": a.seed, "permutations": B, "attackers": [x[:5] for x in R],
         "statistic": "max co-occurrence over (validator, offset)",
         "null": "circular shift of the rotation sequence within the attacker's active span"},
        indent=2))

    print("\n=== Test 1: per attacker, statistic = max co-occurrence over (validator, offset) ===")
    print(t1.to_string(index=False))
    print("\n--- robustness: each rotation counted once ---")
    print(t1u.to_string(index=False))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
