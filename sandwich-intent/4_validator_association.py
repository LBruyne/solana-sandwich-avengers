"""Phase 4: validator association analysis.

Measures how often each attacker's sandwiches fall near each validator's leader rotations,
against that validator's own share of the chain.

SCOPE

  SIGNAL BOTS ONLY. Jito Bots are excluded: their ordering comes from a bundle, so the
  leader they land under is set by Jito's routing.

  ATTACKER-FILTER OUTPUT ONLY. The population is `bot_attackers` from phase 3, minus the
  entities carrying `expert_verdict == "no"`. No further count or profit threshold.

  SINGLE-LEADER SANDWICHES ONLY. A sandwich is placed at its front-run's slot, so a
  cross-leader sandwich has its back-run in a later rotation by construction.
  `--include-cross-leader` keeps them.

  BOTH CROSS-LEADER VARIANTS are run, into separate directories.

WINDOW: +-2 leader rotations -- the rotation holding the sandwich, two before and two
after, 5 positions. Solana rotations are 4 slots, so this spans roughly 20 slots.

ENRICHMENT, per (attacker a, validator v):

    eta = observed_rate / expected_rate
        = (N_av / |S_a|) / sum_off P(v is leader at offset `off`)

  N_av      times v appears anywhere in a's windows
  |S_a|     a's sandwich count
  P(...)    v's slot-weighted share of that offset across the whole range, measured from
            `slot_leaders` over the same slot range

eta = 1 is chance; eta = 10 is ten times what v's share of the chain predicts.

Usage:
    python 4_validator_association.py --database solwich \\
        --start-epoch 946 --end-epoch 990
"""

import argparse
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from utils.db import get_client

SLOTS_PER_EPOCH = 432_000
CATEGORIES = ["standard", "multi_split", "diff_signer_owner"]

# +-2 leader rotations around the sandwich's own rotation.
OFFSET_MAX = 2
OFFSETS = list(range(-OFFSET_MAX, OFFSET_MAX + 1))

# ── Style ────────────────────────────────────────────────────────────────────
# Same palette and slot assignment as phase 2, so a reader moving between the two
# steps does not have to re-learn which colour means which cross-leader variant.
SURFACE = "#fcfcfb"
SERIES = {"include": "#2a78d6", "exclude": "#eb6834"}
INK = "#1c1c1a"
INK_MUTED = "#6b6b66"
GRID = "#e4e4e0"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK, "axes.titlecolor": INK,
    "xtick.color": INK_MUTED, "ytick.color": INK_MUTED,
    "text.color": INK, "font.size": 9.5,
    "axes.spines.top": False, "axes.spines.right": False,
    "grid.color": GRID, "grid.linewidth": 0.8,
})


def parse_args():
    p = argparse.ArgumentParser(description="Phase 4: validator association (Signal Bots)")
    p.add_argument("--database", default="solwich")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990)
    p.add_argument("--min-enrichment", type=float, default=10.0,
                   help="Flag a pair at eta >= this. 10 means ten times the validator's own "
                        "share of the chain (default 10)")
    p.add_argument("--min-near-cnt", type=int, default=5,
                   help="Minimum appearances in the window before a pair can be flagged; stops a "
                        "1-of-1 coincidence scoring as a 100x relationship (default 5)")
    p.add_argument("--cohort-min-shared", type=int, default=3,
                   help="Two attackers are linked when they share this many flagged validators")
    p.add_argument("--trusted-top-n", type=int, default=15,
                   help="Also emit a variant excluding the N largest validators by slot share: a "
                        "big validator is met often by everyone, and its enrichment is the least "
                        "informative kind (default 15)")
    p.add_argument("--include-cross-leader", action="store_true",
                   help="Keep cross-leader sandwiches. Off by default: their back-run sits in a "
                        "later rotation by construction, which credits that rotation's leader at "
                        "offset +1 for free")
    p.add_argument("--out-root", default="data/4_validator_association")
    return p.parse_args()


# ── Population ───────────────────────────────────────────────────────────────

class MissingVerdict(RuntimeError):
    """Phase-3 output predates the expert-review column, so the paper's population
    cannot be reconstructed for that variant. Loud skip, never a silent fallback to
    the unreviewed set."""


def load_signal_bots(database, tag, variant):
    """Signal Bots and their sandwiches, pooled over the three categories.

    Reads phase 3's output as-is, selecting on its own `bot_type` label.
    """
    sf_frames, ps_frames, breakdown = [], [], []
    for cat in CATEGORIES:
        d = f"data/3_attacker_filter/{cat}/{database}/{variant}"
        sf_path = os.path.join(d, f"bot_attackers_{tag}.parquet")
        ps_path = os.path.join(d, f"bot_sandwiches_{tag}.parquet")
        if not (os.path.exists(sf_path) and os.path.exists(ps_path)):
            print(f"    {cat}: no phase-3 output at {d}, skipped")
            continue
        sf = pd.read_parquet(sf_path)
        if "bot_type" not in sf.columns:
            raise SystemExit(
                f"{sf_path} has no `bot_type` column -- it predates the two-track labelling. "
                f"Re-run 3_attacker_filter.py before this step.")
        sf = sf[sf["bot_type"] == "Signal Bot"]
        # Drop the entities the expert panel overturned. Phase 3 annotates rather than
        # filters, so each consumer applies the verdict itself.
        if "expert_verdict" in sf.columns:
            sf = sf[sf["expert_verdict"] != "no"]
        else:
            raise MissingVerdict(sf_path)
        ps = pd.read_parquet(ps_path)
        ps = ps[ps["signer"].isin(sf.index)]
        breakdown.append(f"{cat}={len(sf)}")
        # phase 3 names the index `attacker`; everything downstream here keys on
        # `signer`, which is also what bot_sandwiches uses. Normalise once, at the
        # boundary, rather than carrying two names for one thing.
        sf = sf.reset_index().rename(columns={sf.index.name or "index": "signer"})
        sf_frames.append(sf.assign(origin_category=cat))
        ps_frames.append(ps.assign(origin_category=cat))
    if not sf_frames:
        raise SystemExit(f"no phase-3 output found for {database}/{variant}/{tag}")
    sf = pd.concat(sf_frames, ignore_index=True)
    # A signer classified in two categories would otherwise be counted twice in the
    # denominator |S_a|; its sandwiches are pooled, so its row must be too.
    sf = sf.drop_duplicates("signer", keep="first").set_index("signer")
    ps = pd.concat(ps_frames, ignore_index=True)
    return sf, ps, ", ".join(breakdown)


# ── Leader blocks ────────────────────────────────────────────────────────────

def load_leader_blocks(client, start_slot, end_slot):
    """Consecutive same-leader slot runs, in slot order.

    A "rotation" is one run. Runs are built from the data rather than assumed to be
    4 slots long, because skipped slots make real runs shorter and hard-coding 4
    would misalign every offset after the first gap.
    """
    df = client.query_df(f"""
        SELECT slot, leader FROM slot_leaders
        WHERE slot >= {start_slot} AND slot < {end_slot} ORDER BY slot""")
    if df.empty:
        raise SystemExit(f"slot_leaders is empty over [{start_slot}, {end_slot})")
    slots = df["slot"].to_numpy(dtype=np.int64)
    leaders = df["leader"].to_numpy()
    blocks, cur_leader, cur_start = [], leaders[0], slots[0]
    for i in range(1, len(slots)):
        if leaders[i] != cur_leader or slots[i] != slots[i - 1] + 1:
            blocks.append((cur_start, slots[i - 1], cur_leader))
            cur_leader, cur_start = leaders[i], slots[i]
    blocks.append((cur_start, slots[-1], cur_leader))
    return blocks


def build_slot_to_block_idx(blocks):
    m = {}
    for idx, (s, e, _) in enumerate(blocks):
        for sl in range(int(s), int(e) + 1):
            m[sl] = idx
    return m


def compute_global_offset_freq(blocks):
    """Baseline: each validator's slot-weighted share at each offset.

    Slot-weighted rather than rotation-weighted, so a validator holding longer runs
    is credited for them; that is the share an attacker would meet by chance.
    """
    freq = {off: defaultdict(float) for off in OFFSETS}
    total = sum(int(e) - int(s) + 1 for s, e, _ in blocks)
    for idx, (s, e, _) in enumerate(blocks):
        size = int(e) - int(s) + 1
        for off in OFFSETS:
            j = idx + off
            if 0 <= j < len(blocks):
                freq[off][blocks[j][2]] += size
    for off in OFFSETS:
        for v in freq[off]:
            freq[off][v] /= total
    return freq


def compute_signer_validator_counts(ps, slot_to_block, blocks):
    per_signer = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    signers = ps["signer"].to_numpy()
    slots = ps["slot"].to_numpy(dtype=np.int64)
    missed = 0
    for i in range(len(signers)):
        bidx = slot_to_block.get(int(slots[i]))
        if bidx is None:
            missed += 1
            continue
        for off in OFFSETS:
            j = bidx + off
            if 0 <= j < len(blocks):
                per_signer[signers[i]][blocks[j][2]][off] += 1
    return per_signer, missed


# ── Scoring ──────────────────────────────────────────────────────────────────

BASE_COLS = (["signer", "validator", "signer_total", "near_cnt", "near_pct",
              "expected_pct", "enrichment", "peak_offset", "peak_count"]
             + [f"off_{o}" for o in OFFSETS])


def score_pairs(per_signer, signer_totals, global_freq, min_enrichment, min_near_cnt):
    rows = []
    for signer, vmap in per_signer.items():
        total = signer_totals.get(signer, 0)
        if not total:
            continue
        for validator, offset_cnts in vmap.items():
            near_cnt = sum(offset_cnts.values())
            expected = sum(global_freq[o].get(validator, 0.0) for o in OFFSETS)
            if expected <= 0:
                continue
            near_pct = near_cnt / total
            eta = near_pct / expected
            if eta < min_enrichment or near_cnt < min_near_cnt:
                continue
            dist = {o: offset_cnts.get(o, 0) for o in OFFSETS}
            peak = max(dist, key=dist.get)
            rows.append({"signer": signer, "validator": validator, "signer_total": total,
                         "near_cnt": near_cnt, "near_pct": near_pct,
                         "expected_pct": expected, "enrichment": eta,
                         "peak_offset": peak, "peak_count": dist[peak],
                         **{f"off_{o}": dist[o] for o in OFFSETS}})
    # Fixed schema even when empty, so downstream indexing does not depend on luck.
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=BASE_COLS)


# ── Cohorts ──────────────────────────────────────────────────────────────────

def cluster_cohorts(flagged, cohort_min_shared):
    """Group attackers that answer to the same validators.

    Two mechanisms into one union-find, because they catch different shapes:
      shared-validator   two attackers flagged on >= cohort_min_shared of the same
                         validators -- one operator spreading across a validator set
      single-validator   one validator flagged by >= 2 attackers -- a validator with
                         a pool of signers
    """
    signer_to_vals, val_to_signers = defaultdict(set), defaultdict(set)
    for r in flagged.itertuples():
        signer_to_vals[r.signer].add(r.validator)
        val_to_signers[r.validator].add(r.signer)
    signers = sorted(signer_to_vals)
    parent = {s: s for s in signers}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i, a in enumerate(signers):
        for b in signers[i + 1:]:
            if len(signer_to_vals[a] & signer_to_vals[b]) >= cohort_min_shared:
                union(a, b)
    for _, sigs in val_to_signers.items():
        sl = sorted(sigs)
        for s in sl[1:]:
            union(sl[0], s)

    clusters = defaultdict(list)
    for s in signers:
        clusters[find(s)].append(s)
    cohorts = sorted((m for m in clusters.values() if len(m) >= 2), key=lambda c: -len(c))
    return cohorts, signer_to_vals, val_to_signers


# ── Charts ───────────────────────────────────────────────────────────────────

def chart_enrichment(var, flagged, sf, chart_dir, subtitle):
    """Where the flagged pairs sit: enrichment distribution, and which offset peaks.

    The offset panel is the one that carries the argument. A relationship with the
    validator that ORDERS the sandwich peaks at offset 0; a peak at -1 or +1 says
    the attacker is timing its submission around that validator's turn instead.
    """
    colour = SERIES[var]
    fig, ax = plt.subplots(1, 2, figsize=(11.5, 4.4))
    if len(flagged):
        ax[0].hist(flagged.enrichment, bins=30, color=colour,
                   edgecolor=SURFACE, linewidth=1.2)
        ax[0].set_xlabel("enrichment $\\eta$ (x the validator's own share)")
        ax[0].set_ylabel("flagged pairs")
        counts = flagged.peak_offset.value_counts().reindex(OFFSETS).fillna(0)
        ax[1].bar(range(len(OFFSETS)), counts.to_numpy(), 0.68,
                  color=colour, edgecolor=SURFACE, linewidth=2)
        ax[1].set_xticks(range(len(OFFSETS)))
        ax[1].set_xticklabels([f"{o:+d}" if o else "0\n(own rotation)" for o in OFFSETS])
        ax[1].set_xlabel("leader rotation offset of the pair's peak")
        ax[1].set_ylabel("flagged pairs")
        for i, v in enumerate(counts.to_numpy()):
            if v:
                ax[1].annotate(f"{int(v)}", (i, v), xytext=(0, 4),
                               textcoords="offset points", ha="center", fontsize=9, color=INK)
    else:
        for a in ax:
            a.text(0.5, 0.5, "no pair cleared the thresholds",
                   ha="center", va="center", color=INK_MUTED, transform=a.transAxes)
    ax[0].set_title("Enrichment of flagged pairs", loc="left", fontsize=10.5, pad=8)
    ax[1].set_title("Which rotation the relationship peaks in", loc="left", fontsize=10.5, pad=8)
    for a in ax:
        a.grid(axis="y", alpha=0.55)
        a.set_axisbelow(True)
    fig.suptitle(f"Validator association, Signal Bots only · {subtitle}",
                 x=0.005, ha="left", fontsize=11, y=1.03)
    plt.tight_layout()
    out = os.path.join(chart_dir, "enrichment_overview.png")
    plt.savefig(out, dpi=160, bbox_inches="tight")
    plt.close()
    return out


def chart_top_pairs(var, flagged, chart_dir, subtitle, top_n=20):
    """The strongest relationships, one bar each, so they can be named and checked."""
    if not len(flagged):
        return None
    d = flagged.nlargest(min(top_n, len(flagged)), "enrichment").iloc[::-1]
    labels = [f"{r.signer[:8]}… / {r.validator[:8]}…" for r in d.itertuples()]
    fig, ax = plt.subplots(figsize=(10, max(3.2, 0.34 * len(d))))
    y = np.arange(len(d))
    ax.barh(y, d.enrichment, 0.68, color=SERIES[var], edgecolor=SURFACE, linewidth=1.6)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.5)
    ax.set_xlabel("enrichment $\\eta$")
    ax.axvline(1.0, color=INK_MUTED, linewidth=1.0, linestyle="--")
    ax.annotate("chance", (1.0, len(d) - 0.5), xytext=(4, 0),
                textcoords="offset points", fontsize=8.5, color=INK_MUTED)
    for i, r in enumerate(d.itertuples()):
        ax.annotate(f"{r.enrichment:.0f}x  n={r.near_cnt:,}", (r.enrichment, i),
                    xytext=(4, 0), textcoords="offset points", va="center",
                    fontsize=8.5, color=INK)
    ax.set_title(f"Strongest attacker-validator pairs\n{subtitle}",
                 loc="left", fontsize=11, pad=10)
    ax.grid(axis="x", alpha=0.55)
    ax.set_axisbelow(True)
    plt.tight_layout()
    out = os.path.join(chart_dir, "top_pairs.png")
    plt.savefig(out, dpi=160, bbox_inches="tight")
    plt.close()
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def run_variant(client, a, variant, tag, blocks, slot_to_block, global_freq, stake_share):
    label = "include" if variant == "include" else "exclude"
    out_dir = os.path.join(a.out_root, a.database, f"{label}_XL")
    chart_dir = os.path.join(out_dir, "charts")
    data_dir = os.path.join(out_dir, "data")
    for p in (chart_dir, data_dir):
        os.makedirs(p, exist_ok=True)

    print(f"\n{'=' * 96}\ncross-leader {label}d")
    try:
        sf, ps, breakdown = load_signal_bots(a.database, tag, label)
    except MissingVerdict as e:
        print(f"  SKIPPED: {e} has no `expert_verdict` column. Re-run 3_attacker_filter.py "
              f"for this variant; proceeding without it would use the unreviewed population.")
        return []
    print(f"  Signal Bots: {len(sf):,} ({breakdown}) · {len(ps):,} sandwiches")

    if not a.include_cross_leader:
        if "cross_leader" not in ps.columns:
            raise SystemExit("bot_sandwiches has no `cross_leader` column -- re-run phase 3")
        n_all = len(ps)
        ps = ps[~ps["cross_leader"].astype(bool)]
        print(f"  single-leader only: {len(ps):,} kept, "
              f"{n_all - len(ps):,} cross-leader dropped ({(n_all - len(ps)) / n_all:.2%})")
        # |S_a| is recomputed from `ps` below, so the denominator follows the filter.
        sf = sf[sf.index.isin(ps["signer"].unique())]
        if ps.empty:
            raise SystemExit("no single-leader sandwiches left")

    per_signer, missed = compute_signer_validator_counts(ps, slot_to_block, blocks)
    if missed:
        print(f"  {missed:,} sandwiches ({missed/len(ps):.2%}) sit at slots with no "
              f"slot_leaders row and are dropped from the denominator")
    totals = ps.groupby("signer").size().to_dict()

    flagged = score_pairs(per_signer, totals, global_freq, a.min_enrichment, a.min_near_cnt)
    print(f"  flagged pairs (eta >= {a.min_enrichment:g}, n >= {a.min_near_cnt}): {len(flagged):,}"
          f" · {flagged.signer.nunique() if len(flagged) else 0} attackers"
          f" · {flagged.validator.nunique() if len(flagged) else 0} validators")

    # The largest validators are met often by everyone; their enrichment is the least
    # informative kind, so the same table is emitted with them removed.
    trusted = set(stake_share.nlargest(a.trusted_top_n).index)
    untrusted = flagged[~flagged.validator.isin(trusted)] if len(flagged) else flagged
    print(f"  after removing the top {a.trusted_top_n} validators by slot share: "
          f"{len(untrusted):,} pairs")

    cohorts, signer_to_vals, val_to_signers = cluster_cohorts(flagged, a.cohort_min_shared)
    print(f"  cohorts (>=2 attackers): {len(cohorts)} covering "
          f"{sum(len(c) for c in cohorts)} attackers")
    for i, c in enumerate(cohorts[:5], 1):
        vals = set().union(*(signer_to_vals[s] for s in c))
        print(f"    cohort {i}: {len(c)} attackers · {len(vals)} validators")

    flagged.to_csv(os.path.join(data_dir, f"flagged_pairs_{tag}.csv"), index=False)
    untrusted.to_csv(os.path.join(data_dir, f"untrusted_pairs_{tag}.csv"), index=False)
    pd.DataFrame([{"cohort": i, "signer": s,
                   "n_validators": len(signer_to_vals[s]),
                   "sandwich_count": totals.get(s, 0)}
                  for i, c in enumerate(cohorts, 1) for s in c]
                 ).to_csv(os.path.join(data_dir, f"cohort_members_{tag}.csv"), index=False)
    if len(flagged):
        (flagged.sort_values("enrichment", ascending=False)
                .groupby("signer").head(1)
                .to_csv(os.path.join(data_dir, f"signer_top_validator_{tag}.csv"), index=False))

    sub = (f"epochs {a.start_epoch}–{a.end_epoch} · cross-leader {label}d · "
           f"{len(sf):,} Signal Bots · window ±{OFFSET_MAX} rotations")
    written = [chart_enrichment(label, flagged, sf, chart_dir, sub)]
    tp = chart_top_pairs(label, flagged, chart_dir, sub)
    if tp:
        written.append(tp)
    return written


def main():
    a = parse_args()
    tag = f"{a.start_epoch}_{a.end_epoch}"
    lo, hi = a.start_epoch * SLOTS_PER_EPOCH, (a.end_epoch + 1) * SLOTS_PER_EPOCH
    print("=== Phase 4: validator association (Signal Bots only) ===")
    print(f"Database {a.database} · epochs {a.start_epoch}-{a.end_epoch} · "
          f"window ±{OFFSET_MAX} rotations ({len(OFFSETS)} positions)")
    print(f"Flag at eta >= {a.min_enrichment:g} with >= {a.min_near_cnt} appearances")

    client = get_client(a.database)
    print("\nBuilding leader rotations from slot_leaders ...")
    blocks = load_leader_blocks(client, lo, hi)
    slot_to_block = build_slot_to_block_idx(blocks)
    global_freq = compute_global_offset_freq(blocks)
    sizes = np.array([int(e) - int(s) + 1 for s, e, _ in blocks])
    print(f"  {len(blocks):,} rotations · {sizes.sum():,} slots · "
          f"mean run {sizes.mean():.2f} slots · {len({b[2] for b in blocks}):,} validators")

    stake = defaultdict(int)
    for s, e, v in blocks:
        stake[v] += int(e) - int(s) + 1
    stake_share = pd.Series(stake, dtype=float) / sizes.sum()

    written = []
    for variant in ("include", "exclude"):
        v_tag = f"{tag}_cl-include" if variant == "include" else tag
        written += run_variant(client, a, variant, v_tag, blocks, slot_to_block,
                               global_freq, stake_share)
    print(f"\nSaved under {a.out_root}/{a.database}/")
    for w in written:
        print(f"  {w}")


if __name__ == "__main__":
    main()
