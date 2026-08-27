"""Cache the (attacker, validator, offset) association table.

Read by `sec7_cohort_heatmap.py`, `appd_fee_fingerprint_table.py`,
`appd_lookup_tables.py` and `sec7_team2_robustness.py`. Rotations, the slot-to-rotation
index and the per-offset baseline all come from `4_validator_association.py`, imported by
path.

Population: phase 3's Signal Bots minus the entities carrying `expert_verdict == "no"`.
Cross-leader sandwiches are dropped unless `--include-cross-leader` is passed; the
attacker set read from disk is the cl-include one either way.

Output, under sandwich-intent/data/sec7/:
    assoc_<tag>_sl.parquet        attacker, validator, offset, n, n_sand
    leader_base_<tag>_sl.parquet  leader, offset, freq (+ slots, share)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

import figcfg

INTENT = figcfg.INTENT
OUT = INTENT / "data" / "sec7"


def main():
    p = argparse.ArgumentParser(description="Build the Figure 9 association cache")
    p.add_argument("--database", default="solwich_v2")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--variant", default="include", choices=["include", "exclude"])
    p.add_argument("--include-cross-leader", action="store_true",
                   help="Keep cross-leader sandwiches (off by default; see module docstring)")
    a = p.parse_args()

    ph4, _ = figcfg.phase_module(4)
    tag = f"{a.start_epoch}_{a.end_epoch}"
    lo = a.start_epoch * ph4.SLOTS_PER_EPOCH
    hi = (a.end_epoch + 1) * ph4.SLOTS_PER_EPOCH
    suffix = "" if a.include_cross_leader else "_sl"
    file_tag = tag if a.variant == "exclude" else f"{tag}_cl-include"

    cwd = os.getcwd()
    os.chdir(INTENT)
    try:
        sf, ps, breakdown = ph4.load_signal_bots(a.database, file_tag, a.variant)
    finally:
        os.chdir(cwd)
    print(f"Signal Bots: {len(sf):,} ({breakdown}) · {len(ps):,} sandwiches")

    if not a.include_cross_leader:
        n_all = len(ps)
        ps = ps[~ps["cross_leader"].astype(bool)]
        print(f"  single-leader only: {len(ps):,} kept, {n_all - len(ps):,} dropped "
              f"({(n_all - len(ps)) / n_all:.2%})")

    client = ph4.get_client(a.database)
    print(f"Building rotations over slots [{lo:,}, {hi:,}) ...")
    blocks = ph4.load_leader_blocks(client, lo, hi)
    slot_to_block = ph4.build_slot_to_block_idx(blocks)
    freq = ph4.compute_global_offset_freq(blocks)
    print(f"  {len(blocks):,} rotations · {len(freq[0]):,} validators")

    per_signer, missed = ph4.compute_signer_validator_counts(ps, slot_to_block, blocks)
    if missed:
        print(f"  {missed:,} sandwiches ({missed / len(ps):.2%}) sit at slots with no "
              f"slot_leaders row and are dropped")
    totals = ps.groupby("signer").size().to_dict()

    rows = [{"attacker": s, "validator": v, "offset": o, "n": c, "n_sand": totals[s]}
            for s, vmap in per_signer.items()
            for v, offs in vmap.items()
            for o, c in offs.items() if c]
    A = pd.DataFrame(rows)

    # Slot totals per leader are kept alongside the per-offset frequency: `share` is
    # what a reader expects to see, `freq` is what eta actually divides by.
    slots = {}
    for s, e, v in blocks:
        slots[v] = slots.get(v, 0) + int(e) - int(s) + 1
    total = sum(slots.values())
    B = pd.DataFrame([{"leader": v, "offset": o, "freq": freq[o].get(v, 0.0),
                       "slots": slots.get(v, 0), "share": slots.get(v, 0) / total}
                      for o in ph4.OFFSETS for v in slots])

    OUT.mkdir(parents=True, exist_ok=True)
    fa = OUT / f"assoc_{file_tag}{suffix}.parquet"
    fb = OUT / f"leader_base_{tag}{suffix}.parquet"
    A.to_parquet(fa, index=False)
    B.to_parquet(fb, index=False)
    print(f"  -> {fa}  ({len(A):,} rows, {A.attacker.nunique()} attackers, "
          f"{A.validator.nunique()} validators)")
    print(f"  -> {fb}  ({len(B):,} rows)")


if __name__ == "__main__":
    main()
