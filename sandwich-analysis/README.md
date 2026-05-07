# sandwich-analysis

Analysis notebook and paper-figure scripts that consume the outputs of
[`sandwich-intent`](../sandwich-intent/). This module produces no
intermediate state of its own; every script reads from
`sandwich-intent/data/` and writes a figure or a printed summary.

```
sandwich-intent/data/                   ─►  analysis.ipynb        (interactive overview)
sandwich-intent/data/1_*/                  sec5_2_*.py            (paper §5.2 + appendix E)
sandwich-intent/data/3_attacker_filter/    sec7_*.py              (paper §7.1)
ClickHouse (slot_leaders, sandwich_txs) ─┘
```

## Contents

| File | Purpose |
|---|---|
| `analysis.ipynb` | End-to-end overview notebook: per-category bot/sandwich counts, USD aggregates, token concentration, slot-time distributions, top-attacker tables. |
| `sec5_2_wr_distribution.py` | Paper §5.2: win-rate histogram across the pooled candidate set, with the WR threshold annotated. |
| `sec5_2_appE_charts.py` | Paper §5.2 main scatter (`profit × CNT`, classified) and Appendix E pair figures (signal distributions, avg-USD-by-signal). |
| `sec7_1_enrichment_distribution.py` | Paper §7.1 Figure 8: distribution of `η_max` across the 282 pooled attackers. Self-contained — recomputes enrichment from `sandwich-intent`'s phase-3 outputs and ClickHouse `slot_leaders`. |
| `sec7_validator_charts.py` | Paper §7.1 cohort heatmap (Cohort B: 9 core attackers × 11 shared validators) and the offset distribution. Reads `sandwich-intent/data/4_validator_association/`. |
| `performance.csv` | Watcher throughput / detection-time benchmark snapshots from one of the measurement runs. Reference data, not consumed by any script in this directory. |

## Setup

```bash
cd sandwich-analysis
pip install -r requirements.txt
```

The two `sec7_*.py` scripts query ClickHouse for the leader schedule.
They reuse [`sandwich-intent/utils/db.py`](../sandwich-intent/utils/db.py)
which reads `sandwich-intent/.env`; install / configure that module
before running them.

## Running the figure scripts

All four scripts assume that `sandwich-intent` has already produced its
phase-1, phase-3, and phase-4 outputs for the target window (default
946–960). Run them from this directory:

```bash
python sec5_2_wr_distribution.py
python sec5_2_appE_charts.py
python sec7_1_enrichment_distribution.py
python sec7_validator_charts.py --start-epoch 946 --end-epoch 960
```

Each script writes its figure into the corresponding subdirectory of
`sandwich-intent/data/` (`sec5_2/`, `appE/`, `4_validator_association/`,
`sec7_validator/`) and, if present, mirrors the PDF to
`overleaf-paper/CCS/figures/`.

If the `overleaf-paper/` directory is absent (open-source clone), the
mirror step is skipped; the local PDF/PNG outputs are still written.

## Running the notebook

```bash
jupyter notebook analysis.ipynb
```

The notebook expects:

- `sandwich-intent/data/3_attacker_filter/<category>/bot_attackers_946_960.parquet`
- `sandwich-intent/data/3_attacker_filter/<category>/bot_sandwiches_946_960.parquet`
- `sandwich-intent/data/token_prices/prices.csv`

for `category ∈ {standard, multi_split, diff_signer_owner}`. ClickHouse
is queried from a few cells (slot-time interpolation, sandwich cost
breakdown); `.env` is loaded from `../sandwich-intent/.env` via the
shared `utils.db.get_client()` helper.

To regenerate all required inputs end-to-end:

```bash
cd ../sandwich-intent
for cat in standard multi_split diff_signer_owner; do
    python 1_signer_data_preparation_and_summary.py --category $cat
    python 3_attacker_filter.py --category $cat
done
python 4_validator_association.py
```

See [`../sandwich-intent/README.md`](../sandwich-intent/README.md) for
the full pipeline.

## Layout

```
analysis.ipynb            interactive overview notebook
sec5_2_wr_distribution.py
sec5_2_appE_charts.py
sec7_1_enrichment_distribution.py
sec7_validator_charts.py
performance.csv           detector benchmark snapshots
requirements.txt
```
