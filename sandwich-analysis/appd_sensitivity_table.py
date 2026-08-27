"""Table 3 (App C, `tab:appd-sensitivity`): the attacker set under varied thresholds.

Each row rebuilds the HA2--HA3 selection with one threshold changed and the others at
their published values, which are read from phase 3's argparse defaults. Two sweeps:

    (SC_min, FG_max) over {0.85, 0.90, 0.95} x {70, 75, 80}, with w_min = 0.85
    w_min            over {0.80, 0.85, 0.90},                 with (SC, FG) = (0.90, 75)

Scope: HA2--HA3 only, so HA1 (Jito bundle) entities are excluded, and pre-audit, so no row
removes the entities the expert panel labelled `no`.

Output: tab_appd_sensitivity_<tag>.csv in `--out-dir`.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg

N_MIN = 100          # fixed throughout the table
USD_MIN = 10.0
SC_GRID = [0.85, 0.90, 0.95]
FG_GRID = [70.0, 75.0, 80.0]
WR_GRID = [0.80, 0.85, 0.90]


def load_features(a):
    """Per-entity phase-1 features, pooled over the four categories.

    An entity appearing in two categories stays one attacker: `select` gates each of its
    rows and sums the volume columns over the rows that pass.
    """
    frames = []
    for cat in figcfg.CATEGORIES:
        p = figcfg.phase1(a, cat, "signer_features")
        if not p.exists():
            continue
        d = pd.read_parquet(p)
        d = d.reset_index().rename(columns={d.index.name or "index": "attacker"})
        d["category"] = cat
        frames.append(d)
    if not frames:
        raise SystemExit(f"no phase-1 features for {a.database}/{figcfg.tag(a)}")
    return pd.concat(frames, ignore_index=True)


def select(F, wr_min, slip_min, fg_max):
    """The HA2--HA3 selection under one threshold triple."""
    keep = F[(F["sandwich_count"] >= N_MIN)
             & (F["sol_win_rate"] >= wr_min)
             & (F["usd_net_total"] >= USD_MIN)
             & (F["mean_SC"] >= slip_min)
             & (F["front_gap_p50"] <= fg_max)]
    # Sum over the categories an entity qualifies in; it is one attacker either way.
    g = keep.groupby("attacker").agg(sandwiches=("sandwich_count", "sum"),
                                     usd=("usd_net_total", "sum"))
    return {"attackers": len(g), "sandwiches": int(g["sandwiches"].sum()),
            "usd": float(g["usd"].sum()),
            "usd_per_attacker": float(g["usd"].sum() / len(g)) if len(g) else float("nan"),
            "usd_per_sandwich": float(g["usd"].sum() / g["sandwiches"].sum()) if len(g) else float("nan")}


def main():
    a = figcfg.parse("Table 3: intent-threshold sensitivity")
    figcfg.banner(a, "Table 3: threshold sensitivity")
    g = figcfg.gates()
    print(f"  published gates: n>={g['n_min']} wr>={g['wr_min']} SC>={g['slip_min']} "
          f"FG<={g['fg_median_max']:g} USD>=${g['usd_min']:g}")

    F = load_features(a)
    print(f"  entities: {F['attacker'].nunique():,} over {F['category'].nunique()} categories")

    rows = []
    for sc in SC_GRID:
        for fg in FG_GRID:
            r = select(F, g["wr_min"], sc, fg)
            r.update(sweep="SC x FG", setting=f"({sc:.2f}, {fg:g})",
                     published=(sc == g["slip_min"] and fg == g["fg_median_max"]))
            rows.append(r)
    for wr in WR_GRID:
        r = select(F, wr, g["slip_min"], g["fg_median_max"])
        r.update(sweep="w_min", setting=f"{wr:.2f}", published=(wr == g["wr_min"]))
        rows.append(r)
    T = pd.DataFrame(rows)[["sweep", "setting", "published", "attackers", "sandwiches",
                            "usd", "usd_per_attacker", "usd_per_sandwich"]]

    last = None
    for _, r in T.iterrows():
        if r["sweep"] != last:
            print(f"\n  --- {r['sweep']} ---")
            print(f"  {'setting':<14} {'#Att':>5} {'#Sandwich':>10} {'Profit ($)':>12} "
                  f"{'Avg./Att.':>10} {'Avg./Sw.':>9}")
            last = r["sweep"]
        mark = " <-- published" if r["published"] else ""
        print(f"  {r['setting']:<14} {r['attackers']:>5,} {r['sandwiches']:>10,} "
              f"{r['usd']:>12,.0f} {r['usd_per_attacker']:>10,.0f} "
              f"{r['usd_per_sandwich']:>9.2f}{mark}")

    out = figcfg.outdir(a) / f"tab_appd_sensitivity_{figcfg.tag(a)}.csv"
    T.to_csv(out, index=False)
    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
