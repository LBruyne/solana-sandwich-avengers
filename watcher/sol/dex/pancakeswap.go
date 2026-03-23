package dex

// PancakeSwap CLMM uses the same Anchor swap/swap_v2 discriminators and layout as Raydium CLMM.
// Layout (41-42 bytes):
//   [0:8]   discriminator (swap or swap_v2)
//   [8:16]  amount (u64)
//   [16:24] other_amount_threshold (u64) - slippage limit
//   [24:40] sqrt_price_limit (u128)
//   [40]    amount_specified_is_input (bool)

// decodePancakeSwap decodes PancakeSwap CLMM swap/swap_v2 instructions.
func decodePancakeSwap(data []byte) *SlippageInfo {
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
	isInput := data[40] != 0
	if isInput {
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  threshold,
			NoProtection: threshold == 0,
			DexName:      "pancakeswap",
		}
	}
	return &SlippageInfo{
		LimitType:    LimitTypeInput,
		LimitAmount:  threshold,
		NoProtection: false,
		DexName:      "pancakeswap",
	}
}
