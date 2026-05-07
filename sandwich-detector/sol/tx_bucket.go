package sol

import (
	"math"
	"sort"
	"time"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/types"
	"sandwich-detector/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

// knownAMMPools caches addresses that have been identified as AMM pools.
// Implemented as a bounded LRU set to prevent unbounded memory growth when
// processing many ephemeral memecoin pools. Concurrent-safe via internal mutex.
var knownAMMPools = NewAMMPoolLRU(config.AMM_POOL_CACHE_SIZE)

// accountOwnerCache caches on-chain account owner lookups (address → owner program).
// Used to determine whether an address is an AMM pool by checking if its owner
// is a known DEX program. Reduces RPC calls across blocks.
var accountOwnerCache = NewAccountOwnerLRU(config.ACCOUNT_OWNER_CACHE_SIZE)

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

	// SourceOwner is the owner whose balance change indicates it is the swap source
	// for IncomeToken (from user side, typically decreasing IncomeToken).
	SourceOwner string
	// SinkOwner is the single sink owner for ExpenseToken. If it differs from
	// SourceOwner, the tx likely contains an inline transfer after swap.
	SinkOwner string
	// HasInlineTransfer is true when SourceOwner and SinkOwner differ.
	HasInlineTransfer bool

	// Related pool and token info
	PoolAddress  string
	IncomeToken  string
	ExpenseToken string
	IncomeAmt    float64
	ExpenseAmt   float64
}

// filterAndBuildTxBuckets processes a list of transactions, filters for swap-like transactions, identifies the AMM pool involved, and groups them into buckets by (pool address, income token, expense token).
func filterAndBuildTxBuckets(txs types.Transactions, crossBlock bool) map[PoolKey][]PoolEntry {
	// Pre-fetch: collect candidate pool addresses and batch-query their owners.
	prefetchPoolOwners(txs)

	buckets := make(map[PoolKey][]PoolEntry)
	for idx, tx := range txs {
		if tx == nil || tx.IsFailed || tx.IsVote {
			continue
		}
		if tx.RelatedPools.Cardinality() == 0 {
			continue
		}

		// Identify the unique AMM pool among all pool-like participants.
		pools := tx.RelatedPools.ToSlice()
		ammCandidates := make([]string, 0, 1)
		ammAmountByPool := make(map[string]types.PoolAmount)
		for _, pool := range pools {
			amt, ok := tx.RelatedPoolsInfo[pool]
			if !ok {
				continue
			}
			if !identifyAMMPool(tx, pool, amt) {
				continue
			}
			ammCandidates = append(ammCandidates, pool)
			ammAmountByPool[pool] = amt
			if len(ammCandidates) > 1 {
				break
			}
		}

		// Require exactly one AMM candidate; otherwise this tx is ambiguous.
		if len(ammCandidates) != 1 {
			continue
		}
		ammPool := ammCandidates[0]
		ammAmt := ammAmountByPool[ammPool]
		// Validate AMM pool amounts
		if !(ammAmt.IncomeAmt > 0 && ammAmt.ExpenseAmt < 0) {
			continue
		}
		knownAMMPools.Add(ammPool)

		// Find source/sink owners by matching token balance changes against AMM amounts. Assume at most one source and one sink owner per transaction, which holds for most cases except some complex multi-hop swaps.
		sourceOwner, sinkOwner, hasInlineTransfer := inferSwapSourceAndSinkOwner(tx, ammPool, ammAmt)

		// Create bucket entry from AMM pool's perspective
		key := PoolKey{PoolAddress: ammPool, IncomeToken: ammAmt.IncomeToken, ExpenseToken: ammAmt.ExpenseToken}
		entry := PoolEntry{
			TxIdx:             idx,
			Slot:              tx.Slot,
			Position:          tx.Position,
			Signers:           MapSet.NewSet(tx.Signers...),
			SourceOwner:       sourceOwner,
			SinkOwner:         sinkOwner,
			HasInlineTransfer: hasInlineTransfer,
			PoolAddress:       ammPool,
			IncomeToken:       ammAmt.IncomeToken,
			ExpenseToken:      ammAmt.ExpenseToken,
			IncomeAmt:         ammAmt.IncomeAmt,
			ExpenseAmt:        ammAmt.ExpenseAmt,
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

// identifyAMMPool determines whether a single pool-like participant is an AMM pool.
// It uses a layered approach: explicit labeled pools → signer exclusion →
// caches/owner labels → liquidity heuristics.
func identifyAMMPool(tx *types.Transaction, pool string, amt types.PoolAmount) bool {
	if tx == nil || pool == "" {
		return false
	}
	if amt.IncomeToken == "" || amt.ExpenseToken == "" || amt.IncomeToken == amt.ExpenseToken {
		return false
	}
	if !(amt.IncomeAmt > 0 && amt.ExpenseAmt < 0) {
		return false
	}

	// Some AMM-related accounts (e.g. vault authorities) are explicitly labeled
	// and should be accepted directly.
	if utils.IsLabeledDexPool(pool) {
		knownAMMPools.Add(pool)
		return true
	}

	// A signer-owned participant is typically user side, not AMM pool.
	if utils.HasString(tx.Signers, pool) {
		return false
	}

	// AMM pool must have both income and expense token pre- and post-transaction
	preIn := tx.GetOwnerPreBalance(pool, amt.IncomeToken)
	preOut := tx.GetOwnerPreBalance(pool, amt.ExpenseToken)
	postIn := tx.GetOwnerPostBalance(pool, amt.IncomeToken)
	postOut := tx.GetOwnerPostBalance(pool, amt.ExpenseToken)
	hasBothPre := preIn > 0 && preOut > 0
	hasBothPost := postIn > 0 && postOut > 0
	if !(hasBothPre && hasBothPost) {
		return false
	}

	// Check known AMM pool cache and account owner cache (DEX program)
	if knownAMMPools.Contains(pool) {
		return true
	}

	if isAMM, cached := isAMMByOwner(pool); cached {
		if isAMM {
			knownAMMPools.Add(pool)
		}
		return isAMM
	}

	return false
}

// inferSwapSourceAndSinkOwner infers the user-side source/sink owners against the
// AMM pool amounts.
//
// Priority 1: find a single owner (not the AMM pool) whose IncomeToken decreases
// by ~expectedSource AND whose ExpenseToken increases by ~expectedSink in the same
// transaction. This is the normal swap case — source == sink, no inline transfer.
//
// Priority 2 (fallback): find source and sink owners independently. When they
// differ, the transaction contains an inline transfer from source to sink.
func inferSwapSourceAndSinkOwner(tx *types.Transaction, ammPool string, ammAmt types.PoolAmount) (string, string, bool) {
	if tx == nil {
		return "", "", false
	}

	expectedSource := ammAmt.IncomeAmt // user spends IncomeToken
	expectedSink := -ammAmt.ExpenseAmt // user receives ExpenseToken
	if expectedSource <= 0 || expectedSink <= 0 {
		return "", "", false
	}

	// Priority 1: unified owner — both legs belong to the same wallet.
	if unified := pickUnifiedSwapOwner(tx, ammPool, ammAmt.IncomeToken, ammAmt.ExpenseToken, expectedSource, expectedSink, config.SANDWICH_OWNER_MATCH_TOLERANCE); unified != "" {
		return unified, unified, false
	}

	// Priority 2: source and sink may be different owners (inline transfer).
	source := pickClosestOwnerByTokenDelta(tx, ammPool, ammAmt.IncomeToken, -expectedSource, config.SANDWICH_OWNER_MATCH_TOLERANCE)
	sink := pickClosestOwnerByTokenDelta(tx, ammPool, ammAmt.ExpenseToken, expectedSink, config.SANDWICH_OWNER_MATCH_TOLERANCE)

	hasInlineTransfer := source != "" && sink != "" && source != sink
	return source, sink, hasInlineTransfer
}

// pickUnifiedSwapOwner returns the single owner (excluding ammPool) that has both:
//   - a decrease in incomeToken whose absolute value is within tolerance of expectedSource
//   - an increase in expenseToken within tolerance of expectedSink
//
// Among all qualifying owners the one with the lowest combined relative difference
// score (sourceDiff + sinkDiff) is returned. Returns "" when no such owner exists.
func pickUnifiedSwapOwner(tx *types.Transaction, ammPool, incomeToken, expenseToken string, expectedSource, expectedSink float64, tolerance float64) string {
	if tx == nil || incomeToken == "" || expenseToken == "" || expectedSource <= 0 || expectedSink <= 0 {
		return ""
	}

	bestOwner := ""
	bestScore := math.MaxFloat64

	for owner, tokenChanges := range tx.OwnerBalanceChanges {
		if owner == ammPool {
			continue
		}

		sourceDelta := tokenChanges[incomeToken].TotalAmount
		sinkDelta := tokenChanges[expenseToken].TotalAmount

		// Must be spending incomeToken and receiving expenseToken.
		if sourceDelta >= 0 || sinkDelta <= 0 {
			continue
		}

		sourceDiff := getRelativeDiff(math.Abs(sourceDelta), expectedSource)
		sinkDiff := getRelativeDiff(sinkDelta, expectedSink)
		if sourceDiff < 0 || sourceDiff > tolerance || sinkDiff < 0 || sinkDiff > tolerance {
			continue
		}

		if score := sourceDiff + sinkDiff; score < bestScore {
			bestScore = score
			bestOwner = owner
		}
	}

	return bestOwner
}

func pickClosestOwnerByTokenDelta(tx *types.Transaction, excludedOwner string, token string, expectedDelta float64, tolerance float64) string {
	if tx == nil || token == "" || expectedDelta == 0 {
		return ""
	}

	expectedAbs := math.Abs(expectedDelta)
	bestOwner := ""
	bestDiff := math.MaxFloat64

	for owner, tokenChanges := range tx.OwnerBalanceChanges {
		if owner == excludedOwner {
			continue
		}
		ataAmounts, ok := tokenChanges[token]
		if !ok {
			continue
		}

		observedDelta := ataAmounts.TotalAmount
		if expectedDelta > 0 && observedDelta <= 0 {
			continue
		}
		if expectedDelta < 0 && observedDelta >= 0 {
			continue
		}

		observedAbs := math.Abs(observedDelta)
		relDiff := getRelativeDiff(observedAbs, expectedAbs)
		if relDiff < 0 || relDiff > tolerance {
			continue
		}

		if relDiff < bestDiff {
			bestDiff = relDiff
			bestOwner = owner
		}
	}

	return bestOwner
}

func getRelativeDiff(a, b float64) float64 {
	if a <= 0 || b <= 0 {
		return -1.0
	}
	return math.Abs(a-b) / math.Max(a, b)
}

// prefetchPoolOwners collects all candidate pool addresses from the transactions
// that might need owner-based AMM identification, filters out those already in
// the accountOwnerCache or knownAMMPools, and batch-queries the rest via
// getMultipleAccounts. Results are stored in accountOwnerCache for use by
// identifyAMMPool.
func prefetchPoolOwners(txs types.Transactions) {
	needed := make(map[string]struct{})
	for _, tx := range txs {
		if tx == nil || tx.IsFailed || tx.IsVote {
			continue
		}
		if tx.RelatedPools.Cardinality() == 0 {
			continue
		}
		pools := tx.RelatedPools.ToSlice()
		for _, p := range pools {
			if utils.IsLabeledDexPool(p) {
				knownAMMPools.Add(p)
				continue
			}

			// Skip if already in AMM pool cache or owner cache
			if knownAMMPools.Contains(p) {
				continue
			}
			if _, cached := accountOwnerCache.Get(p); cached {
				continue
			}
			needed[p] = struct{}{}
		}
	}

	if len(needed) == 0 {
		return
	}

	addrs := make([]string, 0, len(needed))
	for a := range needed {
		addrs = append(addrs, a)
	}

	queryStart := time.Now()
	owners, err := GetMultipleAccountOwners(addrs)
	queryCost := time.Since(queryStart)
	if err == nil {
		logger.SolLogger.Info("Fetch owners for possible AMM pools: getMultipleAccounts finished", "count", len(addrs), "duration", queryCost.String())
	} else {
		logger.SolLogger.Warn("Fetch owners for possible AMM pools: getMultipleAccounts failed", "count", len(addrs), "duration", queryCost.String(), "err", err)
	}

	for addr, owner := range owners {
		accountOwnerCache.Put(addr, owner)
		// If owner is a known DEX program, also add to knownAMMPools
		if utils.IsLabeledDexPrograms(owner) {
			knownAMMPools.Add(addr)
		}
	}
}

// isAMMByOwner checks the accountOwnerCache to determine if an address is an
// AMM pool (its on-chain owner is a known DEX program).
// Returns: (isAMM bool, cached bool).
func isAMMByOwner(addr string) (bool, bool) {
	owner, cached := accountOwnerCache.Get(addr)
	if !cached {
		return false, false
	}
	return utils.IsLabeledDexPrograms(owner), true
}

