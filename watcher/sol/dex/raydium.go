package dex

// Raydium V4 uses a non-Anchor format with 1-byte instruction discriminator.
// V2 variants have the same data layout, only accounts differ (no Serum/OpenBook).
const (
	raydiumV4SwapBaseIn    = byte(0x09) // swap_base_in
	raydiumV4SwapBaseOut   = byte(0x0B) // swap_base_out (tag=11)
	raydiumV4SwapBaseInV2  = byte(0x10) // swap_base_in_v2 (tag=16), same layout as swap_base_in
	raydiumV4SwapBaseOutV2 = byte(0x11) // swap_base_out_v2 (tag=17), same layout as swap_base_out
)

// Raydium CPMM / CP Swap Anchor discriminators (sha256("global:{method}")[:8])
var (
	raydiumCPMMSwapBaseInput  = [8]byte{143, 190, 90, 218, 196, 30, 51, 222} // sha256("global:swap_base_input")[:8]
	raydiumCPMMSwapBaseOutput = [8]byte{55, 217, 98, 86, 163, 74, 180, 173}  // sha256("global:swap_base_output")[:8]
)

// Raydium CLMM Anchor discriminators
var (
	raydiumCLMMSwap   = [8]byte{248, 198, 158, 145, 225, 117, 135, 200} // sha256("global:swap")[:8]
	raydiumCLMMSwapV2 = [8]byte{43, 4, 237, 11, 26, 201, 30, 98}       // sha256("global:swap_v2")[:8]
)

// decodeRaydiumV4 decodes Raydium Liquidity Pool V4 swap instructions.
//
// Non-Anchor format (17 bytes):
//
// swap_base_in  (tag=0x09): [tag][amount_in u64][minimum_amount_out u64]
//   → limit = minimum_amount_out at offset 9 (OUTPUT-limited)
//
// swap_base_out (tag=0x0B): [tag][max_amount_in u64][amount_out u64]
//   → limit = max_amount_in at offset 1 (INPUT-limited)
func decodeRaydiumV4(data []byte) *SlippageInfo {
	if len(data) < 17 {
		return nil
	}
	switch data[0] {
	case raydiumV4SwapBaseIn, raydiumV4SwapBaseInV2:
		limit, ok := readU64LE(data, 9) // minimum_amount_out
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "raydium_v4",
		}
	case raydiumV4SwapBaseOut, raydiumV4SwapBaseOutV2:
		limit, ok := readU64LE(data, 1) // max_amount_in
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "raydium_v4",
		}
	}
	return nil
}

// decodeRaydiumCLMM decodes Raydium Concentrated Liquidity swap/swap_v2 instructions.
//
// Anchor format, layout (41 bytes):
//
//	[0:8]   discriminator (swap or swap_v2)
//	[8:16]  amount (u64) - input if is_base_input=true, output if false
//	[16:24] other_amount_threshold (u64) - min output or max input
//	[24:40] sqrt_price_limit_x64 (u128) - price boundary (ignored for slippage)
//	[40]    is_base_input (bool)
//
// is_base_input=true:  amount=exact input,  threshold=min output (OUTPUT-limited)
// is_base_input=false: amount=exact output, threshold=max input  (INPUT-limited)
func decodeRaydiumCLMM(data []byte) *SlippageInfo {
	if !matchDiscriminator(data, raydiumCLMMSwap) && !matchDiscriminator(data, raydiumCLMMSwapV2) {
		return nil
	}
	if len(data) < 41 {
		return nil
	}
	threshold, ok := readU64LE(data, 16)
	if !ok {
		return nil
	}
	isBaseInput := data[40] != 0
	if isBaseInput {
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  threshold,
			NoProtection: threshold == 0,
			DexName:      "raydium_clmm",
		}
	}
	return &SlippageInfo{
		LimitType:    LimitTypeInput,
		LimitAmount:  threshold,
		NoProtection: false,
		DexName:      "raydium_clmm",
	}
}

// decodeRaydiumCPMM decodes Raydium CPMM swap instructions.
//
// Anchor format (24 bytes):
//
// swap_base_input:  [disc 8][amount_in u64][minimum_amount_out u64]
//   → limit = minimum_amount_out at offset 16 (OUTPUT-limited)
//
// swap_base_output: [disc 8][max_amount_in u64][amount_out u64]
//   → limit = max_amount_in at offset 8 (INPUT-limited)
func decodeRaydiumCPMM(data []byte) *SlippageInfo {
	if matchDiscriminator(data, raydiumCPMMSwapBaseInput) {
		limit, ok := readU64LE(data, 16) // minimum_amount_out
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "raydium_cpmm",
		}
	}
	if matchDiscriminator(data, raydiumCPMMSwapBaseOutput) {
		limit, ok := readU64LE(data, 8) // max_amount_in
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "raydium_cpmm",
		}
	}
	return nil
}
