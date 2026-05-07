# Evaluator: Sandwich Attacker Identification Pipeline

This module turns the heuristic sandwich detections produced by
[`sandwich-detector`](../sandwich-detector/) into a labelled set of
intentional attackers. Inputs are the ClickHouse tables filled by the
detector (`sandwiches`, `sandwich_txs`, `slot_leaders`, `slot_txs`,
`jito_bundles`); outputs are per-attacker feature tables and the
sandwiches attributed to each attacker.

The pipeline has five steps. Steps 1, 3, and 4 are required; step 0 is
data acquisition (run once per release window); step 2 is a diagnostic
that does not gate downstream steps.

```
ClickHouse (detector tables)
        │
        ▼
0_*  external data — sandwiched.me, validators (StakeWiz), token prices
        │
        ▼
1_signer_data_preparation_and_summary.py    per-sandwich + per-signer features
        │
        ▼
2_parameter_selection.py                    threshold validation (diagnostic)
        │
        ▼
3_attacker_filter.py                        three-track classifier → attackers
        │
        ▼
4_validator_association.py                  attacker ↔ validator enrichment
```

## Setup

```bash
cd sandwich-intent
pip install -r requirements.txt
cp .env.example .env             # then edit ClickHouse credentials
```

The same `.env` is read by every step. Step 4 also reads
`data/stakewiz/validators.csv` produced in step 0.

`data/` is gitignored: every step regenerates its own outputs from
ClickHouse and the inputs of preceding steps. Charts are emitted next to
the data they describe.

## Step 0 — data acquisition

External datasets that are not in ClickHouse. Each script writes to a
sub-directory of `data/` and is independent of the others.

| Script | Output | Purpose |
|---|---|---|
| `0_crawl_sandwiched_me.py --epoch 946` | `data/site_epoch_946/`, `data/sandwiches_site_epoch_946.csv` | Crawl the public sandwiched.me feed for an epoch. Used only as a recall benchmark for the detector. |
| `0_crawl_stakewiz.py` | `data/stakewiz/validators.{csv,parquet}` | Validator metadata snapshot (identity, stake, jito flag, ASN, ip_country). Required by step 4. |
| `0_crawl_token_price.py [--top-n 100]` | `data/token_prices/prices.csv` | USD price snapshot for the top-N `tokenA` values. Used by step 1 and downstream USD aggregates. |
| `0_report_data_overview.py --start-epoch 946 --end-epoch 960` | stdout | Slot coverage + structural summary of the configured window. Sanity check, not a dependency. |

## Step 1 — per-sandwich and per-signer features

```bash
python 1_signer_data_preparation_and_summary.py \
    --start-epoch 946 --end-epoch 960 \
    --category {standard|multi_split|diff_signer_owner}
```

For the chosen structural category and slot window, this step:

- pulls the relevant `sandwiches` and `sandwich_txs` rows from ClickHouse;
- recomputes per-victim slippage consumption from raw `slippageUtilization`
  and aggregates to the sandwich level (`max` over valid victims; `-2`
  if any victim is `-2` or `-3`);
- computes a unified front-gap (`fg`) and back-gap (`bg`) in tx-count
  units, using `slot_txs.txCount` to span cross-block sandwiches;
- detects Jito same-bundle sandwiches by intersecting `inBundle` tx
  signatures with `jito_bundles.transactions`;
- aggregates per-sandwich rows into per-signer features: sandwich count,
  win-rate, mean slippage, fg/bg distributions, USD profit (using the
  step-0 price snapshot), Jito co-bundle count, etc.

For `diff_signer_owner`, a Union-Find pass merges signers that
co-appear in any sandwich's front/back into a single attacker entity;
downstream the "signer" key is the merged entity root, with
`n_signers` recording how many on-chain keys it controls.

Outputs (`data/1_signer_data_preparation_and_summary/<category>/`):

- `per_sandwich_metrics_<tag>.parquet` — one row per sandwich.
- `signer_features_<tag>.{parquet,csv}` — one row per signer; consumed
  by steps 2 and 3.
- `signer_summary_all_<tag>.csv`, `signer_summary_filtered_<tag>.csv`
  — human-readable summaries.
- `charts/` — WR / slippage / fg / count / USD distributions.

Run sequentially for all three categories before moving to step 3.

## Step 2 — threshold validation (diagnostic)

```bash
python 2_parameter_selection.py --category <cat> --start-epoch 946 --end-epoch 960
```

Justifies the classifier thresholds used in step 3. For each signer,
computes p-values for the three signals (win-rate, slippage,
P(fg ≤ 100)) against a null hypothesis of random trading, combines them
via Fisher's method, runs a power analysis (sample size needed to detect
a true effect at a given α), and a two-dimensional sensitivity sweep
across `(N_min, α)`.

Outputs (`data/2_parameter_selection/<category>/`):

- threshold-analysis CSVs, power curves, and sensitivity heatmaps.

This step is diagnostic — its outputs do not feed step 3 or step 4. It
is here so that any change to the step-3 thresholds can be re-justified
empirically.

## Step 3 — attacker filter (three-track classifier)

```bash
python 3_attacker_filter.py --category <cat> --start-epoch 946 --end-epoch 960
python 3_attacker_filter.py --category multi_split --multi-variant {multi_front|multi_back|both|all}
```

Identifies attackers through three deterministic gates. The thresholds
below match the published release; all are CLI-overridable.

**Track 1 — Jito Bot.** Any signer with at least one Jito same-bundle
sandwich (front-run, all victims, and back-run share a single
`bundleId`) and total USD profit ≥ \$10. Same-bundle co-occurrence is
proof of ordering control. The USD floor filters structurally-matching
but unprofitable bundle candidates that surface in `diff_signer_owner`.

**Track 2 — Signal Bot.** All of the following must hold:

- `sandwich_count ≥ 10` (statistical reliability);
- `win_rate ≥ 0.8` (economic motivation);
- `mean_slippage ≥ 0.75` (the attacker pushes victims toward their
  slippage limit, implying the attacker observed the limit);
- `P(fg ≤ 100) ≥ 0.6` (the attacker controls ordering enough to land
  the front-run within 100 transactions of the victim);
- USD total profit ≥ \$10.

The three behavioural signals correspond to the three necessary
conditions for an intentional attacker (economic motivation, victim
observation, ordering control) introduced in the paper.

**Track 2b — Oneshot Bot** (multi_split only). `sandwich_count ≤ 5` AND
`win_rate ≥ 0.8` AND USD ≥ \$100. Captures attackers that only fire
once or twice (e.g. pump.fun launch races) and would be excluded by
Track 2's count gate. The high USD floor filters CNT-1 noise.

For `multi_split`, the optional `--multi-variant` flag re-runs the
classifier on a structural slice (front-only split, back-only split, or
both) and rebuilds signer features within that slice so that thresholds
reflect behaviour within the slice.

Tier values emitted: `jito_only`, `jito_and_signal`, `signal`,
`signal_and_oneshot`, `oneshot`, `unclassified`.

Outputs (`data/3_attacker_filter/<category>/`):

- `bot_attackers_<tag>.{parquet,csv}` — index `attacker`; one row per
  identified attacker with the full feature set including `tier`.
- `bot_sandwiches_<tag>.parquet` — every sandwich attributed to a bot,
  with the `signer` column denoting the attacker entity.
- `pool_signers_<tag>.csv` — signers passing the pre-filter pool gate
  (USD ≥ \$10, CNT ≥ 10, WR ≥ 0.8).
- `all_signer_tiers_<tag>.csv` — tier label for every signer in the
  category.
- `charts/` — pre-filter WR distribution, signal distributions over the
  pool, and final scatter of slip vs. P(fg ≤ 100).

## Step 4 — validator association

```bash
python 4_validator_association.py --start-epoch 946 --end-epoch 960
```

Quantifies how strongly each identified attacker associates with each
validator. The pipeline:

1. **Pool attackers** across the three categories (`bot_attackers` ∪)
   from step 3, restrict to tiers in `{jito_only, jito_and_signal,
   signal}` (oneshot is excluded by default), then apply
   `sandwich_count ≥ cnt_min`. The default 10 yields the 282-attacker
   set the paper §7.1 reports on.
2. **Count co-occurrence**. For each attacker `a` and each of `a`'s
   sandwiches at slot `s`, count which validators lead the slots within
   `±k` leader rotations of `s` (default `k = 4`, i.e. ±4 four-slot
   blocks). `N_{a,v}` is the resulting per-pair count.
3. **Compute enrichment**:
   ```
                      N_{a,v} · N
       η_{a,v}  =  ─────────────────────
                   (2k+1) · N_v · |S_a|
   ```
   where `N` is the total leader slots in the window, `N_v` is `v`'s
   share of leader slots, and `|S_a|` is `a`'s sandwich count. `η = 1`
   means `v` leads the ±k window of `a`'s sandwiches as often as
   stake-weighted random; `η ≥ 5` is the flag threshold.
4. **Cluster**. Two cluster mechanisms feed a single Union-Find:
   shared-validator clustering (two attackers merge if they share at
   least `cohort_min_shared` validators with `η ≥ 5`) and
   single-validator groups (one validator enriches at least 2
   attackers). Connected components of size ≥ 2 form a reported cohort.

Outputs (`data/4_validator_association/all_attackers/`):

- `signer_leader_summary_<tag>.csv` — per-attacker windowed top
  validator and η.
- `flagged_pairs_<tag>.csv` — every `(attacker, validator)` pair with
  `η ≥ min_enrichment`.
- `untrusted_pairs_<tag>.csv` — same, excluding the top-N stake-weighted
  named validators (the "trusted" set).
- `cohort_members_<tag>.csv` — attackers grouped into cohorts.
- `signer_eta_max_<tag>.csv` — `η_max` per attacker (used by §7.1
  figures).
- `charts/` — enrichment distribution and cohort fingerprint.

## End-to-end

```bash
# 0) data acquisition (run once per window)
python 0_crawl_stakewiz.py
python 0_crawl_token_price.py
python 0_crawl_sandwiched_me.py --epoch 946
python 0_report_data_overview.py --start-epoch 946 --end-epoch 960

# 1) per-category feature prep
for cat in standard multi_split diff_signer_owner; do
    python 1_signer_data_preparation_and_summary.py \
        --start-epoch 946 --end-epoch 960 --category $cat
done

# 2) threshold diagnostic (optional)
for cat in standard multi_split diff_signer_owner; do
    python 2_parameter_selection.py \
        --start-epoch 946 --end-epoch 960 --category $cat
done

# 3) per-category classification
for cat in standard multi_split diff_signer_owner; do
    python 3_attacker_filter.py \
        --start-epoch 946 --end-epoch 960 --category $cat
done

# 4) validator association (pooled across categories)
python 4_validator_association.py --start-epoch 946 --end-epoch 960
```

The paper-figure scripts that read the outputs of steps 1, 3, and 4
live under [`../analyst/`](../analyst/).

## Layout

```
0_*.py                  data acquisition
1_*.py                  feature preparation
2_*.py                  threshold diagnostic
3_*.py                  attacker classification
4_*.py                  validator association
utils/db.py             ClickHouse client (env-driven)
utils/programs.py       on-chain program classification (builtin / DEX / aggregator)
data/                   regenerated outputs (gitignored)
```
