package sol

import (
	"testing"
	"sandwich-detector/types"

	MapSet "github.com/deckarep/golang-set/v2"
)

// Transfer evidence must be gated on a signer CHANGE between front and back: for same-signer
// sandwiches an inferred inline transfer is routing noise, and v1 flagged it as hasTransfer —
// 75% of v1's epoch-955 hasTransfer sandwiches had signerSame=true. These tests pin the fixed
// contract: no transfer evidence when signers overlap; when signers differ, front inline
// evidence is kept only if it lands in the back side's owner set.

func benchTxWithOwnerDelta(deltas map[string]map[string]float64) *types.Transaction {
	obc := make(map[string]map[string]types.AtaAmounts, len(deltas))
	for owner, tokens := range deltas {
		obc[owner] = make(map[string]types.AtaAmounts, len(tokens))
		for token, amt := range tokens {
			aa := types.NewAtaAmounts()
			aa.AddAtaAmount("ata-"+owner+"-"+token, amt)
			obc[owner][token] = aa
		}
	}
	return &types.Transaction{OwnerBalanceChanges: obc}
}

func transferGateEntry(txIdx int, signer, income, expense string, incomeAmt, expenseAmt float64, source, sink string, inline bool) PoolEntry {
	return PoolEntry{
		TxIdx:             txIdx,
		Position:          txIdx,
		Signers:           MapSet.NewSet(signer),
		SourceOwner:       source,
		SinkOwner:         sink,
		HasInlineTransfer: inline,
		PoolAddress:       "POOL1",
		IncomeToken:       income,
		ExpenseToken:      expense,
		IncomeAmt:         incomeAmt,
		ExpenseAmt:        expenseAmt,
	}
}

// buildTransferGateFinder wires a minimal window: fronts A→B, one victim A→B, one back B→A.
func buildTransferGateFinder(fronts []PoolEntry, victim PoolEntry, back PoolEntry, txs types.Transactions) *SandwichFinder {
	f := NewSandwichFinder(txs, nil, 10, "test", nil)
	frontKey := PoolKey{PoolAddress: "POOL1", IncomeToken: "TOKA", ExpenseToken: "TOKB"}
	backKey := PoolKey{PoolAddress: "POOL1", IncomeToken: "TOKB", ExpenseToken: "TOKA"}
	frontBucket := append(append([]PoolEntry{}, fronts...), victim)
	f.buckets = map[PoolKey][]PoolEntry{frontKey: frontBucket, backKey: {back}}
	return f
}

func TestTransferGateSameSignerNoEvidence(t *testing.T) {
	frontTx := benchTxWithOwnerDelta(map[string]map[string]float64{"W2": {"TOKB": 99}})
	victimTx := benchTxWithOwnerDelta(nil)
	backTx := benchTxWithOwnerDelta(nil)
	txs := types.Transactions{frontTx, victimTx, backTx}

	// Front routes TOKB to a different owner (inline transfer), but the BACK signs with the
	// same key — no evasion, so no transfer evidence may be attached.
	front := transferGateEntry(0, "S1", "TOKA", "TOKB", 10, -100, "W1", "W2", true)
	victim := transferGateEntry(1, "VIC", "TOKA", "TOKB", 1, -10, "", "", false)
	back := transferGateEntry(2, "S1", "TOKB", "TOKA", 100, -10, "W2", "", false)

	f := buildTransferGateFinder([]PoolEntry{front}, victim, back, txs)
	if !f.Evaluate([]PoolEntry{front}, []PoolEntry{back}) {
		t.Fatal("same-signer sandwich must still be accepted")
	}
	if len(f.lastFrontTransfers) != 0 || len(f.lastBackTransfers) != 0 {
		t.Fatalf("same-signer sandwich must carry NO transfer evidence, got front=%d back=%d",
			len(f.lastFrontTransfers), len(f.lastBackTransfers))
	}
}

func TestTransferGateSignerChangeKeepsBridge(t *testing.T) {
	frontTx := benchTxWithOwnerDelta(map[string]map[string]float64{"W2": {"TOKB": 99}})
	victimTx := benchTxWithOwnerDelta(nil)
	backTx := benchTxWithOwnerDelta(nil)
	txs := types.Transactions{frontTx, victimTx, backTx}

	// Front (signer S1) routes TOKB inline into W2; the back sells from W2 under a DIFFERENT
	// signer S2 — the inline transfer is the linkage evidence and must be kept.
	front := transferGateEntry(0, "S1", "TOKA", "TOKB", 10, -100, "W1", "W2", true)
	victim := transferGateEntry(1, "VIC", "TOKA", "TOKB", 1, -10, "", "", false)
	back := transferGateEntry(2, "S2", "TOKB", "TOKA", 100, -10, "W2", "", false)

	f := buildTransferGateFinder([]PoolEntry{front}, victim, back, txs)
	if !f.Evaluate([]PoolEntry{front}, []PoolEntry{back}) {
		t.Fatal("owner-linked signer-change sandwich must be accepted")
	}
	if len(f.lastFrontTransfers) != 1 || !f.lastFrontTransfers[0].IsInline || f.lastFrontTransfers[0].SinkOwner != "W2" {
		t.Fatalf("expected exactly the W2 bridge inline evidence, got %+v", f.lastFrontTransfers)
	}
}

func TestTransferGateNoiseHopDropped(t *testing.T) {
	front1Tx := benchTxWithOwnerDelta(map[string]map[string]float64{"W2": {"TOKB": 60}})
	front2Tx := benchTxWithOwnerDelta(map[string]map[string]float64{"W3": {"TOKB": 40}})
	victimTx := benchTxWithOwnerDelta(nil)
	backTx := benchTxWithOwnerDelta(nil)
	txs := types.Transactions{front1Tx, front2Tx, victimTx, backTx}

	// Two front legs, same signer S1: one routes into W2 (which the back sells from — a real
	// bridge), the other into W3 (unrelated hop). Only the W2 evidence may survive.
	front1 := transferGateEntry(0, "S1", "TOKA", "TOKB", 6, -60, "W1", "W2", true)
	front2 := transferGateEntry(1, "S1", "TOKA", "TOKB", 4, -40, "W1", "W3", true)
	victim := transferGateEntry(2, "VIC", "TOKA", "TOKB", 1, -10, "", "", false)
	back := transferGateEntry(3, "S2", "TOKB", "TOKA", 100, -10, "W2", "", false)

	f := buildTransferGateFinder([]PoolEntry{front1, front2}, victim, back, txs)
	if !f.Evaluate([]PoolEntry{front1, front2}, []PoolEntry{back}) {
		t.Fatal("sandwich must be accepted (W2∪W3 ⊇ W2)")
	}
	if len(f.lastFrontTransfers) != 1 || f.lastFrontTransfers[0].SinkOwner != "W2" {
		t.Fatalf("noise hop to W3 must be dropped, only W2 kept; got %+v", f.lastFrontTransfers)
	}
}
