package sol

import (
	"math"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/sol/dex"
	"sandwich-detector/types"
	"sandwich-detector/utils"
	"sort"
	"time"

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

// sortedBucketKeys returns the bucket keys in a stable order (pool, incomeToken, expenseToken).
// Detection claims front/back txs greedily, so the order in which buckets are scanned decides
// which sandwich wins when txs could serve several — iterating the map directly would make results
// depend on Go's randomized map order and be irreproducible.
func sortedBucketKeys(buckets map[PoolKey][]PoolEntry) []PoolKey {
	keys := make([]PoolKey, 0, len(buckets))
	for k := range buckets {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].PoolAddress != keys[j].PoolAddress {
			return keys[i].PoolAddress < keys[j].PoolAddress
		}
		if keys[i].IncomeToken != keys[j].IncomeToken {
			return keys[i].IncomeToken < keys[j].IncomeToken
		}
		return keys[i].ExpenseToken < keys[j].ExpenseToken
	})
	return keys
}

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

	// IsMultiSwap is true when the tx decodes to more than one swap (a multi-hop route or an
	// atomic arbitrage). Such a tx may still be a victim through its leg on this pool, but it is
	// never eligible as a front/back leg — attacker legs are always clean single swaps.
	IsMultiSwap bool

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

		// Identify every AMM pool leg this tx has. A clean swap has exactly one; a multi-hop route
		// or an atomic arbitrage has several. The tx is bucketed under EACH valid leg so it can be
		// a VICTIM through whichever leg trades the sandwiched pair — but a tx with more than one
		// leg (or >1 decodable swap) is marked IsMultiSwap and may never be a front/back attacker
		// leg (attacker legs are always clean single swaps).
		type ammLeg struct {
			pool string
			amt  types.PoolAmount
		}
		legs := make([]ammLeg, 0, 1)
		for _, pool := range tx.RelatedPools.ToSlice() {
			amt, ok := tx.RelatedPoolsInfo[pool]
			if !ok {
				continue
			}
			if !identifyAMMPool(tx, pool, amt) {
				continue
			}
			if !(amt.IncomeAmt > 0 && amt.ExpenseAmt < 0) {
				continue
			}
			legs = append(legs, ammLeg{pool, amt})
		}
		if len(legs) == 0 {
			continue
		}
		isMultiSwap := len(legs) > 1 || countDecodableSwaps(tx) > 1

		for _, leg := range legs {
			knownAMMPools.Add(leg.pool)
			// Find source/sink owners by matching token balance changes against the AMM amounts.
			sourceOwner, sinkOwner, hasInlineTransfer := inferSwapSourceAndSinkOwner(tx, leg.pool, leg.amt)
			key := PoolKey{PoolAddress: leg.pool, IncomeToken: leg.amt.IncomeToken, ExpenseToken: leg.amt.ExpenseToken}
			buckets[key] = append(buckets[key], PoolEntry{
				TxIdx:             idx,
				Slot:              tx.Slot,
				Position:          tx.Position,
				Signers:           MapSet.NewSet(tx.Signers...),
				SourceOwner:       sourceOwner,
				SinkOwner:         sinkOwner,
				HasInlineTransfer: hasInlineTransfer,
				IsMultiSwap:       isMultiSwap,
				PoolAddress:       leg.pool,
				IncomeToken:       leg.amt.IncomeToken,
				ExpenseToken:      leg.amt.ExpenseToken,
				IncomeAmt:         leg.amt.IncomeAmt,
				ExpenseAmt:        leg.amt.ExpenseAmt,
			})
		}
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

// countDecodableSwaps counts how many of a tx's DEX instructions decode as swaps.
// A clean single swap yields exactly one; more indicate a multi-hop route or arbitrage.
func countDecodableSwaps(tx *types.Transaction) int {
	n := 0
	for _, di := range tx.DexInstructions {
		if dex.ExtractSlippage(di.ProgramID, di.Data) != nil {
			n++
		}
	}
	return n
}

// frontRunDexProgram returns the program ID of the front-run's single decodable swap instruction,
// which identifies the sandwiched pool's DEX (the front-run is a clean single swap on that pool).
// Returns "" when the front-run's DEX has no slippage decoder (humidifi/solfi/proprietary etc.):
// the sandwiched pool is then undecodable, so a victim's slippage ON THAT POOL is unmeasurable and
// must not be read off some other decodable pool the victim's route happens to touch.
func frontRunDexProgram(tx *types.Transaction) string {
	if tx == nil {
		return ""
	}
	for _, di := range tx.DexInstructions {
		if dex.ExtractSlippage(di.ProgramID, di.Data) != nil {
			return di.ProgramID
		}
	}
	return ""
}

// isIdentifiedPool reports whether an owner is already known to be an AMM pool
// (explicitly labeled or resolved via the owner cache). Used to keep the swap
// counterparty from being selected as the swap user.
func isIdentifiedPool(owner string) bool {
	if utils.IsLabeledDexPool(owner) || knownAMMPools.Contains(owner) {
		return true
	}
	isAMM, cached := isAMMByOwner(owner)
	return cached && isAMM
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
		if owner == ammPool || isIdentifiedPool(owner) {
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
		if owner == excludedOwner || isIdentifiedPool(owner) {
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

	// Negative-cache addresses with no on-chain account (closed ATAs, ephemeral accounts):
	// they cannot be AMM pools, yet they dominate the candidate set — measured ~98% of
	// prefetch traffic was re-querying the same null addresses every window. An empty owner
	// makes isAMMByOwner answer (false, cached). Skipped when the batch errored so unqueried
	// addresses stay eligible for the next window.
	if err == nil {
		for addr := range needed {
			if _, ok := owners[addr]; !ok {
				accountOwnerCache.Put(addr, "")
			}
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
