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
