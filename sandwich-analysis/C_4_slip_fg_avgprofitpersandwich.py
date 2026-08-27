"""`fig:C_4_slip_fg_avgprofitpersandwich`: the two capability signals, coloured by average
profit per sandwich.

The same plane as C_5 with the colour channel remapped from an entity's total net profit
to its mean net profit per sandwich. Drawn by `figcfg.signal_plane`.

Output: C_4_slip_fg_avgprofitpersandwich.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg


def main() -> None:
    a = figcfg.parse("Fig C.1d: signal plane coloured by average profit per sandwich")
    figcfg.banner(a, "C.1 signal plane - average profit per sandwich")
    g = figcfg.gates()

    pool = figcfg.stage1_pool(a).copy()
    pool["avg_usd"] = pool["usd_net_total"] / pool["sandwich_count"]
    pool["colour"] = np.log10(pool["avg_usd"].clip(lower=0.1))

    fig, st = figcfg.signal_plane(
        pool, "colour", r"$\log_{10}$(avg USD per sandwich)", vmin=-0.5, vmax=2.0,
        gate_sc=g["slip_min"], gate_fg=g["fg_median_max"])
    figcfg.save(fig, a, "C_4_slip_fg_avgprofitpersandwich")

    sel = pool[(pool["mean_SC"].fillna(-1) >= g["slip_min"])
               & (pool["front_gap_p50"].fillna(np.inf) <= g["fg_median_max"])]
    rest = pool.drop(sel.index)
    n_nosig = len(pool) - st["n"] - st["cropped"]
    print(f"  pool {len(pool):,} -> {st['n']:,} plotted "
          f"({n_nosig} without both signals, {st['cropped']} cropped left of the axis)")
    print(f"  intentional attackers {st['passed']:,} · filtered out {st['failed']:,}")
    print(f"  avg USD per sandwich, selected : median ${sel['avg_usd'].median():.2f}, "
          f"p90 ${sel['avg_usd'].quantile(0.9):.2f}, max ${sel['avg_usd'].max():,.2f}")
    print(f"  avg USD per sandwich, filtered : median ${rest['avg_usd'].median():.2f}, "
          f"p90 ${rest['avg_usd'].quantile(0.9):.2f}, max ${rest['avg_usd'].max():,.2f}")


if __name__ == "__main__":
    main()
