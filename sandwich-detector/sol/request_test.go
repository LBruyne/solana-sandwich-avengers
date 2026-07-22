package sol

import (
	"fmt"
	"os"
	"testing"
	"sandwich-detector/logger"

	"github.com/spf13/viper"
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

// TestRpcURLResolution pins the endpoint-resolution contract: config holds base URLs only, API
// keys come from the environment, and the composed URL never appears in config. Viper state is
// process-global, so everything touched is saved and restored.
func TestRpcURLResolution(t *testing.T) {
	keys := []string{"sol.rpc-chainstack", "CHAINSTACK_API_KEY", "sol.rpc", "sol.rpc-helius", "HELIUS_RPC_API_KEY"}
	saved := make(map[string]string, len(keys))
	for _, k := range keys {
		saved[k] = viper.GetString(k)
	}
	savedURL := SolanaRpcURL
	defer func() {
		for k, v := range saved {
			viper.Set(k, v)
		}
		SolanaRpcURL = savedURL
	}()

	SolanaRpcURL = ""
	viper.Set("sol.rpc-chainstack", "https://chainstack.example/") // trailing slash must normalize
	viper.Set("CHAINSTACK_API_KEY", "ck")
	viper.Set("sol.rpc", "http://self-hosted:8899")
	viper.Set("sol.rpc-helius", "https://helius.example")
	viper.Set("HELIUS_RPC_API_KEY", "hk")

	// Chainstack is the default for both live and backfill.
	if got := GetSolanaRpcURL(); got != "https://chainstack.example/ck" {
		t.Errorf("live default = %q, want composed chainstack URL", got)
	}
	if got := buildArchivalURL(); got != "https://chainstack.example/ck" {
		t.Errorf("archival default = %q, want composed chainstack URL", got)
	}

	// No Chainstack key: live falls back to the self-hosted node, backfill to Helius
	// (the self-hosted node is not archival and must never be picked for backfill).
	viper.Set("CHAINSTACK_API_KEY", "")
	if got := GetSolanaRpcURL(); got != "http://self-hosted:8899" {
		t.Errorf("live fallback = %q, want self-hosted", got)
	}
	if got := buildArchivalURL(); got != "https://helius.example/?api-key=hk" {
		t.Errorf("archival fallback = %q, want composed helius URL", got)
	}

	// Placeholder keys count as unset.
	viper.Set("CHAINSTACK_API_KEY", "YOUR-API-KEY")
	viper.Set("HELIUS_RPC_API_KEY", "YOUR-API-KEY")
	if got := buildArchivalURL(); got != "" {
		t.Errorf("archival with placeholder keys = %q, want empty", got)
	}

	// An explicit override (set by backfill) beats everything.
	SolanaRpcURL = "http://override"
	if got := GetSolanaRpcURL(); got != "http://override" {
		t.Errorf("override = %q, want http://override", got)
	}
}

// TestRpcHostRedaction: endpoint log lines must carry the host only — the Chainstack key is a
// path segment and the Helius key a query param; both must be stripped.
func TestRpcHostRedaction(t *testing.T) {
	cases := map[string]string{
		"https://solana-mainnet.core.chainstack.com/secret-key": "solana-mainnet.core.chainstack.com",
		"https://mainnet.helius-rpc.com/?api-key=secret":        "mainnet.helius-rpc.com",
		"http://64.130.32.137:8899":                             "64.130.32.137:8899",
	}
	for in, want := range cases {
		if got := rpcHost(in); got != want {
			t.Errorf("rpcHost(%q) = %q, want %q", in, got, want)
		}
	}
}
