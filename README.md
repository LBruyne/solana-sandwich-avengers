# Solana Sandwich Avengers

Detects sandwich attacks on Solana, separates the intentional attackers from the
coincidental ones, and publishes the result as a dataset with the scripts that reproduce
every figure and table in the accompanying paper.

A *sandwich* is a front-run and a back-run around a victim's swap. On Solana most
sandwich-shaped patterns are not attacks. So detection runs in two layers:
shape first, then intent, and the two live in different modules.

```
                 Solana RPC + Jito bundles API
                              │
                              ▼
              ┌───────────────────────────────┐
              │  sandwich-detector    (Go)    │  shape-level detection
              └───────────────┬───────────────┘
                              ▼
                         ClickHouse
                              │
                              ▼
              ┌───────────────────────────────┐
              │  sandwich-intent  (Python)    │  attacker-level intent
              └───────────────┬───────────────┘
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
        ┌──────────────────┐   ┌─────────────────────┐
        │  dataset/        │   │  sandwich-analysis/ │
        │  public release  │   │  figures and tables │
        └──────────────────┘   └─────────────────────┘
```

## Modules

| Directory | Language | Role |
|---|---|---|
| [`sandwich-detector/`](sandwich-detector/) | Go 1.24 | Ingests blocks, matches sandwich shapes over sliding leader-rotation windows (in-block, same-leader cross-block, cross-leader), marks Jito bundle membership. Writes to ClickHouse. |
| [`sandwich-intent/`](sandwich-intent/) | Python 3.11+ | Five numbered steps: external data → per-entity features → threshold sensitivity → attacker classifier → validator association. Reads ClickHouse, writes `data/`. |
| [`dataset/`](dataset/) | Python 3.11+ | Merges the classifier output with the transaction legs into the public release: `attackers`, `sandwiches`, `sandwich_txs`, plus aux snapshots. |
| [`sandwich-analysis/`](sandwich-analysis/) | Python 3.11+ | One script per figure or table in the paper. Reads `sandwich-intent/data/`. |
| [`db/`](db/) | SQL / shell | Reference DDL for the six ClickHouse tables, plus export and clear helpers. The detector creates the tables itself on first run. |

Each module has its own README with the details; this file gives the run order.

## How the modules connect

The handoff between modules is a directory or a database, never a function call:

| Producer | Artifact | Consumer |
|---|---|---|
| `sandwich-detector` | ClickHouse tables `sandwiches`, `sandwich_txs`, `slot_txs`, `slot_leaders`, `jito_bundles`, `slot_bundles` | `sandwich-intent` steps 0–4, `dataset` |
| `sandwich-intent` step 1 | `data/1_signer_data_preparation_and_summary/<category>/<database>/` | steps 2 and 3 |
| `sandwich-intent` step 3 | `data/3_attacker_filter/<category>/<database>/<variant>/` | step 4, `dataset`, `sandwich-analysis` |
| `sandwich-intent` step 4 | `data/4_validator_association/<database>/<variant>/` | `sandwich-analysis` |
| `dataset` | `dataset/*.parquet`, `dataset/*.csv` | the public release |

Both Python modules read ClickHouse credentials from `sandwich-intent/.env`.

## Run order

### 1. Detector (Go)

```bash
cd sandwich-detector
cp .env.example .env                       # ClickHouse credentials + RPC API keys
cp config.example.yaml config.yaml         # RPC base URLs + Jito API URL
./scripts/build.sh                         # produces ./sandwich-detector

./scripts/leader.sh   400000000            # slot leaders
./scripts/sandwich.sh 400000000            # sandwich detection
./scripts/jito.sh     400000000            # Jito bundle fetch + inBundle marking
```

Three long-running processes against one database. For a bounded historical range use
`--mode backfill -s <start> -e <end>` instead. See
[`sandwich-detector/README.md`](sandwich-detector/README.md) for ClickHouse setup, RPC
requirements and the tunables.

### 2. Intent classification (Python)

```bash
cd sandwich-intent
pip install -r requirements.txt
cp .env.example .env                       # ClickHouse credentials + MORALIS_API_KEY

# step 0, once per measurement window
python 0_crawl_token_price.py     --top-n 100
python 0_crawl_jito_bundle_ids.py --start-epoch 946 --end-epoch 990
python 0_crawl_stakewiz.py

# steps 1 and 3, per category, for BOTH cross-leader variants
for xl in include exclude; do
  for cat in standard multi_split diff_signer_owner diff_signer_transfer; do
    python 1_signer_data_preparation_and_summary.py \
        --start-epoch 946 --end-epoch 990 --category $cat --cross-leader $xl
    python 3_attacker_filter.py \
        --start-epoch 946 --end-epoch 990 --category $cat --cross-leader $xl
  done
done

# step 4, both variants in one invocation
python 4_validator_association.py --start-epoch 946 --end-epoch 990
```

Step 2 (`2_parameter_selection_and_sensitivity_analysis.py`) measures how each step-3 gate
behaves as its threshold moves. It is a diagnostic and feeds nothing downstream.

See [`sandwich-intent/README.md`](sandwich-intent/README.md) for the classifier's tracks,
the slippage-consumption rule and the validator-enrichment metric.

### 3. Public dataset

```bash
pip install -r dataset/requirements.txt
python dataset/build_dataset.py
```

Writes `attackers_946_990`, `sandwiches_946_990` and `sandwich_txs_946_990_<a>_<b>`, the
last split into contiguous epoch parts so no file passes GitHub's 100 MB limit. Parquet
carries the full dataset; the CSVs are a slice, except `attackers.csv`, which is complete.
See [`dataset/README.md`](dataset/README.md) for the schema.

### 4. Figures and tables

```bash
cd sandwich-analysis
pip install -r requirements.txt

python sec7_build_assoc.py             # association cache: Fig. 9 and three tables

python sec6_measure_charts.py          # Fig. 4, 5, 6, 7, 15, 16, 17
python sec7_cohort_heatmap.py          # Fig. 9
for f in C_0_n_distribution C_1_wr_distribution C_2_sc_distribution \
         C_3_fg_distribution C_5_slip_fg_totalprofit; do python $f.py; done

python sec6_geometry_table.py          # Tab. 2
python appd_sensitivity_table.py       # Tab. 3
python appd_fee_fingerprint_table.py   # Tab. 4
python appd_lookup_tables.py           # Tab. 5, 6, 7
python sec7_team2_robustness.py        # App E.1 permutation test
```

Output lands in `sandwich-analysis/figures/`. See
[`sandwich-analysis/README.md`](sandwich-analysis/README.md) for the figure-to-script map
and each script's prerequisites.

## Requirements

- **Solana RPC** with `getBlock` returning full transaction details. The public
  `mainnet-beta` endpoint is rate-limited and not viable for continuous detection; a paid
  or self-hosted endpoint is needed, and an archival one for a historical window.
- **Jito Block Engine bundles API** (`https://bundles.jito.wtf`), public.
- **ClickHouse**.
- **Go 1.24** for the detector, **Python 3.11+** for everything else, with the
  per-directory `requirements.txt`.

## Layout

```
sandwich-detector/   shape-level detector (Go)
sandwich-intent/     attacker-level classification (Python)
sandwich-analysis/   figure and table scripts (Python)
dataset/             public release artifacts + build script
db/                  reference ClickHouse DDL and helpers
LICENSE              MIT
```

`sandwich-intent/data/`, `sandwich-analysis/{data,figures,results}/` and the log
directories are gitignored; the scripts regenerate them from ClickHouse and from the
preceding step. Three inputs under `sandwich-intent/data/` are committed because they are
point-in-time snapshots rather than derived output: the token price table, the Jito bundle
verdicts and the expert-audit verdicts.

## Citing

If you use this code or the published dataset, please cite the accompanying paper.

## License

[MIT](LICENSE).
