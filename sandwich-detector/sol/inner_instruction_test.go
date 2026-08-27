package sol

import (
	"encoding/json"
	"os"
	"testing"

	"sandwich-detector/utils"
)

// TestInnerInstructionBase64Capture pins the base64 compiled inner-instruction fix.
// Under encoding=base64 the RPC returns inner (CPI) instructions in compiled form
// ({programIdIndex, accounts:[indices]}), not the jsonParsed shape ({programId, addresses}).
// Before the fix parseDexInstructions read the jsonParsed fields and silently dropped every
// inner DEX instruction, so CPI-routed victims never got a slippage value on archival RPCs.
//
// The fixture is a real getTransaction(base64) result for a pump.fun victim whose swap is
// reached via CPI; the matching jsonParsed form is testdata/victims/victim_03_pumpfun_buy_99.json.
func TestInnerInstructionBase64Capture(t *testing.T) {
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
		t.Fatalf("parseTransactionFromBase64: %v", err)
	}

	var inner, innerDex int
	for _, di := range tx.DexInstructions {
		if di.IsInner {
			inner++
			if utils.IsLabeledDexPrograms(di.ProgramID) {
				innerDex++
			}
		}
	}
	if innerDex == 0 {
		t.Fatalf("no inner DEX instructions captured from base64 encoding (got %d DEX instrs total, %d inner) — base64 inner parsing is broken", len(tx.DexInstructions), inner)
	}
	if tx.InnerInstructionsNil {
		t.Fatalf("InnerInstructionsNil=true but the fixture has innerInstructions present")
	}
}

// TestInnerInstructionCrossEncodingEquivalence checks that the same transaction parsed from
// base64 (production path) and from jsonParsed (test parser) yields the same set of DEX program
// IDs — i.e. the base64 path no longer loses the inner instructions the jsonParsed path sees.
func TestInnerInstructionCrossEncodingEquivalence(t *testing.T) {
	b64, err := os.ReadFile("testdata/victims_base64/victim_03_base64.json")
	if err != nil {
		t.Fatalf("read base64 fixture: %v", err)
	}
	var txData map[string]any
	if err := json.Unmarshal(b64, &txData); err != nil {
		t.Fatalf("unmarshal base64 fixture: %v", err)
	}
	txB64, err := parseTransactionFromBase64(txData)
	if err != nil {
		t.Fatalf("parseTransactionFromBase64: %v", err)
	}

	txsJSON, err := loadTxsFromJSONFile("testdata/victims/victim_03_pumpfun_buy_99.json")
	if err != nil {
		t.Fatalf("load jsonParsed fixture: %v", err)
	}
	if len(txsJSON) != 1 {
		t.Fatalf("expected 1 tx in jsonParsed fixture, got %d", len(txsJSON))
	}

	setB64 := map[string]int{}
	for _, di := range txB64.DexInstructions {
		setB64[di.ProgramID]++
	}
	setJSON := map[string]int{}
	for _, di := range txsJSON[0].DexInstructions {
		setJSON[di.ProgramID]++
	}

	for prog, n := range setJSON {
		if setB64[prog] < n {
			t.Fatalf("base64 path captured fewer %s DEX instrs than jsonParsed: base64=%d jsonParsed=%d", prog, setB64[prog], n)
		}
	}
}
