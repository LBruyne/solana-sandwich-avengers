# CHANGELOG — sandwich-detector v2 (dev-2 branch)

Semantic and behavioral changes vs the v1 detector (main / DB `solwich`). Kept so the CCS
rebuttal can explain any delta between v1 numbers and a v2 re-run. Correctness-first: several
v1 behaviors were bugs; v2 fixes them, which will shift some outputs.

## Phase 0 — infra (DB name, schema, structs)
- **DB name is now configurable.** All table references dropped the hardcoded `solwich.` qualifier
  and resolve against the connection's default database (`CLICKHOUSE_DATABASE`, default `solwich`).
  Point v2 at `solwich_v2` purely via env. `EnsureDatabaseExists` bootstraps the target DB over a
  short-lived connection on `default` (the main connection can't create the DB it's pointed at —
  the native protocol validates the default DB at handshake, code 81).
- **New `sandwiches` columns:** `crossLeader Bool`, `frontLeader String`, `backLeader String`,
  `windowStartSlot UInt64`, `windowEndSlot UInt64`, `rpcSource LowCardinality(String)`.
  Populated by the detector in Phase 4/5 (the DDL DEFAULT does not apply to struct inserts, so the
  detector sets them explicitly).
- **`jito_bundles` ORDER BY** changed from `timestamp` to `(slot, bundleId)` — every hot query is
  slot-keyed; the old key forced full scans over 670M rows. `QueryLatestBundleIds` keeps its explicit
  `ORDER BY timestamp DESC` (perf only, no correctness change).
- **Fact tables stay plain MergeTree.** (Considered ReplacingMergeTree for dedup but reverted:
  its ORDER-BY-tuple dedup would collapse an inline-transfer evidence row into the attack leg it
  shares a tx with, and would revert mutation-applied `inBundle`/`intentScore` enrichment on any
  re-detection.) Dedup is instead an **insert-time guarantee** owned by Phase 4: each sandwich is
  emitted by exactly one detection window (ownership rule) plus a process-wide seen-set.

## Phase 1 — RPC/parse correctness
- **Inner (CPI) DEX instructions are now parsed under base64 encoding.** v1 read the jsonParsed
  shape (`programId` string, address accounts) while `getBlock` returns compiled inner instructions
  (`programIdIndex`, numeric account indices) — so v1 captured ZERO inner DEX instructions on the
  production path. Effect: CPI-routed victims (router/aggregator front-ends) that scored -3
  (missing inner) or -2 (unsupported) on v1 can now get a real slippage utilization on archival
  RPCs. Also affects `HasTransfer`/transfer-evasion (a CPI swap no longer looks like a bare transfer).
  Verified: `sol/inner_instruction_test.go` against a real base64 getTransaction fixture.
- **Token decimals recorded from `postTokenBalances` too.** v1 recorded decimals only from
  `preTokenBalances`, so a token first appearing in post (fresh ATA receiving a memecoin) defaulted
  to 9 decimals — a 6-decimal token's slippage limit was off by 10^3. Fixed.
- **SPL amount parse widened to u64.** `strconv.Atoi` overflowed high-supply token raw amounts
  (>~9.2e18) to 0; switched to `strconv.ParseUint(_, 10, 64)`.
- **Archival skipped-slot recognized.** Added the `-32009 "Slot N was skipped, or missing in
  long-term storage"` pattern (confirmed live on Helius) so archival skips aren't retried 12×.

## Phase 2 — swap identification / atomic-arbitrage FP defenses
- **(a) Reject multi-swap txs.** A clean single swap decodes to exactly one swap instruction; a tx
  with >1 decodable swap is a multi-hop route or an atomic arbitrage (both legs in one tx) and is
  dropped at bucketing. This is the primary arb defense: an arb's second pool otherwise mimics a
  user (spends IncomeToken, receives ExpenseToken matching the first pool) and the tx buckets as a
  clean swap. Uses the existing DEX decoders (`dex.ExtractSlippage`). **Recall note:** genuine
  multi-hop aggregator-routed victims are also dropped — they can't be attributed to a single pool
  anyway, so they were never usable for single-pool sandwich matching.
- **(b) Swap counterparty must not be a known pool.** The source/sink owner search now skips owners
  that are labeled/known AMM pools, so a second pool can't be picked as the swap user.
- **(c) Aggregator labels enabled.** `utils.IsLabeledAggregator` (Jupiter/OKX/DFlow/…, previously
  loaded but unused) now annotates aggregator-routed txs — used to distinguish "multi-hop route"
  from "arbitrage" in logs; not a rejection reason.
- Verified: `sol/tx_bucket_test.go` — single swap counts as 1 (no over-rejection), two swaps as 2
  (rejected). Existing slippage/sandwich fixtures unchanged.

## Phase 3 — unified finder + deterministic detection (part 1: finder + determinism)
- **Detection is now reproducible.** v1 iterated the pool-bucket map directly; Go randomizes map
  order, and because front/back txs are claimed greedily, blocks with overlapping sandwiches
  produced a DIFFERENT set of sandwiches on each run (verified: v1 in-block gave 38 vs 40 sandwiches
  on the same block across runs). Both finders now iterate buckets in a stable (pool, incomeToken,
  expenseToken) order (`sortedBucketKeys`). **Rebuttal note:** the existing v1 headline counts were
  produced by non-reproducible detection; a v2 re-run is deterministic.
- **New unified `SandwichFinder`** (`sol/sandwich_finder.go`) over a window = ordered tx sequence +
  optional slot→leader map. It generalizes the cross-block finder (keys on TxIdx, which equals the
  block position for a single-slot window) and drops the "front and back must be different slots"
  restriction, so an in-block sandwich is just the single-slot case. Classifies each sandwich by
  crossBlock (front/back span >1 slot) and crossLeader (different leaders), and fills
  frontLeader/backLeader/windowStartSlot/windowEndSlot/rpcSource. Fixes folded in vs v1 in-block:
  positional logic is TxIdx-based throughout (v1 passed block Position as a slice index into
  collectDirectTransfers — a latent bug on parse-gapped blocks).
- Verified: `sol/sandwich_finder_parity_test.go` (env-gated live) — unified finder over a single
  block == v1 in-block finder (identical sandwichIds) on 5 real archival slots (1–40 sandwiches each).
- NOT YET WIRED: orchestration still calls the v1 finders; the sliding double-rotation windows,
  cross-round dedup, and deletion of the v1 finders are part 2 (with Phase 4).
