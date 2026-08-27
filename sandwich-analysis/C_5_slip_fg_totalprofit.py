"""Figure 14 (App C, `fig:C_5_slip_fg_totalprofit`): the two capability signals.

Horizontal axis is mean slippage consumption, vertical is the median frontrun gap; the
white quadrant clears both thresholds. Marker colour is the entity's total net profit,
marker area its sandwich count. Drawn by `figcfg.signal_plane`.

Output: C_5_slip_fg_totalprofit.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg


def main() -> None:
    a = figcfg.parse("Fig C.1e: signal plane coloured by total profit")
    figcfg.banner(a, "C.1 signal plane - total profit")
    g = figcfg.gates()

    pool = figcfg.stage1_pool(a).copy()
    pool["colour"] = np.log10(pool["usd_net_total"].clip(lower=10))

    fig, st = figcfg.signal_plane(
        pool, "colour", r"$\log_{10}$(USD profit)", vmin=2.0, vmax=5.5,
        gate_sc=g["slip_min"], gate_fg=g["fg_median_max"])
    figcfg.save(fig, a, "C_5_slip_fg_totalprofit")

    ps = pool["mean_SC"].fillna(-1) >= g["slip_min"]
    pf = pool["front_gap_p50"].fillna(np.inf) <= g["fg_median_max"]
    tot = pool["usd_net_total"].sum()
    n_nosig = len(pool) - st["n"] - st["cropped"]
    print(f"  pool {len(pool):,} -> {st['n']:,} plotted "
          f"({n_nosig} without both signals, {st['cropped']} cropped left of the axis)")
    print(f"  {'quadrant':>24}{'n':>6}{'total USD':>14}{'per entity':>12}{'share':>8}")
    for m, lab in ((ps & pf, "pass both (selected)"), (~ps & pf, "fail SC only"),
                   (ps & ~pf, "fail FG only"), (~ps & ~pf, "fail both")):
        q = pool[m]
        print(f"  {lab:>24}{len(q):>6}{'$' + format(q['usd_net_total'].sum(), ',.0f'):>14}"
              f"{'$' + format(q['usd_net_total'].mean(), ',.0f'):>12}"
              f"{q['usd_net_total'].sum() / tot:>8.1%}")
    print(f"  {'POOL':>24}{len(pool):>6}{'$' + format(tot, ',.0f'):>14}"
          f"{'$' + format(pool['usd_net_total'].mean(), ',.0f'):>12}{1.0:>8.1%}")


if __name__ == "__main__":
    main()
