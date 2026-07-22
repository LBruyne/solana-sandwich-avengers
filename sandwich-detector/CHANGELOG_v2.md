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
## Phase 3+4 — sliding double-rotation windows, unified orchestration, dedup
- **Detection now runs over sliding double-rotation windows.** Blocks are grouped into leader
  rotations (maximal same-leader, slot-adjacent runs); each rotation is paired with its successor
  `[rot_i, rot_i+1]` (step 1 rotation) and run through the unified finder once. In-block,
  same-leader cross-block and cross-leader sandwiches all come out of one pass, tagged by
  CrossBlock/CrossLeader — replacing v1's separate concurrent in-block (per block) and cross-block
  (per run) passes and the cross-ID duplication between them.
- **Cross-round dedup (owns the whole guarantee — fact tables are plain MergeTree).** A sandwich is
  kept only by the window whose LEFT rotation holds its front-run, so an overlapping same-leader
  sandwich (found in two adjacent windows) is emitted exactly once. A process-wide seen-sandwichId
  LRU (`SEEN_SANDWICH_CACHE_SIZE`) suppresses re-emission across batches as windows slide. Verified
  on 32 real archival blocks: 125 sandwiches (65 in-block / 46 same-leader-cross / 14 cross-leader),
  **0 duplicate ids**, every cross-leader correctly cross-block with distinct known leaders.
- **Finders unified.** in_block.go and cross_block.go (and their near-duplicate ~1300 lines) are
  deleted; the single `SandwichFinder` is the only matcher. InBlockSandwich/CrossBlockSandwich
  collapse to one persisted type (kept the name CrossBlockSandwich; it now carries every variant).
  DB `InsertInBlockSandwiches`+`InsertCrossBlockSandwiches` collapse to `InsertSandwiches`.
- Backfill mode passes deferTail=false (flush every rotation); live passes true (hold the tail
  rotation until the next batch completes it). rpcSource is threaded through to the stored row.
- STILL TODO (later phases): OwnerSame currently still uses the raw-delta owner set (fold the
  Evaluate-set fix in a follow-up); slippage semantics (Phase 6); jito test isolation (Phase 7).

## Phase 5 — dual-mode CLI (live vs Helius backfill)
- `sandwich --mode live|backfill` (default live) with `-e/--end-slot` and `--rps`. Live is
  unchanged (self-hosted RPC, follows the tip). Backfill scans a bounded [start, end] range against
  Helius archival and exits cleanly.
- **Helius endpoint** built from `HELIUS_RPC_API_KEY` (or an explicit `sol.rpc-helius` URL). The key
  was previously dead config; it is now the backfill endpoint.
- **Rate limiting**: a dependency-free token bucket throttles all RPC to `--rps` (default 10 for the
  Helius free tier); HTTP 429 triggers exponential backoff (`RPC_RATE_LIMIT_BACKOFF` → cap) in
  CallRpc. Live mode is unthrottled.
- **Leaders from rewards**: backfill sets `getBlock rewards:true` and resolves the slot leader from
  the Fee reward, seeding the cache and `slot_leaders` — so cross-block/cross-leader detection works
  on ranges the `slot_leaders` table doesn't yet cover (no separate `leader` run needed).
- **Failed-slot retry ledger**: slots that fail to fetch (not genuine skips) are collected and
  retried once at the end, instead of being silently dropped.
- rpcSource is stored as 'helius' for backfill rows so they're distinguishable from live rows.
- Verified via the real CLI over 16 archival slots on the free tier: 63 sandwiches (21 cross-block,
  1 cross-leader), rpcSource=helius, 16 leaders populated from rewards, cross-leader tagged with
  distinct front/back leaders, 0 duplicate ids, clean exit, no 429 failures.

## Phase 6 — slippage semantics
- New `SlippageAmbiguous` (-4) sentinel. A victim tx with >1 decodable swap instruction now scores
  -4 ("can't match a limit to this pool"), distinct from -1 NoProtection (victim genuinely set no
  bound). (After Phase 2 such txs are usually dropped at bucketing, but the sentinel keeps the
  meaning honest where they aren't.)
- Sandwich-level `computeMaxSlippageUtilization` no longer collapses MissingInner(-3) into
  Unsupported(-2). It now returns the max REAL utilization when any victim has one (the binding
  victim), and otherwise the specific unmeasured reason (MissingInner > Ambiguous > Unsupported >
  NoProtection). **Changes vs v1:** a sandwich with a measurable victim plus an unmeasured one now
  reports the measurable utilization instead of -2; and -3 is preserved instead of being reported
  as -2. Relevant to any downstream that reads maxSlippageUtilization.
- (Token-decimals fix that also affects limit conversion was already landed in Phase 1.)

## Phase 8 — cleanup + docs
- **OwnerSame fix**: the sandwich's `ownerSame` label is now computed from the same owner sets the
  Evaluate attacker-linkage check uses (collectFront/BackOwnersByToken, honoring inferred
  sink/source owners), instead of a separate raw-delta owner set — so the label matches the
  decision that accepted the sandwich. (Label only; does not affect sandwichId or detection.)
- Removed the ~90-line commented-out JSON `parseTransaction` dead code in request.go.
- Synced reference DDL: db/create_tables/sandwiches.sql gained the v2 columns; jito_bundles.sql
  ORDER BY (slot, bundleId).
- Updated CLAUDE.md (watcher→sandwich-detector, dual-mode commands, unified detection, test gating).
- KEPT per request: RunJitoCmd2 + the /recent flow.
