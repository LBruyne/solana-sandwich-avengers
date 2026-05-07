# Solana Sandwich

End-to-end research artifact for measuring sandwich attacks on Solana.
The project ingests blocks in near real time, applies a heuristic
detector, classifies signers as intentional attackers, and ships a
labelled dataset together with the analysis scripts that reproduce the
paper figures.

```
                  Solana RPC + Jito API
                          │
                          ▼
      ┌─────────────────────────────────────┐
      │  sandwich-detector  (Go)             │
      │  block ingest, heuristic detection,  │
      │  Jito bundle enrichment              │
      └────────────────┬────────────────────┘
                       │
                       ▼
                 ClickHouse
                       │
                       ▼
      ┌─────────────────────────────────────┐
      │  sandwich-intent  (Python)           │
      │  per-attacker feature aggregation,   │
      │  three-track classifier,             │
      │  validator-association enrichment    │
      └────────────────┬────────────────────┘
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
   ┌────────────────┐    ┌──────────────────┐
   │ dataset/       │    │ sandwich-analysis│
   │ public release │    │ paper figures +  │
   │ (parquet+csv)  │    │ overview notebook│
   └────────────────┘    └──────────────────┘
```

The four top-level directories correspond to the four roles above. Each
directory has its own README with detailed instructions; this top-level
file orients new readers and gives the canonical run order.

## Modules

| Directory | Language | Role |
|---|---|---|
| [`sandwich-detector/`](sandwich-detector/) | Go 1.24 | Real-time sandwich detector (in-block + cross-block) and Jito bundle enrichment. Writes to ClickHouse. |
| [`sandwich-intent/`](sandwich-intent/) | Python 3.11+ | Five-step pipeline (data acquisition → per-signer features → threshold diagnostic → attacker classifier → validator association). Reads from and writes back to ClickHouse + local `data/`. |
| [`sandwich-analysis/`](sandwich-analysis/) | Python 3.11+ | Paper-figure scripts (§5.2, §7.1, Appendix E) and an end-to-end overview notebook. Reads `sandwich-intent/data/`. |
| [`dataset/`](dataset/) | Python 3.11+ | Builds the public release dataset (`attackers`, `sandwiches`, `sandwich_txs` + aux). Reads `sandwich-intent/data/` and ClickHouse. |
| [`db/create_tables/`](db/create_tables/) | SQL | ClickHouse schema for the six detector tables (created automatically by the detector on first run). |

## Quick start

The pipeline is staged: each module depends on the output of the
previous one. A complete reproduction looks like this.

### 1. Detector (Go)

```bash
cd sandwich-detector
cp .env.example .env                       # ClickHouse credentials
cp config.example.yaml config.yaml         # Solana RPC + Jito API endpoints
./scripts/build.sh                         # produces ./sandwich-detector

# in three long-running terminals (or via the wrapper scripts):
./scripts/leader.sh   400000000            # slot leaders
./scripts/sandwich.sh 400000000            # sandwich detection
./scripts/jito.sh     400000000            # Jito bundle fetch + inBundle marking
```

See [`sandwich-detector/README.md`](sandwich-detector/README.md) for
ClickHouse setup, RPC requirements, and the full tunable list.

### 2. Intent classification (Python)

After the detector has produced data for the target window:

```bash
cd sandwich-intent
pip install -r requirements.txt
cp .env.example .env                       # ClickHouse credentials

# step 0 (run once per release window)
python 0_crawl_stakewiz.py
python 0_crawl_token_price.py
python 0_crawl_sandwiched_me.py --epoch 946

# steps 1 + 3 per category (1 runs feature prep; 3 classifies)
for cat in standard multi_split diff_signer_owner; do
    python 1_signer_data_preparation_and_summary.py --start-epoch 946 --end-epoch 960 --category $cat
    python 3_attacker_filter.py                     --start-epoch 946 --end-epoch 960 --category $cat
done

# step 4 (validator association across the three categories)
python 4_validator_association.py --start-epoch 946 --end-epoch 960
```

Step 2 (`2_parameter_selection.py`) is a diagnostic for the classifier
thresholds and is not required by the rest of the pipeline.

See [`sandwich-intent/README.md`](sandwich-intent/README.md) for the
three-track classifier definition (Jito Bot, Signal Bot, Oneshot Bot)
and the validator-enrichment math.

### 3. Public dataset

```bash
pip install -r dataset/requirements.txt
python dataset/build_dataset.py            # merge intent outputs + tx detail
```

This writes `attackers_<start>_<end>.{parquet,csv}`,
`sandwiches_<start>_<end>.{parquet,csv}`,
`sandwich_txs_<start>_<end>.{parquet,csv}`, and copies aux snapshots into
`dataset/aux/`. The CSV mirrors are example slices (top-3% by USD ∪
top-3% by count); Parquet always carries the full 380-attacker dataset.

See [`dataset/README.md`](dataset/README.md) for the full schema and a
data dictionary.

### 4. Paper figures and overview notebook

```bash
cd sandwich-analysis
pip install -r requirements.txt

python sec5_2_wr_distribution.py
python sec5_2_appE_charts.py
python sec7_1_enrichment_distribution.py
python sec7_validator_charts.py --start-epoch 946 --end-epoch 960

jupyter notebook analysis.ipynb            # interactive overview
```

See [`sandwich-analysis/README.md`](sandwich-analysis/README.md) for
the input dependencies of each figure script.

## Requirements

- **Solana RPC** with `getBlock` returning full transaction details. The
  public `mainnet-beta` endpoint is rate-limited and not viable for
  continuous detection; a paid or self-hosted RPC is recommended.
- **Jito Block Engine bundles API** (`https://bundles.jito.wtf`) — public.
- **ClickHouse** (tested with the official Debian package).
- **Go 1.24** (detector).
- **Python 3.11+** with the per-directory `requirements.txt`.

## Repository layout

```
sandwich-detector/   real-time detector (Go)
sandwich-intent/     attacker classification pipeline (Python)
sandwich-analysis/   paper figures and overview notebook (Python)
dataset/             public release artifacts + build script
db/create_tables/    ClickHouse schema (one .sql per table)
LICENSE              MIT
README.md            this file
```

`sandwich-intent/data/` and the per-module log directories are
gitignored; the scripts regenerate them deterministically from
ClickHouse and the inputs of preceding steps.

## Citing

If you use this code or the published dataset, please cite the
accompanying paper. A BibTeX entry will be added once the paper is
public.

## License

[MIT](LICENSE).
