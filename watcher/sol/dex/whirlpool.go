package dex

// Orca Whirlpool Anchor discriminators
// swap and swap_v2 share the same discriminators as Raydium CLMM (sha256("global:swap/swap_v2")[:8])
// Layout (42 bytes):
//   [0:8]   discriminator
//   [8:16]  amount (u64)
//   [16:24] other_amount_threshold (u64) - slippage limit
//   [24:40] sqrt_price_limit (u128)
//   [40]    amount_specified_is_input (bool)
//   [41]    a_to_b (bool)
var (
	whirlpoolSwap          = [8]byte{248, 198, 158, 145, 225, 117, 135, 200} // sha256("global:swap")[:8]
	whirlpoolSwapV2        = [8]byte{43, 4, 237, 11, 26, 201, 30, 98}       // sha256("global:swap_v2")[:8]
	whirlpoolTwoHopSwap    = [8]byte{195, 96, 237, 108, 68, 162, 219, 230}   // sha256("global:two_hop_swap")[:8]
	whirlpoolTwoHopSwapV2  = [8]byte{186, 143, 209, 29, 254, 2, 194, 117}    // sha256("global:two_hop_swap_v2")[:8]
)

// Orca Token Swap V1/V2 instruction tag
const orcaTokenSwapTag = byte(0x01)

// decodeOrcaTokenSwap decodes Orca Token Swap V1/V2 swap instructions.
//
// Non-Anchor format (same as SPL Token Swap), layout (17 bytes):
//
//	[0]    instruction tag (0x01 = swap)
//	[1:9]  amount_in (u64)
//	[9:17] minimum_amount_out (u64) - OUTPUT-limited
func decodeOrcaTokenSwap(data []byte) *SlippageInfo {
	if len(data) < 17 || data[0] != orcaTokenSwapTag {
		return nil
	}
	limit, ok := readU64LE(data, 9)
	if !ok {
		return nil
	}
	return &SlippageInfo{
		LimitType:    LimitTypeOutput,
		LimitAmount:  limit,
		NoProtection: limit == 0,
		DexName:      "orca_token_swap",
	}
}

// decodeWhirlpool decodes Orca Whirlpool swap/swap_v2/two_hop_swap/two_hop_swap_v2 instructions.
func decodeWhirlpool(data []byte) *SlippageInfo {
	// swap/swap_v2: [disc 8][amount u64][threshold u64][sqrt_price u128][is_input bool][a_to_b bool]
	if matchDiscriminator(data, whirlpoolSwap) || matchDiscriminator(data, whirlpoolSwapV2) {
		if len(data) < 41 {
			return nil
		}
		threshold, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return whirlpoolSlippageInfo(threshold, data[40] != 0)
	}
	// two_hop_swap/two_hop_swap_v2: [disc 8][amount u64][threshold u64][is_input bool][a_to_b_one bool][a_to_b_two bool][...]
	if matchDiscriminator(data, whirlpoolTwoHopSwap) || matchDiscriminator(data, whirlpoolTwoHopSwapV2) {
		if len(data) < 27 {
			return nil
		}
		threshold, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return whirlpoolSlippageInfo(threshold, data[24] != 0)
	}
	return nil
}

func whirlpoolSlippageInfo(threshold uint64, isInput bool) *SlippageInfo {
	if isInput {
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  threshold,
			NoProtection: threshold == 0,
			DexName:      "whirlpool",
		}
	}
	return &SlippageInfo{
		LimitType:    LimitTypeInput,
		LimitAmount:  threshold,
		NoProtection: false,
		DexName:      "whirlpool",
	}
}
