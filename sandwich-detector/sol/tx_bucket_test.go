package sol

import (
	"encoding/json"
	"os"
	"testing"

	"sandwich-detector/sol/dex"
)

// TestCountDecodableSwapsSingleSwap guards the atomic-arb defense against over-rejection:
// a genuine single swap must decode to exactly one swap instruction, otherwise
// filterAndBuildTxBuckets would drop every real swap. The fixture is a pump.fun buy.
func TestCountDecodableSwapsSingleSwap(t *testing.T) {
	data, err := os.ReadFile("testdata/victims_base64/victim_03_base64.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var txData map[string]any
	if err := json.Unmarshal(data, &txData); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	tx, err := parseTransactionFromBase64(txData)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := countDecodableSwaps(tx); got != 1 {
		t.Fatalf("single swap should decode to exactly 1 swap instruction, got %d — the arb defense would over-reject", got)
	}

	// Two decodable swaps in one tx (the atomic-arb / multi-hop signature) must count as >1
	// so filterAndBuildTxBuckets drops it. Duplicate the fixture's real swap instruction.
	dupIdx := -1
	for i, di := range tx.DexInstructions {
		if dex.ExtractSlippage(di.ProgramID, di.Data) != nil {
			dupIdx = i
			break
		}
	}
	if dupIdx < 0 {
		t.Fatalf("fixture has no decodable swap instruction to duplicate")
	}
	tx.DexInstructions = append(tx.DexInstructions, tx.DexInstructions[dupIdx])
	if got := countDecodableSwaps(tx); got != 2 {
		t.Fatalf("two swap instructions should count as 2 (arb would be rejected), got %d", got)
	}
}
