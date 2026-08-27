"""Figure 13 (App C, `fig:C_3_fg_distribution`): frontrun-gap distribution.

Population: the same economic-gate survivors as C_2; the slippage gate is not applied.

Bins are geometric, with 75 on a bin edge, and bars are drawn at equal width against the
bucket index. Palette, bar spacing, threshold annotation and the right-hand profit line
are shared with C_1 and C_2.

Output: C_3_fg_distribution.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

# Geometric ladder at ratio ~1.585 (five buckets per decade) pinned so that the
# published gate, 75, is a bin edge rather than a line drawn through a bar.
BINS = np.array([1, 2, 3, 5, 8, 12, 19, 30, 47, 75, 119, 188, 299, 473,
                 750, 1188, 1884, 2985, 4730, 7500], dtype=float)
TICKS = [1, 8, 30, 75, 188, 473, 1188, 2985, 7500]


def main() -> None:
    a = figcfg.parse("Fig C.1c: frontrun gap over the economic pool")
    figcfg.banner(a, "C.1 frontrun-gap distribution")
    g = figcfg.gates()

    pool = figcfg.stage1_pool(a)

    fig, st = figcfg.gated_distribution(
        pool, "front_gap_p50", BINS, g["fg_median_max"], "le",
        xlabel="Median frontrun\u2013victim gap "
                r"$\widetilde{\mathsf{FG}}_e$ (transactions)",
        gate_label=rf"$\widetilde{{\mathsf{{FG}}}}_{{\max}}={g['fg_median_max']:g}$",
        symbol=r"$\widetilde{\mathsf{FG}}_e$",
        tick_at=TICKS, tick_fmt=lambda t: f"{int(t):,}", uniform_bars=True,
        # the profit line spikes to $43k just left of the gate, so the box goes right of it
        headroom=1.20, gate_label_frac=0.94, gate_label_side="right")
    figcfg.save(fig, a, "C_3_fg_distribution")

    fg = pool["front_gap_p50"]
    keep, drop = pool[fg <= g["fg_median_max"]], pool[fg > g["fg_median_max"]]
    print(f"  economic pool                 : {len(pool):,} entities "
          f"({int(fg.isna().sum())} without a measurable gap)")
    print(f"  mean {fg.mean():.1f}   median {fg.median():.1f}   "
          f"min {fg.min():.0f}   max {fg.max():.0f}")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90):
        print(f"    p{q * 100:>4.0f} = {fg.quantile(q):>8,.0f}")
    print(f"  FG <= {g['fg_median_max']:<5}                  : {len(keep):>4} entities, "
          f"${keep['usd_net_total'].sum():>12,.0f} total, "
          f"${keep['usd_net_total'].mean():>8,.0f} each, "
          f"${keep['usd_net_total'].median():>7,.0f} median")
    print(f"  FG >  {g['fg_median_max']:<5}                  : {len(drop):>4} entities, "
          f"${drop['usd_net_total'].sum():>12,.0f} total, "
          f"${drop['usd_net_total'].mean():>8,.0f} each, "
          f"${drop['usd_net_total'].median():>7,.0f} median")
    figcfg.report_buckets(st, lambda x: f"{int(x):>5,}")


if __name__ == "__main__":
    main()
