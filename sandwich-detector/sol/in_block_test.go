package sol

import (
	"fmt"
	"testing"
	"time"

	"sandwich-detector/config"
	"sandwich-detector/types"
)

var inSlot uint64

func init() {
	inSlot = 368858815 // Replace a fresh slot id
}

func TestFindInBlockSandwichesBySlot(t *testing.T) {
	tt := time.Now()
	blk, err := GetBlock(inSlot)
	fmt.Printf("GetBlock time cost: %s\n", time.Since(tt).String())
	if err != nil {
		t.Fatalf("GetBlock(%d) error: %v", inSlot, err)
	}
	if blk == nil || len(blk.Txs) == 0 {
		t.Fatalf("empty block or no txs at slot %d", inSlot)
	}

	// for i, tx := range blk.Txs {
	// 	if tx.Signature == "3ry7r5naR3cB2pPr4GG6XxjUgpjwvAfqQ1C6rG7WSJHe5cNUjbMc3bC3DbV24N8tsqtcB1J9Zqv7HsFNDwUS9j59" ||
	// 		tx.Signature == "5ip6guoScYKGzwYedNXJoiuWtRYHR411PWEKkegHDfDhU6NTkFeMJXGgxd4r7TiPYLvs1wZrMe49zpjL4se846dW" {
	// 		types.PPTx(i, tx, true)
	// 	}
	// }

	res := FindInBlockSandwiches(blk)

	fmt.Printf("Detected %d in-block sandwiches in slot=%d\n\n", len(res), inSlot)
	for i, s := range res {
		types.PPInBlockSandwich(i+1, s)
	}

	for _, s := range res {
		if s == nil {
			t.Fatalf("nil sandwich encountered")
		}
		if len(s.FrontRun) == 0 || len(s.BackRun) == 0 || len(s.Victims) == 0 {
			t.Fatalf("invalid sandwich parts: empty front/back/victim")
		}
		for _, f := range s.FrontRun {
			if !(f.FromToken == s.TokenA && f.ToToken == s.TokenB) {
				t.Fatalf("front direction mismatch: got %s->%s, expect %s->%s",
					f.FromToken, f.ToToken, s.TokenA, s.TokenB)
			}
		}
		for _, v := range s.Victims {
			if !(v.FromToken == s.TokenA && v.ToToken == s.TokenB) {
				t.Fatalf("victim direction mismatch: got %s->%s, expect %s->%s",
					v.FromToken, v.ToToken, s.TokenA, s.TokenB)
			}
		}
		for _, b := range s.BackRun {
			if !(b.FromToken == s.TokenB && b.ToToken == s.TokenA) {
				t.Fatalf("back direction mismatch: got %s->%s, expect %s->%s",
					b.FromToken, b.ToToken, s.TokenB, s.TokenA)
			}
		}
	}
}

func TestFindInBlockSandwichesBySlotParallel(t *testing.T) {
	tt1 := time.Now()
	blks := GetBlocks(inSlot, config.SOL_FETCH_SLOT_DATA_SLOT_NUM)
	fmt.Printf("GetBlocks time cost: %s\n", time.Since(tt1).String())
	if len(blks) == 0 {
		t.Fatalf("GetBlocks returned no blocks")
	}

	tt2 := time.Now()
	res := ProcessInBlockSandwich(blks)
	fmt.Printf("ProcessInBlockSandwich time cost: %s\n", time.Since(tt2).String())

	fmt.Printf("Detected %d in-block sandwiches in slot=%d\n\n", len(res), inSlot)
	for i, s := range res {
		types.PPInBlockSandwich(i+1, s)
	}

	for _, s := range res {
		if s == nil {
			t.Fatalf("nil sandwich encountered")
		}
		if len(s.FrontRun) == 0 || len(s.BackRun) == 0 || len(s.Victims) == 0 {
			t.Fatalf("invalid sandwich parts: empty front/back/victim")
		}
		for _, f := range s.FrontRun {
			if !(f.FromToken == s.TokenA && f.ToToken == s.TokenB) {
				t.Fatalf("front direction mismatch: got %s->%s, expect %s->%s",
					f.FromToken, f.ToToken, s.TokenA, s.TokenB)
			}
		}
		for _, v := range s.Victims {
			if !(v.FromToken == s.TokenA && v.ToToken == s.TokenB) {
				t.Fatalf("victim direction mismatch: got %s->%s, expect %s->%s",
					v.FromToken, v.ToToken, s.TokenA, s.TokenB)
			}
		}
		for _, b := range s.BackRun {
			if !(b.FromToken == s.TokenB && b.ToToken == s.TokenA) {
				t.Fatalf("back direction mismatch: got %s->%s, expect %s->%s",
					b.FromToken, b.ToToken, s.TokenB, s.TokenA)
			}
		}
	}
}
