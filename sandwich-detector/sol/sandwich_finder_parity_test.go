package sol

import (
	"os"
	"strconv"
	"testing"

	"sandwich-detector/config"

	MapSet "github.com/deckarep/golang-set/v2"
)

// TestUnifiedFinderDeterminism checks that the unified finder is reproducible: two runs over the
// same block must return the identical set of sandwichIds. Map iteration order used to make the
// output vary between runs; sortedBucketKeys fixes that, and this test guards the fix.
// Gated on a live archival RPC, since interesting slots age out of a non-archival node:
// set PARITY_RPC=<url> and optionally PARITY_SLOT=<slot>.
func TestUnifiedFinderDeterminism(t *testing.T) {
	rpc := os.Getenv("PARITY_RPC")
	if rpc == "" {
		t.Skip("set PARITY_RPC (archival URL) to run the determinism test")
	}
	SolanaRpcURL = rpc

	slot := uint64(368858815)
	if s := os.Getenv("PARITY_SLOT"); s != "" {
		if v, err := strconv.ParseUint(s, 10, 64); err == nil {
			slot = v
		}
	}

	blk, err := GetBlock(slot)
	if err != nil {
		t.Fatalf("GetBlock(%d): %v", slot, err)
	}

	run := func() MapSet.Set[string] {
		f := NewSandwichFinder(blk.Txs, nil, config.INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "live", nil)
		f.Find()
		ids := MapSet.NewSet[string]()
		for _, s := range f.Sandwiches {
			if s.CrossBlock {
				t.Errorf("unexpected cross-block sandwich on a single block: %s", s.SandwichID)
			}
			ids.Add(s.SandwichID)
		}
		return ids
	}

	first, second := run(), run()
	t.Logf("slot %d: %d sandwiches", slot, first.Cardinality())
	if !first.Equal(second) {
		t.Fatalf("non-deterministic detection: run1=%v run2=%v", first.ToSlice(), second.ToSlice())
	}
}
