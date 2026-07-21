package sol

import (
	"os"
	"strconv"
	"testing"

	"sandwich-detector/config"

	MapSet "github.com/deckarep/golang-set/v2"
)

// TestUnifiedFinderInBlockParity checks that the unified SandwichFinder run over a single
// block finds exactly the same sandwiches as the legacy in-block finder. It is gated on a
// live RPC (archival, since interesting slots age out of the self-hosted node): set
// PARITY_RPC=<url> and optionally PARITY_SLOT=<slot>. Any difference is either a real bug
// or the known Position-vs-TxIdx fix on a parse-gapped block (inspect before dismissing).
func TestUnifiedFinderInBlockParity(t *testing.T) {
	rpc := os.Getenv("PARITY_RPC")
	if rpc == "" {
		t.Skip("set PARITY_RPC (archival URL) to run the in-block parity test")
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

	legacy := &InBlockSandwichFinder{Txs: blk.Txs, AmountThreshold: config.INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD}
	legacy.Find()
	legacyIDs := MapSet.NewSet[string]()
	for _, s := range legacy.Sandwiches {
		legacyIDs.Add(s.SandwichID)
	}

	unified := NewSandwichFinder(blk.Txs, nil, config.INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "live", nil)
	unified.Find()
	unifiedIDs := MapSet.NewSet[string]()
	for _, s := range unified.Sandwiches {
		if s.CrossBlock {
			t.Errorf("unified finder produced a cross-block sandwich on a single block: %s", s.SandwichID)
			continue
		}
		unifiedIDs.Add(s.SandwichID)
	}

	onlyLegacy := legacyIDs.Difference(unifiedIDs)
	onlyUnified := unifiedIDs.Difference(legacyIDs)
	t.Logf("slot %d: legacy=%d unified=%d", slot, legacyIDs.Cardinality(), unifiedIDs.Cardinality())
	if onlyLegacy.Cardinality() != 0 || onlyUnified.Cardinality() != 0 {
		t.Fatalf("parity mismatch: only-legacy=%v only-unified=%v", onlyLegacy.ToSlice(), onlyUnified.ToSlice())
	}
}
