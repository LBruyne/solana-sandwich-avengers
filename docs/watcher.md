# Watcher Module (Golang)

## Scope

The `Watcher` module performs real-time transaction ingestion and heuristic sandwich detection. It separates **in-block** and **cross-block** search, and integrates **Jito bundle** data to mark private-order-flow execution paths.

## Data Pipeline

1. **Block acquisition**: `GetBlocks()` fetches blocks via `getBlock` RPC.
2. **Transaction parsing**: `parseTransactionWithBinary()` decodes each transaction and extracts `fee`, `signature`, `signer`, `programs`, and balance deltas.
3. **Post-processing for detection**: `Transaction.PostprocessForFindSandwich()`
   - Merges SOL/WSOL balance changes.
   - Extracts `RelatedTokens` and `RelatedPools` via owner balance heuristics.
   - Builds `RelatedPoolsInfo` with per-pool `IncomeToken`, `ExpenseToken`, and signed amounts.
4. **Bucketization**: `buildTxBuckets()` groups transactions by `(pool, incomeToken, expenseToken)`.
5. **Detection**: `InBlockSandwichFinder` and `CrossBlockSandwichFinder` apply heuristics and record sandwich structures.
6. **Persistence**: Results are stored in `sandwiches` and `sandwich_txs`, and slot-level metadata is recorded in `slot_txs`.

## Heuristic Detection Logic

### In-Block Detection (Core Rules)

**Key heuristics** (configurable in [watcher/config/config.go](watcher/config/config.go)):

- `INBLOCK_SANDWICH_AMOUNT_THRESHOLD = 5` (percent)
- `SANDWICH_FRONTRUN_MAX_GAP = 100` (positions)
- `SANDWICH_BACKRUN_MAX_GAP = 100` (positions)
- `SANDWICH_AMOUNT_SOL_TOLERANCE = 0.1` (percent)

**Algorithmic conditions**:

1. **Pool/Token consistency**: front-run uses `(pool, A→B)` and back-run uses `(pool, B→A)`.
2. **Signer coherence**: front-run and back-run sets must each have a single signer; multi-front and multi-back are allowed if signer is consistent and position gap is bounded.
3. **Amount similarity**:
   - Let $F_B$ be total `B` out in front-run and $B_B$ be total `B` in on back-run.
   - Require $F_B \geq B_B$ for non-SOL tokens and
     $\frac{|F_B - B_B|}{\max(F_B, B_B)} \leq \tau$, where $\tau=5\%$.
4. **Victim placement**: victim transactions must be between the last front-run and the first back-run, share the same pool direction `(A→B)`, and have a different signer.
5. **Signer mismatch resolution**: if front-run and back-run signers differ, the system checks:
   - **Owner overlap**: front-run owners of token `B` are a superset of back-run owners of token `B`, **or**
   - **Transfer bridge**: an intermediate transfer transaction moves token `B` from front-run owners to back-run owners with unique increase/decrease endpoints.
6. **Adverse flow capture**: for each confirmed sandwich, additional in-between transactions on the same pool with direction `(B→A)` (same as back-run, opposite to front/victim) are recorded as `adverse` to support slippage/profit erosion analysis. Their signer must differ from both front-run and back-run signers.

[Our Contribution] The transfer-bridge check explicitly links signer-disjoint front/back runs, mitigating false negatives caused by multi-account attacker workflows.

### Pseudocode (In-Block)

```text
Algorithm InBlockSandwichDetection(block)
  txs ← block.txs
  buckets ← buildTxBuckets(txs, crossBlock=false)
  for each key (pool, A, B) in buckets:
    frontBucket ← buckets[(pool, A, B)]
    backBucket  ← buckets[(pool, B, A)]
    if backBucket is empty: continue

    for each frontEntry in frontBucket:
      frontSet ← collectFrontTxs(frontEntry, frontBucket, gap=SANDWICH_FRONTRUN_MAX_GAP)
      if frontSet is empty: continue

      backSet ← collectBackTxs(frontSet, backBucket, gap=SANDWICH_BACKRUN_MAX_GAP)
      if backSet is empty: continue

      if not Evaluate(frontSet, backSet): continue
      recordSandwich(frontSet, backSet)
```

### Cross-Block Detection

Cross-block detection reuses the same heuristics but operates on **global ordering** across a sliding window of blocks. The `BlockCache` retains the most recent `CROSS_BLOCK_CACHE_SIZE` blocks and aligns blocks into **leader-contiguous runs**; each run is expanded by one block on the left and right to avoid boundary loss. A valid cross-block sandwich must have front and back runs in **different slots**.

### Pseudocode (Cross-Block)

```text
Algorithm CrossBlockSandwichDetection(blockWindow)
  txs ← concat(blockWindow.txs)
  buckets ← buildTxBuckets(txs, crossBlock=true)
  for each key (pool, A, B) in buckets:
    frontBucket ← buckets[(pool, A, B)]
    backBucket  ← buckets[(pool, B, A)]
    if backBucket is empty: continue

    for each frontEntry in frontBucket:
      frontSet ← collectFrontTxs(frontEntry, frontBucket, gap=SANDWICH_FRONTRUN_MAX_GAP)
      if frontSet is empty: continue

      backSet ← collectBackTxs(frontSet, backBucket, gap=SANDWICH_BACKRUN_MAX_GAP)
      if backSet is empty: continue

      if frontSet.slot == backSet.slot: continue
      if not Evaluate(frontSet, backSet): continue
      recordSandwich(frontSet, backSet)
```

## Jito Bundle Integration

The `RunJitoCmd()` pipeline has two concurrent tasks:

1. **Task 1 (Bundle Fetching)**
   - For each slot, call `GetBundlesBySlot()`.
   - Parse timestamps and persist bundles into `jito_bundles`.
   - Update `slot_bundles` with `bundleFetched`, `bundleCount`, and `bundleTxCount`.

2. **Task 2 (inBundle Marking)**
   - Query slots where `sandwich_txs` exist and `bundleFetched` is true.
   - For each slot, intersect `bundleTxs` with `sandwich_txs` signatures.
   - Update `sandwich_txs.inBundle` for matched signatures.

[Our Contribution] The explicit `inBundle` marking enables a controlled comparison between public mempool exposure and private bundle execution in downstream analysis.

## Outputs

- `sandwiches`: sandwich-level metadata (profit, perfectness, signer/owner relations).
- `sandwich_txs`: per-transaction breakdown with direction labels (`frontRun`/`victim`/`backRun`/`transfer`/`adverse`), attacker and pool token-`B` pre/post balances, and `inBundle` flag.
- `slot_txs`: slot-level counts for data coverage accounting.
- `slot_bundles` and `jito_bundles`: bundle metadata for private-order-flow analysis.
