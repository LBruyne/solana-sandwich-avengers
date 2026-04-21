# Evaluator Results — Summary (epoch 946–958)

Single-page roll-up of every evaluator result. Each section links to the detailed report.

## 1. Detector Validation vs Sandwiched.me (epoch 946)

Detailed report: `docs/evaluator_results_sandwichedme_comparison.md`.

| Source | Sandwiches |
|--------|-----------|
| Sandwiched.me | 3,707 |
| Ours (standard in-block) | 9,393 |
| **Intersection** | **3,505** (94.6 % of sandwiched.me) |
| Site-only | 202 (62 stricter-threshold, 82 unsupported DEX, ~58 parser edge cases) |
| Ours-only | 5,888 (4,409 unprofitable, 1,479 profitable) |
| Random-sample precision | 150 / 150 valid (0 false positives) |

Detector baseline is 94.6 % recall + ≥2.5× extra coverage + 100 % sample precision.

## 2. Intent Classification — All Three Active Categories

Detailed reports: `docs/evaluator_results_intent_standard.md`, `docs/evaluator_results_intent_multisplit.md`, `docs/evaluator_results_intent_signer_rotation.md`.

| Category | Sandwiches | Signers / Entities | Bots | Bot Sandwiches | Bot USD | % of positive USD |
|----------|-----------|--------------------|------|----------------|---------|-------------------|
| `standard` | 1,031,347 | 68,146 | **359** | 57,967 | **\$774,681** | 32.6 % |
| `multi_split` | 35,046 | 5,705 | **100** | 448 | **\$53,998** | 58.1 % |
| `diff_signer_owner` | 8,952 raw / 2,335 entities | — | **1** | 19 | **\$1,465** | 77.4 % |
| `diff_signer_transfer` | 6,920 | 4,759 | — | — | — | excluded |

**Total identified intentional-attacker USD**: \$830,144.

## 3. Standard — Three Attack Patterns

| Pattern | Evidence | Representative | Sandwiches |
|---------|---------|---------------|-----------|
| Self-operated validator | 100 % presence on one validator | `KKKzQmS38mSxHu7A` + `HwGqFnPY6H2sGFoD` → `BfgMdL4FaNHp5z` (126× enrichment) | 1,204 |
| Multi-validator cluster | 17 signers share 8+ Allnodes / 1 NT-Solutions validators (enrichment 20–30×) | cohort 1 | 13,914 |
| Transaction racing | enrichment `< 2×`, validator distribution matches baseline | majority of 342 Signal Bots | ~43,000 |

17 Jito Bots (up from 8 in 946–956) provide deterministic ordering-control evidence.

## 4. Multi-Split — Structural Asymmetry

| All multi-split sandwiches | Count | Net USD | Positive USD |
|---------------------------|-------|---------|--------------|
| `multi_front` only | 18,592 | **-\$58,766** | \$13,320 |
| `multi_back` only | 12,848 | **+\$49,816** | \$78,314 |
| `both` | 3,606 | -\$12,551 | \$1,309 |

| Variant | Bots | Bot SW | Bot USD |
|---------|------|--------|---------|
| all | 100 | 448 | \$53,998 |
| `multi_front` | 14 | 28 | \$1,513 |
| **`multi_back`** | **83** | **378** | **\$54,358** |
| `both` | 1 | 1 | \$116 |

Multi-back is the deliberate exit-splitting tactic (83 of 100 bots, 101 % of bot USD — overshoot from shared-signer overlap). Multi-front is dominantly concurrent-racer noise.

Classifier adds an **Oneshot Bot** track (`CNT ≤ 5 & WR ≥ 0.8 & USD ≥ \$100`) because 58 % of multi-split signers have CNT=1 (pump.fun launch races); 67 of 100 bots come from this track.

Phase 4 produces zero enriched `(signer, validator)` pairs — multi-split attackers rely on transaction racing, not validator collusion.

## 5. Diff-Signer-Owner — Signer Rotation

Phase 1 merges 11.8 K raw signers into 2,335 attacker entities via UnionFind. Only one Signal Bot emerges: `A8zEst4fb9yPRc` (2 signers, 19 sandwiches, +\$1,465). That entity is **already a Signal Bot in the standard category** — its rotation did not hide it.

The 136 Jito-same-bundle entities across 551 sandwiches aggregate to **-\$52 USD** — structurally matching but systematically unprofitable. Track 1 is skipped.

**Verdict**: signer rotation is not an effective obfuscation strategy in the observed data.

## 6. Diff-Signer-Transfer — Excluded

Watcher detects; evaluator does not process. Empirical justification (from a test run on 946–956): 6,920 sandwiches, net -\$46, \$1.78 positive-USD total, 3 Signal-Bot matches all with \$0 profit — pure threshold-boundary coincidence.

## 7. New findings vs the 946–956 baseline

| Metric | 946–956 | 946–958 | Δ |
|--------|---------|---------|---|
| Total standard sandwiches | 856,900 | 1,031,347 | +20 % |
| Standard bots | 312 | 359 | +47 |
| Standard Jito Bots | 8 | 17 | +9 (more bundle-carried attackers) |
| Standard bot USD | \$668 K | \$775 K | +16 % |
| Standard positive-USD total | \$1.52 M | \$2.37 M | **+57 %** |
| Standard bot share of positive | 44.1 % | 32.6 % | **−11.5 pp** |
| Multi-split bots | 86 | 100 | +14 |
| Multi-split `both` bots | 0 | **1** | first qualifying attacker in this variant |
| Allnodes cluster (standard cohort 1) | 15 signers | 17 signers | growing |
| Diff-signer-owner bots | 1 | 1 | unchanged (still A8zEst) |

Main observations:

1. **Non-bot positive USD grew much faster than bot USD** (+57 % vs +16 %) in epochs 957–958 — likely a surge of HFT / sniper-bot activity on newly-listed mints. Our bot classifier still dominates **net** USD (85.5 %) because non-bot activity is roughly zero-sum among participants.
2. **More Jito bot signers** (8 → 17) indicates a mild uptick in bundle-carried sandwiches.
3. **Allnodes cluster is growing**, supporting continued validator-collusion activity rather than a one-off event.
4. **First multi-split `both` bot emerged**, adding a new data point to the multi-split classifier's lower-right quadrant.

## 8. Dataset Index

All canonical outputs at `evaluator/data/` (see `evaluator/data/README.md` for a file-by-file listing). Consumers:

- `analyst/` reads directly from `evaluator/data/3_signer_filter/<cat>/` and `evaluator/data/4_validator_association/<cat>/`.
- `overleaf-paper/` references this summary + per-category detailed reports for numbers / figures.
