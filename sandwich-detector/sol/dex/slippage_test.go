package dex

import (
	"crypto/sha256"
	"encoding/binary"
	"testing"
)

// buildAnchorInstruction creates test instruction data with discriminator + two u64 fields.
func buildAnchorInstruction(disc [8]byte, field1, field2 uint64) []byte {
	data := make([]byte, 24)
	copy(data[:8], disc[:])
	binary.LittleEndian.PutUint64(data[8:16], field1)
	binary.LittleEndian.PutUint64(data[16:24], field2)
	return data
}

func TestPumpFunBuy(t *testing.T) {
	// buy: amount=3466183577020, max_sol_cost=105643883
	data := buildAnchorInstruction(pumpFunBuyDiscriminator, 3466183577020, 105643883)
	info := ExtractSlippage(PumpFunProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeInput {
		t.Errorf("expected limit type %q, got %q", LimitTypeInput, info.LimitType)
	}
	if info.LimitAmount != 105643883 {
		t.Errorf("expected limit 105643883, got %d", info.LimitAmount)
	}
	if info.DexName != "pumpfun" {
		t.Errorf("expected dex name pumpfun, got %s", info.DexName)
	}
}

func TestPumpFunSell(t *testing.T) {
	// sell: amount=1000000, min_sol_output=500000
	data := buildAnchorInstruction(pumpFunSellDiscriminator, 1000000, 500000)
	info := ExtractSlippage(PumpFunProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeOutput {
		t.Errorf("expected limit type %q, got %q", LimitTypeOutput, info.LimitType)
	}
	if info.LimitAmount != 500000 {
		t.Errorf("expected limit 500000, got %d", info.LimitAmount)
	}
}

func TestPumpFunSellNoProtection(t *testing.T) {
	// sell with min_sol_output=0 → no protection
	data := buildAnchorInstruction(pumpFunSellDiscriminator, 1000000, 0)
	info := ExtractSlippage(PumpFunProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if !info.NoProtection {
		t.Error("expected NoProtection=true")
	}
}

func TestPumpFunAMMBuy(t *testing.T) {
	// AMM buy: base_amount_out=6180176637157, max_quote_amount_in=3764500000
	data := buildAnchorInstruction(pumpFunBuyDiscriminator, 6180176637157, 3764500000)
	info := ExtractSlippage(PumpFunAMMProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeInput {
		t.Errorf("expected limit type %q, got %q", LimitTypeInput, info.LimitType)
	}
	if info.LimitAmount != 3764500000 {
		t.Errorf("expected limit 3764500000, got %d", info.LimitAmount)
	}
	if info.DexName != "pumpfun_amm" {
		t.Errorf("expected dex name pumpfun_amm, got %s", info.DexName)
	}
}

func TestPumpFunBuyExactSolIn(t *testing.T) {
	// buy_exact_sol_in: spendable_sol_in=1000000, min_tokens_out=5000000
	data := buildAnchorInstruction(pumpFunBuyExactSolInDiscriminator, 1000000, 5000000)
	info := ExtractSlippage(PumpFunProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeOutput {
		t.Errorf("expected limit type %q, got %q", LimitTypeOutput, info.LimitType)
	}
	if info.LimitAmount != 5000000 {
		t.Errorf("expected limit 5000000, got %d", info.LimitAmount)
	}
}

func TestRaydiumV4Swap(t *testing.T) {
	// Non-Anchor: [0x09][amount_in u64][minimum_amount_out u64]
	data := make([]byte, 17)
	data[0] = 0x09
	binary.LittleEndian.PutUint64(data[1:9], 1000000000)
	binary.LittleEndian.PutUint64(data[9:17], 500000)
	info := ExtractSlippage(RaydiumV4Program, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeOutput {
		t.Errorf("expected limit type %q, got %q", LimitTypeOutput, info.LimitType)
	}
	if info.LimitAmount != 500000 {
		t.Errorf("expected limit 500000, got %d", info.LimitAmount)
	}
	if info.DexName != "raydium_v4" {
		t.Errorf("expected dex name raydium_v4, got %s", info.DexName)
	}
}

func TestRaydiumCPMMSwapBaseInput(t *testing.T) {
	data := buildAnchorInstruction(raydiumCPMMSwapBaseInput, 1000000, 500000)
	info := ExtractSlippage(RaydiumCPMMProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeOutput {
		t.Errorf("expected limit type %q, got %q", LimitTypeOutput, info.LimitType)
	}
}

func TestRaydiumCPMMSwapBaseOutput(t *testing.T) {
	// swap_base_output: [disc 8][max_amount_in u64][amount_out u64]
	// limit should be max_amount_in (first u64 = 500000), not amount_out (second u64 = 1000000)
	data := buildAnchorInstruction(raydiumCPMMSwapBaseOutput, 500000, 1000000)
	info := ExtractSlippage(RaydiumCPMMProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeInput {
		t.Errorf("expected limit type %q, got %q", LimitTypeInput, info.LimitType)
	}
	if info.LimitAmount != 500000 {
		t.Errorf("expected limit 500000 (max_amount_in), got %d", info.LimitAmount)
	}
}

func TestRaydiumV4SwapBaseOut(t *testing.T) {
	// swap_base_out (tag=0x0B): [tag][max_amount_in u64][amount_out u64]
	// limit should be max_amount_in (first u64 = 2000000), not amount_out
	data := make([]byte, 17)
	data[0] = 0x0B
	binary.LittleEndian.PutUint64(data[1:9], 2000000)   // max_amount_in
	binary.LittleEndian.PutUint64(data[9:17], 500000000) // amount_out
	info := ExtractSlippage(RaydiumV4Program, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeInput {
		t.Errorf("expected limit type %q, got %q", LimitTypeInput, info.LimitType)
	}
	if info.LimitAmount != 2000000 {
		t.Errorf("expected limit 2000000 (max_amount_in), got %d", info.LimitAmount)
	}
}

func TestMeteoraDAMMv2Swap(t *testing.T) {
	data := buildAnchorInstruction(meteoraSwapDiscriminator, 348577270, 486681226)
	info := ExtractSlippage(MeteoraDAMMv2Program, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.LimitType != LimitTypeOutput {
		t.Errorf("expected limit type %q, got %q", LimitTypeOutput, info.LimitType)
	}
	if info.LimitAmount != 486681226 {
		t.Errorf("expected limit 486681226, got %d", info.LimitAmount)
	}
	if info.DexName != "meteora_damm_v2" {
		t.Errorf("expected dex name meteora_damm_v2, got %s", info.DexName)
	}
}

func TestMeteoraDLMMSwap(t *testing.T) {
	data := buildAnchorInstruction(meteoraSwapDiscriminator, 1000000, 500000)
	info := ExtractSlippage(MeteoraDLMMProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo")
	}
	if info.DexName != "meteora_dlmm" {
		t.Errorf("expected dex name meteora_dlmm, got %s", info.DexName)
	}
}

// TestMeteoraDLMMDiscriminators pins every DLMM discriminator to its on-chain Anchor value
// (sha256("global:<method>")[:8]) so a mistyped constant fails here instead of silently
// under-decoding real txs to -2. A stale meteoraDLMMSwapExactOut shipped this way once.
func TestMeteoraDLMMDiscriminators(t *testing.T) {
	cases := []struct {
		name string
		disc [8]byte
	}{
		{"swap", meteoraSwapDiscriminator},
		{"swap2", meteoraSwap2Discriminator},
		{"swap_exact_out", meteoraDLMMSwapExactOut},
		{"swap_exact_out2", meteoraDLMMSwapExactOut2},
		{"swap_with_price_impact", meteoraDLMMSwapPriceImp},
		{"swap_with_price_impact2", meteoraDLMMSwapPriceImp2},
	}
	for _, c := range cases {
		sum := sha256.Sum256([]byte("global:" + c.name))
		var want [8]byte
		copy(want[:], sum[:8])
		if c.disc != want {
			t.Errorf("%s discriminator = %v, want %v", c.name, c.disc, want)
		}
	}
}

// TestMeteoraDLMMExactOut confirms swap_exact_out decodes as an INPUT limit (max_in_amount is the
// first u64) with the corrected discriminator — the regression the on-chain slippage audit found.
func TestMeteoraDLMMExactOut(t *testing.T) {
	data := buildAnchorInstruction(meteoraDLMMSwapExactOut, 987654321, 111111)
	info := ExtractSlippage(MeteoraDLMMProgram, data)
	if info == nil {
		t.Fatal("expected non-nil SlippageInfo for swap_exact_out (was under-decoding to -2)")
	}
	if info.LimitType != LimitTypeInput {
		t.Errorf("expected limit type %q, got %q", LimitTypeInput, info.LimitType)
	}
	if info.LimitAmount != 987654321 {
		t.Errorf("expected max_in_amount 987654321, got %d", info.LimitAmount)
	}
	if info.DexName != "meteora_dlmm" {
		t.Errorf("expected dex name meteora_dlmm, got %s", info.DexName)
	}
}

func TestUnknownProgram(t *testing.T) {
	data := buildAnchorInstruction(pumpFunBuyDiscriminator, 1000000, 500000)
	info := ExtractSlippage("unknownProgramId", data)
	if info != nil {
		t.Error("expected nil for unknown program")
	}
}

func TestTruncatedData(t *testing.T) {
	// Only 10 bytes — not enough for disc + two u64 fields
	data := make([]byte, 10)
	copy(data[:8], pumpFunBuyDiscriminator[:])
	info := ExtractSlippage(PumpFunProgram, data)
	if info != nil {
		t.Error("expected nil for truncated data")
	}
}
