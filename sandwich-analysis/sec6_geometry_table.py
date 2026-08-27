"""Table 2 (S6.1, `tab:geometry-econ`): per-geometry statistics for the top attackers.

Prints two blocks: the top `--top-n` attackers by net profit (the table) and all attackers
(the surrounding prose).

Three win-rate-related columns:

    WR      profitable over ALL of the class's sandwiches; the paper's column
    WR|$    the same rate over the sandwiches carrying a token price
    cover   the share of the class's sandwiches carrying a token price

Output: tab_geometry_econ_<tag>.csv in `--out-dir`.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg
from sec6_measure_charts import CLASSES, CLASS_LABEL, classify


def block(sw, label):
    """One row per geometry class: count, profit, mean profit per sandwich, win rate."""
    tot_n, tot_usd = len(sw), sw["usd_profit_net"].sum()
    rows = []
    for c in CLASSES:
        d = sw[sw["cls"] == c]
        # Two win rates over a sandwich whose tokenA carries no price:
        #   wr_all     counts it as not-profitable; the paper's column
        #   wr_priced  drops it from numerator and denominator
        known = d["usd_profit_net"].notna()
        rows.append({
            "population": label,
            "class": CLASS_LABEL[c],
            "sandwiches": len(d),
            "sandwich_share": len(d) / tot_n if tot_n else float("nan"),
            "profit_usd": d["usd_profit_net"].sum(),
            "profit_share": d["usd_profit_net"].sum() / tot_usd if tot_usd else float("nan"),
            "avg_usd": d["usd_profit_net"].mean(),
            "wr_all": (d["usd_profit_net"].fillna(0) > 0).mean(),
            "wr_priced": (d.loc[known, "usd_profit_net"] > 0).mean(),
            "price_coverage": known.mean(),
        })
    return pd.DataFrame(rows)


def show(d):
    for _, r in d.iterrows():
        print(f"  {r['class']:<6} {r['sandwiches']:>9,} ({r['sandwich_share']:6.1%})  "
              f"${r['profit_usd']:>12,.0f} ({r['profit_share']:6.1%})  "
              f"{r['avg_usd']:>7.2f}  {r['wr_all']:6.1%}  {r['wr_priced']:6.1%}  "
              f"{r['price_coverage']:6.1%}")


def main():
    p = figcfg.add_args(argparse.ArgumentParser(description="Table 2: geometry economics"))
    p.add_argument("--top-n", type=int, default=20,
                   help="Attackers in the table, ranked by net profit (default 20)")
    a = p.parse_args()
    figcfg.banner(a, "Table 2: geometry economics")

    att = figcfg.load_attackers(a)
    sw = figcfg.load_sandwiches(a, att)
    sw["cls"] = classify(sw)

    top = (sw.groupby("signer")["usd_profit_net"].sum()
             .sort_values(ascending=False).head(a.top_n).index)
    tbl = pd.concat([block(sw[sw["signer"].isin(top)], f"top{a.top_n}"),
                     block(sw, "all")], ignore_index=True)

    print(f"\n=== Table 2: top {a.top_n} attackers by profit ===")
    print(f"  {'class':<6} {'sandwiches':>9}            {'profit':>13}            "
          f"{'avg':>7}  {'WR':>6}  {'WR|$':>6}  {'cover':>6}")
    show(tbl[tbl["population"] == f"top{a.top_n}"])
    print(f"\n=== the surrounding prose: all {att['attacker'].nunique()} attackers ===")
    show(tbl[tbl["population"] == "all"])

    out = figcfg.outdir(a) / f"tab_geometry_econ_{figcfg.tag(a)}.csv"
    tbl.to_csv(out, index=False)
    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
