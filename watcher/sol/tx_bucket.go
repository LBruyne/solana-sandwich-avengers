package sol

import (
	"fmt"
	"sort"
	"watcher/config"
	"watcher/types"
	"watcher/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

// knownAMMPools caches addresses that have been identified as AMM pools.
// Implemented as a bounded LRU set to prevent unbounded memory growth when
// processing many ephemeral memecoin pools. Concurrent-safe via internal mutex.
var knownAMMPools = NewAMMPoolLRU(config.AMM_POOL_CACHE_SIZE)

// unionSigners returns the union of all Signers sets from the given entries.
// Used to build a comprehensive signer set for multi-front/multi-back comparisons.
func unionSigners(entries []PoolEntry) MapSet.Set[string] {
	if len(entries) == 1 {
		return entries[0].Signers
	}
	result := MapSet.NewSet[string]()
	for _, e := range entries {
		result = result.Union(e.Signers)
	}
	return result
}

// PoolKey is used to group transactions that interact with the same pool and token pair
type PoolKey struct {
	PoolAddress  string // Suppose a sandwich consists of front-run A->B, victim(s) A->B, and back-run B->A. A pool address must have two sides of amount change about A and B.
	IncomeToken  string // From pool's perspective, in frontTx, A is incomeToken, delta > 0; in backTx, B is incomeToken, delta < 0
	ExpenseToken string // From pool's perspective, in frontTx, b is expenseToken, delta < 0; in backTx, A is expenseToken, delta > 0
}

type PoolEntry struct {
	TxIdx    int // Index of the transaction in the original txs slice (for sandwich detection)
	Slot     uint64
	Position int
	Signers  MapSet.Set[string] // Signers of the transaction, used for multi-signer detection

	// Related pool and token info
	PoolAddress  string
	IncomeToken  string
	ExpenseToken string
	IncomeAmt    float64
	ExpenseAmt   float64
}

// filterAndBuildTxBuckets processes a list of transactions, filters for swap-like transactions, identifies the AMM pool involved, and groups them into buckets by (pool address, income token, expense token).
func filterAndBuildTxBuckets(txs types.Transactions, crossBlock bool) map[PoolKey][]PoolEntry {
	buckets := make(map[PoolKey][]PoolEntry)
	for idx, tx := range txs {
		if tx == nil || tx.IsFailed || tx.IsVote {
			continue
		}
		// A swap transaction must have exactly 2 pool-like participants (AMM pool + user)
		if tx.RelatedPools.Cardinality() != 2 {
			continue
		}

		// Get the two pools and validate they form a swap (same tokens, opposite directions)
		pools := tx.RelatedPools.ToSlice()
		pool0, pool1 := pools[0], pools[1]
		amt0 := tx.RelatedPoolsInfo[pool0]
		amt1 := tx.RelatedPoolsInfo[pool1]

		if amt0.IncomeToken == "" || amt0.ExpenseToken == "" || amt1.IncomeToken == "" || amt1.ExpenseToken == "" {
			continue
		}
		// The two pools must have the same tokens in opposite directions to form a swap
		if !(amt0.IncomeToken == amt1.ExpenseToken && amt0.ExpenseToken == amt1.IncomeToken) {
			continue
		}

		// Determine which pool is the AMM and which is the user
		ammPool, ammAmt := identifyAMMPool(tx, pool0, amt0, pool1, amt1)
		fmt.Printf("Tx %d: Identified AMM pool %s with income %.2f %s and expense %.2f %s\n", idx, ammPool, ammAmt.IncomeAmt, ammAmt.IncomeToken, ammAmt.ExpenseAmt, ammAmt.ExpenseToken)

		// Validate AMM pool amounts
		if !(ammAmt.IncomeAmt > 0 && ammAmt.ExpenseAmt < 0) {
			continue
		}

		// Create bucket entry from AMM pool's perspective
		key := PoolKey{PoolAddress: ammPool, IncomeToken: ammAmt.IncomeToken, ExpenseToken: ammAmt.ExpenseToken}
		entry := PoolEntry{
			TxIdx:        idx,
			Slot:         tx.Slot,
			Position:     tx.Position,
			Signers:      MapSet.NewSet(tx.Signers...),
			PoolAddress:  ammPool,
			IncomeToken:  ammAmt.IncomeToken,
			ExpenseToken: ammAmt.ExpenseToken,
			IncomeAmt:    ammAmt.IncomeAmt,
			ExpenseAmt:   ammAmt.ExpenseAmt,
		}
		buckets[key] = append(buckets[key], entry)
	}

	if crossBlock {
		// Sort each bucket first by slotId, then by position
		for k := range buckets {
			sort.Slice(buckets[k], func(i, j int) bool {
				if buckets[k][i].Slot != buckets[k][j].Slot {
					return buckets[k][i].Slot < buckets[k][j].Slot
				}
				return buckets[k][i].Position < buckets[k][j].Position
			})
		}
	} else {
		// Sort each bucket by position only
		for k := range buckets {
			sort.Slice(buckets[k], func(i, j int) bool {
				return buckets[k][i].Position < buckets[k][j].Position
			})
		}
	}

	return buckets
}

func isEntriesConsecutive(es []PoolEntry, crossBlock bool) bool {
	if len(es) <= 1 {
		return true
	}

	if crossBlock {
		for i := 1; i < len(es); i++ {
			if es[i].Slot != es[i-1].Slot {
				return false
			}
			if es[i].Position != es[i-1].Position+1 {
				return false
			}
		}
	} else {
		for i := 1; i < len(es); i++ {
			if es[i].Position != es[i-1].Position+1 {
				return false
			}
		}
	}
	return true
}

// identifyAMMPool determines which of the two pools is the AMM pool (liquidity pool).
// It uses a layered approach: cache → signer → pre/post-balance heuristics.
func identifyAMMPool(tx *types.Transaction, pool0 string, amt0 types.PoolAmount, pool1 string, amt1 types.PoolAmount) (string, types.PoolAmount) {
	result, resultAmt := identifyAMMPoolInner(tx, pool0, amt0, pool1, amt1)
	// Cache the identified AMM pool for future lookups
	knownAMMPools.Add(result)
	return result, resultAmt
}

func identifyAMMPoolInner(tx *types.Transaction, pool0 string, amt0 types.PoolAmount, pool1 string, amt1 types.PoolAmount) (string, types.PoolAmount) {
	// Rule 1: If one pool matches a transaction signer, it's the user; the other is the AMM pool.
	isPool0Signer := utils.HasString(tx.Signers, pool0)
	isPool1Signer := utils.HasString(tx.Signers, pool1)
	if isPool0Signer && !isPool1Signer {
		return pool1, amt1
	}
	if isPool1Signer && !isPool0Signer {
		return pool0, amt0
	}

	// Neither or both are signers (rare case, e.g., PDA executing swap).
	// Use layered fallback heuristics.
	tokenA, tokenB := amt0.IncomeToken, amt0.ExpenseToken
	preA0 := tx.GetOwnerPreBalance(pool0, tokenA)
	preB0 := tx.GetOwnerPreBalance(pool0, tokenB)
	preA1 := tx.GetOwnerPreBalance(pool1, tokenA)
	preB1 := tx.GetOwnerPreBalance(pool1, tokenB)

	// Fallback 1a: Check if only one pool holds both tokens before the swap.
	pool0HasBothPre := preA0 > 0 && preB0 > 0
	pool1HasBothPre := preA1 > 0 && preB1 > 0
	if pool0HasBothPre && !pool1HasBothPre {
		return pool0, amt0
	}
	if pool1HasBothPre && !pool0HasBothPre {
		return pool1, amt1
	}

	// Fallback 1b: Check post-balance — an AMM pool always retains both tokens after a swap.
	postA0 := tx.GetOwnerPostBalance(pool0, tokenA)
	postB0 := tx.GetOwnerPostBalance(pool0, tokenB)
	postA1 := tx.GetOwnerPostBalance(pool1, tokenA)
	postB1 := tx.GetOwnerPostBalance(pool1, tokenB)
	pool0HasBothPost := postA0 > 0 && postB0 > 0
	pool1HasBothPost := postA1 > 0 && postB1 > 0
	if pool0HasBothPost && !pool1HasBothPost {
		return pool0, amt0
	}
	if pool1HasBothPost && !pool0HasBothPost {
		return pool1, amt1
	}

	// Fallback 2: Check the known AMM pool cache.
	known0 := knownAMMPools.Contains(pool0)
	known1 := knownAMMPools.Contains(pool1)
	if known0 && !known1 {
		return pool0, amt0
	}
	if known1 && !known0 {
		return pool1, amt1
	}

	// Fallback 3: Both hold both tokens (or neither does).
	// Compare total pre-balance — the AMM pool holds significantly more liquidity.
	total0 := preA0 + preB0
	total1 := preA1 + preB1
	if total0 > total1 {
		return pool0, amt0
	}
	if total1 > total0 {
		return pool1, amt1
	}

	// Final fallback: cannot distinguish, default to pool0.
	return pool0, amt0
}
