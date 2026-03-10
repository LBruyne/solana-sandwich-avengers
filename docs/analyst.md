# Analyst Module (Python)

## Scope

The `Analyst` module performs quantitative analysis over the detected sandwich set. It builds a reproducible feature table, reconstructs attacker profitability, and performs macro-level statistics across epochs, validators, and bundle status.

## Data Sources

- **ClickHouse** tables: `sandwiches`, `sandwich_txs`, `slot_leaders`, `slot_txs`, `jito_bundles`.
- **Parquet exports**: produced by `query_sandwiches_with_txs_and_leader_to_parquet()` in [analyst/1_query_data.py](analyst/1_query_data.py).

The export query de-duplicates `sandwiches` and `sandwich_txs` by `sandwichId`, merges slot leaders, and writes a per-slot partitioned Parquet dataset for scalable analysis.

## Feature Engineering

The feature pipeline in `analyze_sandwiches()` constructs sandwich-level records with the following dimensions:

- **Transaction topology**
  - `fr_count`, `br_count`, `victim_count`, `transfer_count`, `adverse_count`
  - `consecutive` in-block ordering based on position continuity
- **Distance metrics**
  - `inblock_distance` (position gap between last front-run and first back-run)
  - `crossblock_gap_slots` (slot gap across blocks)
  - `crossblock_distance` (slot-weighted transaction distance using `slot_txs.txCount`)
- **Bundle membership**
  - `bundle_status ∈ {all, partial, none, no_tx}` from `sandwich_txs.inBundle`
- **Fee exposure**
  - `fr_fee`, `br_fee`, `tx_fee`

## Profit Model

The `sandwiches.profitA` field provides the base profit in token `A` as computed by `Watcher`. The analyst module constructs profit views as follows:

1. **Token price lookup**: `fetch_token_prices()` fetches SOL and USD price for top-`N` tokens using the Moralis API.
2. **USD conversion**: `profit_in_usd = profitA × usd_price`.
3. **SOL-only slice**: `profit_SOL = profitA` when `tokenA == "SOL"`.

This separation supports robust aggregation across heterogeneous token sets while preserving SOL-denominated comparability.

## Attacker Identification

The attacker clustering procedure in [analyst/2_analysis_attacker.py](analyst/2_analysis_attacker.py) uses a Union-Find structure over signer addresses:

- **Edge creation**: For each `sandwichId`, all signers in `frontRun/backRun/transfer` are unioned.
- **Signer consistency check**: If `signerSame` is true but multiple signers appear, the sample is flagged.
- **Attacker key**: Each component is hashed into a stable `attacker_key`.

The output aggregates attacker-level performance (`win_rate`, `total_profit_in_usd`, `inbundle_count`, etc.) and attaches the list of signer addresses per component.

## Macro Statistics

- **Epoch statistics**: [analyst/sandwich_statistic.py](analyst/sandwich_statistic.py) groups sandwiches by `(signerSame, ownerSame)` and reports counts, victims, and SOL profit per category.
- **External validation**: [analyst/sandwiched_me_comparison.py](analyst/sandwiched_me_comparison.py) compares local detections with the `sandwiched.me` public API.
- **Bundle enrichment**: [analyst/3_fetch_bundle_info.py](analyst/3_fetch_bundle_info.py) matches sandwich transaction signatures to Jito bundles by timestamp range, emitting bundle-specific tip statistics.

## Outputs

- `data/parquet_out`: sandwich-level Parquet dataset for scalable analysis.
- `data/sandwich_stat.csv`: consolidated feature table with profit and distance metadata.
- `data/attacker_summary.csv`: attacker clusters and profitability summary.
- Figures and CSVs for validator-level and epoch-level comparisons.
