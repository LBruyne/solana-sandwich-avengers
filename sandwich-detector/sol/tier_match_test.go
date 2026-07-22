package sol

import (
	"testing"
	"sandwich-detector/types"
	"sandwich-detector/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

// A real same-leader sandwich's back-run (a SELL) must not be stolen by a coincidental
// cross-leader match that would treat that SELL as its front. The two-tier claiming in Find
// (same-leader first, cross-leader second) guarantees this. Without it, greedy bucket order
// (the token mint sorts before the SOL sentinel, so the sell-front bucket is scanned first)
// hands the back to the reverse cross-leader match and the real sandwich is lost — the exact
// failure the epoch-955 audit found on 88 CORE sandwiches.

func tierTx(sig string, slot uint64) *types.Transaction {
	return &types.Transaction{
		Signature:           sig,
		Slot:                slot,
		Signers:             []string{},
		OwnerBalanceChanges: map[string]map[string]types.AtaAmounts{},
		TokenDecimals:       map[string]int{},
	}
}

func tierEntry(txIdx int, slot uint64, pos int, signer, income, expense string, incomeAmt, expenseAmt float64) PoolEntry {
	return PoolEntry{
		TxIdx:        txIdx,
		Slot:         slot,
		Position:     pos,
		Signers:      MapSet.NewSet(signer),
		PoolAddress:  "POOLpppppppppppppppppppppppppppppppppppppppp",
		IncomeToken:  income,
		ExpenseToken: expense,
		IncomeAmt:    incomeAmt,
		ExpenseAmt:   expenseAmt,
	}
}

func TestTwoTierSameLeaderWinsContestedBack(t *testing.T) {
	const (
		POOL = "POOLpppppppppppppppppppppppppppppppppppppppp"
		TOK  = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" // sorts before the SOL sentinel
		S1   = "attackerS1"
	)
	SOL := utils.SOL

	// Window: slots 100 (leader LA) and 102 (leader LB).
	//   idx0 F  slot100 buy  1000 TOK for 10 SOL     (S1)
	//   idx1 V  slot100 buy   100 TOK for  1 SOL     (victim)
	//   idx2 B  slot100 sell 1000 TOK for 10.2 SOL   (S1)  -> same-leader sandwich F/V/B
	//   idx3 V2 slot102 sell  200 TOK for  2 SOL     (victim of the reverse candidate)
	//   idx4 X  slot102 buy  1000 TOK for 10.2 SOL   (S1)  -> reverse cross-leader back for B
	txs := types.Transactions{
		tierTx("sigF", 100), tierTx("sigV", 100), tierTx("sigB", 100),
		tierTx("sigV2", 102), tierTx("sigX", 102),
	}
	txs[0].Signers = []string{S1}
	txs[2].Signers = []string{S1}
	txs[4].Signers = []string{S1}
	txs[1].Signers = []string{"victimW"}
	txs[3].Signers = []string{"victimW2"}

	buyKey := PoolKey{PoolAddress: POOL, IncomeToken: SOL, ExpenseToken: TOK}  // buy-front direction
	sellKey := PoolKey{PoolAddress: POOL, IncomeToken: TOK, ExpenseToken: SOL} // sell-front direction

	buyBucket := []PoolEntry{
		tierEntry(0, 100, 0, S1, SOL, TOK, 10, -1000),
		tierEntry(1, 100, 1, "victimW", SOL, TOK, 1, -100),
		tierEntry(4, 102, 4, S1, SOL, TOK, 10.2, -1000),
	}
	sellBucket := []PoolEntry{
		tierEntry(2, 100, 2, S1, TOK, SOL, 1000, -10.2),
		tierEntry(3, 102, 3, "victimW2", TOK, SOL, 200, -2),
	}

	leaderBySlot := map[uint64]string{100: "LA", 101: "LA", 102: "LB"}

	run := func(tiered bool) []*types.CrossBlockSandwich {
		f := NewSandwichFinder(txs, leaderBySlot, 10, "test", nil)
		f.buckets = map[PoolKey][]PoolEntry{buyKey: buyBucket, sellKey: sellBucket}
		if tiered {
			f.sameLeaderPass = true
			f.scanBuckets()
			f.sameLeaderPass = false
			f.scanBuckets()
		} else {
			// single-pass (pre-fix) behavior for contrast
			f.sameLeaderPass = false
			f.scanBuckets()
		}
		return f.Sandwiches
	}

	// Pre-fix (single pass): the sell-front bucket sorts first, so the reverse cross-leader match
	// claims B and the real same-leader sandwich is lost. This documents the bug the fix removes.
	single := run(false)
	if len(single) != 1 || !single[0].CrossLeader {
		t.Fatalf("sanity: expected single-pass to yield the (wrong) cross-leader match, got %d sandwiches", len(single))
	}

	// Fixed (two tiers): the same-leader sandwich claims B first; the cross-leader reverse can't form.
	got := run(true)
	if len(got) != 1 {
		t.Fatalf("expected exactly 1 sandwich, got %d", len(got))
	}
	s := got[0]
	if s.CrossLeader {
		t.Errorf("expected the same-leader sandwich to win, got a cross-leader one")
	}
	if s.FrontRun[0].Signature != "sigF" {
		t.Errorf("expected front = sigF, got %s", s.FrontRun[0].Signature)
	}
	if s.BackRun[0].Signature != "sigB" {
		t.Errorf("expected back = sigB (the contested SELL), got %s", s.BackRun[0].Signature)
	}
}
