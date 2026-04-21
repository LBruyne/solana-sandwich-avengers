# Definitions & Terminology

Canonical definitions used across the codebase, evaluator scripts, and paper.
All analysis code should reference this file for consistent semantics.

---

## Sandwich Categories

### All Sandwiches

Everything detected by Watcher. Includes:

1. **Textbook in-block sandwiches**: A single front-run and a single back-run in the same slot/block, trading in the same pool, where the front-run and back-run tokenB amounts differ by no more than 10% (`INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD` in `config/config.go`). One or more victims fall between front and back.

2. **Cross-block sandwiches**: Front/back/victim may be distributed across multiple consecutive slots belonging to the same leader. Detection uses a 64-block sliding window with leader-contiguity checks (`CROSS_BLOCK_CACHE_SIZE` in `config/config.go`).

3. **Multi-split sandwiches**: The attacker splits a single front-run or back-run into multiple transactions (`multiFrontRun=true` / `multiBackRun=true`), possibly to evade pattern-based detection. Candidates must share the same signer and fall within a 500-position gap (`SANDWICH_FRONTRUN_MAX_GAP`).

4. **Signer-changing sandwiches**: The front-run and back-run use different signers (`signerSame=false`). Sub-variants:
   - **Shared-owner (ownerSame=true)**: Multiple signers control the same owner's assets via shared PDA. Most common signer-change pattern.
   - **Transfer-bridged (ownerSame=false, hasTransfer=true)**: When signers AND owners differ, Watcher looks for linking evidence: inline transfers within a tx (source/sink owner differ) or a separate direct transfer tx between front and back positions. The transfer is not a mechanism to "change" the signer; it is **evidence linking two otherwise-unrelated signers** as the same attacker entity.

### Standard Sandwiches

The core analysis subset used in the evaluator's intent classification framework:

- `signerSame = true`
- `multiFrontRun = false` AND `multiBackRun = false`
- `hasTransfer` can be true or false (transfer-pattern bots included)
- Includes both in-block and cross-block

This covers ~95% of all detected sandwiches.

### Multi-split Sandwiches

`signerSame=true` AND (`multiFrontRun=true` OR `multiBackRun=true`). The attacker splits the front-run or back-run into multiple transactions. Analyzed separately from standard sandwiches.

**Structural sub-variants** (tracked in evaluator Step 3 via `--multi-variant`):

- **multi_front only** (`multiFrontRun=true`, `multiBackRun=false`): multiple front-run txs, one back-run. Empirically dominated by concurrent-racer artefacts rather than deliberate obfuscation.
- **multi_back only** (`multiFrontRun=false`, `multiBackRun=true`): one front-run, multiple back-run txs. The primary deliberate multi-split pattern — splitting the exit to limit price impact, dodge interleaved adverse trades, or avoid single-swap slippage limits.
- **both** (`multiFrontRun=true`, `multiBackRun=true`): both legs split. Strongly correlated with multi-way bot races (e.g. pump.fun contention) where no single participant achieves a clean sandwich.

### Diff-signer (Owner-same) Sandwiches

`signerSame=false`, `ownerSame=true`, not multi-split. Front and back use different signing keys that control the same owner's assets.

### Diff-signer (Transfer-linked) Sandwiches

`signerSame=false`, `ownerSame=false`, `hasTransfer=true`, not multi-split. Front and back use different signers and owners, linked by an inline or direct token transfer.

**Note**: This category is detected by the Watcher but is **excluded from the evaluator's intent-classification pipeline**. Empirically (epoch 946-956), the 6,920 sandwiches in this category produce -\$46 net USD and \$1.78 of positive USD in aggregate; the only 3 "Signal Bots" that would be flagged all hit CNT=5 with exactly \$0 USD profit (structural coincidence, not attackers). Running the classifier on this category dilutes signal without finding any reliable attacker. See `evaluator_design.md` for the empirical justification.

### Jito Bundle Sandwiches

A sandwich where **all** front-run, victim, and back-run transactions appear in the **same** Jito bundle. This is the strongest deterministic signal of intentional ordering control, because the attacker placed all three transactions into a single bundle submitted to the block builder.

- Checked via: `sandwich_txs.inBundle` joined with `jito_bundles.transactions`
- A sandwich where only some txs are in bundles (or in different bundles) does NOT qualify

---

## Profit & Revenue

### SOL Profit

Computed only for sandwiches where `tokenA = 'SOL'`. The value `profitA` is already in human-readable SOL units (not lamports). Represents the attacker's net gain/loss in SOL from the sandwich.

### USD Total Profit

For all sandwiches regardless of tokenA:

```
usd_profit = profitA * token_price[tokenA]
```

- Token prices sourced from `evaluator/data/token_prices/prices.csv` (crawled via `0_crawl_token_price.py` using the Moralis API)
- SOL price: fetched for `So11111111111111111111111111111111111111112`, fallback $86.00
- If a token has no price data, that sandwich is excluded from USD aggregation (coverage % is reported)

### profitA Semantics

`profitA` is the attacker's net change in tokenA across the sandwich. It is stored in the token's human-readable decimal units (e.g., SOL, not lamports; USDT with 6-decimal precision, etc.). Positive = attacker gained; negative = attacker lost.

**Important**: Raw `profitA` values must NOT be summed across sandwiches with different tokenA — the units are incomparable (SOL vs SPL token amounts). Always use USD-converted profit for cross-token aggregation.

### Signer-level Profit Filtering

When filtering signers for intent analysis, use USD-denominated profit:
- `usd_total_profit = sum(profitA * token_price[tokenA])` per signer, across all sandwiches with available prices
- Signers with no price coverage have `usd_total_profit = 0` (NaN sum defaults to 0) and are excluded by the `> 0` filter
- Standard filter: `win_rate >= 0.5 AND usd_total_profit > 0`

---

## Signals & Features

### Slippage Consumption (slippage_consumption)

Measures how much of a victim's slippage tolerance was consumed by the sandwich attack.

**Per-victim computation** (in Watcher, `sol/victim_slippage.go`):
- DEX swap instructions declare the victim's tolerance: `min_amount_out` (output-limited) or `max_amount_in` (input-limited)
- Watcher decodes these from 12 supported DEX programs (PumpFun, Raydium V4/CPMM/CLMM, Meteora DBC/DAMMv2/DLLM, Whirlpool, Orca V1/V2, PancakeSwap)
- Consumption = how close the actual execution was to the victim's limit:
  - Output-limited: `limit_amount / actual_amount` (higher = closer to minimum)
  - Input-limited: `actual_amount / limit_amount` (higher = closer to maximum)
- Range: `[0, 1]` where 1.0 = victim was pushed exactly to their slippage limit

**Special values:**
- `-1` (NoProtection): Victim set no slippage limit (limit = 0 or not specified)
- `-2` (Unsupported): DEX program not in the 12 supported decoders, or instruction data unavailable
- `-3` (MissingInner): Swap instruction is in an inner call but RPC returned null for inner instructions

**Per-sandwich computation** (in evaluator, recomputed from stored per-victim values):
```python
def compute_sandwich_slippage(victim_utils):
    if any(v in (-2, -3) for v in victim_utils):
        return -2  # data unavailable, cannot assess
    return max(victim_utils)  # max across all victims (including -1)
```

**Per-signer aggregation** (in evaluator):
```
mean_slippage = mean of per-sandwich slippage, computed ONLY over sandwiches
               with valid slippage in [0, 1].
```
Sandwiches with slippage = -1 (no protection), -2 (unsupported), or -3 (missing inner) are excluded from the average. Signers with zero valid samples get NaN, which automatically excludes them from the slippage threshold gate in Step 3 (NaN ≥ 0.75 → False).

**Why this signal matters**: High slippage consumption proves the attacker **observed the victim's transaction parameters** and calibrated the front-run to push the price near the victim's tolerance limit. HFT traders cannot consistently achieve this because their trades are not counterparty-specific.

### Front Gap (fg) / Proximity

Measures the distance between the front-run and the first victim transaction, in **transaction-count units**.

**Same block (in-block)**:
```
proximity = victim_position - frontrun_position
```

**Cross block**:
```
proximity = (frontrun_block_txCount - frontrun_position)
           + victim_position
           + sum(txCount of intermediate blocks)
```

This unification allows direct comparison across in-block and cross-block sandwiches.

**Why this signal matters**: Tight front-run positioning requires either validator ordering power (can place txs at exact positions) or fast transaction racing (can observe and react to pending transactions). Smaller front gap = stronger evidence of ordering control.

### Transfer Types (for signer-changing sandwiches)

Three boolean fields track how attackers link front and back runs when using different signers:

| Field | Meaning |
|-------|---------|
| `hasFrontInlineTransfer` | Within a front-run tx, the swap's source owner differs from its sink owner (detected via `inferSwapSourceAndSinkOwner`) |
| `hasDirectTransfer` | A separate non-swap transaction exists between front and back positions, transferring tokens from a front-run owner to a back-run owner |
| `hasBackInlineTransfer` | Within a back-run tx, the swap's source owner differs from its sink owner |

---

## Scope & Filtering

### Epoch-Slot Mapping

Each Solana epoch = 432,000 slots. Epoch N starts at slot `N * 432,000`, ends at slot `(N+1) * 432,000 - 1`.

### Deduplication

ClickHouse tables use MergeTree engines that may produce duplicate rows. All analytical queries must deduplicate:
- `sandwiches`: `LIMIT 1 BY sandwichId`
- `sandwich_txs`: `LIMIT 1 BY sandwichId, signature`

### Slippage Data Availability

Slippage extraction was deployed at slot 408,285,759. Sandwiches before this slot have `slippageUtilization = 0` (default), not `-2`. The evaluator recomputes slippage consumption from scratch and does not rely on stored `maxSlippageUtilization` values.
