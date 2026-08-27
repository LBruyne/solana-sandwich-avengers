"""Figure 11 (App C, `fig:C_1_wr_distribution`): win-rate distribution.

Population: entities passing the count gate (`sandwich_count >= n_min`). Bars are entities
per win-rate bucket, the line is mean net USD profit per entity in that bucket. Drawn by
`figcfg.gated_distribution`, shared with C_2 and C_3.

Output: C_1_wr_distribution.{pdf,png} in `--out-dir`.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import numpy as np

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg


def main() -> None:
    a = figcfg.parse("Fig C.1a: win-rate distribution over the count-gated pool")
    figcfg.banner(a, "C.1 win-rate distribution")
    g = figcfg.gates()

    feats = figcfg.pooled_features(a)
    pool = feats[feats["sandwich_count"] >= g["n_min"]]

    fig, st = figcfg.gated_distribution(
        pool, "sol_win_rate", np.linspace(0.0, 1.0, 21), g["wr_min"], "ge",
        xlabel=r"Win rate $w_e \;=\; \Pr[\Delta x_B > \Delta x_F]$",
        gate_label=rf"$w_{{\min}}={g['wr_min']:g}$", symbol=r"$w_e$", headroom=1.20, gate_label_frac=0.94)
    figcfg.save(fig, a, "C_1_wr_distribution")

    print(f"  entities after the phase-1 floor : {len(feats):,}")
    print(f"  CNT >= {g['n_min']:<4}                      : {len(pool):,} "
          f"({len(pool) / len(feats):.2%})")
    print(f"  ... of which WR >= {g['wr_min']}          : "
          f"{int((pool['sol_win_rate'] >= g['wr_min']).sum()):,}")
    c = st["counts"]
    print(f"  modal bucket : [{st['bins'][c.argmax()]:.2f}, "
          f"{st['bins'][c.argmax() + 1]:.2f})  n={c.max():,}")
    t = int(np.argmin(c.to_numpy()[10:])) + 10
    print(f"  trough >0.5  : [{st['bins'][t]:.2f}, {st['bins'][t + 1]:.2f})  "
          f"n={c.iloc[t]:,}   <- w_min sits at its left edge")
    figcfg.report_buckets(st, lambda x: f"{x:.2f}")


if __name__ == "__main__":
    main()
