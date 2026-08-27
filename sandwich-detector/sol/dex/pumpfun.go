package dex

// Anchor discriminators for Pump.fun instructions (sha256("global:{method}")[:8])
// Both Pump.fun Bonding Curve and Pump.fun AMM share the same buy/sell discriminators.
var (
	pumpFunBuyDiscriminator           = [8]byte{102, 6, 61, 18, 1, 218, 235, 234}      // buy
	pumpFunSellDiscriminator          = [8]byte{51, 230, 133, 164, 1, 127, 131, 173}   // sell
	pumpFunBuyExactSolInDiscriminator = [8]byte{56, 252, 116, 8, 158, 223, 205, 95}    // buy_exact_sol_in (bonding curve)
	pumpFunAMMBuyExactQuoteIn         = [8]byte{198, 46, 21, 82, 180, 217, 232, 112}   // buy_exact_quote_in (AMM)
	pumpFunAMMSellExactQuoteOut       = [8]byte{152, 146, 222, 158, 98, 137, 248, 152} // sell_exact_quote_out (AMM)
)

// decodePumpFun decodes Pump.fun Bonding Curve instructions.
//
// buy: [disc][amount u64][max_sol_cost u64] → INPUT-limited (limit = max_sol_cost)
// sell: [disc][amount u64][min_sol_output u64] → OUTPUT-limited (limit = min_sol_output)
// buy_exact_sol_in: [disc][spendable_sol_in u64][min_tokens_out u64] → OUTPUT-limited (limit = min_tokens_out)
//
// All layouts share the pattern: [8 disc][8 first_param][8 slippage_limit]
func decodePumpFun(data []byte) *SlippageInfo {
	if matchDiscriminator(data, pumpFunBuyDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "pumpfun",
		}
	}
	if matchDiscriminator(data, pumpFunSellDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "pumpfun",
		}
	}
	if matchDiscriminator(data, pumpFunBuyExactSolInDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "pumpfun",
		}
	}
	return nil
}

// decodePumpFunAMM decodes Pump.fun AMM instructions.
//
// buy: [disc][base_amount_out u64][max_quote_amount_in u64] → INPUT-limited
// sell: [disc][base_amount_in u64][min_quote_amount_out u64] → OUTPUT-limited
// buy_exact_quote_in: [disc][spendable_quote_in u64][min_base_amount_out u64] → OUTPUT-limited
// sell_exact_quote_out: [disc][exact_quote_out u64][max_base_amount_in u64] → INPUT-limited
func decodePumpFunAMM(data []byte) *SlippageInfo {
	if matchDiscriminator(data, pumpFunBuyDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "pumpfun_amm",
		}
	}
	if matchDiscriminator(data, pumpFunSellDiscriminator) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "pumpfun_amm",
		}
	}
	if matchDiscriminator(data, pumpFunAMMBuyExactQuoteIn) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeOutput,
			LimitAmount:  limit,
			NoProtection: limit == 0,
			DexName:      "pumpfun_amm",
		}
	}
	if matchDiscriminator(data, pumpFunAMMSellExactQuoteOut) {
		limit, ok := readU64LE(data, 16)
		if !ok {
			return nil
		}
		return &SlippageInfo{
			LimitType:    LimitTypeInput,
			LimitAmount:  limit,
			NoProtection: false,
			DexName:      "pumpfun_amm",
		}
	}
	return nil
}
