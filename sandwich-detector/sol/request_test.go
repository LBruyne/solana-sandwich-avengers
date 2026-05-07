package sol

import (
	"fmt"
	"testing"
	"sandwich-detector/logger"
)

func init() {
	logger.InitLogs("sol-test")
	SolanaRpcURL = "http://64.130.32.137:8899"
}

func TestGetSlotLeadersRealAPI(t *testing.T) {
	if SolanaRpcURL == "" {
		t.Skip("SolanaRpcURL not configured, skipping real API test")
	}

	start := uint64(366000000)
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
	if SolanaRpcURL == "" {
		t.Skip("SolanaRpcURL not configured, skipping real API test")
	}

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
