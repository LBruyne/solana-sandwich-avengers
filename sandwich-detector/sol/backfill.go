package sol

import (
	"fmt"
	"strings"
	"time"

	"sandwich-detector/config"
	"sandwich-detector/db"
	"sandwich-detector/logger"
	"sandwich-detector/types"
	"sandwich-detector/utils"
)

// parseLeaderFromRewards returns the block producer, taken from the Fee reward recipient. It
// returns "" when rewards were not requested or no Fee reward is present.
func parseLeaderFromRewards(obj map[string]any) string {
	rewards, ok := obj["rewards"].([]any)
	if !ok {
		return ""
	}
	for _, r := range rewards {
		rm, ok := r.(map[string]any)
		if !ok {
			continue
		}
		if t, _ := rm["rewardType"].(string); t == "Fee" {
			pk, _ := rm["pubkey"].(string)
			return pk
		}
	}
	return ""
}

// tokenBucket is a minimal rate limiter: acquire() blocks until a token is free, and tokens
// refill at a fixed rate. A nil bucket is unlimited.
type tokenBucket struct {
	tokens chan struct{}
	stop   chan struct{}
}

func newTokenBucket(ratePerSec int) *tokenBucket {
	if ratePerSec <= 0 {
		return nil
	}
	tb := &tokenBucket{tokens: make(chan struct{}, ratePerSec), stop: make(chan struct{})}
	// Pre-fill so the first burst up to ratePerSec is immediate.
	for i := 0; i < ratePerSec; i++ {
		tb.tokens <- struct{}{}
	}
	go func() {
		ticker := time.NewTicker(time.Second / time.Duration(ratePerSec))
		defer ticker.Stop()
		for {
			select {
			case <-tb.stop:
				return
			case <-ticker.C:
				select {
				case tb.tokens <- struct{}{}:
				default: // bucket full
				}
			}
		}
	}()
	return tb
}

func (tb *tokenBucket) acquire() {
	if tb == nil {
		return
	}
	<-tb.tokens
}

func (tb *tokenBucket) close() {
	if tb != nil {
		close(tb.stop)
	}
}

// rpcLimiter throttles outbound RPC calls (see CallRpc). nil = unlimited (live self-hosted node).
var rpcLimiter *tokenBucket

// buildArchivalURL resolves the endpoint for historical backfill. It must serve blocks older than
// the self-hosted node's ~few-hour ledger window, so only the archival providers qualify:
// Chainstack (paid default), then Helius. sol.rpc (self-hosted) is never used here.
func buildArchivalURL() string {
	if u := buildChainstackURL(); u != "" {
		return u
	}
	return buildHeliusURL()
}

// rpcHost strips the scheme and path (which may embed an API key) so an endpoint can be logged
// without leaking credentials.
func rpcHost(url string) string {
	if i := strings.Index(url, "://"); i >= 0 {
		rest := url[i+3:]
		if j := strings.IndexAny(rest, "/?"); j >= 0 {
			return rest[:j]
		}
		return rest
	}
	return url
}

// RunBackfillCmd scans a bounded [startSlot, endSlot] range against Helius archival RPC and
// exits when done. Unlike the live command it does not follow the tip, resolves leaders from
// block rewards (so cross-block/cross-leader detection works on ranges the slot_leaders table
// does not yet cover), rate-limits requests, and retries slots that failed on the first pass.
func RunBackfillCmd(startSlot, endSlot uint64, rps int) error {
	if endSlot < startSlot {
		return fmt.Errorf("end slot (%d) must be >= start slot (%d)", endSlot, startSlot)
	}

	url := buildArchivalURL()
	if url == "" {
		return fmt.Errorf("backfill needs an archival RPC: set sol.rpc-chainstack + CHAINSTACK_API_KEY, or HELIUS_RPC_API_KEY")
	}
	SolanaRpcURL = url
	FetchRewards = true // resolve leaders from rewards
	FetchParallelism = config.BACKFILL_FETCH_PARALLEL_NUM
	defer func() { FetchParallelism = config.SOL_FETCH_SLOT_DATA_PARALLEL_NUM }()
	// rps <= 0 means no explicit throttle: the paid default RPC (Chainstack) absorbs the 8-worker
	// fetch concurrency, and CallRpc still backs off on any 429. Set --rps on rate-limited tiers
	// (e.g. Helius free = 10).
	if rps > 0 {
		rpcLimiter = newTokenBucket(rps)
		defer func() { rpcLimiter.close(); rpcLimiter = nil }()
	}
	defer func() { FetchRewards = false }()
	logger.SolLogger.Info("Backfill mode", "start", startSlot, "end", endSlot, "rps", rps, "endpoint", rpcHost(url))

	ch = db.NewClickhouse()
	defer ch.Close()

	// Reset the sliding-window state so a fresh run is self-contained.
	crossBlockCache = NewBlockCache(config.CROSS_BLOCK_CACHE_SIZE)
	seenSandwichIDs = NewAMMPoolLRU(config.SEEN_SANDWICH_CACHE_SIZE)

	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)
	step := uint64(config.BACKFILL_FETCH_SLOT_NUM)

	var failed []uint64
	for s := startSlot; s <= endSlot; s += step {
		n := step
		if s+n-1 > endSlot {
			n = endSlot - s + 1
		}
		blocks := GetBlocks(s, n)
		failed = append(failed, missingSlots(s, n, blocks)...)
		populateLeaders(blocks)
		// Last batch flushes the tail rotation (deferTail=false) so no sandwich is left pending.
		deferTail := s+n-1 < endSlot
		processAndStore(blocks, "backfill", deferTail)
		logger.SolLogger.Info("Backfill progress", "processed_through", s+n-1, "end", endSlot)
	}

	// One retry pass for slots that failed to fetch (transient RPC errors, not real skips).
	if len(failed) > 0 {
		logger.SolLogger.Warn("Retrying failed slots", "count", len(failed))
		retryBlocks := make(types.Blocks, 0, len(failed))
		for _, s := range failed {
			if b, err := GetBlock(s); err == nil && b != nil {
				retryBlocks = append(retryBlocks, b)
			}
		}
		populateLeaders(retryBlocks)
		processAndStore(retryBlocks, "backfill", false)
		logger.SolLogger.Info("Retried failed slots", "recovered", len(retryBlocks), "still_missing", len(failed)-len(retryBlocks))
	}

	// RPC usage for quota accounting (counts since process start; backfill is the only caller here).
	usage := RpcCallCountSnapshot()
	var totalCalls uint64
	kv := make([]any, 0, len(usage)*2+2)
	for m, n := range usage {
		totalCalls += n
		kv = append(kv, m, n)
	}
	kv = append(kv, "total", totalCalls)
	logger.SolLogger.Info("Backfill RPC usage", kv...)

	logger.SolLogger.Info("Backfill done", "start", startSlot, "end", endSlot)
	return nil
}

// missingSlots reports which of [start, start+n) produced no block (skipped or failed to fetch).
func missingSlots(start, n uint64, blocks types.Blocks) []uint64 {
	got := make(map[uint64]struct{}, len(blocks))
	for _, b := range blocks {
		got[b.Slot] = struct{}{}
	}
	var miss []uint64
	for s := start; s < start+n; s++ {
		if _, ok := got[s]; !ok {
			miss = append(miss, s)
		}
	}
	return miss
}

// populateLeaders records leaders resolved from block rewards into the cache and the DB, so
// window construction and later stages can classify cross-leader sandwiches.
func populateLeaders(blocks types.Blocks) {
	leaders := make(types.SlotLeaders, 0, len(blocks))
	for _, b := range blocks {
		if b == nil || b.Leader == "" {
			continue
		}
		crossBlockCache.SetLeader(b.Slot, b.Leader)
		leaders = append(leaders, &types.SlotLeader{Slot: b.Slot, Leader: b.Leader})
	}
	if len(leaders) > 0 {
		if err := ch.InsertSlotLeaders(leaders); err != nil {
			logger.SolLogger.Warn("failed to persist rewards-derived leaders", "err", err)
		}
	}
}
