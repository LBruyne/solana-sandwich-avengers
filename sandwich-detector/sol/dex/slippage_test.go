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
	binary.LittleEndian.PutUint64(data[1:9], 2000000)    // max_amount_in
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

const (
	wsol    = "So11111111111111111111111111111111111111112"
	testTok = "TOK"
)

var testDecimals = map[string]int{wsol: 9, testTok: 6}

// TestSlippageAdmissibleBand pins the admissible band [SlippageMinAdmissible, SlippageMaxAdmissible]
// and the ratio direction, at and just outside both boundaries, for both limit types.
//
// It replaces TestSlippageOverLimitGuard, which asserted a 5%-over-1.0 tolerance plus a clamp to
// 1.0. The ceiling is now 1.0 with SlippageCeilingTolerance of relative slack: a fill AT the limit
// clamps onto the ceiling, and anything genuinely past it (including the former "gross over-limit ->
// Unsupported" case) is SlippageOutOfRange. There is no lower rejection at all — a nominal limit
// yields a consumption near 0, which is a measurement.
//
// Boundary arithmetic note: every "exact" case below divides by a power-of-ten-scaled value whose
// quotient is the correctly-rounded float64 of the decimal literal, so the boundary values hold
// bit-exactly and the comparisons in ComputeVictimSlippage are not knife-edge. The two cases that
// ARE knife-edge are deliberate: one_ulp_over and inside_ceiling_slack exercise the slack itself.
func TestSlippageAdmissibleBand(t *testing.T) {
	// buy  = INPUT-limited  (limit = max_sol_cost in fromToken)  -> ratio = actual / limit
	// sell = OUTPUT-limited (limit = min_sol_output in toToken)  -> ratio = limit / actual
	buy := func(limitLamports uint64) []byte {
		return buildAnchorInstruction(pumpFunBuyDiscriminator, 1, limitLamports)
	}
	sell := func(limitLamports uint64) []byte {
		return buildAnchorInstruction(pumpFunSellDiscriminator, 1, limitLamports)
	}

	tests := []struct {
		name       string
		data       []byte
		fromAmount float64 // token A spent  (actual, for input-limited)
		toAmount   float64 // token B receiv (actual, for output-limited)
		fromToken  string
		toToken    string
		wantUtil   float64 // exact expected Utilization (sentinel or ratio)
		wantLimit  float64 // expected LimitAmount — must be populated even on the sentinel path
		wantActual float64 // expected ActualAmount
		wantType   string
	}{
		// ── INPUT-limited: ratio = actual / limit ───────────────────────────────────────────
		// Only the ceiling rejects. A tiny ratio is a victim that set a very loose bound, which is
		// a fact about the victim and stays a measurement; a ratio above 1 is a decode that
		// contradicts the swap, which is not.
		{"input/exactly_min_is_zero", buy(1_000_000_000), 0, 100, wsol, testTok,
			SlippageOutOfRange, 1.0, 0, LimitTypeInput}, // actual==0: nothing was spent
		{"input/tiny_ratio_is_a_measurement", buy(1_000_000_000), 0.009999, 100, wsol, testTok,
			0.009999, 1.0, 0.009999, LimitTypeInput},
		{"input/mid_band", buy(1_000_000_000), 0.5, 100, wsol, testTok,
			0.5, 1.0, 0.5, LimitTypeInput},
		{"input/former_ceiling_is_now_admissible", buy(1_000_000_000), 0.99, 100, wsol, testTok,
			0.99, 1.0, 0.99, LimitTypeInput},
		{"input/just_under_one", buy(1_000_000_000), 0.990001, 100, wsol, testTok,
			0.990001, 1.0, 0.990001, LimitTypeInput},
		{"input/exactly_one", buy(1_000_000_000), 1.0, 100, wsol, testTok,
			SlippageMaxAdmissible, 1.0, 1.0, LimitTypeInput},
		// A fill AT the limit reaches the ratio path a few ULP over 1.0, because the limit is one
		// float64(u64)/Pow10 and the realized amount is a sum of balance deltas. It must clamp to
		// the ceiling, not be discarded as a decode failure.
		{"input/one_ulp_over_clamps", buy(1_000_000_000), 1.0000000000000002, 100, wsol, testTok,
			SlippageMaxAdmissible, 1.0, 1.0000000000000002, LimitTypeInput},
		{"input/inside_ceiling_slack_clamps", buy(1_000_000_000), 1.0000000005, 100, wsol, testTok,
			SlippageMaxAdmissible, 1.0, 1.0000000005, LimitTypeInput},
		// Past the slack the victim would have paid more than its own ceiling and the program would
		// have reverted, so the decoded limit is not the one that bound this swap.
		{"input/just_above_one", buy(1_000_000_000), 1.000001, 100, wsol, testTok,
			SlippageOutOfRange, 1.0, 1.000001, LimitTypeInput},
		{"input/former_clamp_band", buy(1_000_000_000), 1.03, 100, wsol, testTok,
			SlippageOutOfRange, 1.0, 1.03, LimitTypeInput},
		// Formerly SlippageUnsupported (-2). Must be OutOfRange (-5): a decoded-but-inconsistent
		// limit is not the same fact as "this DEX has no decoder".
		{"input/gross_over_limit", buy(5_000), 0.000478, 73.8, wsol, testTok,
			SlippageOutOfRange, 0.000005, 0.000478, LimitTypeInput},
		// pump.fun `buy` sets NoProtection=false even at limit 0, so this reaches the ratio path.
		// A max-cost ceiling of zero is a bound the swap must have violated, not a loose one.
		{"input/zero_limit", buy(0), 0.5, 100, wsol, testTok,
			SlippageOutOfRange, 0, 0.5, LimitTypeInput},
		{"input/zero_actual", buy(1_000_000_000), 0, 100, wsol, testTok,
			SlippageOutOfRange, 1.0, 0, LimitTypeInput},
		{"input/negative_actual", buy(1_000_000_000), -0.5, 100, wsol, testTok,
			SlippageOutOfRange, 1.0, -0.5, LimitTypeInput},

		// ── OUTPUT-limited: ratio = limit / actual ──────────────────────────────────────────
		{"output/tiny_ratio_is_a_measurement", sell(9_999_000), 5, 1.0, testTok, wsol,
			0.009999, 0.009999, 1.0, LimitTypeOutput},
		{"output/mid_band", sell(500_000_000), 5, 1.0, testTok, wsol,
			0.5, 0.5, 1.0, LimitTypeOutput},
		{"output/former_ceiling_is_now_admissible", sell(990_000_000), 5, 1.0, testTok, wsol,
			0.99, 0.99, 1.0, LimitTypeOutput},
		{"output/just_under_one", sell(990_001_000), 5, 1.0, testTok, wsol,
			0.990001, 0.990001, 1.0, LimitTypeOutput},
		{"output/exactly_one", sell(1_000_000_000), 5, 1.0, testTok, wsol,
			SlippageMaxAdmissible, 1.0, 1.0, LimitTypeOutput},
		{"output/above_one", sell(1_100_000_000), 5, 1.0, testTok, wsol,
			SlippageOutOfRange, 1.1, 1.0, LimitTypeOutput},
		// actual==0 with a nonzero limit: the divide guard. Must be a sentinel, never 0/0 = NaN.
		{"output/zero_actual", sell(500_000_000), 5, 0, testTok, wsol,
			SlippageOutOfRange, 0.5, 0, LimitTypeOutput},
		// limit==0 on `sell` is decoded as NoProtection before the ratio is formed. It now yields a
		// consumption of 0 — the victim demanded nothing back and consumed none of a tolerance it
		// never set — and the realized amount must be carried, because the Python pipeline
		// recomputes from the two stored columns and 0/0 there is a decode failure, not a zero.
		{"output/zero_limit_is_zero_consumption", sell(0), 5, 1.0, testTok, wsol,
			0, 0, 1.0, LimitTypeOutput},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := ComputeVictimSlippage([]DexInstructionRef{{PumpFunProgram, tt.data}}, testDecimals,
				tt.fromAmount, tt.toAmount, tt.fromToken, tt.toToken)
			if r == nil {
				t.Fatalf("ComputeVictimSlippage returned nil")
			}
			if r.Utilization != tt.wantUtil {
				t.Errorf("Utilization = %v, want %v", r.Utilization, tt.wantUtil)
			}
			if r.LimitType != tt.wantType {
				t.Errorf("LimitType = %q, want %q", r.LimitType, tt.wantType)
			}
			if r.LimitAmount != tt.wantLimit {
				t.Errorf("LimitAmount = %v, want %v", r.LimitAmount, tt.wantLimit)
			}
			if r.ActualAmount != tt.wantActual {
				t.Errorf("ActualAmount = %v, want %v", r.ActualAmount, tt.wantActual)
			}
		})
	}
}

// TestSlippageSentinelsDistinct pins that the new anomaly code does not collide with the four it
// must leave alone. The Python pipeline discriminates anomaly causes by these exact values.
func TestSlippageSentinelsDistinct(t *testing.T) {
	seen := map[float64]string{}
	for name, v := range map[string]float64{
		"NoProtection": SlippageNoProtection,
		"Unsupported":  SlippageUnsupported,
		"MissingInner": SlippageMissingInner,
		"Ambiguous":    SlippageAmbiguous,
		"OutOfRange":   SlippageOutOfRange,
	} {
		if prev, dup := seen[v]; dup {
			t.Fatalf("sentinel collision: %s and %s both = %v", prev, name, v)
		}
		seen[v] = name
		if v >= 0 {
			t.Fatalf("sentinel %s = %v must be negative to stay out of the admissible band", name, v)
		}
	}
	if SlippageNoProtection != -1 || SlippageUnsupported != -2 || SlippageMissingInner != -3 || SlippageAmbiguous != -4 {
		t.Fatal("the -1/-2/-3/-4 sentinels must keep their pre-alignment values")
	}
	// SlippageNoProtection is legacy: rows written before 2026-08 carry it, and
	// computeMaxSlippageUtilization still reads it as the 0 it stood for, but nothing may emit it.
	if SlippageMinAdmissible != 0 || SlippageMaxAdmissible != 1 {
		t.Fatalf("admissible band is [%v, %v], want [0, 1] — a victim with no real bound must be a "+
			"measurement of 0, and only a ratio above 1 may be rejected",
			SlippageMinAdmissible, SlippageMaxAdmissible)
	}
}

// TestSlippageAmbiguousUnchanged: a multi-swap tx is still Ambiguous, not OutOfRange — the band
// check must not swallow the "can't attribute a limit to this pool" case.
//
// The empty LimitType is load-bearing and is asserted here, because it is the ONLY channel by which
// the Python pipeline learns this leg is unmeasurable: that pipeline deliberately does not read
// slippageUtilization, and recomputes from the two amount columns. Populating LimitType here "for
// consistency" would store ('output', 0, 0), which Python now reads as a legitimate consumption of
// 0 — and every ambiguous sandwich would silently stop being voided.
func TestSlippageAmbiguousUnchanged(t *testing.T) {
	a := buildAnchorInstruction(pumpFunBuyDiscriminator, 1, 1_000_000_000)
	b := buildAnchorInstruction(pumpFunSellDiscriminator, 1, 500_000_000)
	r := ComputeVictimSlippage([]DexInstructionRef{{PumpFunProgram, a}, {PumpFunProgram, b}},
		testDecimals, 0.5, 1.0, wsol, testTok)
	if r == nil || r.Utilization != SlippageAmbiguous {
		t.Fatalf("multi-swap tx should be Ambiguous, got %+v", r)
	}
	if r.LimitType != "" {
		t.Fatalf("ambiguous legs must store an empty LimitType, got %q — the Python side reads "+
			"('output', 0, 0) as a consumption of 0, not as an anomaly", r.LimitType)
	}
}

// TestSlippageUndecodableIsNil: no decodable instruction yields nil, which the sol-package caller
// turns into Unsupported/MissingInner with an EMPTY limit type. That nil path is the Go analogue of
// the Python pipeline's "empty limit type" anomaly; there is no way to reach ComputeVictimSlippage
// with a decoded-but-empty LimitType, since every decoder sets input or output.
func TestSlippageUndecodableIsNil(t *testing.T) {
	if r := ComputeVictimSlippage([]DexInstructionRef{{"someUnknownProgram", []byte{1, 2, 3}}},
		testDecimals, 0.5, 1.0, wsol, testTok); r != nil {
		t.Fatalf("undecodable instruction should yield nil, got %+v", r)
	}
}
