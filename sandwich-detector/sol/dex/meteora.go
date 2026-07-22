package dex

// Meteora DAMM v2 and DLMM share the same Anchor swap discriminator (sha256("global:swap")[:8])
var meteoraSwapDiscriminator = [8]byte{248, 198, 158, 145, 225, 117, 135, 200} // 0xf8c69e91e17587c8

// Meteora swap2 discriminator shared by DAMM v2, DBC, DLMM (sha256("global:swap2")[:8])
var meteoraSwap2Discriminator = [8]byte{65, 75, 63, 76, 235, 91, 91, 136}

// decodeSwap2WithMode decodes a Meteora DAMM v2 / DBC swap2 instruction
// that includes a swap_mode byte.
//
// Layout (25 bytes):
//
//	[0:8]   discriminator
//	[8:16]  amount (u64)
//	[16:24] other_amount_threshold (u64)
//	[24]    swap_mode (u8)
//
// SwapMode enum (from Meteora IDL):
//
//	0 = ExactIn:    amount=input,  threshold=min_output → OUTPUT-limited
//	1 = PartialFill: same semantics as ExactIn           → OUTPUT-limited
//	2 = ExactOut:   amount=output, threshold=max_input  → INPUT-limited
//
// Note: DLMM swap2 does NOT have swap_mode; it uses a different struct.
// This function is only used by DAMM v2 and DBC.
func decodeSwap2WithMode(data []byte, dexName string) *SlippageInfo {
	if !matchDiscriminator(data, meteoraSwap2Discriminator) {
		return nil
	}
	limit, ok := readU64LE(data, 16)
	if !ok {
		return nil
	}
	// swap_mode byte at offset 24: only mode=2 (ExactOut) is INPUT-limited
	if len(data) >= 25 && data[24] == 2 {
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      dexName,
		}
	}
	// mode=0 (ExactIn) or mode=1 (PartialFill): threshold is min output
	return &SlippageInfo{
		LimitType:    LimitTypeOutput,
		LimitAmount:  limit,
		NoProtection: limit == 0,
		DexName:      dexName,
	}
}

// decodeMeteoraDAMMv2 decodes Meteora DAMM v2 swap/swap2 instructions.
//
// swap:  [disc 8][amount_in u64][min_amount_out u64] (24 bytes) - always OUTPUT-limited
// swap2: [disc 8][amount u64][threshold u64][swap_mode u8] (25 bytes) - mode-dependent
func decodeMeteoraDAMMv2(data []byte) *SlippageInfo {
	if matchDiscriminator(data, meteoraSwapDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "meteora_damm_v2",
		}
	}
	return decodeSwap2WithMode(data, "meteora_damm_v2")
}

// decodeMeteoraDBC decodes Meteora Dynamic Bonding Curve swap/swap2 instructions.
//
// swap:  same disc and layout as DAMM v2 (24 bytes) - always OUTPUT-limited
// swap2: [disc 8][amount u64][threshold u64][swap_mode u8] (25 bytes) - mode-dependent
func decodeMeteoraDBC(data []byte) *SlippageInfo {
	if matchDiscriminator(data, meteoraSwapDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "meteora_dbc",
		}
	}
	return decodeSwap2WithMode(data, "meteora_dbc")
}

// Meteora DLMM additional discriminators
var (
	meteoraDLMMSwapExactOut  = [8]byte{250, 73, 101, 33, 38, 207, 75, 184}   // sha256("global:swap_exact_out")[:8]
	meteoraDLMMSwapExactOut2 = [8]byte{43, 215, 247, 132, 137, 60, 243, 81}   // sha256("global:swap_exact_out2")[:8]
	meteoraDLMMSwapPriceImp  = [8]byte{56, 173, 230, 208, 173, 228, 156, 205} // sha256("global:swap_with_price_impact")[:8]
	meteoraDLMMSwapPriceImp2 = [8]byte{74, 98, 192, 214, 177, 51, 75, 51}     // sha256("global:swap_with_price_impact2")[:8]
)

// decodeMeteoraDLMM decodes Meteora DLMM swap instructions.
//
// All DLMM swap variants (no swap_mode byte — direction is implicit in instruction name):
//
// ExactIn (OUTPUT-limited):
//   swap:   [disc 8][amount_in u64][min_amount_out u64]
//   swap2:  [disc 8][amount_in u64][min_amount_out u64][remaining_accounts_info...]
//
// ExactOut (INPUT-limited):
//   swap_exact_out:  [disc 8][max_in_amount u64][out_amount u64]
//   swap_exact_out2: [disc 8][max_in_amount u64][out_amount u64][remaining_accounts_info...]
//
// Price impact (uses BPS, not amount — cannot compute utilization):
//   swap_with_price_impact:  [disc 8][amount_in u64][active_id Option<i32>][max_price_impact_bps u16]
//   swap_with_price_impact2: same + remaining_accounts_info
//
// Note: DLMM swap2 does NOT have a swap_mode byte like DAMM v2.
// The "2" suffix means Token-2022 transfer hook support, not a different swap mode.
func decodeMeteoraDLMM(data []byte) *SlippageInfo {
	// ExactIn: swap / swap2
	if matchDiscriminator(data, meteoraSwapDiscriminator) || matchDiscriminator(data, meteoraSwap2Discriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "meteora_dlmm",
		}
	}
	// ExactOut: swap_exact_out / swap_exact_out2
	if matchDiscriminator(data, meteoraDLMMSwapExactOut) || matchDiscriminator(data, meteoraDLMMSwapExactOut2) {
		limit, ok := readU64LE(data, 8) // max_in_amount is the first u64
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "meteora_dlmm",
		}
	}
	// swap_with_price_impact / swap_with_price_impact2: uses BPS, not decodable to amount-based utilization
	// Return nil → will be marked as SlippageUnsupported (-2) by caller
	return nil
}
