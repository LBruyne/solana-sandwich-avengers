"""Figure 12 (App C, `fig:C_2_sc_distribution`): slippage-consumption distribution.

Population: entities passing the economic gate (`sandwich_count >= n_min`,
`sol_win_rate >= w_min`, `usd_net_total >= usd_min`). The front-gap gate is not applied.

The axis is cropped to [0.60, 1.00] with 0.025 bins. Entities below 0.60 are dropped from
the plot rather than piled onto the left bar, and their count is printed.

Output: C_2_sc_distribution.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

LO, HI, STEP = 0.60, 1.00, 0.025


def main() -> None:
    a = figcfg.parse("Fig C.1b: slippage consumption over the economic pool")
    figcfg.banner(a, "C.1 slippage-consumption distribution")
    g = figcfg.gates()

    pool = figcfg.stage1_pool(a)
    bins = np.round(np.arange(LO, HI + 1e-9, STEP), 4)

    fig, st = figcfg.gated_distribution(
        pool, "mean_SC", bins, g["slip_min"], "ge",
        xlabel=r"Mean slippage consumption $\overline{\mathsf{SC}}_e$",
        gate_label=rf"$\overline{{\mathsf{{SC}}}}_{{\min}}={g['slip_min']:.2f}$",
        symbol=r"$\overline{\mathsf{SC}}_e$", headroom=1.20, gate_label_frac=0.94)
    figcfg.save(fig, a, "C_2_sc_distribution")

    sc = pool["mean_SC"]
    keep, drop = pool[sc >= g["slip_min"]], pool[sc < g["slip_min"]]
    print(f"  economic pool                 : {len(pool):,} entities "
          f"({int(sc.isna().sum())} without a measurable SC)")
    print(f"  mean {sc.mean():.4f}   median {sc.median():.4f}   "
          f"min {sc.min():.4f}   max {sc.max():.4f}")
    print(f"  SC >= {g['slip_min']:<5}                  : {len(keep):>4} entities, "
          f"${keep['usd_net_total'].sum():>12,.0f} total, "
          f"${keep['usd_net_total'].mean():>8,.0f} each, "
          f"${keep['usd_net_total'].median():>7,.0f} median")
    print(f"  SC <  {g['slip_min']:<5}                  : {len(drop):>4} entities, "
          f"${drop['usd_net_total'].sum():>12,.0f} total, "
          f"${drop['usd_net_total'].mean():>8,.0f} each, "
          f"${drop['usd_net_total'].median():>7,.0f} median")
    figcfg.report_buckets(st, lambda x: f"{x:.3f}",
                          gate_note=f" (below {LO}: {int((sc < LO).sum())})")


if __name__ == "__main__":
    main()
