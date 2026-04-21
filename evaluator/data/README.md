# Evaluator Output Datasets — epoch 946-958

End-to-end outputs of the evaluator pipeline, consumed by `analyst/` and `overleaf-paper/`.

**Measurement window**: epoch 946–958 (13 epochs, ~26 days, 5,616,000 theoretical slots).

## Pipeline

```
0_crawl_sandwiched_me.py       ->  data/sandwiches_site_epoch_*.csv (sandwiched.me snapshot)
0_crawl_stakewiz.py            ->  data/stakewiz/validators.csv (validator identity + stake + ASN)
0_crawl_token_price.py         ->  data/token_prices/prices.csv (USD price per tokenA, Moralis)

1_signer_data_preparation_and_summary.py --category X
    -> data/1_signer_data_preparation_and_summary/X/
       - per_sandwich_metrics_946_958.parquet  # one row per sandwich, all computed metrics
       - signer_features_946_958.{parquet,csv} # aggregated to signer, used by Phase 3
       - signer_summary_all_946_958.csv        # human-readable per-signer summary
       - signer_summary_filtered_946_958.csv   # only WR>=0.5 & USD>0 signers
       - charts/                               # WR / slippage / fg / USD distributions

2_parameter_selection.py --category X
    -> data/2_parameter_selection/X/
       - threshold analysis CSVs + avg-USD/signer transition charts

3_signer_filter.py --category X [--multi-variant V]
    -> data/3_signer_filter/X/
       - bot_signers_946_958.{parquet,csv}     # THE bot list for this category
       - bot_sandwiches_946_958.parquet        # all sandwiches executed by those bots
       - all_signer_tiers_946_958.csv          # tier assignment for every signer
       - charts/                               # scatter slip vs fg100 + WR / slip histograms

4_validator_association.py --category X
    -> data/4_validator_association/X/
       - signer_leader_summary_946_958.csv     # per-bot windowed validator enrichment
       - flagged_pairs_946_958.csv             # (signer, validator) with enrichment >= min
       - untrusted_pairs_946_958.csv           # same but excluding top-15-stake validators
       - cohort_members_946_958.csv            # present only when >=1 cohort found
       - charts/                               # enrichment overview + cohort fingerprint
```

## Active Categories (epoch 946-958)

| Category | Sandwiches | Signers | Net USD | Bots | Bot SW | Bot USD | % of positive USD |
|----------|-----------|---------|---------|------|--------|---------|-------------------|
| `standard` | 1,031,347 | 68,146 | +\$905,873 | **359** | 57,967 | **\$774,681** | 32.6 % |
| `multi_split` | 35,046 | 5,705 | -\$21,501 | **100** | 448 | **\$53,998** | 58.1 % |
| `diff_signer_owner` | 8,952 | 2,335 | +\$1,204 | **1** | 19 | **\$1,465** | 77.4 % |

`diff_signer_transfer` is **not in the pipeline** — see `docs/evaluator_design.md §7b`: 6,920 sandwiches, net ~-\$46, no reliable attacker. Watcher still detects the category.

## Multi-Split Sub-Variants (epoch 946-958)

`3_signer_filter.py --category multi_split --multi-variant {multi_front,multi_back,both,all}` writes to `data/3_signer_filter/multi_split/<variant>/`.

| Variant | Bots | Bot sandwiches | Bot USD |
|---------|------|---------------|---------|
| `all` | 100 | 448 | \$53,998 |
| `multi_front` | 14 | 28 | \$1,513 |
| `multi_back` | 83 | 378 | \$54,358 |
| `both` | 1 | 1 | \$116 |

## Canonical Paths for Downstream Consumers

For each active category X in {standard, multi_split, diff_signer_owner}:

**Attacker set**: `data/3_signer_filter/X/bot_signers_946_958.parquet`
Full per-signer feature set including `tier` (jito/signal/oneshot), CNT, WR, slippage, FG distribution (fg_median/bg_median, fg_le_{1,2,5,10,20,50,100}_ratio, bg_le_*_ratio), USD breakdown (total / sol_part / nonsol_part / price_coverage), Jito co-bundle count.

**Their sandwiches**: `data/3_signer_filter/X/bot_sandwiches_946_958.parquet`
One row per sandwich executed by a bot: slot, tokens, profit, proximity/back_gap, slippage_consumption, jito_bundle, multi_front/multi_back flags, cross_block.

**Validator association**: `data/4_validator_association/X/signer_leader_summary_946_958.csv`
Per-bot windowed (±0 to ±4 leader blocks) top validator and enrichment.

## Category-Specific Notes

- **standard**: Phase 4 produces 19-row cohort table. Two cohorts: (a) 17-signer Allnodes-ASN cluster sharing 8+ enriched Allnodes validators, 13,914 sandwiches, \$390 K; (b) 2-signer KKK + HwGq using BfgMdL4F (self-operated validator, 100 % presence, enrichment 125-126×), 1,204 sandwiches.
- **multi_split**: Classifier is Signal Bot (repeat attackers) + Oneshot Bot (CNT ≤ 5, WR ≥ 0.8, USD ≥ \$100). Phase 4 emits zero enriched pairs by design — multi-split relies on tx racing, not validator collusion.
- **diff_signer_owner**: Phase 1 applies UnionFind entity merging — the "signer" identity is the merged attacker entity, and the `n_signers` column records how many raw signers it controls.

## Input Data

- `data/stakewiz/validators.csv` — regenerate monthly; Phase 4 uses `identity` as the join key.
- `data/token_prices/prices.csv` — token USD prices crawled at analysis time; Phase 1 & 3 join on `token_a`.
- `data/sandwiches_site_epoch_946.csv` + `data/site_epoch_946/` — sandwiched.me comparison snapshot (used only by `docs/evaluator_results_sandwichedme_comparison.md`).

## Case-Study Figures

- `data/1_signer_data_preparation_and_summary/multi_split/slot_410993036_4way_race.png` — 4-way pump.fun sandwich race timeline, used in `docs/evaluator_results_intent_multisplit.md`.
- `data/1_signer_data_preparation_and_summary/multi_split/slot_410993036_phase1_detail.png` — zoom on the front-run phase.
