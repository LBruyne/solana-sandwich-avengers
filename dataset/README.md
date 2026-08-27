# Solana Sandwich Attacker Dataset

A labelled dataset of intentional sandwich attackers on Solana over epochs 946–990, with
the sandwiches attributed to each and the transaction legs of each sandwich.

*Attacker* is a classified entity; *signer* is the on-chain signer of one transaction. They
differ for sandwiches whose front-run and back-run use distinct keys under one owner.

Produced by the pipeline in this repository: [`sandwich-detector/`](../sandwich-detector/)
emits the shape-level detections, [`sandwich-intent/`](../sandwich-intent/) classifies
entities, and [`build_dataset.py`](build_dataset.py) merges the two into the files here.

## Files

```
attackers_946_990.parquet             322 rows
attackers_946_990.csv                 322 rows
sandwiches_946_990.parquet            286,219 rows
sandwiches_946_990.csv                the CSV attacker slice
sandwich_txs_946_990_<a>_<b>.parquet  5,297,560 rows over 6 contiguous epoch parts
sandwich_txs_csv_946_990_<a>_<b>.csv  213,244 rows over 3 parts; the CSV slice, own legs only
aux/
    validators.csv                    Solana validator metadata (StakeWiz)
    token_prices.csv                  token → USD price snapshot (Moralis)
    sandwiched_me_epoch_946.csv       third-party sandwich list, epoch 946
build_dataset.py
requirements.txt
```

### Parquet vs CSV

**Parquet is the full dataset.** `attackers` and `sandwiches` are one file each;
`sandwich_txs` is split into contiguous epoch parts so no file passes GitHub's 100 MB
limit. The parts concatenate back to the whole table:

```python
import glob, pandas as pd
txs = pd.concat(pd.read_parquet(p) for p in sorted(glob.glob("sandwich_txs_946_990_*.parquet")))
```

**CSV is a slice**, except for `attackers`, which is complete in both formats. The slice is
the top 20 attackers by `usd_profit_net` together with every attacker the paper names —
27 attackers, 106,473 sandwiches.

`sandwich_txs_csv_*` narrows further, to those attackers' own `frontRun` and `backRun`
legs — 213,244 rows. Their victim and adverse legs are in the parquet parts only: a CSV row
costs about five times a parquet row, and the full slice runs to 775 MB.

CSV parts are packed to their own epoch boundaries, not the parquet ones, for the same
reason. To widen or narrow the slice, change `--csv-top-n` or `PAPER_ATTACKERS` in
[`build_dataset.py`](build_dataset.py) and re-run.

## Window

| | Start | End |
|---|---:|---:|
| Epoch | 946 | 990 |
| Slot  | 408,672,000 | 428,111,999 |

45 epochs, 19,440,000 theoretical slots.

## Schema

### `attackers`

One row per attacker entity, ranked by `usd_profit_net`.

| Column | Type | Description |
|---|---|---|
| `attacker` | str | Attacker identifier: an on-chain key, or the union-find root over the keys of a `diff-signer-owner` entity. |
| `bot_type` | str | Which track admitted the attacker: `Jito Bot`, `Signal Bot`, or both, comma-joined. |
| `categories` | str | Comma-joined subset of `{standard, multi-split, diff-signer-owner, diff-signer-transfer}`. |
| `sandwich_count` | int | Sandwiches attributed to this entity. |
| `usd_profit_net` | float | Sum of `sandwiches.usd_profit_net`. |
| `usd_avg_profit` | float | `usd_profit_net / sandwich_count`. |
| `sol_profit_net` | float | Net profit over `token_a == "SOL"` sandwiches, in $SOL. |
| `fee_sol_total` | float | $SOL this entity spent on its own front-run and back-run fees. |
| `win_rate` | float | Share of priceable sandwiches with `usd_profit_net > 0`. |
| `sol_win_rate` | float | The same rate over `token_a == "SOL"` sandwiches, where no price table is involved. |
| `mean_slippage_consumption` | float | Mean of `sandwiches.slippage_consumption` over the entity's scored sandwiches. |
| `slippage_coverage` | float | Share of the entity's sandwiches that produced a slippage value. |
| `front_gap_median` | float | Median `front_gap`. |
| `front_gap_eq1_ratio` | float | Share of sandwiches with `front_gap <= 1`. |
| `in_block_count` | int | Sandwiches with `cross_block == False`. |
| `cross_block_count` | int | Sandwiches spanning more than one slot. |
| `cross_leader_count` | int | Sandwiches spanning more than one leader rotation. |
| `multi_split_count` | int | Sandwiches with a split front-run or back-run. |
| `jito_bundle_count` | int | Sandwiches whose front, victims and back share one Jito bundle. |
| `first_slot`, `last_slot` | int | First and last slot the entity is seen at. |
| `expert_verdict` | str | `yes`, `ambiguous`, or `not_audited`. Entities labelled `no` are not in the dataset. |
| `n_signing_keys` | int | On-chain keys merged into this entity; > 1 only for `diff-signer-owner`. |

`categories` is a multi-label field, not a partition: an entity carries a label if at least
one of its sandwiches has that shape.

- `standard` — one signer for the whole sandwich, single front-run, single back-run.
- `multi-split` — front-run and/or back-run split across several transactions.
- `diff-signer-owner` — front-run and back-run under different keys sharing a token-B owner.
- `diff-signer-transfer` — front-run and back-run share neither signer nor owner, linked by
  an inline transfer. The attacker is named by the front-run's fee payer.

### `sandwiches`

One row per attributed sandwich. `attacker` joins to `attackers.attacker`.

| Column | Type | Description |
|---|---|---|
| `sandwich_id` | str | Identifier from the detection layer; joins to `sandwich_txs.sandwich_id`. |
| `attacker` | str | Attacker entity. |
| `slot` | int64 | Slot of the first front-run leg. |
| `timestamp` | datetime | Block time of that slot, UTC. |
| `token_a` | str | The asset the attacker holds. `"SOL"` covers native SOL and wSOL. |
| `token_b` | str | The asset the attacker swaps into and back out of. |
| `profit_token_a` | float | `backRun.toTotal - frontRun.fromTotal`, in token A. |
| `fee_sol` | float | $SOL the attacker paid on its own front-run and back-run legs. |
| `usd_profit_net` | float | `profit_token_a` priced in USD, minus `fee_sol` priced in USD. NaN when `token_a` is absent from `aux/token_prices.csv`. |
| `is_profitable` | bool | `usd_profit_net > 0`. |
| `victim_count` | int32 | Victim transactions between the legs. |
| `adverse_count` | int32 | Transactions between the legs that traded against the attacker. |
| `front_count`, `back_count` | int32 | Legs on each side. |
| `cross_block` | bool | The legs span more than one slot. |
| `cross_leader` | bool | The legs span more than one leader rotation. Implies `cross_block`. |
| `multi_split` | bool | `front_count > 1` or `back_count > 1`. |
| `front_gap` | int64 | Transactions between the first front-run leg and the first victim. |
| `back_gap` | int64 | Transactions between the last victim and the first back-run leg. |
| `slippage_consumption` | float | Fraction of victim slippage tolerance the attack consumed, in `[0, 1]`. NaN unless `slippage_state == "scored"`. |
| `slippage_state` | str | `scored`, `anomalous`, or `unprotected`. |
| `slippage_reason` | str | Why a sandwich is not scored: `no_protection`, `empty_type`, `limit_nonpositive`, `no_victim`, and so on. |
| `jito_bundle` | bool | Front, victims and back share one Jito bundle, with the same signer and positive profit. |
| `pool_dex` | str | Venue of the front-run leg, e.g. `pumpfun`, `raydium_v4`, `meteora_dlmm`. |
| `category` | str | Structural category of this sandwich. |

Gaps are transaction counts along the slot axis, spanning block boundaries via each slot's
transaction count, so a cross-block gap includes the intervening blocks in full.

`slippage_consumption` is the maximum over the sandwich's victims at or above a 0.01
protection floor. `anomalous` means at least one victim's consumption could not be read;
`unprotected` means every victim's decoded limit was below the floor. Both are NaN, never 0.

### `sandwich_txs`

One row per transaction leg of an attributed sandwich. Joins on `sandwich_id`.

| Column | Type | Description |
|---|---|---|
| `sandwich_id` | str | Foreign key to `sandwiches.sandwich_id`. |
| `type` | str | `frontRun`, `backRun`, `victim`, or `adverse`. |
| `slot` | int64 | Slot of this transaction. |
| `position` | int32 | Position within the slot. |
| `signature` | str | Transaction signature. |
| `tx_signer` | str | First signer. |
| `from_token`, `to_token` | str | Swap direction. |
| `from_amount`, `to_amount` | float | Swap amounts in token units. |
| `fee` | uint64 | Transaction fee in lamports. |
| `in_bundle` | bool | This transaction landed inside a Jito bundle. |
| `programs` | list[str] | Program IDs the transaction invoked. |
| `slippage_limit_type` | str | `output` for a minimum-out bound, `input` for a maximum-in bound. Empty for non-victims. |
| `slippage_limit_amount` | float | The bound the victim set. NaN for non-victims. |
| `slippage_actual_amount` | float | What the victim actually received or paid. NaN for non-victims. |

Victim slippage consumption is `limit / actual` for an `output` bound and `actual / limit`
for an `input` bound. The raw amounts are published rather than the ratio so the rule can be
re-derived.

### `aux/validators.csv`

StakeWiz validator metadata: `identity`, `vote_identity`, `name`, `activated_stake`,
`stake_ratio`, `commission`, `is_jito`, `ip_city`, `ip_country`, `ip_asn`, `ip_org` and
more. Join on `identity` to the leader of a slot; `slot_leaders` in the detector database
carries the schedule.

### `aux/token_prices.csv`

`token, sandwich_count, usd_price, symbol, name, decimals` for the 100 most frequent
`token_a` values plus SOL, from the Moralis Solana API. Every USD figure in this dataset and
in the paper is computed against this file. A token absent from it has no price, and the USD
columns derived from it are NaN.

### `aux/sandwiched_me_epoch_946.csv`

Every sandwich [sandwiched.me](https://sandwiched.me) flagged for epoch 946, used as an
external recall comparison. Columns: `slot, front_sig, front_signer, front_sell_amount,
front_buy_amount, back_sig, back_signer, back_sell_amount, back_buy_amount`.

## Rebuild

```bash
pip install -r dataset/requirements.txt
python dataset/build_dataset.py
```

Reads `sandwich-intent/data/3_attacker_filter/<category>/<database>/<variant>/` for the
target range, so phases 1 and 3 must have been run first — see
[`sandwich-intent/README.md`](../sandwich-intent/README.md). ClickHouse credentials come
from `sandwich-intent/.env`.

| Flag | Default | Effect |
|---|---|---|
| `--database` | `solwich_v2` | Detector database |
| `--start-epoch` `--end-epoch` | 946, 990 | Inclusive epoch range; also the filename suffix |
| `--cross-leader` | `include` | Which phase-3 population to read |
| `--csv-top-n` | 20 | Attackers by profit in the CSV slice, before the union with `PAPER_ATTACKERS` |
| `--max-part-mb` | 90 | Upper bound on one `sandwich_txs` parquet part |
| `--max-csv-part-mb` | 45 | Upper bound on one `sandwich_txs` CSV part |
| `--no-tx-detail` | off | Skip `sandwich_txs` and the ClickHouse query |
| `--copy-aux` | off | Refresh `aux/token_prices.csv` from the pipeline |

`aux/validators.csv` and `aux/sandwiched_me_epoch_946.csv` are not regenerated by the
script; they are point-in-time crawls written by `sandwich-intent/0_crawl_stakewiz.py` and
`0_crawl_sandwiched_me.py`.

## Usage

```python
import glob, pandas as pd

attackers  = pd.read_parquet("dataset/attackers_946_990.parquet")
sandwiches = pd.read_parquet("dataset/sandwiches_946_990.parquet")

attackers.head(10)

# every sandwich by an attacker that used Jito bundles
jito = set(attackers.loc[attackers["jito_bundle_count"] > 0, "attacker"])
sandwiches[sandwiches["attacker"].isin(jito)]

# slippage consumption over the scored sandwiches
sandwiches.loc[sandwiches["slippage_state"] == "scored", "slippage_consumption"].describe()

# victim legs joined to their sandwich
txs = pd.concat(pd.read_parquet(p)
                for p in sorted(glob.glob("dataset/sandwich_txs_946_990_*.parquet")))
victims = txs[txs["type"] == "victim"].merge(
    sandwiches[["sandwich_id", "attacker", "slot"]], on="sandwich_id", how="left")
```

## Scope

- Token prices are one snapshot, not a per-slot series. A token absent from the snapshot
  has NaN in every USD column derived from it, not 0.
- Slippage decoding covers Raydium, Whirlpool, Meteora, Pump.fun and PancakeSwap. A victim
  on another venue, or one whose bound sits in an outer aggregator route, is `unprotected`
  or `anomalous` rather than scored.
- Entities the blinded expert panel labelled `no` are excluded. Entities labelled
  `ambiguous` are included and carry that value in `expert_verdict`.

## License and citation

Released under the parent repository's license (see [`../LICENSE`](../LICENSE)). If you use
this dataset, please cite the accompanying paper.
