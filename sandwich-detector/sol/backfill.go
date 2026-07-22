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

	"github.com/spf13/viper"
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

// buildHeliusURL resolves the Helius archival endpoint: an explicit Helius URL in config wins,
// otherwise it is built from HELIUS_RPC_API_KEY. Returns "" when no key is configured.
func buildHeliusURL() string {
	if u := viper.GetString("sol.rpc-helius"); u != "" && strings.Contains(u, "helius") {
		return u
	}
	key := viper.GetString("HELIUS_RPC_API_KEY")
	if key == "" || key == "YOUR-API-KEY" {
		return ""
	}
	return "https://mainnet.helius-rpc.com/?api-key=" + key
}

// RunBackfillCmd scans a bounded [startSlot, endSlot] range against Helius archival RPC and
// exits when done. Unlike the live command it does not follow the tip, resolves leaders from
// block rewards (so cross-block/cross-leader detection works on ranges the slot_leaders table
// does not yet cover), rate-limits requests, and retries slots that failed on the first pass.
func RunBackfillCmd(startSlot, endSlot uint64, rps int) error {
	if endSlot < startSlot {
		return fmt.Errorf("end slot (%d) must be >= start slot (%d)", endSlot, startSlot)
	}

	url := buildHeliusURL()
	if url == "" {
		return fmt.Errorf("backfill needs a Helius endpoint: set HELIUS_RPC_API_KEY or sol.rpc-helius")
	}
	SolanaRpcURL = url
	FetchRewards = true // resolve leaders from rewards
	if rps <= 0 {
		rps = config.HELIUS_DEFAULT_BACKFILL_RPS
	}
	rpcLimiter = newTokenBucket(rps)
	defer func() { rpcLimiter.close(); rpcLimiter = nil; FetchRewards = false }()
	logger.SolLogger.Info("Backfill mode", "start", startSlot, "end", endSlot, "rps", rps, "endpoint", "helius")

	ch = db.NewClickhouse()
	defer ch.Close()

	// Reset the sliding-window state so a fresh run is self-contained.
	crossBlockCache = NewBlockCache(config.CROSS_BLOCK_CACHE_SIZE)
	seenSandwichIDs = NewAMMPoolLRU(config.SEEN_SANDWICH_CACHE_SIZE)

	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)
	step := uint64(config.SOL_FETCH_SLOT_DATA_SLOT_NUM)

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
		processAndStore(blocks, "helius", deferTail)
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
		processAndStore(retryBlocks, "helius", false)
		logger.SolLogger.Info("Retried failed slots", "recovered", len(retryBlocks), "still_missing", len(failed)-len(retryBlocks))
	}

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
