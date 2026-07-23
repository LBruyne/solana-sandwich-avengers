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
> **NOTE: (a) and (c) below were SUPERSEDED by Phase 9** — multi-swap txs are no longer dropped at
> bucketing (that lost real multi-hop victims); instead they are kept as victim candidates but
> barred from being front/back legs. Defense (b) remains. `utils.IsLabeledAggregator` (from (c)) is
> still defined but has no call sites (the isAggregatorRouted annotation was removed in Phase 9), so
> the `labeled_aggregators` list is currently unused — kept only as reference documentation of which
> programs are routers/aggregators (relevant because they are deliberately NOT in `labeled_dex`, so
> the poolDex/slippage classifiers see through them to the inner AMM swap).
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
> **NOTE: the Helius-specific bits below were generalized in Phase 10** — the backfill endpoint is
> now any archival RPC (default Chainstack), `rpcSource` is `backfill` not `helius`, and `--rps`
> defaults to 0 (no throttle). The dual-mode CLI shape (flags, leaders-from-rewards, retry ledger,
> 429 backoff) is unchanged.
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

## Phase 9 — front/back-vs-victim contract (validation-driven)
The epoch-950 spot-check (3000 slots) confirmed the invariant and refined the swap/arb contract.
- **Invariant (unchanged, confirmed correct):** a front/back tx belongs to exactly one sandwich as
  a front/back (tracked in confirmedSandwichTxIdx, skipped in collectFront/BackTxs); a victim tx may
  serve many sandwiches (never added to confirmedSandwichTxIdx, not skipped in collectVictimEntries);
  one tx can be BOTH a front/back of one sandwich AND a victim of others.
- **Contract:** front/back legs must be clean single swaps; a victim may be any tx (including a
  multi-hop route or an arbitrage) as long as one of its legs trades the sandwiched (pool, direction).
- **Change:** bucketing no longer requires exactly one AMM pool. A tx is bucketed under EACH of its
  identified AMM legs, so a multi-hop/arb tx is a victim candidate on every pool it actually traded.
  Any tx with >1 leg or >1 decodable swap is marked `PoolEntry.IsMultiSwap` and skipped when
  selecting front/back seeds — so attacker legs stay clean single swaps and arbitrage can never be a
  fake front/back, with no balance/fee heuristic. Replaced (and removed) the earlier
  `isArbLikeFlow`/`isAggregatorRouted` attempt, which an adversarial review showed was evadable
  (fees/tips are not added back into OwnerBalanceChanges; arb profit can land on a non-signer PDA).
- **Recall (spot-check):** CORE (high-confidence, intent-classified) attacker recall is 72/73
  sandwiches and 346/347 victims — the real attacks are captured. The remaining ~7% victim gap vs v1
  is NOT arb-filtering and NOT multi-hop victims (an early diagnostic that skipped prefetchPoolOwners
  mis-measured this); it is greedy front/back matching resolving a genuinely ambiguous case
  differently: a tx that is a SELL can be the back of a buy-front sandwich OR the front of a reverse
  sell-front sandwich, and the stable bucket-scan order picks one. A global-TxIdx ordering was tried
  (it recovered some non-CORE real sandwiches) but LOST 12 CORE victims, so it was reverted — the
  stable bucket order maximizes CORE recall. This ambiguity affects only non-CORE shapes (removed by
  the intent filter) and is an inherent property of greedy sandwich matching, documented here.

## Phase 10 — default RPC is Chainstack (archival, paid); base-URL/key split
- **Default RPC is now Chainstack Core**, used for BOTH live and backfill. It is archival (serves
  genesis-to-tip — verified on slots 100M / 300M / 410M) and paid, so backfill no longer needs a
  Helius-specific endpoint or an aggressive throttle. Measured throughput: ~37–39 full-block base64
  fetches/sec at 40 concurrency, 0 errors / no 429 — well above the 8-worker fetch pool. (Go's
  `http` client is not WAF-blocked; only Python-urllib's default User-Agent was — a probe artifact,
  not a code path.)
- **Config holds base URLs only; API keys live in .env.** `sol.rpc-chainstack` +
  `CHAINSTACK_API_KEY` (key = URL path segment), `sol.rpc-helius` + `HELIUS_RPC_API_KEY`
  (key = `?api-key=`), `sol.rpc` = the self-hosted node. Composed in `buildChainstackURL` /
  `buildHeliusURL`; no keyed URL is ever written to config, and endpoint log lines are host-only
  (`rpcHost`).
- **Resolution order** — live (`GetSolanaRpcURL`): Chainstack → self-hosted `sol.rpc` → Helius.
  Backfill (`buildArchivalURL`): Chainstack → Helius; the self-hosted node keeps ~6h of ledger and
  is never picked for backfill. Contract pinned by `TestRpcURLResolution`/`TestRpcHostRedaction`.
- **`--rps` default is now 0 = no throttle** (a paid RPC absorbs the 8-worker fetch concurrency, and
  CallRpc still backs off on any 429). Set `--rps 10` only on rate-limited tiers (Helius free).
  Removed the now-unused `HELIUS_DEFAULT_BACKFILL_RPS` constant.
- **rpcSource for backfill rows is now `backfill`** (was `helius`) — a provider-agnostic mode label,
  since backfill and live can both run on Chainstack.
- Verified end-to-end via the real CLI: 400 archival slots (epoch 950) on Chainstack → 204
  sandwiches (3 in-block / 27 same-leader / 174 cross-leader), rpcSource=backfill, 400/400 leaders
  resolved from rewards, 0 duplicate ids, clean exit, no 429; and with CHAINSTACK_API_KEY unset the
  same command resolves to mainnet.helius-rpc.com (fallback path exercised live).

## Phase 11 — performance: measured, then fixed (3.1x backfill throughput, identical results)
Benchmark (`sol/perf_bench_test.go`, env-gated PERF_TEST) + E2E timing on 1,600 archival slots,
all deltas verified against an unchanged sandwich-id set (1,349 = 1,349 on every config).
- **Negative owner-caching** (tx_bucket.go): getMultipleAccounts omits accounts that do not exist
  (closed ATAs, ephemeral accounts) — ~98% of prefetch candidates — and they were never cached, so
  EVERY window re-queried the same null addresses. They are now cached as owner="" (isAMMByOwner
  answers (false, cached); provably the same "not an AMM" decision, just remembered). Effect:
  getMultipleAccounts 3,114 → 890 per 1,600 slots (-71%), E2E +43%. Hardened: a malformed-but-200
  getMultipleAccounts response (nil result / short value array) is an error, never negative-cached.
- **Backfill batch 32 × 16 fetch workers** (BACKFILL_FETCH_SLOT_NUM/BACKFILL_FETCH_PARALLEL_NUM;
  live keeps 8/8 via the FetchParallelism package var). A 32-slot batch spans ~8 windows, actually
  filling the 8-worker window pool (batch 8 yielded ~2), and amortizes per-batch fixed costs.
  Effect: 15.4 → 34.2 slots/s. Combined with negative caching: **10.8 → 32.8–34.2 slots/s (~3.1x)**.
- **RPC usage counter** (rpcCallCounts in request.go): per-method request counts logged at backfill
  completion ("Backfill RPC usage") for quota accounting. Counts attempt-groups; inner transport
  retries (up to 3 on failures) are not counted — reconcile against provider billing accordingly.
- **Measured pure-CPU detection cost** (400-slot corpus, zero-RPC-guarded, 5 iterations ±1%):
  bucketize 3.69 ms/block, full matcher 7.27 ms/block serial, 1.54 ms/block at the production
  8-worker shape; cross-validated on a June-2026 corpus (3.75 / 5.29 / 1.36). Detection is NOT the
  bottleneck — fetch is (~36 ms/block at 8 workers; JSON decode into untyped maps dominates its CPU).
- **Bucketing vs naive linear-scan matcher**: a bucket-free variant of the same greedy algorithm
  (candidate sets by scanning all swap legs; provably identical output, asserted per iteration) costs
  only ~1.3–1.6x more matching CPU at today's bucket shape (avg 2.5–2.8 legs/bucket, Σ(K·S)≈330x more
  scan ops but scans are cheap vs Evaluate). Bucketing's real value is the per-(pool,direction)
  greedy semantics + bounded worst case on hot pools (max bucket 139–179), not a big constant factor.
- **Negative results worth remembering**: Chainstack negotiates HTTP/2, so Go multiplexes all
  workers over one connection — the "MaxIdleConnsPerHost=2 causes TLS churn" hypothesis is moot, and
  forcing HTTP/1.1 with 32 connections changed nothing (34.4 vs 34.2 slots/s). Reverted.
- Known remaining headroom (not done): typed JSON decoding of getBlock (est. biggest lever now),
  fetch/process pipelining (depth-1 prefetch), singleflight on concurrent-window prefetch, negative-
  cache TTL for live, per-rotation bucket caching (bucketize is only ~3.7 ms/block, low priority).

## Phase 12 — transfer evidence gated on signer change (fix hasTransfer overcount)
- **Inline-transfer evidence is now collected only when front and back signers do NOT overlap.**
  Transfer evidence is the linkage mechanism for wallet-rotating attackers; for a same-signer
  sandwich an inferred inline transfer is routing noise (aggregator hop, owner-inference tolerance).
  v1 collected it unconditionally, so **1,733 of v1's 2,310 epoch-955 hasTransfer sandwiches (75%)
  had signerSame=true** — flagged transfer-style evasion where no wallet rotation happened. Front
  inline evidence is further restricted to transfers landing in the back side's owner set (the same
  criterion `sumFrontInlineBridgeAmount` already applied to bridge amounts).
- Label-only change: on 3,000 archival slots the sandwich id set AND every per-sandwich victimCount
  are identical to the pre-fix run (2,587 = 2,587); hasTransfer∧signerSame went 14 → 0 and
  signerSame sandwiches now carry zero transfer evidence rows. Pinned by `sol/transfer_gate_test.go`.

## Phase 12b — Meteora DLMM swap_exact_out discriminator fix
- `meteoraDLMMSwapExactOut` was `[194,203,142,150,137,110,81,94]`, which is **not**
  `sha256("global:swap_exact_out")[:8]` (`[250,73,101,33,38,207,75,184]`) and matches no known
  Meteora method — a stale/mistyped constant. DLMM v1 ExactOut swaps therefore never matched and
  fell through to `SlippageUnsupported` (-2) instead of yielding their `max_in_amount` input limit.
  `swap_exact_out2` and the price-impact discriminators were already correct.
- Found by an on-chain slippage audit: 4 agents re-derived 30 victim slippage values from raw
  instruction bytes (discriminators recomputed via sha256, byte offsets, decimals, utilizations) —
  **all 30 reproduced to <1.5e-8 relative**; this was the single decoder defect surfaced, and the
  only >1 utilization was a boundary case (limit == actual). Pinned by `TestMeteoraDLMMDiscriminators`
  (all six DLMM discriminators vs sha256) and `TestMeteoraDLMMExactOut`.

## Phase 13 — two-tier matching (same-leader claims legs before cross-leader)
- Cross-leader detection shares one greedy pass with same-leader detection over each two-rotation
  window. Coincidental cross-leader matches are far more numerous (epoch-955: 434k cross-leader vs
  64k same-leader) and span wider windows, so they were **stealing real same-leader sandwiches'
  back-runs**: a SELL that is the back of a buy-front sandwich is also a valid front for a reverse
  cross-leader match, and whichever (pool, direction) bucket sorts first claimed it.
- `Find` now runs **two tiers per window**: same-leader sandwiches (in-block + same-leader
  cross-block) are matched first and claim their legs (`confirmedSandwichTxIdx`), then cross-leader
  runs on whatever remains. `scanBuckets` is the extracted per-tier loop; `stagedIsCrossLeader`
  gates tier 1. With no leader map every candidate is same-leader, so single-slot/in-block behavior
  is unchanged (the parity/determinism tests still pass).
- **Impact (epoch-955 audit):** of 98 missing CORE (high-intent) sandwiches in the un-tiered run,
  88 had a leg stolen by a cross-leader match. Validated on a 35k-slot window: 8 of 11 previously
  missing CORE recovered (all correctly same-leader), **0 regression** on 469 previously-retained
  CORE, same-leader up / cross-leader down. The 3 residual are same-leader-vs-same-leader direction
  ambiguity (unrelated to cross-leader; the txs are still detected, just paired differently) — the
  inherent greedy limit from Phase 9. Pinned by `TestTwoTierSameLeaderWinsContestedBack` (which also
  asserts the single-pass path still exhibits the bug).

## Phase 13b — slippage utilization capped at 1.0 (non-physical >1 guard)
- A successful swap cannot spend more than its max input or receive less than its min output, so
  victim slippage utilization is physically in [0,1]. An on-chain audit found 282 victims (0.008%)
  with util > 1, up to **95x** — traced to 264 pump.fun `buy` victims whose decoded `max_sol_cost`
  is ~100x below the SOL actually paid to the bonding curve (limit 5,015 lamports vs 477,964 paid,
  on a tx that succeeded). The actual-amount measurement is correct; the decoded limit simply does
  not bind the realized swap (a variant we don't fully model).
- `ComputeVictimSlippage` now caps utilization: within 5% over 1.0 (fee/rounding) clamps to 1.0; a
  gross excess returns `SlippageUnsupported` (-2) rather than a spurious value that would poison
  `maxSlippageUtilization`. Post-fix the epoch-955 victim distribution has zero >1 values. Pinned by
  `TestSlippageOverLimitGuard`.

## Epoch-955 full-run audit (validation of Phases 12–13b)
Full backfill of epoch 955 (431,213 slots) vs the v1 `solwich` dataset:
- **Variants:** 495,057 sandwiches = 430,321 cross-leader (a NEW variant — 0 overlap with v1) +
  64,736 same-leader (8,353 in-block + 56,383 same-leader cross-block); 2,191 multi-front/back.
- **Transfer:** 7,934 hasTransfer, **all** with a signer change (0 same-signer, per Phase 12).
- **CORE (302 high-intent attackers) retention: 4,270 / 4,288 = 99.58%** (was 97.7% before the
  two-tier fix); **all 126 active CORE attackers keep sandwiches** (none fully lost). The 18 residual
  are same-leader direction ambiguity — every leg is still detected, just paired differently.
- **v1-only (15,595, all non-CORE):** avg 1.96 victims vs 4.7 for shared; spot-checked as multi-swap
  route/arb txs paired as front/back by v1, which v2's clean-swap contract (Phase 9) correctly
  excludes. v2 finds slightly MORE victims than v1 on shared sandwiches (305,199 vs 302,053).
- **Slippage:** 30/30 victims reproduced from raw bytes to <1.5e-8; distribution 97.8% real,
  1.66% no-protection, 0.11% unsupported, 0.40% ambiguous, 0 anomalies after the >1 guard.

## Phase 14 — poolDex: per-leg exchange classification
- New `sandwich_txs.poolDex` (LowCardinality String) tags which exchange the sandwiched pool belongs
  to, on every leg — so sandwich-by-pool distribution is queryable even for victims whose slippage
  can't be decoded (PropAMM, multi-hop), which `slippageDexName` leaves empty.
- **Classified from the front-run's DEX instruction**, not the pool account owner: the balance-delta
  "pool" is often a shared vault authority (Meteora/Raydium pool authorities) whose owner is not a
  DEX program, so a naive owner lookup left 78% of victims (all Meteora DAMM v2 + Raydium v4/cpmm)
  unclassified. The front is a clean single swap on the sandwiched pool → exactly one exchange,
  applied to all legs; owner lookup kept only as a fallback.
- Covers all 25 labeled_dex programs incl. proprietary AMMs (solfi/bisonfi/tessera/...); a
  normalized-label fallback means a newly-added labeled DEX is never silently unclassified.
- Verified on two epoch-955 windows (23,101 and 60,872 victims): **100% classified, 0 unclassified**
  across every leg type; one poolDex per sandwich. Distribution is Meteora DAMM v2-dominated (~77%),
  then pump.fun / pump.fun AMM, with a long tail of Raydium variants and PropAMMs. Pinned by
  `sol/pool_dex_test.go`.
- **`slippageDexName` merged into `poolDex` and removed.** The slippage-decoder's DEX name was
  identical to poolDex on 99.96% of victims (differing only for ~0.04% multi-hop victims whose
  decodable leg wasn't the sandwiched pool), was empty for undecodable/-2/-4 victims, and was never
  read downstream. `poolDex` is now the single DEX field; the `sandwich_txs.slippageDexName` column
  and `SandwichTx.SlippageDexName` are gone (`dex.SlippageInfo.DexName` stays as the decoder's
  self-id, used by dex tests).
