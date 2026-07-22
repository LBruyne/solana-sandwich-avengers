package config

import "time"

// Path config
const (
	LogPath    = "./logs/"
	ConfigPath = "./"
)

// Input config
const (
	MIN_START_SLOT  = 400000000
	PER_LEADER_SLOT = 4 // number of continuous slots per leader
)

// Network config
const (
	DefaultRetryTimes    = 3
	DefaultRetryInterval = 50 * time.Millisecond
	DefaultTimeout       = 20 * time.Second
)

// Fetch config
const (
	// Jito recent bundles
	// Averagely 60-80 bundles in a slot
	// A slot is 0.4s
	// Around 20000 bundles in a minute
	JITO_RECENT_FETCH_LIMIT    = 30000
	JITO_RECENT_FETCH_LOWER    = 1000
	JITO_RECENT_FETCH_INTERVAL = 5 * time.Second

	// Jito bundles by slot
	JITO_CHECK_SANDWICH_INTERVAL = 5 * time.Second
	JITO_FETCH_BUNDLE_SAFE_LAG   = uint64(1000) // only fetch bundles for slots at least this far behind the sandwich detection frontier

	SOL_FETCH_SLOT_LEADER_MAX_GAP        = 4000000 // the API can preserve slot-leader data ~0.5 month ago
	SOL_FETCH_SLOT_LEADER_LIMIT          = 5000
	SOL_FETCH_SLOT_LEADER_LOWER          = 2000
	SOL_FETCH_SLOT_LEADER_SHORT_INTERVAL = 400 * time.Millisecond
	SOL_FETCH_SLOT_LEADER_LONG_INTERVAL  = 1000 * time.Second

	SOL_FETCH_SLOT_DATA_MAX_GAP      = 10000 // the API can preserve block data ~3 hours ago
	SOL_FETCH_SLOT_DATA_LATEST_GAP   = 5000  // only sync up to this many slots behind the latest block
	SOL_FETCH_SLOT_DATA_SLOT_NUM     = 8     // number of slots to fetch each time
	SOL_FETCH_SLOT_DATA_PARALLEL_NUM = 8     // number of parallel requests

	// Backfill fetches bigger batches with more workers than live: a bounded historical range has
	// no tip-latency constraint, larger batches amortize per-batch fixed costs and actually fill
	// the window-worker pool (a 32-slot batch spans ~8 rotations → ~8 windows vs ~2 at batch 8),
	// and the paid archival RPC absorbs the concurrency. Measured on 1,600 archival slots:
	// 3.2x throughput vs 8/8 with an identical sandwich set.
	// MUST stay <= CROSS_BLOCK_CACHE_SIZE (64): the sliding-window cache has to span at least one
	// full batch or cross-batch boundary windows silently stop forming.
	BACKFILL_FETCH_SLOT_NUM            = 32
	BACKFILL_FETCH_PARALLEL_NUM        = 16
	SOL_FETCH_SLOT_DATA_RETRYS         = 3 // number of retries on failure
	SOL_FETCH_SLOT_DATA_LONG_INTERVAL  = 400 * time.Millisecond * SOL_FETCH_SLOT_DATA_SLOT_NUM
	SOL_FETCH_SLOT_DATA_SHORT_INTERVAL = 400 * time.Millisecond
)

// Detection config
const (
	SOL_PROCESS_IN_BLOCK_SANDWICH_PARALLEL_NUM = 8 // number of parallel processing in-block sandwiches

	JITO_MARK_IN_BUNDLE_SANDWICH_TX_INTERVAL = 10 * time.Second // interval to mark sandwich txs in bundle
	JITO_MARK_IN_BUNDLE_SLOT_NUM             = 1000
	JITO_MARK_IN_BUNDLE_PARALLEL_NUM         = 8
	JITO_MARK_IN_BUNDLE_SAFE_LAG             = uint64(2000) // only check slots at least this far behind the sandwich detection frontier, to ensure cross-block sandwiches are fully written and bundles are correctly fetched

	INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD    = uint(10) // relative threshold between front-run/back-run
	CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD = uint(10)
	SANDWICH_AMOUNT_SOL_TOLERANCE             = 0.1
	SANDWICH_OWNER_MATCH_TOLERANCE            = 0.05 // relative tolerance for matching source/sink owners by token delta

	SANDWICH_BACKRUN_MAX_GAP  = 500 // Max positional gap between consecutive multi-back-run txs from same attacker
	SANDWICH_FRONTRUN_MAX_GAP = 500 // Max positional gap between consecutive multi-front-run txs from same attacker

	AMM_POOL_CACHE_SIZE      = 10000 // LRU capacity for known AMM pool addresses
	ACCOUNT_OWNER_CACHE_SIZE = 50000 // LRU capacity for account owner lookups (address -> owner program)

	CROSS_BLOCK_CACHE_SIZE                        = 64
	SOL_PROCESS_CROSS_BLOCK_SANDWICH_PARALLEL_NUM = 8
	SOL_PROCESS_CROSS_BLOCK_BUCKETS_PARALLEL_NUM  = 8

	// Sliding double-rotation windows overlap and are re-checked as the frontier advances,
	// so a sandwich can be re-found across batches. This LRU of recently emitted sandwichIds
	// suppresses duplicate inserts; it only needs to cover the few rotations near the frontier.
	SEEN_SANDWICH_CACHE_SIZE = 200000

	// Extra backoff applied on HTTP 429, doubling per consecutive throttle up to a cap.
	RPC_RATE_LIMIT_BACKOFF     = 500 * time.Millisecond
	RPC_RATE_LIMIT_BACKOFF_MAX = 8 * time.Second
)
