"""Table 4 (App E, `tab:fee-fingerprint`): per-attacker fee profile.

One column per attacker named in `--attackers`, given as 5-character address prefixes.

Fees are reported as a multiple of the Solana base fee, 5,000 lamports per signature: a
leg's `sandwich_txs.fee` divided by the base fee for its signature count. `eta_max` is the
attacker's strongest validator over the whole +-2 window, the same quantity phase 4 writes
to `signer_top_validator_<tag>.csv`.

Output: tab_fee_fingerprint_<tag>.csv in `--out-dir`.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg
from sec6_measure_charts import load_legs
from sec7_cohort_heatmap import ASSOC, BASE

BASE_FEE_SOL = 5_000 / 1e9        # one signature at the Solana base rate
DEFAULT = ["HwGqF", "HqXf7", "E45YL"]
MECHANIC = {"HwGqF": "Type I", "HqXf7": "Type II", "E45YL": "Fee racing"}


def eta_max():
    """eta^max_a: each attacker's strongest validator, summed over the whole +-2 window.

    The same quantity phase 4 writes to `signer_top_validator_<tag>.csv`.
    """
    if not ASSOC.exists():
        print("  NOTE: no association cache — eta_max omitted (run sec7_build_assoc.py)")
        return {}
    A = pd.read_parquet(ASSOC)
    freq = pd.read_parquet(BASE).groupby("leader")["freq"].sum()
    t = (A.groupby(["attacker", "validator"])
          .agg(n=("n", "sum"), n_sand=("n_sand", "first")).reset_index())
    t["eta"] = (t["n"] / t["n_sand"]) / t["validator"].map(freq)
    return t.groupby("attacker")["eta"].max().to_dict()


def main():
    p = figcfg.add_args(argparse.ArgumentParser(description="Table 4: fee fingerprints"))
    p.add_argument("--attackers", default=",".join(DEFAULT),
                   help="Comma-separated 5-character address prefixes")
    a = p.parse_args()
    figcfg.banner(a, "Table 4: fee fingerprints")

    want = [x.strip() for x in a.attackers.split(",") if x.strip()]
    att = figcfg.load_attackers(a)
    sw = figcfg.load_sandwiches(a, att)
    sw["signer"] = sw["signer"].astype(str)
    legs = load_legs(a, sw)

    resolved = {}
    for pre in want:
        hit = sorted({s for s in sw["signer"].unique() if s.startswith(pre)})
        if len(hit) != 1:
            raise SystemExit(f"{pre} matches {len(hit)} attackers in this dataset: {hit}")
        resolved[pre] = hit[0]

    etas = eta_max()
    sol_usd = figcfg.sol_usd()
    f = att.set_index("attacker")
    f = f[~f.index.duplicated()]

    rows = []
    for pre, addr in resolved.items():
        s = sw[sw["signer"] == addr]
        L = legs[legs["sid"].isin(set(s.index.astype(str)))]
        fee_total = L[L["type"].isin(("frontRun", "backRun"))]["fee_sol"].sum()
        gross = s["usd_profit"].sum()
        rows.append({
            "attacker": pre, "address": addr, "mechanic": MECHANIC.get(pre, ""),
            "sandwiches": len(s),
            "profit_usd": s["usd_profit_net"].sum(),
            "eta_max": etas.get(addr, float("nan")),
            "cross_block_share": s["cross_block"].astype(bool).mean(),
            "fg_median": f.loc[addr, "front_gap_p50"],
            "front_fee_x": L.loc[L["type"] == "frontRun", "fee_sol"].mean() / BASE_FEE_SOL,
            "back_fee_x": L.loc[L["type"] == "backRun", "fee_sol"].mean() / BASE_FEE_SOL,
            "fee_per_sandwich_sol": fee_total / len(s),
            # Denominator is GROSS profit.
            "fee_over_profit": (fee_total * sol_usd) / gross
            if gross else float("nan"),
        })
    T = pd.DataFrame(rows).set_index("attacker")

    print()
    labels = [("mechanic", "Mechanic", "{}"), ("sandwiches", "# Sandwiches", "{:,.0f}"),
              ("profit_usd", "Profit ($)", "{:,.0f}"), ("eta_max", "eta_max", "{:.1f}x"),
              ("cross_block_share", "Cross-block share", "{:.1%}"),
              ("fg_median", "median FG", "{:.0f}"),
              ("front_fee_x", "Frontrun fee", "{:.0f}x"),
              ("back_fee_x", "Backrun fee", "{:.0f}x"),
              ("fee_per_sandwich_sol", "Fee / sandwich ($SOL)", "{:.1e}"),
              ("fee_over_profit", "Fee / profit", "{:.2%}")]
    print(f"  {'':<24}" + "".join(f"{x:>14}" for x in T.index))
    for col, label, fmt in labels:
        print(f"  {label:<24}" + "".join(f"{fmt.format(T.loc[i, col]):>14}" for i in T.index))

    out = figcfg.outdir(a) / f"tab_fee_fingerprint_{figcfg.tag(a)}.csv"
    T.to_csv(out)
    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
