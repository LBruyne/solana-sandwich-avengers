# Evaluator Results — Standard Sandwich Intent Classification

**Scope**: `category = standard` on epoch 946–958.
**Authoritative code path**: `evaluator/3_signer_filter.py --category standard`, `evaluator/4_validator_association.py --category standard`.
**Companion docs**: `docs/evaluator_design.md` (methodology), `docs/evaluator_results_summary.md` (cross-category roll-up).

## 1. Scope & Baseline

| Quantity | Value |
|----------|-------|
| Sandwiches | 1,031,347 |
| Signers | 68,146 |
| Net USD profit | +\$905,873 |
| Positive-USD total | \$2,372,968 |
| Baseline per-sandwich WR | 0.434 |
| Baseline mean slippage (valid samples) | 0.715 |
| Baseline `P(fg ≤ 100)` | 0.371 |

In-block share of sandwiches: ~13 %; cross-block: ~87 %.

## 2. Classifier Results

| Tier | Signers | Sandwiches | USD |
|------|---------|-----------|-----|
| `jito_and_signal` | 4 | 89 | +\$3,691 |
| `jito_only` | 13 | 34 | +\$225 |
| `signal` | 342 | 57,844 | +\$770,765 |
| `unclassified` | 67,787 | 973,380 | +\$131,192 |
| **Bot total** | **359** | **57,967** | **+\$774,681** |

Bot share of positive USD = **32.6 %**, of net USD = **85.5 %**. Jito Bot contribution is small (17 signers, \$3.9 K) because bundle-carried sandwiches are rare at measurement time; most intentional attackers rely on transaction racing.

Bot profile (means): WR = 0.925, slippage = 0.893, `P(fg ≤ 100)` = 0.788, median proximity = 14.

## 3. Top Attackers

### 3.1 By USD profit (top 10)

| Signer | Tier | CNT | WR | Slip | FG100 | USD |
|--------|------|-----|----|----|------|-----|
| 7CtW3vMTyNm9u8 | signal | 1,845 | 0.93 | 0.895 | 0.79 | \$59,688 |
| HqXf7BH8F35yyZ | signal | 1,862 | 0.93 | 0.932 | 0.80 | \$59,366 |
| 7nCRbePZDbVyyb | signal | 1,909 | 0.92 | 0.927 | 0.78 | \$57,822 |
| 8qAsHZNPsWriCc | signal | 1,524 | 0.90 | 0.955 | 0.68 | \$47,044 |
| 6m4bqcJyyrvSoy | signal | 1,442 | 0.91 | 0.946 | 0.69 | \$45,326 |
| FSSFn3JDeo68dy | signal | 1,044 | 0.94 | 0.930 | 0.72 | \$38,583 |
| E45YLW6LV2GdvP | signal | 3,179 | 0.98 | 0.976 | 0.80 | \$29,661 |
| 4hASKAobJourFv | signal | 1,317 | 0.87 | 0.954 | 0.66 | \$24,333 |
| DDm1Bc9KuXB7Q2 | signal | 1,270 | 0.87 | 0.957 | 0.61 | \$24,150 |
| F8LoqWUjjbD3xq | signal | 1,262 | 0.86 | 0.944 | 0.63 | \$24,004 |

`E45YLW6LV2GdvP` is the highest-frequency attacker at 3,179 sandwiches with near-perfect metrics (WR 0.98, slip 0.98, fg100 0.80) — a pure transaction-racing operator.

## 4. Validator Association (Phase 4)

| Enrichment tier (±4 leader blocks) | Signers | % of bots |
|-----------------------------------|---------|-----------|
| `<2×` (baseline-like, tx-racing) | 259 | 72 % |
| `2–5×` (moderate) | 70 | 20 % |
| `≥5×` (high enrichment) | 30 | 8 % |

**Three attack patterns** identified:

### 4.1 Self-operated validator

| Signer | → Validator | Presence | Enrichment | Sandwiches |
|--------|-------------|----------|-----------|-----------|
| `KKKzQmS38mSxHu7A` | `BfgMdL4FaNHp5z` (Serverside.com) | **100.4 %** | 126× | 813 |
| `HwGqFnPY6H2sGFoD` | `BfgMdL4FaNHp5z` (Serverside.com) | **100.0 %** | 125× | 391 |

These two signers produce sandwiches **only** when `BfgMdL4FaNHp5z` is the leader — the >100 % presence is a measurement artefact where the signer also appears at ±1 leader blocks of that same validator. This is the textbook self-operated-validator pattern: the validator is the attacker's own node.

### 4.2 Multi-validator cluster (17 signers, Allnodes ASN)

Cohort 1 in `cohort_members_946_958.csv`: **17 signers, 13,914 sandwiches, +\$390,337**. All 17 signers share the same set of enriched validators — 8 validators with `ip_org = "Allnodes Inc."` plus one `"NT SOLUTIONS LLC"` validator — at 1–5 % presence each, with enrichment 20–30× per pair. Presence shares of ~1–5 % times 8 shared validators account for **13–21 % total enriched presence** per signer.

Top validators in the cluster (enrichment order):
`8a4juhtQScHcXP` (Allnodes), `Mwz8VgAEnPtfqS` / "Rain Works 💧MEV💧" (Allnodes), `Sh1ro1CaaVjNui` (Allnodes), `MargusJeV9bkeP` / "MARGUS.ONE" (NT SOLUTIONS), plus 4 more Allnodes validators.

This is a **hosted-validator collusion cluster**: a single operator-cohort rents multiple validator slots on Allnodes and redirects orderflow preferentially to any of them. Explicit proof of validator-level coordination beyond the KKK self-validator case.

### 4.3 Transaction racing (majority)

The remaining ~340 signers show enrichment `< 2×` at ±4 — their validator distribution matches stake-weighted baseline. These attackers achieve ordering control through network-layer racing, not validator coordination.

Cohort 2: `KKKzQmS38mSxHu7A` + `HwGqFnPY6H2sGFoD`, linked because they share the self-validator `BfgMdL4FaNHp5z` (§4.1).

## 5. New findings vs the 946–956 baseline

| Metric | 946–956 | 946–958 | Δ |
|--------|---------|---------|---|
| Standard sandwiches | 856,900 | 1,031,347 | +20 % |
| Standard signers | 59,270 | 68,146 | +15 % |
| Bot signers | 312 | 359 | +47 |
| Jito Bot signers | 8 | 17 | +9 |
| Bot USD | \$668,459 | \$774,681 | +16 % |
| Positive-USD total | \$1,515,679 | \$2,372,968 | +57 % |
| Bot share of positive | 44.1 % | 32.6 % | **−11.5 pp** |
| Phase 4 cohorts | 17 rows | 19 rows | +2 signers in Allnodes cluster |

The two notable changes:

1. **Positive-USD total grew much faster than bot USD** (+57 % vs +16 %). Epochs 957–958 saw a sharp rise in profitable activity outside the bot list — likely elevated HFT / sniper-bot volume on newly-listed mints. The attacker-net remains dominant in **net** USD (85.5 %) because non-bot activity is roughly zero-sum among participants.
2. **Jito Bot count doubled** (8 → 17). More signers emitted at least one same-bundle sandwich in the new data, suggesting a mild uptick in bundle-carried attacks.
3. **Allnodes cluster grew**: cohort 1 membership expanded from 15 to 17 signers sharing the same Allnodes fingerprint.

## 6. Artefacts

```
data/1_signer_data_preparation_and_summary/standard/
    per_sandwich_metrics_946_958.parquet    # 1,031,347 rows
    signer_features_946_958.{parquet,csv}   # 68,146 rows, full feature set
    signer_summary_{all,filtered}_946_958.csv
    charts/                                  # WR / slippage / FG / USD distributions

data/2_parameter_selection/standard/
    signer_pvalues_946_958.csv               # per-signer p-values (diagnostic)
    charts/                                  # threshold-transition charts

data/3_signer_filter/standard/
    bot_signers_946_958.{parquet,csv}        # 359 bots
    bot_sandwiches_946_958.parquet           # 57,967 sandwiches
    all_signer_tiers_946_958.csv             # 68,146 rows
    charts/scatter_slip_fg100_*.png          # the key classifier figure

data/4_validator_association/standard/
    signer_leader_summary_946_958.csv        # 359 rows
    flagged_pairs_946_958.csv                # 141 pairs
    untrusted_pairs_946_958.csv              # excluding top-15
    cohort_members_946_958.csv               # 19 rows (2 cohorts)
    charts/
```
