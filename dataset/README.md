# Solana Sandwich Attacker Dataset

A labelled dataset of sandwich attackers on Solana, derived from on-chain
data over a 15-epoch measurement window. Each row in `attackers` is one
attacker entity; `sandwiches` and `sandwich_txs` carry the underlying
detection at the sandwich and transaction level. The term *attacker*
denotes a classified entity; *signer* denotes the on-chain signer of a
specific transaction. They differ for sandwiches whose front-run and
back-run use distinct keys controlled by the same owner.

The dataset is the output of the detection and classification pipeline in
this repository: [`sandwich-detector/`](../sandwich-detector/) emits the
raw heuristic detections; [`sandwich-intent/`](../sandwich-intent/) filters them to
intentional attackers using win-rate, slippage-consumption, and front-gap
signals plus Jito bundle co-occurrence. See the paper for the full method.

## Files

```
attackers_<start>_<end>.parquet      # 380 rows; one entity per row
sandwiches_<start>_<end>.parquet     # 64,206 rows; one sandwich per row
sandwich_txs_<start>_<end>.parquet   # 590,441 rows; one front/back/victim/adverse tx per row
attackers_<start>_<end>.csv          # CSV files are example slices — see below
sandwiches_<start>_<end>.csv
sandwich_txs_<start>_<end>.csv
aux/
    validators.csv                    # Solana validator metadata snapshot (StakeWiz)
    token_prices.csv                  # tokenA → USD price snapshot (Moralis)
    sandwiched_me_epoch_946.csv       # third-party sandwich list, epoch 946 (sandwiched.me)
build_dataset.py                      # rebuild script
```

The `<start>_<end>` suffix is the inclusive epoch range of the release.
The published release is `946_960`.

### Parquet vs. CSV

The **Parquet files are the canonical dataset** and contain every row.

The **CSV files are example slices**, not a full mirror. They contain
the union of two top-3 % cohorts:

- the top 3 % of attackers by **`usd_total_profit`** (12 attackers), and
- the top 3 % of attackers by **`sandwich_count`** (12 attackers).

In the published `946_960` release these two cohorts overlap on 9
attackers, giving 15 unique attackers. The CSVs include those 15
attackers along with all of their sandwiches and transactions.

This selection covers both the most profitable bots and the highest-
volume bots in a single readable file. For any analysis beyond a quick
look, use the Parquet files.

If you need the full data in CSV form, edit `CSV_EXAMPLE_FRACTION` (or
the slicing logic) in [`build_dataset.py`](build_dataset.py) and re-run
it locally.

## Data window

| | Start | End |
|---|---:|---:|
| Epoch | 946 | 960 |
| Slot  | 408,672,000 | 415,151,999 |

Approximately 15 × 432,000 = 6,480,000 theoretical slots; observed slot
coverage exceeds 99 % over the window. Actual UTC date range depends on
chain timing; consult `slot_leaders` in the source database for exact
boundaries.

## Schema

### `attackers`

One row per attacker entity. For 379 of 380 rows the entity is a single
on-chain key. For one entity (`A8zEst…`, the only one classified in the
`diff-signer` track), `attacker` is the union-find root over a set of
on-chain keys that operate a shared owner / PDA.

| Column                 | Type        | Description |
|------------------------|-------------|-------------|
| `attacker`             | str         | Attacker identifier (on-chain key, or merged entity root for diff-signer). |
| `categories`           | list[str]   | Multi-label tags ⊆ `{standard, multi-split, diff-signer, jito}`. See [Categories](#categories). |
| `sandwich_count`       | int         | Total sandwiches attributed to this entity in the window. |
| `profitable_count`     | int         | Sandwiches with `profit > 0`. |
| `win_rate`             | float       | `profitable_count / sandwich_count`. |
| `usd_total_profit`     | float       | Sum of `usd_profit` over all sandwiches; tokens without a price snapshot contribute 0. |
| `usd_avg_profit`       | float       | Mean `usd_profit`. |
| `sol_total_profit`     | float       | Sum of `profit` restricted to `token_a == "SOL"`, in SOL. |
| `sol_avg_profit`       | float       | Mean `profit` over SOL-base sandwiches; NaN if none. |
| `jito_count`           | int         | Sandwiches whose front + victim(s) + back share a single Jito bundle. |
| `jito_rate`            | float       | `jito_count / sandwich_count`. |
| `mean_slippage`        | float       | Mean of sandwich-level `slippage_consumption` over valid samples; NaN if none. |
| `median_proximity`     | float       | Median `fg` (front-run-to-first-victim distance, in tx count). |
| `fg1_ratio`            | float       | Fraction of sandwiches with `fg == 1`. |
| `fg100_ratio`          | float       | Fraction of sandwiches with `fg <= 100`. |
| `in_block_count`       | int         | Sandwiches with `cross_block == False`. |
| `cross_block_count`    | int         | Sandwiches spanning multiple slots. |
| `multi_split_count`    | int         | Sandwiches with multiple front-run or back-run legs. |
| `n_signing_keys`       | int         | Number of on-chain keys merged into this entity (≥ 2 only for the diff-signer entity; 1 otherwise). |

#### Categories

`categories` is a multi-label set, not a partition. An attacker carries
a tag if at least one of their sandwiches matches that tag.

- **`standard`** — at least one sandwich with a single signer for the
  whole sandwich, single front-run, single back-run.
- **`multi-split`** — at least one sandwich whose front-run and/or
  back-run is split across multiple transactions by the same attacker.
- **`diff-signer`** — at least one sandwich where front-run and back-run
  use different on-chain keys but share the same owner / PDA. Only the
  union-find merged entity (`A8zEst…`) carries this tag in the published
  release.
- **`jito`** — at least one sandwich was a Jito same-bundle sandwich
  (front-run, all victims, and back-run all share a single `bundleId`).
  Orthogonal to the structural tags above.

The `diff-signer-transfer` category that the watcher detects (signer
rotation via on-chain transfers) is intentionally excluded from this
dataset; in the measurement window it produced no entity that survived
the intent classifier (see paper §5b).

### `sandwiches`

One row per attributed sandwich. The `attacker` column is the foreign key
into `attackers`.

| Column                 | Type        | Description |
|------------------------|-------------|-------------|
| `sandwich_id`          | str         | Stable identifier from the detection layer. |
| `attacker`             | str         | Attacker entity (matches `attackers.attacker`). |
| `slot`                 | uint64      | Slot of the sandwich. For cross-block sandwiches, the slot of the first front-run. |
| `token_a`              | str         | The asset the attacker holds (the "target"). `"SOL"` denotes native SOL or wSOL, merged. |
| `token_b`              | str         | The asset the attacker swaps to and back from. |
| `profit`               | float       | Net `tokenA` gain across the sandwich. |
| `usd_profit`           | float       | `profit` × token_a USD price (snapshot in `aux/token_prices.csv`). 0 for tokens not in the snapshot. |
| `is_profitable`        | bool        | `profit > 0`. |
| `victim_count`         | uint16      | Number of victim transactions. |
| `adverse_count`        | uint16      | Number of adverse transactions (pool-direction matches back-run). |
| `cross_block`          | bool        | True if front-run and back-run are in different slots. |
| `multi_split`          | bool        | True if `front_count > 1` or `back_count > 1`. |
| `front_count`          | uint16      | Number of front-run transactions. |
| `back_count`           | uint16      | Number of back-run transactions. |
| `slippage_consumption` | float       | Sandwich-level fraction of victim slippage absorbed; valid in `[0, 1]`, NaN otherwise. See [Slippage](#slippage). |
| `jito_bundle`          | bool        | All of front-run, victim(s), and back-run share one Jito bundle. |
| `fg`                   | int64       | Front-gap: tx-count distance from last front-run to first victim. |
| `bg`                   | int64       | Back-gap: tx-count distance from last victim to first back-run. |
| `front_sig`            | list[str]   | Signatures of all front-run transactions in this sandwich. |
| `victim_sig`           | list[str]   | Signatures of all victim transactions. |
| `back_sig`             | list[str]   | Signatures of all back-run transactions. |

### `sandwich_txs`

One row per non-transfer transaction inside an attributed sandwich. Joins
on `sandwich_id`. Excludes `transfer` rows (intermediate token-bridge
transfers, retained only in the upstream watcher database).

| Column                 | Type        | Description |
|------------------------|-------------|-------------|
| `sandwich_id`          | str         | Foreign key to `sandwiches.sandwich_id`. |
| `sandwich_attacker`    | str         | Foreign key to `attackers.attacker` (the entity owning the sandwich). |
| `type`                 | str         | One of `frontRun`, `backRun`, `victim`, `adverse`. |
| `slot`                 | uint64      | Slot of this transaction. |
| `position`             | int32       | Position of the transaction within the slot. |
| `signature`            | str         | Transaction signature. |
| `tx_signer`            | str         | First signer of the transaction. Equal to `sandwich_attacker` for non-`diff-signer` sandwiches. |
| `from_token`           | str         | Source token. |
| `to_token`             | str         | Destination token. |
| `from_amount`          | float       | Source amount in token units. |
| `to_amount`            | float       | Destination amount in token units. |
| `in_bundle`            | bool        | True if this single transaction landed inside a Jito bundle. |
| `fee`                  | uint64      | Transaction fee in lamports. |
| `programs`             | list[str]   | Program IDs invoked by the transaction. |
| `slippage_consumption` | float       | Victim slippage utilization. Non-NaN only for `type == "victim"`. See [Slippage](#slippage). |

#### Slippage

For victims, the upstream detector decodes the swap instruction to extract
the user's slippage limit (`min_amount_out` for output-bound swaps,
`max_amount_in` for input-bound), compares it to the actual execution,
and reports a fraction in `[0, 1]` indicating how close execution came to
the limit. Three sentinels are also possible:

| Code | Meaning |
|------|---------|
| `-1` | The swap defines no slippage protection (limit is 0 or absent). |
| `-2` | The DEX is recognized but slippage decoding is not implemented. |
| `-3` | The DEX is recognized and decoding is implemented, but inner instruction data needed to compute the actual amount is missing. |

`sandwich_txs.slippage_consumption` carries these raw values for victim
rows.

`sandwiches.slippage_consumption` is the sandwich-level summary computed
as follows: if any victim has code `-2` or `-3`, the sandwich is NaN;
otherwise the sandwich value is the maximum over its victims, where the
maximum is again replaced by NaN if all victims report `-1`. The result
is therefore either NaN or a value in `[0, 1]`. About 30 % of sandwiches
in the window carry a numeric value; the rest are NaN, predominantly
because at least one victim DEX is not yet decoded.

### `aux/validators.csv`

Snapshot of Solana validator metadata from the StakeWiz API at the time
the dataset was built. Columns include `identity`, `vote_identity`,
`name`, `activated_stake`, `stake_weight`, `commission`, `is_jito`,
`ip_country`, etc. Joined to sandwich data via the slot leader of each
sandwich's slot (see `slot_leaders` in the source ClickHouse database).

### `aux/token_prices.csv`

Snapshot of `(tokenA, USD price, SOL price)` for all `tokenA` values
encountered in the merged sandwich set, queried from the Moralis Solana
API. `usd_profit` and the `usd_*` columns in `attackers` are computed
against this snapshot. Tokens not present in the snapshot contribute 0.

### `aux/sandwiched_me_epoch_946.csv`

The full set of sandwiches that the third-party site
[sandwiched.me](https://sandwiched.me) flagged for epoch 946. Used in the
paper for an external recall comparison. Columns:
`slot, front_sig, front_signer, front_sell_amount, front_buy_amount, back_sig, back_signer, back_sell_amount, back_buy_amount`.

## Provenance

```
Solana RPC blocks  ─►  sandwich-detector  ─►  ClickHouse
                            (Go)               (sandwiches, sandwich_txs,
                                                jito_bundles, slot_leaders, …)
                                            │
                                            ▼
ClickHouse  ─►  sandwich-intent phase 1  ─►  per-sandwich features (per category)
                sandwich-intent phase 3  ─►  bot_attackers + bot_sandwiches per category
                                            │
                                            ▼
                                       build_dataset.py
                                            │
                                            ▼
                                       this dataset
```

- The **detector** ([sandwich-detector/](../sandwich-detector/)) runs
  three subcommands (`leader`, `sandwich`, `jito`) that ingest blocks,
  apply in-block and cross-block sandwich heuristics, and mark Jito
  bundle membership. Detection thresholds are documented in
  `sandwich-detector/config/config.go` and tabled in its README.
- The **sandwich-intent** ([sandwich-intent/](../sandwich-intent/)) reads the detector
  output from ClickHouse, computes signer-level features per structural
  category (`standard`, `multi_split`, `diff_signer_owner`), and applies
  a three-track classifier: deterministic Jito-bundle proof, a
  behavioural signal-based filter (CNT ≥ 10, win-rate ≥ 0.8,
  mean slippage ≥ 0.75, P(fg ≤ 100) ≥ 0.6, USD ≥ \$10), and a
  multi-split one-shot extension (CNT ≤ 5, win-rate ≥ 0.8, USD ≥ \$100).
- **build_dataset.py** then merges per-category outputs across attackers,
  recomputes per-attacker aggregates from the unified sandwich set,
  pulls tx-level rows from ClickHouse, and writes the files in this
  directory.

## Build / reproduce

```bash
pip install -r dataset/requirements.txt
```

The build script needs only a small subset of `sandwich-intent`'s
dependencies (pandas, pyarrow, clickhouse-connect, python-dotenv); the
deps are listed in `dataset/requirements.txt`. ClickHouse credentials
are read from `sandwich-intent/.env`, so that file must be present.

### Merge from existing sandwich-intent outputs (no ClickHouse rebuild)

```bash
python dataset/build_dataset.py
```

Requires `sandwich-intent/data/3_attacker_filter/<category>/bot_*.parquet` to
exist for the target epoch range. ClickHouse is still queried for the
tx-level rows; pass `--no-tx-detail` to skip.

### Full rebuild (also re-runs sandwich-intent phases 1 and 3)

```bash
python dataset/build_dataset.py --rebuild
```

Re-runs sandwich-intent phase 1 and phase 3 for each of the three structural
categories before merging. Requires a populated ClickHouse instance
serving the underlying detector tables. Total wall time is on the order
of an hour for a 15-epoch window.

### Custom range

```bash
python dataset/build_dataset.py --start-epoch 946 --end-epoch 960
```

The output filenames carry the range as a suffix
(`attackers_946_960.parquet`, …).

### Refresh aux files

```bash
python dataset/build_dataset.py --copy-aux
```

Copies the latest validators / token-prices / sandwiched.me snapshots
from `sandwich-intent/data/` into `dataset/aux/`.

## Usage

```python
import pandas as pd

attackers  = pd.read_parquet("dataset/attackers_946_960.parquet")
sandwiches = pd.read_parquet("dataset/sandwiches_946_960.parquet")

# Top 10 attackers by USD profit
print(attackers.sort_values("usd_total_profit", ascending=False).head(10))

# All sandwiches by attackers tagged with both "standard" and "jito"
mask = attackers["categories"].apply(lambda s: {"standard", "jito"} <= set(s))
target = set(attackers.loc[mask, "attacker"])
print(sandwiches[sandwiches["attacker"].isin(target)])

# Slippage statistics over sandwiches with a numeric value
slip = sandwiches["slippage_consumption"].dropna()
print(slip.describe())

# Joining victim transactions to their sandwich:
txs = pd.read_parquet("dataset/sandwich_txs_946_960.parquet")
victims = txs[txs["type"] == "victim"]
joined = victims.merge(sandwiches[["sandwich_id", "attacker"]],
                       on="sandwich_id", how="left")
```

## Limitations

- **Window**: 15 epochs (≈ 6 days of mainnet activity). Detection rates
  and classifier behaviour outside this window may differ.
- **Slot coverage**: ≈ 99.9 %; gaps are dominated by RPC unavailability,
  not by selection bias.
- **Token prices** are a one-time snapshot, not a per-slot price series.
  USD aggregates are biased on tokens whose price changed materially
  during the window. Tokens absent from the snapshot contribute 0 USD.
- **Slippage decoding** covers the major Solana DEXes (Raydium, Whirlpool,
  Meteora, Pump.fun, PancakeSwap). Sandwiches whose victims trade on
  unsupported DEXes carry NaN at the sandwich level.
- **`diff-signer` coverage**: only one entity in the published window
  passes the classifier. The `diff-signer-transfer` category is excluded
  from this dataset entirely (see paper §5b for the empirical
  justification).
- **Cross-category attackers**: in this window 4 attackers placed
  sandwiches in both the `standard` and `multi-split` structural pools.
  They appear as a single row in `attackers` with the merged set in
  `categories`; their per-attacker aggregates are recomputed over the
  union.
- **Detector recall**: the upstream watcher uses signer-coherent
  amount-similarity heuristics (Δ ≤ 10 %, SOL fee tolerance 0.1 SOL).
  External comparison against sandwiched.me on epoch 946 (see
  `aux/sandwiched_me_epoch_946.csv` and the paper) shows ≈ 95 % of
  sandwiched.me's sandwiches recovered.

## License and citation

The dataset is released under the same license as the parent repository
(see `../LICENSE`).

If you use this dataset, please cite the accompanying paper. A BibTeX
entry will be added once the paper is published.

## Versioning

Each release is tagged by its epoch range, embedded in the filenames.
Older releases are kept on the project's release page rather than in the
working tree of this directory.
