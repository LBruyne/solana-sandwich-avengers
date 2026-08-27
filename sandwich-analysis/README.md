# sandwich-analysis

Regenerates every figure and table in the paper from the attacker dataset produced by
[`sandwich-intent`](../sandwich-intent/).

## What draws what

Figure and table numbers are as they appear in the compiled paper; `S6.1` is a body
section, `App C` an appendix. Fig. 1, 2, 3 and 8 are hand-drawn diagrams with no script.

| # | Label | Where | Asset | Script |
|---|---|---|---|---|
| Fig. 4  | `fig:daily-sandwich`          | S5    | `6.1_volume_and_profit.pdf`    | `sec6_measure_charts.py` |
| Fig. 5  | `fig:profit-concentration`    | S6.1  | `6.1_profit_concentration.pdf` | `sec6_measure_charts.py` |
| Fig. 6  | `fig:dist-profit`             | S6.2  | `6.1_distance_profit.pdf`      | `sec6_measure_charts.py` |
| Fig. 7  | `fig:rotation-heatmap`        | S6.2  | `6.3_rotation_heatmap.pdf`     | `sec6_measure_charts.py` |
| Fig. 9  | `fig:7_1_cohort_heatmap`      | S7.3  | `7.1_cohort_heatmap.pdf`       | `sec7_cohort_heatmap.py` |
| Fig. 10 | `fig:C_0_n_distribution`      | App C | `C_0_n_distribution.pdf`       | `C_0_n_distribution.py` |
| Fig. 11 | `fig:C_1_wr_distribution`     | App C | `C_1_wr_distribution.pdf`      | `C_1_wr_distribution.py` |
| Fig. 12 | `fig:C_2_sc_distribution`     | App C | `C_2_sc_distribution.pdf`      | `C_2_sc_distribution.py` |
| Fig. 13 | `fig:C_3_fg_distribution`     | App C | `C_3_fg_distribution.pdf`      | `C_3_fg_distribution.py` |
| Fig. 14 | `fig:C_5_slip_fg_totalprofit` | App C | `C_5_slip_fg_totalprofit.pdf`  | `C_5_slip_fg_totalprofit.py` |
| Fig. 15 | `fig:tx_fee`                  | App D | `6.5_cost.pdf`                 | `sec6_measure_charts.py` |
| Fig. 16 | `fig:profit-bin`              | App D | `6.1_profit_range.pdf`         | `sec6_measure_charts.py` |
| Fig. 17 | `fig:program`                 | App D | `6.6_program.pdf`              | `sec6_measure_charts.py` |
| Tab. 2  | `tab:geometry-econ`           | S6.1  | —                              | `sec6_geometry_table.py` |
| Tab. 3  | `tab:appd-sensitivity`        | App C | —                              | `appd_sensitivity_table.py` |
| Tab. 4  | `tab:fee-fingerprint`         | App E | —                              | `appd_fee_fingerprint_table.py` |
| Tab. 5  | `tab:appd-attacker`           | App F | —                              | `appd_lookup_tables.py --only attackers` |
| Tab. 6  | `tab:appd-validator`          | App F | —                              | `appd_lookup_tables.py --only validators` |
| Tab. 7  | `tab:appd-prog`               | App F | —                              | `appd_lookup_tables.py --only programs` |
| App E.1 | permutation test              | App E | —                              | `sec7_team2_robustness.py` |

Two more scripts:

- `sec7_build_assoc.py` caches the (attacker, validator, offset) table that Fig. 9,
  Tab. 4, Tab. 6 and the permutation test read.
- `C_4_slip_fg_avgprofitpersandwich.py` draws the same plane as Fig. 14 coloured by
  profit per sandwich rather than in total.

## Setup

```bash
cd sandwich-analysis
pip install -r requirements.txt          # plus sandwich-intent's own requirements
```

Every script takes the same flags, defined in `figcfg.add_args` and defaulting to the
paper's run:

```
--database solwich_v2      the detector database the dataset was built from
--start-epoch 946 --end-epoch 990
--cross-leader include     which phase-3 population (include | exclude)
--out-dir ./figures        where figures and table CSVs are written
--keep-audited-rejects     keep the 7 entities the expert panel labelled `no`
```

The default population is 322 attackers / 286,219 sandwiches: phase 3's selection minus
the entities carrying `expert_verdict == "no"`.

## Prerequisites

```bash
cd ../sandwich-intent

# 1. the dataset: phases 1 and 3, all four categories, --cross-leader include
#    (see sandwich-intent/README.md)

# 2. Fig. 10 additionally needs a phase-1 run with the profit floor disabled
for cat in standard multi_split diff_signer_owner diff_signer_transfer; do
  python 1_signer_data_preparation_and_summary.py --database solwich_v2 \
      --start-epoch 946 --end-epoch 990 --category $cat --cross-leader include \
      --profit-floor-usd 0 --out-root data/1_nofloor --no-charts
done

# 3. Tab. 6 additionally needs the validator metadata snapshot
python 0_crawl_stakewiz.py

# 4. Fig. 9, Tab. 4, Tab. 6 and App E.1 need the association cache
cd ../sandwich-analysis
python sec7_build_assoc.py
```

## Running

```bash
# Section 6 + Appendix D: seven figures in one pass
python sec6_measure_charts.py

# Appendix C
for f in C_0_n_distribution C_1_wr_distribution C_2_sc_distribution \
         C_3_fg_distribution C_5_slip_fg_totalprofit; do python $f.py; done

# Section 7
python sec7_cohort_heatmap.py          # Fig. 9
python sec7_team2_robustness.py        # App E.1, ~5 min at B=10,000

# Tables
python sec6_geometry_table.py          # Tab. 2
python appd_sensitivity_table.py       # Tab. 3
python appd_fee_fingerprint_table.py   # Tab. 4
python appd_lookup_tables.py           # Tab. 5, 6, 7
```

Each script prints the numbers it drew. Figures go to `--out-dir` as PDF and PNG, tables
as CSV.

## Caches

`sec6_measure_charts.py` and `sec7_team2_robustness.py` build these under `./data` on
first use:

| Cache | Built by | Holds |
|---|---|---|
| `legs_<db>_<tag>.parquet` | `load_legs` | one row per attacker leg: type, slot, position, fee, tip, programs (~3.4M rows, 108 MB) |
| `rotations_<db>_<epochs>.npz` | `load_rotations` | the leader of each rotation |

`--refresh-legs` rebuilds the first. Delete the file to rebuild the second.

## Flags beyond the shared set

| Script | Flag | Effect |
|---|---|---|
| `sec6_measure_charts.py` | `--refresh-legs` | re-query the per-leg frame |
| `sec6_measure_charts.py` | `--tips {fee-floor,known-only}` | a leg in a bundle whose tip is not in `jito_bundles`: count it at its fee (default) or drop it |
| `sec6_geometry_table.py` | `--top-n 20` | attackers in the table, ranked by net profit |
| `appd_lookup_tables.py` | `--only {attackers,validators,programs}` | build one table |
| `appd_fee_fingerprint_table.py` | `--attackers HwGqF,HqXf7,E45YL` | 5-character address prefixes |
| `sec7_team2_robustness.py` | `--top-n 3` | Team 2 attackers to test |
| `sec7_team2_robustness.py` | `--permutations 10000` `--seed 20260824` | null distribution |
| `sec7_build_assoc.py` | `--include-cross-leader` | keep cross-leader sandwiches |

## Layout

```
figcfg.py                        shared flags, dataset loading, palette, shared drawing
sec6_measure_charts.py           Fig. 4, 5, 6, 7, 15, 16, 17
sec6_geometry_table.py           Tab. 2
C_0..C_5_*.py                    Fig. 10-14 and the C_4 variant
sec7_build_assoc.py              association cache
sec7_cohort_heatmap.py           Fig. 9
sec7_team2_robustness.py         App E.1
appd_sensitivity_table.py        Tab. 3
appd_fee_fingerprint_table.py    Tab. 4
appd_lookup_tables.py            Tab. 5, 6, 7
data/                            caches (gitignored)
figures/                         output (gitignored)
results/                         permutation-test output (gitignored)
```
