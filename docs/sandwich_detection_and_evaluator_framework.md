# Sandwich Detection & Intent-Classification Framework

**Audience**: analyst team and paper authors.
**Purpose**: a single, precise reference for (a) what sandwiches we detect and how our detector differs from prior work, and (b) how the evaluator turns raw detections into an attacker-level intent classification.

Companion references:
- Pipeline design: `docs/evaluator_design.md`
- Canonical definitions: `docs/definitions.md`
- Watcher internals: `docs/watcher.md`
- DB schema: `docs/database.md`
- Per-category results: `docs/evaluator_results_*.md`

---

## 1. Detector — What We Changed vs Prior Work

Prior MEV tooling (e.g. sandwiched.me, eigenphi) applies a narrow in-block textbook definition to EVM-style ordering. The Solana setting is different in three ways that force changes: (i) leaders can see pending transactions for up to four consecutive blocks and can re-order across that window; (ii) attackers cooperate with validators far more tightly than on Ethereum, making cross-block attacks the common case; (iii) pump.fun-style short-lived pools produce a long tail of micro-attacks at extreme frequency. Our detector keeps the textbook heuristic as its inner kernel and then **adds four orthogonal extensions** to catch realistic attack shapes that prior tools miss.

### 1.1 Baseline heuristic (kept)

For a pair of transactions `(T_f, T_b)` in a single block trading on the same pool, we accept the pair as a sandwich front/back if:

1. **Pool & token consistency** — `T_f` is A→B on pool `P`, `T_b` is B→A on pool `P`.
2. **Amount similarity** — `|F_B − B_B| / max(F_B, B_B) ≤ 10 %` for non-SOL tokenB (5 % in the code: `INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD`), and `F_B ≥ B_B` when tokenB is non-SOL (so the attacker cannot accumulate net inventory). Native-SOL tokenB gets an absolute tolerance of 0.1 SOL (`SANDWICH_AMOUNT_SOL_TOLERANCE`) to account for fee noise.
3. **Victim placement** — at least one intervening tx `T_v` trades A→B on pool `P` with a different signer.
4. **Positional bounds** — `T_f` / `T_b` / victim positions all within `SANDWICH_FRONTRUN_MAX_GAP` (100 positions).

This is the same heuristic used in our micro-benchmark against sandwiched.me.

### 1.2 Extension ①: cross-block detection

**What's different.** A single Solana leader may hold four consecutive slots. Attackers place `T_f` in the last tx of slot `s`, let victims queue in slot `s+1` at any position, and place `T_b` anywhere in slot `s+1` (occasionally `s+2, s+3`). sandwiched.me sees these as two unrelated trades and discards them.

**What we do.** We maintain a `BlockCache` of the most recent 64 blocks. Within any run of consecutive slots whose leader identity is constant, we flatten the transactions into a single virtual ordering and apply the same textbook heuristic. The run is **expanded by one block on each side** so a run boundary does not hide a sandwich whose front-run sits in the preceding leader's last slot. We reject pairs whose front and back land in the same slot (those belong to the in-block detector) and we subject them to the same amount-similarity and victim-placement constraints as the baseline.

**Why this matters.** On epoch-level data, cross-block sandwiches are the dominant form (~80 % of standard). Prior work's "cross-block is rare" claim is an artefact of not looking for them.

### 1.3 Extension ②: multi-split front / back runs

**What's different.** A sophisticated attacker (or a resource-limited one) splits one side of the sandwich into N transactions — e.g. one front-run, three back-run txs — to (a) avoid a single swap exceeding the pool's slippage limit, (b) dilute price impact so victims still execute, or (c) make the pattern harder to spot.

**What we do.** The bucketizer collects all same-signer same-direction swaps within `SANDWICH_FRONTRUN_MAX_GAP` positions into a single `frontSet` / `backSet`. `F_B` / `B_B` then becomes the **sum** across the set. Amount-similarity is checked on the aggregated totals. We record `multiFrontRun=true` / `multiBackRun=true` flags so downstream analysis can filter the structural sub-variants: **multi_front only**, **multi_back only**, **both**.

**Why this matters.** ~4-5 % of standard-form sandwiches are multi-split. Our analysis (§ evaluator_results_intent_multisplit.md) shows multi_back is a deliberate attacker tactic while multi_front is largely ambient racing noise — a distinction invisible without this split.

### 1.4 Extension ③: signer-changing sandwiches

**What's different.** Attackers rotate signing keys — different keypair for front vs back — so per-signer heuristics see two independent traders. On Solana, the same owner can delegate multiple signers to the same token account (PDA), so "different signer" ≠ "different attacker".

**What we do.** When front and back signers disagree, we emit a sandwich if either link holds:

- **Owner overlap** (`ownerSame=true`): the token-B owner set of front-run is a superset of the token-B owner set of back-run. Attacker is the shared owner entity; we keep `signerSame=false` and record `ownerSame=true`.
- **Transfer bridge** (`hasTransfer=true`): one of (a) an **inline transfer** within the front-run or back-run tx moves token B between distinct owners, (b) a **direct transfer tx** between front and back positions transfers token B from a front-run owner to a back-run owner with unique source/sink endpoints.

**Why this matters.** Owner-same captures the PDA-delegation case (≈1 % of attacks, 1 new verified attacker across our dataset). Transfer-bridged captures an even smaller residual. Our analysis finds that **signer rotation is not an effective obfuscation strategy** in practice — one additional Signal Bot beyond standard (and that bot is already a standard Signal Bot in parallel).

### 1.5 Extension ④: adverse-flow tagging

**What's different.** Single-sandwich papers treat each sandwich in isolation. Real Solana traffic has multiple bots competing on the same pool, so a "back-run" for bot A is frequently an "adverse sell" that eats into bot B's sandwich.

**What we do.** For each confirmed sandwich, we record every intervening tx on the same pool that goes in the back-run direction (B→A) by a third party as an `adverse` tx in `sandwich_txs`. This supports later profit/slippage erosion analysis and surfaces bot-vs-bot contention (§ case study in evaluator_results_intent_multisplit.md: 4-way pump.fun race).

### 1.6 Outputs

Per detected sandwich the Watcher writes:

| Table | Key fields |
|-------|-----------|
| `sandwiches` | `sandwichId, slot, tokenA, tokenB, signerSame, ownerSame, hasTransfer, multiFrontRun, multiBackRun, frontCount, backCount, victimCount, adverseCount, crossBlock, consecutive, profitA` |
| `sandwich_txs` | `sandwichId, type ∈ {frontRun, victim, backRun, adverse, transfer}, slot, position, signers, fromToken, toToken, fromAmount, toAmount, slippageLimitType, slippageLimitAmount, slippageUtilization, slippageDexName, inBundle, attackerPreBalanceB, attackerPostBalanceB, poolPreBalanceB, poolPostBalanceB` |
| `slot_txs` | ingestion / coverage bookkeeping |
| `slot_leaders` | slot → leader identity |
| `jito_bundles`, `slot_bundles` | Jito bundle metadata, used to compute `inBundle` |

Slippage is recomputed by the evaluator from `slippageUtilization` (per-victim) aggregated to a per-sandwich max, because upstream parsing was improved after the initial ingest.

---

## 2. Evaluator — Intent Classification

The detector says "these transactions form the shape of a sandwich." It does **not** say "the attacker intended to sandwich anyone." On Solana, many benign structures (HFT rebalancers, pump.fun snipers, concurrent-racer artefacts) produce sandwich shapes by accident. The evaluator answers the intent question.

### 2.1 Causal framing

An intentional attacker must exhibit all three of:

| Signal | What it proves | Metric |
|--------|---------------|--------|
| **Economic motivation** | The attacker has sustained positive expected value | Per-signer `win_rate` — fraction of their sandwiches with `profitA > 0` |
| **Victim observation** | The attacker saw the victim's slippage tolerance and calibrated the front-run amount | Per-signer `mean_slippage` — mean over valid-range victim slippage consumption |
| **Ordering control** | The attacker can place the front-run before the victim — by Jito bundle, tx-racing with network advantage, or validator-level privilege | Per-signer `P(fg ≤ 100)` — fraction of the attacker's sandwiches where the front-run sits within 100 positions of the first victim |

A coincidental HFT satisfies at most one of these at a statistically meaningful level over many sandwiches.

### 2.2 Pipeline

Four stages, one CLI per stage, `--category` to select the sandwich class. Outputs land in `evaluator/data/<phase>/<category>/`.

```
1_signer_data_preparation_and_summary.py   # raw → per-sandwich + per-signer features
2_parameter_selection.py                   # threshold justification charts (diagnostics)
3_signer_filter.py                         # THE bot list (main product)
4_validator_association.py                 # per-bot leader enrichment + cohorts
```

### 2.3 Phase 1 — Data prep

For every sandwich in the scope, recompute:

- **Slippage consumption** = max over victim-level values in `[0, 1]`; -1/-2/-3 encode no-limit/unsupported-DEX/missing-inner-instructions; the per-signer average later uses only valid samples.
- **Unified proximity (fg)** = tx-count distance between front-run and first victim; cross-block → actual tx-count via `slot_tx_counts`.
- **Back gap (bg)** = tx-count distance from last victim to back-run (symmetric to fg).
- **Jito same-bundle** = whether front, all victims, and back share one `bundleId`. The strongest ordering-control proof.

Aggregate to per-signer: `sandwich_count`, `win_rate`, `sol_avg_profit`, `jito_rate`, `jito_count`, `median_proximity`, `mean_slippage`, `in_block_count`, `cross_block_count`, `sol_count`, `nonsol_count`, `slippage_valid_ratio`, `fg_median`, `bg_median`, `fg_le_{1,2,5,10,20,50,100}_ratio`, `bg_le_*_ratio`.

### 2.4 Phase 2 — Threshold selection

Within the pool of `CNT ≥ 5, WR ≥ 0.8` signers we partition on each behavioural signal into bands of width 0.05 and plot **avg USD profit per signer** per band. Profit is never a filter input — it is independent validation. Thresholds are placed at the band where avg-USD jumps ≳2×, which operationally is where the population shifts from "lucky HFT" to "attackers with measurable per-attack economics". The step-change is sharp on both slippage and fg (documented in `docs/evaluator_design.md §5.2`).

### 2.5 Phase 3 — Classifier

Two deterministic tracks plus a multi-split-only addition:

```
Track 1  Jito Bot     : ∃ ≥1 Jito same-bundle sandwich (ordering proof ⇒ attacker).
Track 2  Signal Bot   : CNT ≥ 5  AND  WR ≥ 0.8
                        AND  mean_slippage ≥ 0.75
                        AND  P(fg ≤ 100) ≥ 0.6
Track 2b Oneshot Bot  : multi_split only.  CNT ≤ 5  AND  WR ≥ 0.8  AND  USD_total ≥ $100
```

- For **standard**, both tracks are active. Default thresholds: `n_min=5, wr_min=0.8, slip_min=0.75, fg100_min=0.6`.
- For **multi_split**, Jito Bot is skipped (empirically 0 same-bundle events — multi-split doesn't fit inside a Jito bundle). Track 2 and 2b are both active. Sub-variant filtering (`--multi-variant`) re-computes signer features over the structural slice so thresholds reflect behaviour *within* that variant.
- For **diff_signer_owner**, Jito Bot is skipped (structurally-matching Jito entities are systematically unprofitable at aggregate level). Track 2 runs over **merged attacker entities**, not raw signers: Phase 1 already unioned all signers that co-appear in the same sandwich and stored the merged identity.
- For **diff_signer_transfer**, the whole classifier is skipped: the raw data (6,920 sandwiches, -$46 net, $1.78 positive-USD) contains no reliable attacker; any Track-2 hit is a statistical coincidence at the threshold boundary.

Outputs per category:
- `bot_signers_*.parquet/csv` — the bot list with the full per-signer feature set plus `tier` ∈ {`jito_and_signal`, `jito_only`, `signal`, `signal_and_oneshot`, `oneshot`, `unclassified`} and USD breakdown (total / avg / sol_part / nonsol_part / price_coverage).
- `bot_sandwiches_*.parquet` — every per-sandwich row executed by a bot.
- `all_signer_tiers_*.csv` — tier assignment for every signer in the category.

### 2.6 Phase 4 — Validator association

For each bot and each of its sandwiches, count the leader at `±0, ±1, ±2, ±3, ±4` leader blocks. For every `(signer, validator)` pair compute `presence_share = observed / signer_total` and `enrichment = presence_share / stake_weighted_expected`. A pair is flagged when `enrichment ≥ 5×` and `observed ≥ 5`. Trusted top-15 validators by stake are excluded from cohort analysis.

Cohort discovery via Union-Find: signers sharing ≥3 enriched validators merge; validators enriching ≥2 signers merge. Outputs `flagged_pairs_*.csv`, `untrusted_pairs_*.csv`, `signer_leader_summary_*.csv`, and `cohort_members_*.csv` (present only when ≥1 cohort exists).

### 2.7 Three observed attack patterns (standard category)

| Pattern | Evidence | Scale (sandwiches) |
|---------|---------|-------------------|
| Self-operated validator | 100 % of bot's sandwiches land on one validator, `enrichment > 100×` | ~1 K |
| Multi-validator cluster | ≥3 signers share a common enriched-validator set with matching ASN/hosting fingerprint | ~8 K |
| Transaction racing | validator distribution matches baseline (`enrichment < 2×`) | majority |

Details with specific signer / validator addresses live in `docs/evaluator_results_intent_standard.md`.

---

## 3. Canonical Datasets for Downstream Consumers

**All final outputs live under `evaluator/data/`.** Both analyst (`analyst/`) and paper (`overleaf-paper/`) read from these paths. No copies are maintained elsewhere — if a figure needs new aggregation, add an analyst script that reads these files.

| Purpose | File |
|---------|------|
| Per-sandwich metrics | `data/1_signer_data_preparation_and_summary/<cat>/per_sandwich_metrics_<tag>.parquet` |
| Per-signer features | `data/1_signer_data_preparation_and_summary/<cat>/signer_features_<tag>.parquet` |
| **The bot list** | `data/3_signer_filter/<cat>/bot_signers_<tag>.parquet` |
| **Bot sandwiches** | `data/3_signer_filter/<cat>/bot_sandwiches_<tag>.parquet` |
| All-signer tier assignment | `data/3_signer_filter/<cat>/all_signer_tiers_<tag>.csv` |
| Per-bot leader enrichment | `data/4_validator_association/<cat>/signer_leader_summary_<tag>.csv` |
| Flagged (signer, validator) pairs | `data/4_validator_association/<cat>/flagged_pairs_<tag>.csv` |
| Cohorts | `data/4_validator_association/<cat>/cohort_members_<tag>.csv` (present if non-empty) |

`<cat>` ∈ {`standard`, `multi_split`, `diff_signer_owner`}. Multi-split sub-variants at `data/3_signer_filter/multi_split/{multi_front,multi_back,both}/`. `<tag>` is `<start_epoch>_<end_epoch>` (e.g. `946_958`).

Supporting inputs:
- `data/token_prices/prices.csv` — USD prices per tokenA (Moralis).
- `data/stakewiz/validators.csv` — validator identity, stake, ASN/city (StakeWiz).
- `data/sandwiches_site_epoch_<e>.csv`, `data/site_epoch_<e>/` — sandwiched.me comparison snapshots.

A flat inventory with one-line notes is maintained at `evaluator/data/README.md`.

---

## 4. Reproducibility

```
# Inputs (run once per measurement window):
python 0_crawl_sandwiched_me.py       --epoch 946
python 0_crawl_stakewiz.py
python 0_crawl_token_price.py

# For each category in {standard, multi_split, diff_signer_owner}:
python 1_signer_data_preparation_and_summary.py --start-epoch 946 --end-epoch 958 --category <cat>
python 2_parameter_selection.py                 --start-epoch 946 --end-epoch 958 --category <cat>
python 3_signer_filter.py                       --start-epoch 946 --end-epoch 958 --category <cat>
python 4_validator_association.py               --start-epoch 946 --end-epoch 958 --category <cat>

# Optional multi-split structural sub-variants:
python 3_signer_filter.py --category multi_split --multi-variant multi_front
python 3_signer_filter.py --category multi_split --multi-variant multi_back
python 3_signer_filter.py --category multi_split --multi-variant both
```

All Phase 3/4 parameters are CLI-overridable. Defaults reproduce the numbers in `docs/evaluator_results_*.md`.

---

## 5. Design Principles (for the paper)

1. **Prefer deterministic proof over statistical inference.** Jito same-bundle is always Track 1 because it is a proof, not an estimate.
2. **Use statistical aggregation only where N is large.** Hence `CNT ≥ 5` for Signal Bot; we explicitly accept some false negatives rather than call individual CNT=1 traders "attackers".
3. **Profit is validation, not a filter.** Thresholds come from behavioural signals; profit shows up at the threshold transition, confirming the split is real.
4. **Categories are treated independently.** Each non-standard category passes or fails on its own empirical grounds (diff_signer_transfer fails; diff_signer_owner produces one new attacker; multi_split needs an extra Oneshot track).
5. **One detector, one truth.** The classifier never re-implements detection logic. If a bot's sandwich is controversial, the controversy belongs in the detector.
