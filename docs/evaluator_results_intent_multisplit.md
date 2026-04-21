# Evaluator Results — Multi-Split Sandwich Intent Classification

**Scope**: `category = multi_split` on epoch 946–958.
**Authoritative code path**: `evaluator/3_signer_filter.py --category multi_split [--multi-variant V]`, `evaluator/4_validator_association.py --category multi_split`.
**Companion docs**: `docs/evaluator_design.md §6.1` (methodology), `docs/evaluator_results_summary.md`.

## 1. Why Multi-Split Needs a Custom Classifier

Multi-split sandwiches (`signerSame = true`, `multiFrontRun = true` OR `multiBackRun = true`) present three empirical shifts vs the standard category:

1. **0 Jito same-bundle events.** Multi-split needs ≥5 slots for front/s + victim/s + back/s, exceeding bundle capacity; cross-block spread also breaks bundle atomicity. Track 1 is skipped.
2. **CNT distribution is heavy-right-skewed.** 58 % of multi-split signers have exactly one sandwich. Most genuine single-shot attackers (e.g. pump.fun launch races) cannot clear the `CNT ≥ 5` gate.
3. **multi_front vs multi_back are asymmetric.**

| All multi-split sandwiches | Count | Net USD | Positive USD |
|---------------------------|-------|---------|--------------|
| `multi_front only` | 18,592 | **-\$58,766** | \$13,320 |
| `multi_back only` | 12,848 | **+\$49,816** | \$78,314 |
| `both` | 3,606 | -\$12,551 | \$1,309 |
| **Total** | **35,046** | **-\$21,501** | \$92,944 |

**multi_back is a deliberate attacker tactic**; **multi_front is dominated by concurrent-racer artefacts** (multiple independent buyers piling into the same pump.fun mint), hence its aggregate loss.

## 2. Classifier (multi_split specialisation)

```
Track 1 (Jito Bot)    : skipped (0 events)
Track 2 (Signal Bot)  : CNT ≥ 5  AND WR ≥ 0.8
                        AND mean_slippage ≥ 0.75
                        AND P(fg ≤ 100) ≥ 0.6
Track 2b (Oneshot)    : CNT ≤ 5  AND WR ≥ 0.8
                        AND USD_total ≥ $100
```

Oneshot's \$100 floor matches the observed step-change in attacker fingerprint within the CNT=1 stratum: `P(prox = 1)` jumps from 2.6 % in `[\$1, \$10]` to 38.7 % in `[\$100, \$1K]` and 45.5 % in `[≥\$1K]` — identical to the Signal Bot profile on all non-CNT signals.

## 3. Results (variant = all)

| Tier | Signers | Sandwiches | USD |
|------|---------|-----------|-----|
| `signal` | 32 | 334 | +\$3,337 |
| `signal_and_oneshot` | 1 | 5 | +\$182 |
| `oneshot` | 67 | 109 | +\$50,479 |
| **Bot total** | **100** (1.75 %) | **448** (1.28 %) | **+\$53,998** |
| Non-bot | 5,605 | 34,598 | -\$75,499 |

Bot share of positive USD = **58.1 %**. Bot profile (medians): CNT = 2, WR = 1.00, slippage = 0.866, `P(fg ≤ 100)` = 0.857, proximity = 5.

## 4. Top Attackers (by USD)

| Signer | Tier | CNT | WR | Prox median | USD |
|--------|------|-----|----|-----|-----|
| D7VbH8Mi7LjkHf | oneshot | 1 | 1.00 | 1 | \$5,732 |
| Cc9ySfEENs87ZG | oneshot | 1 | 1.00 | 1 | \$3,709 |
| 8Nujxr9CkiXdk8 | oneshot | 1 | 1.00 | 1 | \$3,011 |
| 1kEvqTozSpFafN | oneshot | 1 | 1.00 | 58 | \$2,909 |
| C5NweKLAMrL5Vi | oneshot | 1 | 1.00 | 1 | \$2,500 |
| 2ErfaAgC5J8ze4 | oneshot | 1 | 1.00 | 1 | \$2,470 |
| FucoijAxLit9TH | oneshot | 1 | 1.00 | 64 | \$2,417 |
| 5s4F9of2LYCURs | oneshot | 1 | 1.00 | 172 | \$2,012 |
| 2ezv4U5HmPpkt2 | oneshot | 1 | 1.00 | 75 | \$1,977 |
| rDQLEYzRmDUQME | oneshot | 2 | 1.00 | 102 | \$1,697 |

Every top-10 entry is a CNT≤2 one-shot attacker on a pump.fun launch pool. A `CNT ≥ 5` filter would miss all ten.

## 5. Per-Variant Results

| Variant | Bots | Bot SW | Bot USD | Tier mix |
|---------|------|--------|---------|---------|
| `all` | 100 | 448 | \$53,998 | 67 oneshot, 32 signal, 1 both |
| `multi_front` | 14 | 28 | \$1,513 | 12 oneshot, 2 signal |
| **`multi_back`** | **83** | **378** | **\$54,358** | 59 oneshot, 23 signal, 1 both |
| `both` | 1 | 1 | \$116 | 1 oneshot |

**multi_back captures 83 of 100 bots and 100.7 % of the bot USD** (the ~\$360 overshoot comes from shared-signer bots whose sandwiches are split across variants). `both` now has one qualifying bot (new vs 946-956: there were zero).

## 6. Validator Association (Phase 4)

The 100-bot cohort produced **0 `(signer, validator)` pairs** with `enrichment ≥ 5×` and `near_cnt ≥ 5`. No cohort emerged. The max enrichment observed is 11.5× (`9UM8wQ8F5oMi`, 4 sandwiches) — collapses to a single pump.fun bonanza slot (410993036) where 4 oneshot bots co-executed on the same mint. That is a **concurrency artefact, not a collusion signal**.

Conclusion: multi-split attackers rely on **transaction racing**, not validator-level privilege. Consistent with the absence of Jito bundles.

## 7. Case Study — The 4-Way Pump.fun Race at Slot 410993036

Four multi-split oneshot bots executed on the same token (`9AHRc6...pump`) in the same slot window:

| Attacker | Profit | Victims | Back count | Front position |
|----------|--------|---------|-----------|----------------|
| D7VbH8Mi7Ljk | 66.65 SOL (\$5,732) | 10 | 3 | 714 |
| C5NweKLAMrL5 | 29.07 SOL (\$2,500) | 5 | 3 | 715 |
| C1sgP4YVT5uK | 9.80 SOL (\$843) | 3 | 3 | 716 |
| 9AxVkLGc2XcP | 1.33 SOL (\$114) | 1 | 3 | 717 |

Four attackers stacked into consecutive positions 714-717; each later arrival becomes an earlier arrival's victim. Back-runs in slot 037 interleave in a strict cross pattern. Two of the four share the back-run signer `EEpoV7kjN997`, and the four co-appear in three unrelated slot windows attacking different tokens — hinting at owner-level coordination behind the apparent 4-way race.

Figures:
- `evaluator/data/1_signer_data_preparation_and_summary/multi_split/slot_410993036_4way_race.png`
- `evaluator/data/1_signer_data_preparation_and_summary/multi_split/slot_410993036_phase1_detail.png`

Owner-level clustering for multi-split is deferred to future work — the current pipeline stays at signer level for this category.

## 8. New findings vs the 946–956 baseline

| Metric | 946–956 | 946–958 | Δ |
|--------|---------|---------|---|
| Multi-split sandwiches | 26,996 | 35,046 | +30 % |
| Multi-split signers | 5,051 | 5,705 | +13 % |
| Bot signers | 86 | 100 | +14 |
| multi_back bots | 72 | 83 | +11 |
| multi_front bots | 15 | 14 | −1 |
| `both` bots | 0 | **1** | new |
| Bot USD | \$48,625 | \$53,998 | +11 % |
| Bot share of positive | 65.9 % | 58.1 % | −7.8 pp |

First `both`-variant bot in the dataset — a single sandwich, small profit (\$116), but the tier is now populated.

## 9. Artefacts

```
data/1_signer_data_preparation_and_summary/multi_split/
    per_sandwich_metrics_946_958.parquet    # 35,046 rows, includes multi_front/multi_back/front_count/back_count
    signer_features_946_958.{parquet,csv}   # 5,705 rows
    signer_summary_*.csv
    slot_410993036_*.png                    # case study figures
    charts/

data/3_signer_filter/multi_split/
    bot_signers_946_958.{parquet,csv}       # variant=all, 100 bots
    bot_sandwiches_946_958.parquet          # 448 rows
    all_signer_tiers_946_958.csv            # 5,705 rows
    multi_front/    # 14 bots, 28 sandwiches
    multi_back/     # 83 bots, 378 sandwiches
    both/           # 1 bot, 1 sandwich
    charts/

data/4_validator_association/multi_split/
    flagged_pairs_946_958.csv               # 0 rows (by design)
    untrusted_pairs_946_958.csv             # 0 rows
    signer_leader_summary_946_958.csv       # 100 bots
    charts/
```
