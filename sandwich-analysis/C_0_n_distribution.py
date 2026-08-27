"""Figure 10 (App C, `fig:C_0_n_distribution`): sandwich-count distribution.

Population: every entity phase 1 attributes a sandwich to, BEFORE the profit floor. Those
features come from a separate phase-1 run:

    python 1_signer_data_preparation_and_summary.py --database solwich_v2 \\
        --start-epoch 946 --end-epoch 990 --cross-leader include \\
        --category <cat> --profit-floor-usd 0 --out-root data/1_nofloor --no-charts

Run the four categories serially: they share a `default._verified_bundle_ids` scratch
table and concurrent runs race on it.

Bars are entities per sandwich-count bucket, drawn at equal width against the bucket
index; the line is mean net USD profit per entity in that bucket. Bins are the 1-2-5
ladder, so `n_min = 100` falls on a bin edge.

Output: C_0_n_distribution.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

# Runs to 500,000: the busiest entity in the raw population has 255,099 sandwiches.
BINS = np.array([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
                 10000, 20000, 50000, 100000, 500000], dtype=float)
TICKS = [1, 10, 100, 1000, 10000, 100000]
NOFLOOR_ROOT = "data/1_nofloor"


def main() -> None:
    p = figcfg.add_args(figcfg.argparse.ArgumentParser(
        description="Fig C.0: sandwich-count distribution over the raw attributed population"))
    p.add_argument("--phase1-root", default=NOFLOOR_ROOT,
                   help="Phase-1 output directory. Defaults to the pre-floor run; pass the "
                        "normal root to see the same figure after the $100 floor.")
    a = p.parse_args()
    figcfg.banner(a, "C.0 sandwich-count distribution")
    g = figcfg.gates()

    pool = figcfg.pooled_features(a, root=a.phase1_root)
    print(f"  phase-1 root : {a.phase1_root}")
    print(f"  entities     : {len(pool):,}")

    fig, st = figcfg.gated_distribution(
        pool, "sandwich_count", BINS, g["n_min"], "ge",
        xlabel=r"Sandwiches per entity $n_e$",
        gate_label=rf"$n_{{\min}}={g['n_min']:g}$",
        tick_at=TICKS, tick_fmt=lambda t: f"{int(t):,}", uniform_bars=True,
        symbol=r"$n_e$", headroom=1.10, gate_label_frac=0.95,
        log_profit=True, log_count=True)
    figcfg.save(fig, a, "C_0_n_distribution")

    n = pool["sandwich_count"]
    keep, drop = pool[n >= g["n_min"]], pool[n < g["n_min"]]
    print(f"  median {n.median():.0f}   mean {n.mean():.1f}   max {n.max():,}")
    for q in (0.50, 0.75, 0.90, 0.99):
        print(f"    p{q * 100:>4.0f} = {n.quantile(q):>9,.0f}")
    for lab, d in (("n >= %g" % g["n_min"], keep), ("n <  %g" % g["n_min"], drop)):
        print(f"  {lab:<10} : {len(d):>7,} entities ({len(d) / len(pool):>6.2%}), "
              f"${d['usd_net_total'].sum():>13,.0f} total, "
              f"${d['usd_net_total'].mean():>9,.2f} each, "
              f"{int(d['sandwich_count'].sum()):>10,} sandwiches")
    figcfg.report_buckets(st, lambda x: f"{int(x):>6,}")


if __name__ == "__main__":
    main()
