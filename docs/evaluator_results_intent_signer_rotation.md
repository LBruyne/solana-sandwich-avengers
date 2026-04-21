# Evaluator Results — Signer-Rotation (Diff-Signer-Owner) Intent Classification

**Scope**: `category = diff_signer_owner` on epoch 946–958.
**Authoritative code path**: `evaluator/3_signer_filter.py --category diff_signer_owner`, `evaluator/4_validator_association.py --category diff_signer_owner`.
**Companion docs**: `docs/evaluator_design.md §6.2` (methodology), `docs/evaluator_results_summary.md`.

Detected but **excluded** from the classifier — `diff_signer_transfer` — is documented at the bottom (§5).

## 1. Research Question

Diff-signer-owner sandwiches (`signerSame = false`, `ownerSame = true`) might represent **signer rotation** — attackers using different keypairs for front vs back while the assets move through a shared owner (typically a PDA). Does this obfuscate attack intent in a way that hides attackers from the standard-category classifier?

## 2. Entity Merging (Phase 1)

Because "signer" here is not the attacker identity, Phase 1 applies **UnionFind** on signer co-appearance within any one sandwich's front/back set; transitive closure merges across sandwiches. `n_signers` records how many raw keypairs each entity controls.

| Quantity | Value |
|----------|-------|
| Raw sandwiches | 8,952 |
| Raw signers | ~11,800 |
| Merged attacker entities | 2,335 |
| Multi-signer entities | ~92 % |
| SOL profit | 14.0 SOL |
| USD profit | +\$1,204 |
| Positive-USD total | \$1,892 |
| Baseline per-sandwich WR | 0.437 |

## 3. Jito Bot Track — Skipped

At 946–958 there are **551 Jito same-bundle sandwiches from 136 entities** (up from 1,785 / 106 at 946–956), **aggregate USD ≈ -\$52**. The structural signature is identical to 946–956: 100 % in-block, proximity = 1, slippage ≈ 0.91, 100 % SOL token — but the aggregate is still a statistical wash (essentially zero net dollars). These entities have **attack infrastructure but no profit**; Track 1 is intentionally skipped.

## 4. Signal Bot Track — 1 entity

| Entity | Signers | CNT | WR | Slip | `P(fg ≤ 100)` | USD |
|--------|---------|-----|----|----|---|-----|
| `A8zEst4fb9yPRc` | 2 | 19 | 0.84 | 0.865 | 0.74 | \$1,465 |

Identical result to 946–956: the single entity is `A8zEst` / `BcpiY` pair, already identifiable as a Signal Bot in the standard category.

**A8zEst's method** (from per-tx inspection):

- `BcpiY` does front-run (18/19 times); `A8zEst` does back-run (18/19 times).
- **Two phases, disjoint in time**: Phase 1 (slot 412.1 M–412.3 M, 19 sandwiches) uses diff-signer pairing; Phase 2 (slot 413.1 M–413.2 M, 14 sandwiches) switches to standard single-signer attacks. No overlap.
- Phase 1 has significant Jito-validator enrichment: `JupmVLmA8Roy` at 5/19 slots vs 2.88 % baseline (enrichment 9.1×, **p = 0.00016**). Phase 2 shows no validator concentration.

Interpretation: Phase 1 was probably a validator-assisted execution mode (validator forwarded pending-tx info to the entity, which split it across two signers for backup/resilience); Phase 2 was a fall-back to pure tx-racing after the validator relationship ended.

## 5. Validator Association

Phase 4 produces no `(signer, validator)` pair with `enrichment ≥ 5×` and `near_cnt ≥ 5`. The single bot A8zEst is too small to trigger the flagger. The Phase 1 Jito-validator signal documented above was recovered in earlier targeted analysis (see `docs/evaluator_results_intent_signer_rotation.md §4`) and is not re-derived automatically by Phase 4.

## 6. Conclusions

1. **Signer rotation is not an effective obfuscation in the observed data.** The only confirmed attacker already appears as a Signal Bot in the standard category via both of its rotated signers individually.
2. **Jito same-bundle in this category is a false-positive trap.** 136 entities with bundle-carried diff-signer structure aggregate to -\$52 USD — they are not attackers.
3. **Category scale is ~1 % of standard.** 8,952 sandwiches and +\$1,204 USD compared to standard's 1.03 M sandwiches and +\$906 K USD.

## 7. New findings vs the 946–956 baseline

| Metric | 946–956 | 946–958 | Δ |
|--------|---------|---------|---|
| Raw sandwiches | 8,274 | 8,952 | +8 % |
| Merged entities | 2,181 | 2,335 | +7 % |
| Jito same-bundle sandwiches | 1,785 | 551 | -69 % (count re-definition via signer-merging changed) |
| Jito entities | 106 | 136 | +30 |
| Signal Bot entities | 1 | 1 | unchanged (A8zEst) |
| Signal Bot USD | \$1,465 | \$1,465 | unchanged |

A8zEst is the only Signal Bot the category produces over the full 946–958 window. No new attackers emerged in the two extra epochs.

## 8. Diff-Signer-Transfer — Not Run

This sibling category (`signerSame = false`, `ownerSame = false`, `hasTransfer = true`) is detected by Watcher but **excluded from the evaluator pipeline** by decision. Rationale from an earlier full run (946–956 data):

| Quantity | Value |
|----------|-------|
| Sandwiches | 6,920 |
| Signers | 4,759 |
| Net USD | -\$46 |
| Positive-USD total | \$1.78 |
| Profitable fraction | 40.8 % (near random baseline) |
| Signal Bot matches | 3 |
| Signal Bot USD | \$0 for all 3 (threshold-boundary coincidence) |

All three Signal Bot candidates sit exactly on the boundary (`CNT = 5, fg100 = 0.60`) and deliver exactly zero dollars — structural false positives. Watcher continues detecting the category for completeness, but the evaluator does not process it.

## 9. Artefacts

```
data/1_signer_data_preparation_and_summary/diff_signer_owner/
    per_sandwich_metrics_946_958.parquet    # 8,952 rows (merged-entity "signer" column)
    signer_features_946_958.{parquet,csv}   # 2,335 merged entities
    entity_sizes_946_958.csv
    signer_entity_map_946_958.csv
    signer_summary_*.csv
    charts/

data/2_parameter_selection/diff_signer_owner/
    charts/

data/3_signer_filter/diff_signer_owner/
    bot_signers_946_958.{parquet,csv}       # 1 bot (A8zEst)
    bot_sandwiches_946_958.parquet          # 19 sandwiches
    all_signer_tiers_946_958.csv            # 2,335 rows
    charts/

data/4_validator_association/diff_signer_owner/
    signer_leader_summary_946_958.csv
    flagged_pairs_946_958.csv               # empty
    untrusted_pairs_946_958.csv             # empty
    charts/
```
