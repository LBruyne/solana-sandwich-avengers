package sol

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"sandwich-detector/config"
	"sandwich-detector/types"
	"sandwich-detector/utils"
)

// SandwichFinder detects sandwiches within a single detection window — an ordered
// sequence of transactions that may span one or more slots. It unifies the former
// in-block and cross-block finders: an in-block sandwich is simply the case where
// every leg shares a slot. Positional logic keys on TxIdx (the index within the
// window's tx sequence); for a single-slot window TxIdx equals the block position,
// so results match the old in-block finder on cleanly-parsed blocks.
//
// leaderBySlot, when provided, classifies a sandwich as cross-leader (front and back
// under different leaders). It may be nil, in which case crossLeader is always false.
type SandwichFinder struct {
	Txs             types.Transactions
	LeaderBySlot    map[uint64]string
	RpcSource       string
	AmountThreshold uint
	Sandwiches      []*types.CrossBlockSandwich

	// Internal states
	buckets                map[PoolKey][]PoolEntry
	confirmedSandwichTxIdx map[int]bool // TxIdx confirmed as a front/back leg of some sandwich
	sameLeaderPass         bool         // when true, cross-leader candidates are deferred to a later pass

	// Last evaluated sandwich, staged by Evaluate and consumed by RecordSandwich.
	lastTokenA           string
	lastTokenB           string
	lastFrontTxEntries   []PoolEntry
	lastBackTxEntries    []PoolEntry
	lastVictimEntries    []PoolEntry
	lastAdverseEntries   []PoolEntry
	lastFrontTransfers   []*Transfer
	lastBackTransfers    []*Transfer
	perfect              bool
	relativeAmtDiffB     float64
	profitA              float64
	FrontFromTotalAmount float64
	FrontToTotalAmount   float64
	BackFromTotalAmount  float64
	BackToTotalAmount    float64
}

// NewSandwichFinder builds a finder over an already slot/position-ordered tx window.
// confirmedSandwichTxIdx may be shared across windows so a front/back leg is claimed by
// at most one sandwich; pass nil for an isolated window.
func NewSandwichFinder(txs types.Transactions, leaderBySlot map[uint64]string, amountThreshold uint, rpcSource string, confirmed map[int]bool) *SandwichFinder {
	if confirmed == nil {
		confirmed = make(map[int]bool)
	}
	return &SandwichFinder{
		Txs:                    txs,
		LeaderBySlot:           leaderBySlot,
		RpcSource:              rpcSource,
		AmountThreshold:        amountThreshold,
		Sandwiches:             make([]*types.CrossBlockSandwich, 0),
		confirmedSandwichTxIdx: confirmed,
	}
}

// Find scans every (pool, A, B) bucket against its reverse (pool, B, A) bucket for
// front→victim→back patterns. Suppose a sandwich is front-run(s) A→B, victim(s) A→B,
// back-run(s) B→A.
func (f *SandwichFinder) Find() {
	f.Sandwiches = make([]*types.CrossBlockSandwich, 0)
	if f.confirmedSandwichTxIdx == nil {
		f.confirmedSandwichTxIdx = make(map[int]bool)
	}
	f.buckets = filterAndBuildTxBuckets(f.Txs, true)

	// Two-tier claiming. Same-leader sandwiches (in-block + same-leader cross-block) are matched
	// FIRST and claim their front/back legs; only then is cross-leader matching run, on whatever
	// legs remain. Without this, a coincidental cross-leader match — of which there are far more,
	// found over wider two-rotation windows — greedily steals a real same-leader sandwich's back-run
	// (a SELL that a reverse cross-leader match treats as its front), so the real sandwich is lost.
	// With no leader map every candidate is same-leader, so the second pass adds nothing and
	// single-slot/in-block behavior is unchanged.
	f.sameLeaderPass = true
	f.scanBuckets()
	if f.LeaderBySlot != nil {
		f.sameLeaderPass = false
		f.scanBuckets()
	}
}

// scanBuckets scans each (pool, A, B) front bucket against its reverse (pool, B, A) back bucket in a
// stable key order. Front/back txs are claimed greedily (confirmedSandwichTxIdx), so bucket order is
// significant when a tx could serve several sandwiches; a stable order keeps detection reproducible.
// (A tx can be a front in one direction and a back in the other; which sandwich wins a contested tx
// is an inherent ambiguity of greedy matching, resolved by this order and the same-leader-first tier.)
func (f *SandwichFinder) scanBuckets() {
	for _, key := range sortedBucketKeys(f.buckets) {
		frontTxBucket := f.buckets[key]
		if len(frontTxBucket) == 0 {
			continue
		}
		revKey := PoolKey{PoolAddress: key.PoolAddress, IncomeToken: key.ExpenseToken, ExpenseToken: key.IncomeToken}
		backTxBucket, ok := f.buckets[revKey]
		if !ok || len(backTxBucket) == 0 {
			continue
		}

		for i := 0; i < len(frontTxBucket); i++ {
			candidateFrontTxEntries := f.collectFrontTxs(frontTxBucket[i], frontTxBucket)
			if len(candidateFrontTxEntries) == 0 {
				continue
			}

			// Small-to-large: match the seed front alone first, then widen. This keeps
			// independent sandwiches (F1-V1-B1-F2-V2-B2) from being grouped as one.
			var backTxEntries []PoolEntry
			for k := 1; k <= len(candidateFrontTxEntries); k++ {
				backTxEntries = f.collectBackTxs(candidateFrontTxEntries[:k], backTxBucket)
				if len(backTxEntries) > 0 {
					break
				}
			}
			if len(backTxEntries) == 0 {
				continue
			}

			f.RecordSandwich()
			f.ResetSandwichState()
		}
	}
}

// stagedIsCrossLeader reports whether the sandwich currently staged in f.last* spans two leaders,
// using the same rule RecordSandwich applies. Used to defer cross-leader matches to the second tier.
func (f *SandwichFinder) stagedIsCrossLeader() bool {
	if f.LeaderBySlot == nil || len(f.lastFrontTxEntries) == 0 || len(f.lastBackTxEntries) == 0 {
		return false
	}
	fl := f.LeaderBySlot[f.lastFrontTxEntries[0].Slot]
	bl := f.LeaderBySlot[f.lastBackTxEntries[len(f.lastBackTxEntries)-1].Slot]
	return fl != "" && bl != "" && fl != bl
}

func (f *SandwichFinder) ResetSandwichState() {
	f.lastFrontTxEntries = nil
	f.lastBackTxEntries = nil
	f.lastVictimEntries = nil
	f.lastAdverseEntries = nil
	f.lastFrontTransfers = nil
	f.lastBackTransfers = nil
}

// collectFrontTxs grows a multi-front group from a seed: same-signer entries within
// SANDWICH_FRONTRUN_MAX_GAP of the previous accepted front (measured in TxIdx).
func (f *SandwichFinder) collectFrontTxs(seed PoolEntry, frontTxBucket []PoolEntry) []PoolEntry {
	res := make([]PoolEntry, 0)
	maxGap := config.SANDWICH_FRONTRUN_MAX_GAP

	// A front leg must be a clean single swap (not a multi-hop route or arbitrage) and unclaimed.
	if seed.IsMultiSwap || f.confirmedSandwichTxIdx[seed.TxIdx] {
		return res
	}
	if tx := f.Txs[seed.TxIdx]; tx == nil || tx.IsFailed || tx.IsVote {
		return res
	}

	signers := seed.Signers
	res = append(res, seed)
	lastIdx := seed.TxIdx

	// frontTxBucket is sorted by (slot, position), so scan forward.
	for _, entry := range frontTxBucket {
		if entry.TxIdx <= lastIdx {
			continue
		}
		if entry.TxIdx-lastIdx > maxGap {
			break
		}
		if entry.IsMultiSwap || f.confirmedSandwichTxIdx[entry.TxIdx] || f.Txs[entry.TxIdx] == nil || f.Txs[entry.TxIdx].IsFailed || f.Txs[entry.TxIdx].IsVote {
			continue
		}
		if !utils.SignersOverlap(entry.Signers, signers) {
			continue
		}
		res = append(res, entry)
		lastIdx = entry.TxIdx
	}
	return res
}

// collectBackTxs finds the back-run group that forms a sandwich with the given fronts.
// The first back seed must sit at least two positions after the last front (leaving room
// for a victim); accompanying backs share a signer and stay within SANDWICH_BACKRUN_MAX_GAP.
func (f *SandwichFinder) collectBackTxs(frontTxEntries []PoolEntry, backTxBucket []PoolEntry) []PoolEntry {
	maxGap := config.SANDWICH_BACKRUN_MAX_GAP
	lastFrontIdx := frontTxEntries[len(frontTxEntries)-1].TxIdx + 1

	for i, startBackEntry := range backTxBucket {
		if startBackEntry.TxIdx <= lastFrontIdx {
			continue
		}
		// A back leg must be a clean single swap (not a multi-hop route or arbitrage) and unclaimed.
		if startBackEntry.IsMultiSwap || f.confirmedSandwichTxIdx[startBackEntry.TxIdx] || f.Txs[startBackEntry.TxIdx] == nil || f.Txs[startBackEntry.TxIdx].IsFailed || f.Txs[startBackEntry.TxIdx].IsVote {
			continue
		}

		signers := startBackEntry.Signers
		candidateBackTxEntries := []PoolEntry{startBackEntry}
		lastIdx := startBackEntry.TxIdx

		for j := i + 1; j < len(backTxBucket); j++ {
			backEntry := backTxBucket[j]
			if backEntry.TxIdx <= lastIdx {
				continue
			}
			if backEntry.TxIdx-lastIdx > maxGap {
				break
			}
			if backEntry.IsMultiSwap || f.confirmedSandwichTxIdx[backEntry.TxIdx] || f.Txs[backEntry.TxIdx] == nil || f.Txs[backEntry.TxIdx].IsFailed || f.Txs[backEntry.TxIdx].IsVote {
				continue
			}
			if !utils.SignersOverlap(backEntry.Signers, signers) {
				continue
			}
			candidateBackTxEntries = append(candidateBackTxEntries, backEntry)
			lastIdx = backEntry.TxIdx
		}

		// Small-to-large over the back group as well.
		for k := 1; k <= len(candidateBackTxEntries); k++ {
			if f.Evaluate(frontTxEntries, candidateBackTxEntries[:k]) {
				// In the same-leader tier, defer a cross-leader match so its legs stay available
				// to any tighter same-leader sandwich; the second pass will pick it up.
				if f.sameLeaderPass && f.stagedIsCrossLeader() {
					f.ResetSandwichState()
					continue
				}
				return candidateBackTxEntries[:k]
			}
		}
	}
	return make([]PoolEntry, 0)
}

// Evaluate checks the amount round-trip, victim presence and attacker linkage, staging
// the sandwich in f.last* on success. Unlike the old cross-block finder it does NOT require
// front and back to be in different slots — a same-slot match is an in-block sandwich.
func (f *SandwichFinder) Evaluate(frontTxEntries []PoolEntry, backTxEntries []PoolEntry) bool {
	if len(frontTxEntries) == 0 || len(backTxEntries) == 0 {
		return false
	}

	tokenA := frontTxEntries[0].IncomeToken
	if tokenA == "" || tokenA != backTxEntries[0].ExpenseToken {
		return false
	}
	tokenB := frontTxEntries[0].ExpenseToken
	if tokenB == "" || tokenB != backTxEntries[0].IncomeToken {
		return false
	}
	frontSigners := unionSigners(frontTxEntries)
	backSigners := unionSigners(backTxEntries)

	threshold := f.AmountThreshold
	var frontAmtB float64
	for _, fe := range frontTxEntries {
		if fe.ExpenseAmt < 0 {
			frontAmtB += -fe.ExpenseAmt
		}
	}
	var backAmtB float64
	for _, be := range backTxEntries {
		if be.IncomeAmt > 0 {
			backAmtB += be.IncomeAmt
		}
	}

	// frontAmtB should cover backAmtB (SOL tokenB is exempt to tolerate fee noise).
	if tokenB != utils.SOL && utils.FloatRound(frontAmtB, 3) < utils.FloatRound(backAmtB, 3) {
		return false
	}
	similar, relativeAmtDiff := f.HasSimilarAmount(frontAmtB, backAmtB, float64(threshold))
	if !similar {
		return false
	}
	var perfect bool
	if tokenB == utils.SOL {
		perfect = relativeAmtDiff <= (config.SANDWICH_AMOUNT_SOL_TOLERANCE / 100)
	} else {
		perfect = relativeAmtDiff == 0.0
	}

	victimEntries := f.collectVictimEntries(frontTxEntries, backTxEntries)
	if len(victimEntries) == 0 {
		return false
	}
	adverseEntries := f.collectAdverseEntries(frontTxEntries, backTxEntries)

	// Transfer evidence is only meaningful when the signer CHANGES between front and back — it is
	// the linkage mechanism of wallet-rotating attackers. For same-signer sandwiches an inferred
	// inline transfer is routing noise (aggregator hop, owner-inference tolerance), and flagging it
	// overcounts transfer-style evasion: on v1 epoch-955 data, 1,733 of 2,310 hasTransfer
	// sandwiches (75%) had signerSame=true. Front inline evidence is further restricted to
	// transfers that actually land in the back side's owner set — the same criterion
	// sumFrontInlineBridgeAmount applies to bridge amounts.
	frontTransfers := make([]*Transfer, 0, 4)
	backTransfers := make([]*Transfer, 0)

	// Attacker linkage: same signer → confirmed. Otherwise same owner set, else transfer evidence.
	if !utils.SignersOverlap(frontSigners, backSigners) {
		frontInlineTransfers := collectInlineTransfers(frontTxEntries, f.Txs, transferSideFront)
		backTransfers = append(backTransfers, collectInlineTransfers(backTxEntries, f.Txs, transferSideBack)...)
		ownersOfBInFrtTxs := collectFrontOwnersByToken(frontTxEntries, f.Txs, tokenB)
		ownersOfBInBckTxs := collectBackOwnersByToken(backTxEntries, f.Txs, tokenB)
		for _, ev := range frontInlineTransfers {
			if ev.SinkOwner != "" && ownersOfBInBckTxs.Contains(ev.SinkOwner) {
				frontTransfers = append(frontTransfers, ev)
			}
		}
		if !ownersOfBInFrtTxs.IsSuperset(ownersOfBInBckTxs) {
			directFrontTransfers := collectDirectTransfers(
				f.Txs,
				frontTxEntries[len(frontTxEntries)-1].TxIdx+1,
				backTxEntries[0].TxIdx,
				tokenB,
				ownersOfBInFrtTxs,
				ownersOfBInBckTxs,
				transferSideFront,
			)
			directAmtB := sumTransferAmount(directFrontTransfers)
			inlineBridgeAmtB := sumFrontInlineBridgeAmount(frontInlineTransfers, tokenB, ownersOfBInFrtTxs, ownersOfBInBckTxs)
			bridgeAmtB := directAmtB + inlineBridgeAmtB
			if bridgeAmtB <= 0 {
				return false
			}
			similarTransfer, _ := f.HasSimilarAmount(backAmtB, bridgeAmtB, float64(threshold))
			if !similarTransfer {
				return false
			}
			frontTransfers = append(frontTransfers, directFrontTransfers...)
		}
	}

	f.lastTokenA = tokenA
	f.lastTokenB = tokenB
	f.lastFrontTxEntries = frontTxEntries
	f.lastBackTxEntries = backTxEntries
	f.lastVictimEntries = victimEntries
	f.lastAdverseEntries = adverseEntries
	f.lastFrontTransfers = frontTransfers
	f.lastBackTransfers = backTransfers
	f.perfect = perfect
	f.relativeAmtDiffB = relativeAmtDiff
	f.FrontToTotalAmount = frontAmtB
	f.BackFromTotalAmount = backAmtB
	var frontAmtA float64
	for _, fe := range frontTxEntries {
		if fe.IncomeAmt > 0 {
			frontAmtA += fe.IncomeAmt
		}
	}
	var backAmtA float64
	for _, be := range backTxEntries {
		if be.ExpenseAmt < 0 {
			backAmtA += -be.ExpenseAmt
		}
	}
	estimateBackAmtA := frontAmtB * backAmtA / backAmtB
	f.profitA = estimateBackAmtA - frontAmtA
	f.FrontFromTotalAmount = frontAmtA
	f.BackToTotalAmount = backAmtA
	return true
}

func (f *SandwichFinder) collectVictimEntries(frontTxEntries, backTxEntries []PoolEntry) []PoolEntry {
	if len(frontTxEntries) == 0 || len(backTxEntries) == 0 {
		return make([]PoolEntry, 0)
	}
	frontEndIdx := frontTxEntries[len(frontTxEntries)-1].TxIdx
	backBeginIdx := backTxEntries[0].TxIdx
	if backBeginIdx <= frontEndIdx+1 {
		return make([]PoolEntry, 0)
	}
	// Victims trade the same pool and direction as the front.
	frontKey := PoolKey{
		PoolAddress:  frontTxEntries[0].PoolAddress,
		IncomeToken:  frontTxEntries[0].IncomeToken,
		ExpenseToken: frontTxEntries[0].ExpenseToken,
	}
	frontTxBucket := f.buckets[frontKey]
	frontSigners := unionSigners(frontTxEntries)
	backSigners := unionSigners(backTxEntries)

	victims := make([]PoolEntry, 0)
	for _, e := range frontTxBucket {
		// A victim may serve multiple sandwiches, so confirmed txs are not excluded here.
		if f.Txs[e.TxIdx] == nil || f.Txs[e.TxIdx].IsFailed || f.Txs[e.TxIdx].IsVote {
			continue
		}
		if e.TxIdx <= frontEndIdx || e.TxIdx >= backBeginIdx {
			continue
		}
		if utils.SignersOverlap(e.Signers, frontSigners) || utils.SignersOverlap(e.Signers, backSigners) {
			continue
		}
		victims = append(victims, e)
	}
	return victims
}

// collectAdverseEntries collects txs between front and back trading in the back direction
// (B→A) with non-attacker signers — recorded for counts only.
func (f *SandwichFinder) collectAdverseEntries(frontTxEntries, backTxEntries []PoolEntry) []PoolEntry {
	if len(frontTxEntries) == 0 || len(backTxEntries) == 0 {
		return make([]PoolEntry, 0)
	}
	frontEndIdx := frontTxEntries[len(frontTxEntries)-1].TxIdx
	backBeginIdx := backTxEntries[0].TxIdx
	if backBeginIdx <= frontEndIdx+1 {
		return make([]PoolEntry, 0)
	}
	backKey := PoolKey{
		PoolAddress:  backTxEntries[0].PoolAddress,
		IncomeToken:  backTxEntries[0].IncomeToken,
		ExpenseToken: backTxEntries[0].ExpenseToken,
	}
	backTxBucket := f.buckets[backKey]
	frontSigners := unionSigners(frontTxEntries)
	backSigners := unionSigners(backTxEntries)

	adverse := make([]PoolEntry, 0)
	for _, e := range backTxBucket {
		if f.Txs[e.TxIdx] == nil || f.Txs[e.TxIdx].IsFailed || f.Txs[e.TxIdx].IsVote {
			continue
		}
		if e.TxIdx <= frontEndIdx || e.TxIdx >= backBeginIdx {
			continue
		}
		if utils.SignersOverlap(e.Signers, frontSigners) || utils.SignersOverlap(e.Signers, backSigners) {
			continue
		}
		adverse = append(adverse, e)
	}
	return adverse
}

// HasSimilarAmount reports whether two amounts are within threshold percent, returning the
// rounded relative difference. Differences below 1e-6 are treated as exact (float64 noise).
func (f *SandwichFinder) HasSimilarAmount(frontAmt, backAmt float64, threshold float64) (bool, float64) {
	if frontAmt <= 0 || backAmt <= 0 {
		return false, -1.0
	}
	relativeDiff := math.Abs(frontAmt-backAmt) / math.Max(frontAmt, backAmt)
	if relativeDiff <= 1e-6 {
		return true, 0.0
	}
	return relativeDiff <= threshold/100.0, utils.FloatRound(relativeDiff, 6)
}

// crossBlock reports whether the front and back legs span more than one slot.
func (f *SandwichFinder) isCrossBlock() bool {
	minSlot, maxSlot := f.lastFrontTxEntries[0].Slot, f.lastFrontTxEntries[0].Slot
	for _, e := range append(append([]PoolEntry{}, f.lastFrontTxEntries...), f.lastBackTxEntries...) {
		if e.Slot < minSlot {
			minSlot = e.Slot
		}
		if e.Slot > maxSlot {
			maxSlot = e.Slot
		}
	}
	return minSlot != maxSlot
}

func (f *SandwichFinder) RecordSandwich() {
	if len(f.lastFrontTxEntries) == 0 || len(f.lastBackTxEntries) == 0 || len(f.lastVictimEntries) == 0 {
		return
	}

	sandwichId := makeSandwichID(f.Txs[f.lastFrontTxEntries[0].TxIdx].Signature, f.Txs[f.lastBackTxEntries[0].TxIdx].Signature)

	// The exchange the sandwiched pool belongs to, read from the front-run (a clean single swap on
	// that pool). Shared by every leg so sandwich-by-pool distribution can be queried.
	front0 := f.lastFrontTxEntries[0]
	poolDex := classifySandwichDex(f.Txs[front0.TxIdx], front0.PoolAddress)

	frontTxs := make([]*types.SandwichTx, 0, len(f.lastFrontTxEntries))
	for _, fe := range f.lastFrontTxEntries {
		frontTxs = append(frontTxs, f.makeSandwichTx(sandwichId, fe, "frontRun", poolDex))
	}
	frontTransferTxs := make([]*types.SandwichTx, 0, len(f.lastFrontTransfers))
	for _, evidence := range f.lastFrontTransfers {
		if transferTx := makeTransferSandwichTx(sandwichId, evidence, f.lastTokenB); transferTx != nil {
			frontTransferTxs = append(frontTransferTxs, transferTx)
		}
	}
	backTxs := make([]*types.SandwichTx, 0, len(f.lastBackTxEntries))
	for _, be := range f.lastBackTxEntries {
		backTxs = append(backTxs, f.makeSandwichTx(sandwichId, be, "backRun", poolDex))
	}
	backTransferTxs := make([]*types.SandwichTx, 0, len(f.lastBackTransfers))
	for _, evidence := range f.lastBackTransfers {
		if transferTx := makeTransferSandwichTx(sandwichId, evidence, f.lastTokenB); transferTx != nil {
			backTransferTxs = append(backTransferTxs, transferTx)
		}
	}
	victimTxs := make([]*types.SandwichTx, 0, len(f.lastVictimEntries))
	for _, ve := range f.lastVictimEntries {
		victimTxs = append(victimTxs, f.makeSandwichTx(sandwichId, ve, "victim", poolDex))
	}
	adverseTxs := make([]*types.SandwichTx, 0, len(f.lastAdverseEntries))
	for _, ae := range f.lastAdverseEntries {
		adverseTxs = append(adverseTxs, f.makeSandwichTx(sandwichId, ae, "adverse", poolDex))
	}
	if len(frontTxs) > 0 {
		lf := frontTxs[len(frontTxs)-1]
		lf.SandwichTxTokenInfo.FromTotalAmount = f.FrontFromTotalAmount
		lf.SandwichTxTokenInfo.ToTotalAmount = f.FrontToTotalAmount
	}
	if len(backTxs) > 0 {
		lb := backTxs[len(backTxs)-1]
		lb.SandwichTxTokenInfo.FromTotalAmount = f.BackFromTotalAmount
		lb.SandwichTxTokenInfo.ToTotalAmount = f.BackToTotalAmount
		lb.SandwichTxTokenInfo.DiffA = f.BackToTotalAmount - f.FrontFromTotalAmount
		lb.SandwichTxTokenInfo.DiffB = f.FrontToTotalAmount - f.BackFromTotalAmount
	}

	allFrontSigners := unionSigners(f.lastFrontTxEntries)
	allBackSigners := unionSigners(f.lastBackTxEntries)
	signerSame := utils.SignersOverlap(allFrontSigners, allBackSigners)
	// OwnerSame uses the SAME owner sets the attacker-linkage check in Evaluate uses
	// (honoring inferred sink/source owners), so the label matches the decision that
	// accepted the sandwich.
	frontOwners := collectFrontOwnersByToken(f.lastFrontTxEntries, f.Txs, f.lastTokenB)
	backOwners := collectBackOwnersByToken(f.lastBackTxEntries, f.Txs, f.lastTokenB)
	ownerSame := frontOwners.IsSuperset(backOwners)

	// Classify block/leader span.
	crossBlock := f.isCrossBlock()
	frontSlot := f.lastFrontTxEntries[0].Slot
	backSlot := f.lastBackTxEntries[len(f.lastBackTxEntries)-1].Slot
	var frontLeader, backLeader string
	crossLeader := false
	if f.LeaderBySlot != nil {
		frontLeader = f.LeaderBySlot[frontSlot]
		backLeader = f.LeaderBySlot[backSlot]
		crossLeader = frontLeader != "" && backLeader != "" && frontLeader != backLeader
	}
	windowStartSlot, windowEndSlot := f.sandwichSlotSpan()

	slot := f.Txs[f.lastFrontTxEntries[0].TxIdx].Slot
	timestamp := f.Txs[f.lastFrontTxEntries[0].TxIdx].Timestamp

	hasFrontInlineTransfer, hasDirectTransfer, hasBackInlineTransfer := classifyTransferTypes(f.lastFrontTransfers, f.lastBackTransfers)

	frontTxs = append(frontTxs, frontTransferTxs...)
	backTxs = append(backTxs, backTransferTxs...)

	s := &types.CrossBlockSandwich{
		Sandwich: types.Sandwich{
			SandwichID:  sandwichId,
			TokenA:      f.lastTokenA,
			TokenB:      f.lastTokenB,
			CrossBlock:  crossBlock,
			CrossLeader: crossLeader,
			FrontLeader: frontLeader,
			BackLeader:  backLeader,

			WindowStartSlot: windowStartSlot,
			WindowEndSlot:   windowEndSlot,
			RpcSource:       f.RpcSource,

			Consecutive:       isSandwichConsecutive(f.lastFrontTxEntries, f.lastVictimEntries, f.lastBackTxEntries),
			FrontConsecutive:  isEntriesConsecutive(f.lastFrontTxEntries, crossBlock),
			BackConsecutive:   isEntriesConsecutive(f.lastBackTxEntries, crossBlock),
			VictimConsecutive: isEntriesConsecutive(f.lastVictimEntries, crossBlock),

			SignerSame:             signerSame,
			OwnerSame:              ownerSame,
			ATASame:                false, // TODO
			HasTransfer:            len(frontTransferTxs)+len(backTransferTxs) > 0,
			HasFrontInlineTransfer: hasFrontInlineTransfer,
			HasDirectTransfer:      hasDirectTransfer,
			HasBackInlineTransfer:  hasBackInlineTransfer,

			Perfect:       f.perfect,
			RelativeDiffB: f.relativeAmtDiffB,
			ProfitA:       f.profitA,

			MultiFrontRun: len(f.lastFrontTxEntries) > 1,
			MultiBackRun:  len(f.lastBackTxEntries) > 1,
			MultiVictim:   len(f.lastVictimEntries) > 1,
			FrontCount:    uint16(len(frontTxs)),
			BackCount:     uint16(len(backTxs)),
			VictimCount:   uint16(len(victimTxs)),
			AdverseCount:  uint16(len(adverseTxs)),
			FrontRun:      frontTxs,
			BackRun:       backTxs,
			Victims:       victimTxs,
			Adverse:       adverseTxs,
		},
		Slot:      slot,
		Timestamp: timestamp,
	}
	s.MaxSlippageUtilization = computeMaxSlippageUtilization(victimTxs)

	f.Sandwiches = append(f.Sandwiches, s)
	for _, e := range f.lastFrontTxEntries {
		f.confirmedSandwichTxIdx[e.TxIdx] = true
	}
	for _, e := range f.lastBackTxEntries {
		f.confirmedSandwichTxIdx[e.TxIdx] = true
	}
}

// sandwichSlotSpan returns the min and max slot over all legs of the staged sandwich.
func (f *SandwichFinder) sandwichSlotSpan() (uint64, uint64) {
	minSlot, maxSlot := f.lastFrontTxEntries[0].Slot, f.lastFrontTxEntries[0].Slot
	consider := func(entries []PoolEntry) {
		for _, e := range entries {
			if e.Slot < minSlot {
				minSlot = e.Slot
			}
			if e.Slot > maxSlot {
				maxSlot = e.Slot
			}
		}
	}
	consider(f.lastFrontTxEntries)
	consider(f.lastVictimEntries)
	consider(f.lastBackTxEntries)
	return minSlot, maxSlot
}

// makeSandwichTx builds a SandwichTx of the given kind ("frontRun"/"backRun"/"victim"/"adverse").
func (f *SandwichFinder) makeSandwichTx(sandwichId string, entry PoolEntry, kind, poolDex string) *types.SandwichTx {
	orig := f.Txs[entry.TxIdx]
	stx := &types.SandwichTx{
		SandwichID:  sandwichId,
		Transaction: *orig,
		SandwichTxTokenInfo: types.SandwichTxTokenInfo{
			FromToken:            entry.IncomeToken,
			ToToken:              entry.ExpenseToken,
			FromAmount:           math.Abs(entry.IncomeAmt),
			ToAmount:             math.Abs(entry.ExpenseAmt),
			OwnersOfB:            []string{},
			AttackerPreBalanceB:  0.0,
			AttackerPostBalanceB: 0.0,
			PoolPreBalanceB:      orig.GetOwnerPreBalance(entry.PoolAddress, f.lastTokenB),
			PoolPostBalanceB:     orig.GetOwnerPostBalance(entry.PoolAddress, f.lastTokenB),
			PoolDex:              poolDex,
		},
		InBundle: false,
		Type:     kind,
	}
	switch kind {
	case "frontRun":
		for owner, bc := range orig.OwnerBalanceChanges {
			if bc[f.lastTokenB].TotalAmount > 0 {
				stx.SandwichTxTokenInfo.OwnersOfB = append(stx.SandwichTxTokenInfo.OwnersOfB, owner)
				stx.SandwichTxTokenInfo.AttackerPreBalanceB += orig.OwnerPreBalances[owner][f.lastTokenB]
				stx.SandwichTxTokenInfo.AttackerPostBalanceB += orig.OwnerPostBalances[owner][f.lastTokenB]
			}
		}
	case "backRun":
		for owner, bc := range orig.OwnerBalanceChanges {
			if bc[f.lastTokenB].TotalAmount < 0 {
				stx.SandwichTxTokenInfo.OwnersOfB = append(stx.SandwichTxTokenInfo.OwnersOfB, owner)
				stx.SandwichTxTokenInfo.AttackerPreBalanceB += orig.OwnerPreBalances[owner][f.lastTokenB]
				stx.SandwichTxTokenInfo.AttackerPostBalanceB += orig.OwnerPostBalances[owner][f.lastTokenB]
			}
		}
	case "victim":
		fillVictimSlippage(stx, orig, entry)
	}
	return stx
}

// makeSandwichID derives a deterministic id from the first front and back signatures.
func makeSandwichID(frontSig, backSig string) string {
	h := sha256.Sum256([]byte(frontSig + ":" + backSig))
	return hex.EncodeToString(h[:])
}

// isSandwichConsecutive reports whether front, victim and back are back-to-back by position
// (F_last+1 == V_first and V_last+1 == B_first). Meaningful only within a single slot.
func isSandwichConsecutive(frontTxs, victimTxs, backTxs []PoolEntry) bool {
	if len(frontTxs) == 0 || len(victimTxs) == 0 || len(backTxs) == 0 {
		return false
	}
	if frontTxs[len(frontTxs)-1].Position+1 != victimTxs[0].Position {
		return false
	}
	if victimTxs[len(victimTxs)-1].Position+1 != backTxs[0].Position {
		return false
	}
	return true
}

// isEntriesConsecutive reports whether entries are back-to-back by position. When crossBlock is
// true they must also share a slot, so a cross-slot group is never consecutive.
func isEntriesConsecutive(es []PoolEntry, crossBlock bool) bool {
	if len(es) <= 1 {
		return true
	}
	for i := 1; i < len(es); i++ {
		if crossBlock && es[i].Slot != es[i-1].Slot {
			return false
		}
		if es[i].Position != es[i-1].Position+1 {
			return false
		}
	}
	return true
}
