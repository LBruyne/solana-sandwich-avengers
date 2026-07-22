package sol

import (
	"fmt"
	"os"
	"testing"
	"sandwich-detector/logger"
)

func init() {
	logger.InitLogs("sol-test")
	// Default endpoint for tests that resolve pool-owner accounts via getMultipleAccounts
	// (account owners are current state, not ledger history, so the self-hosted node serves them).
	// Live RealAPI tests and the fixture tests override this as needed.
	SolanaRpcURL = "http://64.130.32.137:8899"
}

// solRealRPC returns the RPC endpoint for live tests. Set SOL_REAL_TEST=1 and optionally
// SOL_REAL_RPC=<url> (defaults to the self-hosted node).
func solRealRPC(t *testing.T) string {
	if os.Getenv("SOL_REAL_TEST") == "" {
		t.Skip("set SOL_REAL_TEST=1 (and optionally SOL_REAL_RPC) to hit a live RPC")
	}
	url := os.Getenv("SOL_REAL_RPC")
	if url == "" {
		url = "http://64.130.32.137:8899"
	}
	return url
}

func TestGetSlotLeadersRealAPI(t *testing.T) {
	SolanaRpcURL = solRealRPC(t)

	// getSlotLeaders only serves recent slots, so anchor to the current tip.
	current, err := GetCurrentSlot()
	if err != nil {
		t.Fatalf("GetCurrentSlot failed: %v", err)
	}
	start := current - 1000
	limit := uint64(10)

	fmt.Println("Testing real getSlotLeaders RPC...")

	leaders, err := GetSlotLeaders(start, limit)
	if err != nil {
		t.Fatalf("GetSlotLeaders failed: %v", err)
	}

	if len(leaders) == 0 {
		t.Fatalf("expected at least 1 leader, got 0")
	}

	for i, l := range leaders {
		fmt.Printf("[%d] Leader: %d, %s\n", i, l.Slot, l.Leader)
	}
}

func TestGetCurrentSlotRealAPI(t *testing.T) {
	SolanaRpcURL = solRealRPC(t)

	fmt.Println("Testing real getSlot RPC...")

	slot, err := GetCurrentSlot()
	if err != nil {
		t.Fatalf("GetCurrentSlot failed: %v", err)
	}

	fmt.Printf("Current Slot: %d\n", slot)
	if slot == 0 {
		t.Errorf("unexpected slot number: 0")
	}
}
