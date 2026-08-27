package sol

import (
	"os"
	"testing"

	"sandwich-detector/config"
	"sandwich-detector/db"

	"github.com/spf13/viper"
)

// TestProcessWindowsIntegration exercises the full sliding double-rotation orchestration against
// real archival blocks with real leaders, and asserts the dedup invariant: no sandwichId is
// emitted twice across overlapping windows. Gated: set WINDOW_TEST=1, PARITY_RPC=<archival url>.
// Requires ClickHouse solwich (slot_leaders populated for the range) reachable locally.
func TestProcessWindowsIntegration(t *testing.T) {
	if os.Getenv("WINDOW_TEST") == "" {
		t.Skip("set WINDOW_TEST=1 (and PARITY_RPC) to run the window integration test")
	}
	rpc := os.Getenv("PARITY_RPC")
	if rpc == "" {
		t.Fatal("PARITY_RPC required")
	}
	SolanaRpcURL = rpc

	viper.Set("CLICKHOUSE_ADDR", "localhost:9000")
	viper.Set("CLICKHOUSE_DATABASE", "solwich")
	viper.Set("CLICKHOUSE_USERNAME", "default")
	viper.Set("CLICKHOUSE_PASSWORD", "sol")
	ch = db.NewClickhouse()
	defer ch.Close()

	// Fresh package-global caches so the run is self-contained.
	crossBlockCache = NewBlockCache(config.CROSS_BLOCK_CACHE_SIZE)
	seenSandwichIDs = NewAMMPoolLRU(config.SEEN_SANDWICH_CACHE_SIZE)

	const start = uint64(418768000)
	const nSlots = uint64(32) // ~8 leader rotations
	blocks := GetBlocks(start, nSlots)
	if len(blocks) == 0 {
		t.Fatalf("GetBlocks returned nothing")
	}

	sandwiches := ProcessBlocksForSandwich(blocks, "helius", false)

	seen := make(map[string]int)
	var inBlock, sameLeaderCross, crossLeader int
	for _, s := range sandwiches {
		seen[s.SandwichID]++
		switch {
		case s.CrossLeader:
			crossLeader++
		case s.CrossBlock:
			sameLeaderCross++
		default:
			inBlock++
		}
		// A cross-leader sandwich must be cross-block, and its leaders must differ and be known.
		if s.CrossLeader {
			if !s.CrossBlock {
				t.Errorf("sandwich %s crossLeader but not crossBlock", s.SandwichID)
			}
			if s.FrontLeader == "" || s.BackLeader == "" || s.FrontLeader == s.BackLeader {
				t.Errorf("sandwich %s crossLeader but leaders front=%q back=%q", s.SandwichID, s.FrontLeader, s.BackLeader)
			}
		}
	}

	dups := 0
	for id, c := range seen {
		if c > 1 {
			dups++
			t.Errorf("duplicate sandwichId emitted %d times: %s", c, id)
		}
	}

	t.Logf("blocks=%d sandwiches=%d (inBlock=%d sameLeaderCross=%d crossLeader=%d) dups=%d",
		len(blocks), len(sandwiches), inBlock, sameLeaderCross, crossLeader, dups)
	if len(sandwiches) == 0 {
		t.Fatalf("no sandwiches detected over %d blocks; expected a non-empty result", len(blocks))
	}
}
