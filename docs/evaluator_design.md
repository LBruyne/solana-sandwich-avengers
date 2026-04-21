# Evaluator Design — Intent Classification

**Purpose**: exact specification of how `evaluator/` turns Watcher-detected sandwiches into attacker-level intent classifications. This document is binding on the code; any deviation is a bug.

## 1. Problem Statement

Watcher detects ~1 M sandwich-shaped transaction patterns on Solana in a 26-day window (epoch 946–958). Not all are intentional attacks — some are coincidental HFT trading. The evaluator partitions signers into **intentional attackers** vs **coincidental**.

## 2. Canonical Definitions (authoritative copy in `docs/definitions.md`)

### Sandwich Categories

- **All sandwiches**: everything emitted by Watcher — textbook in-block, cross-block, multi-split, and signer-changing variants.
- **Standard**: `signerSame = true` AND `multiFrontRun = false` AND `multiBackRun = false`. Includes in-block + cross-block, includes `hasTransfer = true` (transfer-pattern bots). ~95 % of detections.
- **Multi-split**: `signerSame = true` AND (`multiFrontRun = true` OR `multiBackRun = true`). Sub-variants: `multi_front` (front split only), `multi_back` (back split only), `both` (both split).
- **Diff-signer-owner**: `signerSame = false`, `ownerSame = true`, not multi-split. Signer rotation over a shared PDA.
- **Diff-signer-transfer**: `signerSame = false`, `ownerSame = false`, `hasTransfer = true`. Detected by Watcher, **excluded from the classifier pipeline** (empirical justification in §7b).

### Jito Bundle Sandwich
All front-run, victim(s), and back-run txs share **one** `bundleId`. This is the strongest ordering-control proof.

### Profit & Slippage

- **USD profit**: `profitA × token_price[tokenA]`. Tokens without price data are dropped. Never sum raw profitA across different tokenA.
- **SOL profit**: only when `tokenA = SOL`, human-readable SOL units.
- **Slippage consumption (per-victim)**: how close the actual execution was to the victim's `min_amount_out` / `max_amount_in`. Range `[0, 1]`. Special codes: `-1` (no protection), `-2` (unsupported DEX), `-3` (missing inner instructions).
- **Slippage (per-sandwich)**: `max` over valid-range victim values. If any victim is `-2` / `-3` the sandwich value is `-2`.
- **Slippage (per-signer, `mean_slippage`)**: mean over valid-range per-sandwich values; NaN when a signer has no valid samples. NaN ≥ 0.75 is False, so the slippage gate silently excludes no-data signers.

### Front Gap (`fg`) and Back Gap (`bg`)

`fg` = tx-count distance front-run → first victim. `bg` = tx-count distance last victim → back-run. For cross-block, we convert to unified tx-count using `slot_txs.txCount`:
```
slot_distance(a, b) = (txCount[slot_a] − pos_a) + Σ_{s=slot_a+1..slot_b-1} txCount[s] + pos_b
```
Gaps > 100 slots use an average `txCount = 1272` for the missing tail.

## 3. The Three Intent Signals

An intentional attacker must satisfy:

1. **Economic motivation** (`win_rate`): attacks are costly; coincidence does not sustain profitability.
2. **Victim observation** (`mean_slippage`): pushing victims to their slippage limit requires observing the limit.
3. **Ordering control** (`P(fg ≤ 100)`): front-running requires a mechanism — Jito bundle, network-layer racing, or validator privilege.

Thresholds are selected at the avg-USD/signer step-change, not fit to a hypothesis (§5.2 below).

## 4. Dataset

**Scope**: epoch 946–958, 13 epochs, 5.616 M theoretical slots, coverage ≥99 %.

**Per-category 946-958 totals** (Phase 1 output):

| Category | Sandwiches | Signers |
|----------|-----------|---------|
| standard | 1,031,347 | 68,146 |
| multi_split | 35,046 | 5,705 |
| diff_signer_owner | 8,952 raw sandwiches → 2,335 merged entities | |
| diff_signer_transfer | (excluded — see §7b) | |

## 5. Pipeline

### 5.1 Phase 1 — `1_signer_data_preparation_and_summary.py`

For each sandwich in the scoped category:
- Recompute slippage consumption from `sandwich_txs.slippageUtilization` (victim rows).
- Compute unified `proximity` (fg) and `back_gap` (bg) using `slot_tx_counts`.
- Detect Jito same-bundle by intersecting `sandwich_txs.inBundle`/signature with `jito_bundles.transactions`.
- Attach `multiFrontRun`, `multiBackRun`, `frontCount`, `backCount` from the sandwiches table.

For `diff_signer_owner` only, a UnionFind pass merges all signers that co-appear as front/back of any sandwich into a single **attacker entity**; downstream "signer" is the merged entity and `n_signers` records how many keys it controls.

Aggregate to per-signer: `sandwich_count`, `active_slot_span`, `sandwich_frequency`, `avg_interval_slots`, `win_rate`, `sol_avg_profit`, `jito_rate`, `jito_count`, `median_proximity`, `mean_slippage`, `in_block_count`, `cross_block_count`, `sol_count`, `nonsol_count`, `sol_win_rate`, `nonsol_win_rate`, `slippage_valid_ratio`, `slippage_no_protection_ratio`, `fg_median`, `bg_median`, `fg_le_{1,2,5,10,20,50,100}_ratio`, `bg_le_{1,2,5,10,20,50,100}_ratio`.

Outputs (`data/1_signer_data_preparation_and_summary/<cat>/`):
- `per_sandwich_metrics_946_958.parquet`
- `signer_features_946_958.{parquet,csv}`
- `signer_summary_all_946_958.csv` (human readable)
- `signer_summary_filtered_946_958.csv` (WR ≥ 0.5 AND USD > 0)
- `charts/` (WR / slippage / fg / count / USD distributions)

### 5.2 Phase 2 — `2_parameter_selection.py`

Threshold justification (diagnostic; not required by Phase 3).

Pool: `CNT ≥ 5, WR ≥ 0.8`. For each of `mean_slippage` and `P(fg ≤ 100)`, band-partition the pool with width 0.05 and compute **avg USD profit per signer** in each band. Thresholds sit at the band where avg-USD jumps ≥2×.

Observed step changes (standard 946–958 pool):

| Signal | Threshold | Below | Above | Jump |
|--------|-----------|-------|-------|------|
| `mean_slippage` | `0.75` | ≈\$700/signer | ≈\$1,400/signer | ≈2× |
| `P(fg ≤ 100)` | `0.60` | ≈\$600/signer | ≈\$1,400/signer | ≈2× |

Meta-thresholds:
- `CNT ≥ 5`: with baseline `WR ≈ 0.44`, `P(WR=1.0 | CNT=5) = 0.015`; below this the observed WR=100 % count matches random expectation.
- `WR ≥ 0.8`: WR distribution is trimodal (peaks at 0, ~0.5, ~1.0); 0.8 sits between the middle and high peaks.

Outputs (`data/2_parameter_selection/<cat>/`): threshold analysis CSVs and transition charts.

### 5.3 Phase 3 — `3_signer_filter.py`

Two deterministic tracks (+ a multi-split addition).

**Track 1 — Jito Bot** (deterministic, ordering-control proof):
- Any signer with ≥1 Jito same-bundle sandwich.
- **Skipped** for `diff_signer_owner` (106 structural Jito entities produce -\$210 aggregate — not attackers) and for `multi_split` (0 same-bundle events in the data; multi-split doesn't fit a bundle).

**Track 2 — Signal Bot** (behavioural):
```
CNT ≥ n_min (default 5)
AND WR ≥ wr_min (default 0.80)
AND mean_slippage ≥ slip_min (default 0.75)
AND P(fg ≤ 100) ≥ fg100_min (default 0.60)
```

**Track 2b — Oneshot Bot** (multi_split only):
```
CNT ≤ n_min
AND WR ≥ wr_min
AND USD_total ≥ oneshot_usd_min (default $100)
```
Rationale: 58 % of multi_split signers have CNT=1; the CNT=5 gate rejects genuine single-shot attackers (e.g. pump.fun launch races). The \$100 USD floor isolates them from CNT=1 noise. Empirically (946-958 data): CNT=1 WR=1.0 signers at USD≥\$100 sit at proximity=1 and slippage≈0.8 — identical to Signal Bot profile on non-CNT signals.

**Variant filter** (multi-split only): `--multi-variant {all, multi_front, multi_back, both}` slices `per_sandwich_metrics` to the matching structural subset and **rebuilds** signer features so thresholds reflect behaviour within the slice.

**Category-specific behaviours** (implemented as conditionals in `classify_signers`):

| Category | Jito track | Signal Bot | Oneshot |
|----------|-----------|-----------|---------|
| `standard` | active | active | n/a |
| `multi_split` | skipped (0 bundles) | active | **active** |
| `diff_signer_owner` | skipped (unprofitable) | active | n/a |
| `diff_signer_transfer` | n/a | n/a | n/a — **excluded from pipeline** |

**Tiers emitted**: `jito_and_signal`, `jito_only`, `signal`, `signal_and_oneshot`, `oneshot`, `unclassified`.

Outputs (`data/3_signer_filter/<cat>/`):
- `bot_signers_946_958.{parquet,csv}` — per-bot features + tier + USD breakdown.
- `bot_sandwiches_946_958.parquet` — raw per-sandwich rows for bots.
- `all_signer_tiers_946_958.csv` — tier for every signer in the category.
- `charts/classification_summary_*.png`, `scatter_slip_fg100_*.png`, `scatter_slip_fg100_avgprofit_*.png`.

### 5.4 Phase 4 — `4_validator_association.py`

For each bot and each of its sandwiches, count leaders at offsets ±0..±4 **leader blocks**. Compute `(signer, validator)` enrichment = `(observed / signer_total) / stake_weighted_expected`. Flag pairs with `enrichment ≥ 5×` AND `near_cnt ≥ 5`. Exclude top-15 trusted validators by stake for cohort analysis.

**Cohort clustering** (Union-Find):
- Two signers merge if they share ≥3 enriched validators.
- Two validators merge if they enrich ≥2 signers.

Outputs (`data/4_validator_association/<cat>/`):
- `signer_leader_summary_946_958.csv` — per-bot windowed top-validator + enrichment.
- `flagged_pairs_946_958.csv` — `(signer, validator)` with enrichment ≥ min.
- `untrusted_pairs_946_958.csv` — same, excluding top-15.
- `cohort_members_946_958.csv` — cohort-level signer list (only when ≥1 cohort exists).
- `charts/` — enrichment overview + cohort fingerprint.

When `flagged_pairs` is empty (observed for `multi_split` and `diff_signer_owner`), Phase 4 still emits empty-but-schema-preserving CSVs so downstream scripts see an explicit "ran, zero enriched" signal.

## 6. Non-Standard Categories

### 6.1 Multi-Split

Three empirical shifts vs standard dictate the extended classifier:

1. **0 Jito same-bundle events** → Track 1 skipped.
2. **CNT distribution is heavy-right-skewed** — 58 % of multi-split signers have CNT=1 (pump.fun launch races).
3. **Multi-front vs multi-back asymmetry** — multi_front is dominated by concurrent-racer artefacts (aggregate loss); multi_back is deliberate exit-splitting (aggregate profit).

The Oneshot track is the response to (2); sub-variant filtering is the tool for (3). See `docs/evaluator_results_intent_multisplit.md` for outcomes.

### 6.2 Diff-Signer-Owner

UnionFind entity merging is in Phase 1 (all signers co-appearing in one sandwich's front/back are merged; transitive closure merges across sandwiches). Track 1 skipped: 106 structural Jito entities across 3,628 raw signers produce aggregate -\$210 — not attackers. Track 2 over entities.

### 6.3 Diff-Signer-Transfer — Excluded

The Watcher detects this category but we removed it from the classifier pipeline. Justification from a test run: 6,920 sandwiches, -\$46 net USD, \$1.78 positive-USD total, 40.8 % profitable fraction (near baseline). The three signers that would pass the Signal Bot gate all hit CNT=5 exactly, WR ∈ {0.80, 1.00}, slip ≈ 0.92, `P(fg ≤ 100)` = 0.60, and **produce \$0 USD profit** — pure threshold-boundary coincidence. Including this category would dilute the bot list with structural false positives for no benefit.

## 7. Reproducibility

```bash
# Inputs (run once per window):
python 0_crawl_sandwiched_me.py  --epoch 946
python 0_crawl_stakewiz.py
python 0_crawl_token_price.py

# Per category (standard / multi_split / diff_signer_owner):
python 1_signer_data_preparation_and_summary.py --start-epoch 946 --end-epoch 958 --category <cat>
python 2_parameter_selection.py                 --start-epoch 946 --end-epoch 958 --category <cat>
python 3_signer_filter.py                       --start-epoch 946 --end-epoch 958 --category <cat>
python 4_validator_association.py               --start-epoch 946 --end-epoch 958 --category <cat>

# Optional multi-split structural sub-variants:
python 3_signer_filter.py --category multi_split --multi-variant multi_front
python 3_signer_filter.py --category multi_split --multi-variant multi_back
python 3_signer_filter.py --category multi_split --multi-variant both
```

All Phase 3 / 4 parameters are CLI-overridable. Defaults reproduce `docs/evaluator_results_*.md`.

## 8. Design Principles

1. **Prefer deterministic proof over statistical inference.** Jito same-bundle is Track 1 because it is proof.
2. **Use statistical aggregation only where N is large.** Hence `CNT ≥ 5` for Signal Bot; we accept some false negatives rather than call CNT=1 traders "attackers".
3. **Profit is validation, not a filter.** Thresholds come from behavioural signals; profit shows up at the threshold transition, confirming the split is real.
4. **Categories are treated independently.** Each non-standard category passes or fails on its own empirical grounds.
5. **One detector, one truth.** The classifier never re-implements detection logic.
