"""Build frozen, blinded evidence bundles for the expert attacker audit.

A generator, not a test; pytest does not collect it.

Three reviewers judge the same case from the same frozen evidence rather than from their
own live lookups.

A bundle carries raw facts and no derived gate quantity:

    included   leg amounts, token mints, pool balances before/after each leg, positions
               within the block, slot and leader, signatures, fees, the victim's own
               slippage LIMIT and the amount it ACTUALLY received, per-sandwich SOL net,
               and a sample of the account's own transaction history
    withheld   mean_SC, slippageUtilization, front_gap, sol_win_rate, win_rate, bot_type,
               tier, USD figures, and whether the case passed the gates

ACCOUNT HISTORY is sampled around the active period, not from the chain head: each bundle
anchors on the entity's own transactions at three points spread across its active slot
range and pages backwards from each. The anchor used is recorded in the bundle.

BLINDING. Bundles are named `case_NNN` in shuffled order. The map from case to address,
category and stratum is written to `_key.csv`, which the reviewing agents must not open.
Controls are drawn from entities that cleared the count, win-rate and profit gates but
failed SC, failed FG, or failed both.

Needs an RPC helper; see rpc.sh.example.

Usage:
    python build_audit_bundles.py --batch top20      # 20 highest-profit signal bots + 10 controls
    python build_audit_bundles.py --batch full       # all 177 + 90 controls
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time

import pandas as pd

# Scripts live one level below the package root but address `utils.*`, the numbered
# pipeline modules, and `data/` relative to it. Anchor both to the root so they can be
# run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


from utils.db import get_client
from utils.intent import load_phase1

# Both on-chain helpers shell out to a small script with the contract
#     rpc.sh <method> <params-json>   ->   the raw JSON-RPC response on stdout
# rather than building the URL here, so the endpoint's API key stays out of this
# process, out of argv and out of every log line. See rpc.sh.example.
RPC = os.environ.get("SOLANA_RPC_CMD", os.path.join(_ROOT, "rpc.sh"))


def _require_rpc():
    if not os.path.exists(RPC):
        raise SystemExit(
            f"No RPC helper at {RPC}. Copy rpc.sh.example to rpc.sh (or point "
            f"SOLANA_RPC_CMD at your own), then make it executable.")
SLOTS_PER_EPOCH = 432_000
CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]
# Gate values, needed ONLY to build the control strata. They never enter a bundle.
N_MIN, WR_MIN, SC_MIN, FG_MAX, USD_MIN = 100, 0.85, 0.90, 75.0, 10.0
# A fixed seed makes the sample and the case ordering reproducible from the script alone.
SEED = 20260820


def parse_args():
    p = argparse.ArgumentParser(description="Build blinded evidence bundles for the expert audit")
    p.add_argument("--database", default="solwich")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--tag", default="946_990_cl-include")
    p.add_argument("--variant", default="include")
    p.add_argument("--batch", choices=["top20", "full"], default="top20")
    p.add_argument("--sandwich-samples", type=int, default=10,
                   help="detected sandwiches rendered per case (default 8)")
    p.add_argument("--history-anchors", type=int, default=3,
                   help="points across the active range to page history back from (default 3)")
    p.add_argument("--history-per-anchor", type=int, default=40)
    p.add_argument("--decode-per-anchor", type=int, default=4,
                   help="transactions fully decoded per anchor, to show what the account does")
    p.add_argument("--out-dir", default="data/expert_audit")
    return p.parse_args()


def rpc(method, params, retries=2):
    _require_rpc()
    for _ in range(retries + 1):
        try:
            r = subprocess.run([RPC, method, json.dumps(params)],
                               capture_output=True, text=True, timeout=120)
            j = json.loads(r.stdout)
            if "result" in j:
                return j["result"]
        except Exception:
            pass
        time.sleep(1)
    return None


# ── Population ───────────────────────────────────────────────────────────────

def build_population(args):
    """(selected, controls) — the cases to bundle, with their stratum labels.

    Controls come from the stage-1 pool (count, win-rate and profit already cleared) so that they
    differ from the selected set on the two gates under test and on nothing else. Drawing them from
    the whole rejected mass instead would make them trivially separable and the specificity result
    meaningless.
    """
    merged = pd.read_csv(
        f"data/3_attacker_filter/_merged/{args.database}/{args.variant}/"
        f"all_attackers_{args.tag}.csv", index_col=0)
    sig = merged[merged["bot_type"] == "signal"].copy()

    pool_rows = []
    for cat in CATEGORIES:
        try:
            _, sf = load_phase1(cat, args.database, args.tag)
        except SystemExit:
            continue
        if not len(sf):
            continue
        pool = sf[(sf["usd_net_total"] >= USD_MIN) & (sf["sandwich_count"] >= N_MIN)
                  & (sf["sol_win_rate"] >= WR_MIN)].copy()
        pool["category"] = cat
        fsc = pool["mean_SC"] < SC_MIN
        ffg = pool["front_gap_p50"] > FG_MAX
        pool["stratum"] = None
        pool.loc[fsc & ~ffg, "stratum"] = "control_fail_SC"
        pool.loc[~fsc & ffg, "stratum"] = "control_fail_FG"
        pool.loc[fsc & ffg, "stratum"] = "control_fail_both"
        pool_rows.append(pool[pool["stratum"].notna()])
    controls_all = pd.concat(pool_rows) if pool_rows else pd.DataFrame()

    if args.batch == "top20":
        sel = sig.nlargest(20, "usd_net_total")
        quota = {"control_fail_SC": 3, "control_fail_FG": 4, "control_fail_both": 3}
    else:
        sel = sig
        quota = {"control_fail_SC": 20, "control_fail_FG": 40, "control_fail_both": 30}

    rng = random.Random(SEED)
    picks = []
    for stratum, k in quota.items():
        sub = controls_all[controls_all["stratum"] == stratum]
        # Highest-profit first, so a control is a plausible attacker rather than obvious noise; a
        # control the reviewer can dismiss on sight measures nothing.
        sub = sub.nlargest(min(k * 3, len(sub)), "usd_net_total")
        idx = list(sub.index)
        rng.shuffle(idx)
        for a in idx[:k]:
            picks.append((str(a), sub.loc[a, "category"], stratum))
    sel_rows = [(str(a), sel.loc[a, "primary_category"] if "primary_category" in sel.columns
                 else "standard", "selected") for a in sel.index]
    return sel_rows, picks


# ── Evidence ─────────────────────────────────────────────────────────────────

LEG_COLS = ("type", "slot", "position", "signature", "signers", "poolDex",
            "fromToken", "toToken", "fromAmount", "toAmount",
            "fromTotalAmount", "toTotalAmount", "diffA", "diffB",
            "attackerPreBalanceB", "attackerPostBalanceB",
            "poolPreBalanceB", "poolPostBalanceB",
            "slippageLimitType", "slippageLimitAmount", "slippageActualAmount", "fee")


def fetch_sandwich_legs(client, database, ids, lo, hi):
    q = "','".join(ids)
    cols = ", ".join(["sandwichId"] + list(LEG_COLS))
    return client.query_df(f"""
        SELECT {cols} FROM {database}.sandwich_txs
        WHERE slot >= {lo} AND slot < {hi} AND sandwichId IN ('{q}')
          AND type IN ('frontRun', 'victim', 'backRun')
        ORDER BY sandwichId, slot, position""")


def history_around_active(addr, anchors, per_anchor, decode_per_anchor):
    """Signatures from the account's OWN active period, plus a few decoded in full.

    `before` must be a signature the address itself appears in, so the anchors are taken from
    the entity's own detected legs.
    """
    out, decoded, used = [], [], []
    for sig in anchors:
        r = rpc("getSignaturesForAddress", [addr, {"before": sig, "limit": per_anchor}])
        if r is None:
            continue
        used.append(sig)
        out.extend(r)
        for e in r[:decode_per_anchor]:
            t = rpc("getTransaction", [e["signature"],
                                       {"encoding": "json", "maxSupportedTransactionVersion": 0}])
            if not t:
                continue
            msg = (t.get("transaction") or {}).get("message") or {}
            keys = msg.get("accountKeys") or []
            progs = sorted({keys[i["programIdIndex"]]
                            for i in (msg.get("instructions") or [])
                            if isinstance(i.get("programIdIndex"), int)
                            and i["programIdIndex"] < len(keys)})
            meta = t.get("meta") or {}
            decoded.append({
                "signature": e["signature"], "slot": t.get("slot"),
                "err": meta.get("err"), "fee": meta.get("fee"),
                "programs": progs, "n_instructions": len(msg.get("instructions") or []),
                "n_inner": len(meta.get("innerInstructions") or []),
                "log_head": (meta.get("logMessages") or [])[:6],
            })
    if not out:                              # anchor unusable — say so rather than silently drop it
        r = rpc("getSignaturesForAddress", [addr, {"limit": per_anchor}])
        out = r or []
        used = ["<fallback: chain head, no usable in-period anchor>"]
    return out, decoded, used


def render(case_id, meta, legs, sw_rows, hist, decoded, anchors_used):
    """The markdown an expert actually reads. Raw facts only — see the module docstring."""
    L = []
    a = L.append
    a(f"# Case {case_id}")
    a("")
    a("You are auditing ONE on-chain entity. Everything below is raw on-chain data or a direct")
    a("aggregate of it. No score, ranking or classification from our pipeline is included, and")
    a("nothing here tells you whether this entity was selected by our filters.")
    a("")
    a("## 1. Account card")
    a("")
    a(f"- entity address: `{meta['address']}`")
    a(f"- first activity: slot {meta['first_slot']:,} ({meta['first_ts']})")
    a(f"- last activity : slot {meta['last_slot']:,} ({meta['last_ts']})")
    a(f"- active days (distinct UTC days with at least one detected pattern): {meta['active_days']}")
    a(f"- detected sandwich-shaped patterns in window: {meta['n_patterns']:,}")
    a(f"- distinct pools touched: {meta['n_pools']:,}   distinct counter-tokens: {meta['n_tokens']:,}")
    a(f"- net across all detected patterns: {meta['sol_net']:+,.4f} SOL "
      f"(fees paid on its own legs: {meta['fee_sol']:,.4f} SOL)")
    a(f"- patterns with a positive net: {meta['n_pos']:,} of {meta['n_patterns']:,}")
    a("")
    a("## 2. Account transaction history, sampled from its ACTIVE period")
    a("")
    a(f"Anchored on the entity's own transactions at {len(anchors_used)} points across its active")
    a("range, paging backwards from each. This is what the account was doing while it was working,")
    a("not what it did most recently.")
    a("")
    for s in anchors_used:
        a(f"- anchor: `{s}`")
    a("")
    a(f"{len(hist)} signatures retrieved. Error rate in this sample: "
      f"{sum(1 for h in hist if h.get('err')):,}/{len(hist):,}.")
    a("")
    a("### 2a. Fully decoded sample")
    a("")
    for d in decoded:
        a(f"- `{d['signature'][:32]}…` slot {d['slot']:,}  err={d['err']}  fee={d['fee']}")
        a(f"  programs: {', '.join(p[:20] + '…' if len(p) > 20 else p for p in d['programs'])}")
        a(f"  {d['n_instructions']} top-level instructions, {d['n_inner']} with inner")
        for lg in d["log_head"][:3]:
            a(f"  log: {lg[:120]}")
    a("")
    a("## 3. Detected patterns, leg by leg")
    a("")
    a("Quantities between the two legs will NOT match to the digit. The pool price moves between")
    a("them, several venues take their fee in kind, and bonding curves reprice continuously, so a")
    a("position can be closed in substance while the two numbers differ by several percent. Judge")
    a("closure in substance, not by exact equality.")
    a("")
    a("Each block below is one detected pattern. `position` is the index within the block, so the")
    a("ordering of legs inside a slot is visible. Pool balances are the token-B reserve immediately")
    a("before and after that leg. For victim legs, `slippageLimitAmount` is the minimum the victim")
    a("itself asked for in its instruction, and `slippageActualAmount` is what it received.")
    a("")
    for sid, grp in legs.groupby("sandwichId", sort=False):
        r = sw_rows.get(sid, {})
        a(f"### pattern `{sid[:16]}…`")
        a(f"slot(s) {int(grp['slot'].min()):,}–{int(grp['slot'].max()):,} · "
          f"tokenA `{r.get('token_a','?')}` / tokenB `{str(r.get('token_b','?'))[:12]}…` · "
          f"net {r.get('profit', float('nan')):+,.6f} tokenA · "
          f"victims {int(r.get('victim_count', 0))}")
        a("")
        if str(r.get("token_b", "")) == "SOL":
            a("ORIENTATION: the asset held between the legs is **SOL** — the entity sold the SPL "
              "token first and bought it back. A round trip whose closing quantity is SOL closes "
              "against literally any token, so quantity closure carries little information here; "
              "weigh the price displacement and the third party instead.")
        else:
            a("ORIENTATION: the asset held between the legs is the SPL token — the entity bought "
              "it first and sold it back.")
        a("")
        a("| leg | slot | pos | signer | dex | from → to | amount in | amount out | "
          "pool B before → after | victim limit / actual |")
        a("|---|---|---|---|---|---|---|---|---|---|")
        for _, g in grp.iterrows():
            sg = (g["signers"][0] if isinstance(g["signers"], (list, tuple)) and len(g["signers"])
                  else "")
            lim = (f"{g['slippageLimitAmount']} / {g['slippageActualAmount']}"
                   if g["type"] == "victim" else "")
            a(f"| {g['type']} | {int(g['slot'])} | {int(g['position'])} | `{str(sg)[:10]}…` | "
              f"{g['poolDex']} | `{str(g['fromToken'])[:8]}…`→`{str(g['toToken'])[:8]}…` | "
              f"{g['fromAmount']} | {g['toAmount']} | "
              f"{g['poolPreBalanceB']} → {g['poolPostBalanceB']} | {lim} |")
        a("")
    a("## 4. What you must decide")
    a("")
    a("Apply the codebook you were given. Record a verdict of **yes**, **ambiguous** or **no**,")
    a("the per-criterion findings, and one sentence of reasoning citing specific evidence above.")
    return "\n".join(L)


def main():
    args = parse_args()
    client = get_client(args.database)
    lo = args.start_epoch * SLOTS_PER_EPOCH
    hi = (args.end_epoch + 1) * SLOTS_PER_EPOCH

    sel, ctl = build_population(args)
    cases = [(a, c, s) for a, c, s in sel] + [(a, c, s) for a, c, s in ctl]
    rng = random.Random(SEED)
    rng.shuffle(cases)
    print(f"=== Evidence bundles: {len(sel)} selected + {len(ctl)} controls = {len(cases)} cases ===")

    bundle_dir = os.path.join(args.out_dir, "bundles", args.batch)
    os.makedirs(bundle_dir, exist_ok=True)
    key_rows = []

    ps_cache = {}
    for i, (addr, cat, stratum) in enumerate(cases, 1):
        case_id = f"case_{i:03d}"
        if cat not in ps_cache:
            ps_cache[cat] = load_phase1(cat, args.database, args.tag)[0]
        ps = ps_cache[cat]
        mine = ps[ps["signer"] == addr]
        if not len(mine):
            print(f"  {case_id}: NO patterns for {addr[:12]}… in {cat}; skipped")
            continue

        # Uniform random, NOT profit-ranked. The first batch drew from the top of the profit
        # distribution, which showed reviewers the entity's outliers rather than its ordinary
        # behaviour -- and an entity is judged on what it usually does.
        smp = mine.sample(min(args.sandwich_samples, len(mine)), random_state=SEED)
        legs = fetch_sandwich_legs(client, args.database, list(smp.index.astype(str)), lo, hi)

        # History anchors: the entity's OWN legs at points spread across the active range.
        # Queried directly rather than reused from the rendered sample, whose slots cluster
        # because it is chosen by profit.
        own = mine.sort_values("slot")
        qs = [int(own.iloc[int(q * (len(own) - 1))]["slot"])
              for q in (0.1, 0.5, 0.9)][:args.history_anchors]
        anchor_sigs = []
        if qs:
            ids_q = "','".join(str(x) for x in own[own["slot"].isin(qs)].index[:60])
            aq = client.query_df(f"""
                SELECT slot, signature FROM {args.database}.sandwich_txs
                WHERE slot IN ({','.join(str(s) for s in qs)})
                  AND sandwichId IN ('{ids_q}')
                  AND type IN ('frontRun', 'backRun')
                  AND has(signers, '{addr}')
                ORDER BY slot""")
            if len(aq) == 0:
                # A diff_signer_owner entity may sign nothing at all; anchor on any leg of its own
                # patterns instead, which still references the account.
                aq = client.query_df(f"""
                    SELECT slot, signature FROM {args.database}.sandwich_txs
                    WHERE slot IN ({','.join(str(s) for s in qs)})
                      AND sandwichId IN ('{ids_q}')
                      AND type IN ('frontRun', 'backRun')
                    ORDER BY slot""")
            for s in qs:
                sub = aq[aq["slot"] == s]
                if len(sub):
                    anchor_sigs.append(str(sub.iloc[0]["signature"]))
        if not anchor_sigs and len(legs):
            anchor_sigs = [str(legs.iloc[0]["signature"])]
        hist, decoded, used = history_around_active(
            addr, anchor_sigs, args.history_per_anchor, args.decode_per_anchor)

        meta = {
            "address": addr,
            "first_slot": int(mine["slot"].min()), "last_slot": int(mine["slot"].max()),
            "first_ts": str(mine["ts"].min()), "last_ts": str(mine["ts"].max()),
            "active_days": int(pd.to_datetime(mine["ts"]).dt.date.nunique()),
            "n_patterns": int(len(mine)),
            "n_pools": int(mine["pool_dex"].nunique()) if "pool_dex" in mine else 0,
            "n_tokens": int(mine["token_b"].nunique()),
            "sol_net": float(mine.loc[mine["token_a"] == "SOL", "profit"].sum()
                             - mine["fee_sol"].sum()),
            "fee_sol": float(mine["fee_sol"].sum()),
            "n_pos": int((mine["profit"] > 0).sum()),
        }
        sw_rows = smp.to_dict("index")
        md = render(case_id, meta, legs, sw_rows, hist, decoded, used)
        path = os.path.join(bundle_dir, f"{case_id}.md")
        with open(path, "w") as fh:
            fh.write(md)
        key_rows.append({
            "case_id": case_id, "address": addr, "category": cat, "stratum": stratum,
            "selected": stratum == "selected",
            "bundle_sha256": hashlib.sha256(md.encode()).hexdigest(),
            "n_legs": int(len(legs)), "n_history": len(hist), "n_decoded": len(decoded),
            "anchor_fallback": any(u.startswith("<fallback") for u in used),
        })
        print(f"  {case_id}: {addr[:12]}… {cat:18s} legs={len(legs):>3} hist={len(hist):>3} "
              f"{'FALLBACK' if key_rows[-1]['anchor_fallback'] else ''}")

    k = pd.DataFrame(key_rows)
    kp = os.path.join(args.out_dir, f"_key_{args.batch}.csv")
    k.to_csv(kp, index=False)
    print(f"\nBundles -> {bundle_dir}/  ({len(k)} cases)")
    print(f"SEALED KEY -> {kp}   reviewing agents must NOT read this file")
    print(f"  selected {int(k['selected'].sum())} / controls {int((~k['selected']).sum())}  "
          f"({k['stratum'].value_counts().to_dict()})")
    if k["anchor_fallback"].any():
        print(f"  {int(k['anchor_fallback'].sum())} case(s) fell back to chain-head history — "
              f"their bundles say so explicitly")


if __name__ == "__main__":
    main()
