"""Tables 5-7 (App F): the lookup tables for the entities the paper names.

    tab:appd-attacker    Tab. 5    attackers, ranked by net profit
    tab:appd-validator   Tab. 6    validators flagged in the association analysis
    tab:appd-prog        Tab. 7    attacker custom programs

Tab. 6 additionally needs the StakeWiz snapshot (`0_crawl_stakewiz.py`) and the association
cache (`sec7_build_assoc.py`); Tab. 7 additionally needs the per-leg frame
`sec6_measure_charts.load_legs` caches. Each is skipped with a message if its input is
absent.

Output: tab_appd_{attacker,validator,prog}_<tag>.csv in `--out-dir`.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figcfg
from sec6_measure_charts import CORE_PROGRAMS, VENUE_PROGRAMS, load_legs, leg_programs
from sec7_cohort_heatmap import ASSOC, BASE, PEAK_OFFSET, MIN_ETA, MIN_PAIR_N, core_members

STAKEWIZ = figcfg.INTENT / "data" / "stakewiz" / "validators.csv"
TOP_N = 20


def attacker_table(a, att, sw):
    """Table 5: one row per attacker, ranked by net profit."""
    g = sw.groupby("signer")
    d = pd.DataFrame({
        "sandwiches": g.size(),
        "profit_usd": g["usd_profit_net"].sum(),
    })
    d["avg_usd"] = d["profit_usd"] / d["sandwiches"]
    # WR is the \\$SOL-denominated rate, per the caption: profit and fee share a unit, so
    # no price table enters it and coverage cannot move it.
    d["wr_sol"] = g["is_profitable_sol"].mean()
    # mean_SC and the front-gap columns are taken from phase 1's attacker-level features.
    f = att.set_index("attacker")
    f = f[~f.index.duplicated()]
    d["mean_SC"] = f["mean_SC"]
    d["fg_median"] = f["front_gap_p50"]
    d["fg_eq1_share"] = f["front_gap_le1_ratio"]
    return d.sort_values("profit_usd", ascending=False)


def validator_table(a):
    """Table 6: validators flagged in the association analysis, with their metadata."""
    if not ASSOC.exists():
        print("  SKIPPED: no association cache — run sec7_build_assoc.py")
        return None
    A = pd.read_parquet(ASSOC)
    Bo = pd.read_parquet(BASE)
    B = Bo[Bo["offset"] == PEAK_OFFSET].set_index("leader")["freq"]
    team2 = set(core_members(A, B))

    d = A[A["offset"] == PEAK_OFFSET].copy()
    d["eta"] = (d["n"] / d["n_sand"]) / d["validator"].map(B)
    f = d[(d["eta"] >= MIN_ETA) & (d["n"] >= MIN_PAIR_N)]
    t = (f[f["attacker"].isin(team2)]
         .groupby("validator")
         .agg(team2_members=("attacker", "nunique"), eta_mean=("eta", "mean"),
              eta_max=("eta", "max"), n=("n", "sum"))
         .sort_values(["team2_members", "n"], ascending=False))

    if STAKEWIZ.exists():
        # `slot_leaders.leader` is the node identity, so join on `identity`.
        v = pd.read_csv(STAKEWIZ).set_index("identity")
        v = v[~v.index.duplicated()]
        for col in ("name", "commission", "is_jito", "ip_city", "ip_country", "ip_asn",
                    "ip_org", "stake_ratio"):
            if col in v.columns:
                t[col] = v[col].reindex(t.index)
        missed = int(t["name"].isna().sum()) if "name" in t.columns else 0
        if missed:
            print(f"  NOTE: {missed} of {len(t)} validators are absent from the StakeWiz "
                  f"snapshot (it is a point-in-time crawl; a validator that has since left "
                  f"the set is not in it)")
    else:
        print(f"  NOTE: {STAKEWIZ} is missing — metadata columns omitted "
              f"(run 0_crawl_stakewiz.py)")
    return t


def program_table(a, sw, legs, top_n=10):
    """Table 7: the custom programs carrying the most sandwiches.

    "Custom" means outside `CORE_PROGRAMS | VENUE_PROGRAMS`; the ranking and both sets
    come from `sec6_measure_charts`, so this table and Fig. 17 name the same programs.
    """
    progs = leg_programs(legs)
    cand = progs[~progs["prog"].isin(CORE_PROGRAMS | VENUE_PROGRAMS)]
    rank = cand["prog"].value_counts().head(top_n)
    usd = sw["usd_profit_net"]
    sgn = sw["signer"].astype(str)
    rows = []
    for prog in rank.index:
        sids = set(cand.loc[cand["prog"] == prog, "sid"])
        m = sw.index.astype(str).isin(sids)
        rows.append({"program": prog, "sandwiches": int(m.sum()),
                     "profit_usd": float(usd[m].sum()),
                     "attackers": int(sgn[m].nunique())})
    return pd.DataFrame(rows).sort_values("sandwiches", ascending=False)


def main():
    p = figcfg.add_args(argparse.ArgumentParser(description="Appendix F lookup tables"))
    p.add_argument("--only", choices=["attackers", "validators", "programs"],
                   help="Build one table instead of all three")
    p.add_argument("--top-n", type=int, default=TOP_N)
    a = p.parse_args()
    figcfg.banner(a, "Appendix F lookup tables")
    out = figcfg.outdir(a)
    want = lambda k: a.only in (None, k)

    att = sw = legs = None
    if want("attackers") or want("programs"):
        att = figcfg.load_attackers(a)
        sw = figcfg.load_sandwiches(a, att)

    if want("attackers"):
        T = attacker_table(a, att, sw)
        print(f"\n=== Table 5: attackers (top {a.top_n} of {len(T)}) ===")
        print(f"  {'attacker':<7} {'#Sw':>7} {'Profit($)':>11} {'Avg($)':>8} {'WR%':>6} "
              f"{'SC':>5} {'FG~':>5} {'FG=1%':>6}")
        for x, r in T.head(a.top_n).iterrows():
            print(f"  {x[:5]:<7} {r['sandwiches']:>7,} {r['profit_usd']:>11,.0f} "
                  f"{r['avg_usd']:>8.2f} {r['wr_sol'] * 100:>6.1f} {r['mean_SC']:>5.2f} "
                  f"{r['fg_median']:>5.0f} {r['fg_eq1_share'] * 100:>6.1f}")
        T.to_csv(out / f"tab_appd_attacker_{figcfg.tag(a)}.csv")

    if want("validators"):
        V = validator_table(a)
        if V is not None:
            print(f"\n=== Table 6: validators flagged for Team 2 ({len(V)}) ===")
            cols = [c for c in ("team2_members", "eta_mean", "eta_max", "n", "name",
                                "commission", "is_jito", "ip_city", "ip_country",
                                "ip_asn") if c in V.columns]
            print(V[cols].to_string())
            V.to_csv(out / f"tab_appd_validator_{figcfg.tag(a)}.csv")

    if want("programs"):
        legs = load_legs(a, sw)
        P = program_table(a, sw, legs)
        tot_n = len(sw)
        print(f"\n=== Table 7: top {len(P)} attacker custom programs ===")
        print(f"  {'program':<46} {'#Sw':>8} {'share':>7} {'Profit($)':>11} {'#Att':>5}")
        for _, r in P.iterrows():
            print(f"  {r['program']:<46} {r['sandwiches']:>8,} "
                  f"{r['sandwiches'] / tot_n:>7.1%} {r['profit_usd']:>11,.0f} "
                  f"{r['attackers']:>5}")
        # Deduplicated across the ten: a sandwich can invoke more than one, so the
        # column above does not sum to the paper's aggregate.
        sids = set(leg_programs(legs).query("prog in @P.program")["sid"])
        m = sw.index.astype(str).isin(sids)
        print(f"  {'TEN COMBINED (a sandwich may invoke several)':<46} "
              f"{int(m.sum()):>8,} {m.mean():>7.1%} {sw.loc[m, 'usd_profit_net'].sum():>11,.0f} "
              f"{sw.loc[m, 'signer'].astype(str).nunique():>5}")
        P.to_csv(out / f"tab_appd_prog_{figcfg.tag(a)}.csv", index=False)

    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
