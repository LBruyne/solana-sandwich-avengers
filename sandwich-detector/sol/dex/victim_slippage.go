package dex

import (
	"math"
)

// VictimSlippageResult holds the computed slippage utilization for a victim transaction.
type VictimSlippageResult struct {
	LimitType    string
	LimitAmount  float64 // limit converted to float64 with decimals
	ActualAmount float64 // actual cost or output from balance deltas
	Utilization  float64 // consumption in [0, 1] (0 = victim set no real bound), or a negative sentinel
	DexName      string
}

// ComputeVictimSlippage extracts slippage info from DEX instructions and computes utilization.
// Parameters:
//   - dexInstructions: all DEX instructions in the transaction
//   - tokenDecimals: token mint -> decimals mapping
//   - fromAmount: victim's token A spending (abs value, for INPUT-limited)
//   - toAmount: victim's token B received (abs value, for OUTPUT-limited)
//   - fromToken: token A mint address (SOL or token address)
//   - toToken: token B mint address
//
// Returns nil if no DEX instruction can be decoded. If multiple decodable instructions exist
// (multi-swap tx where we can't match instruction to pool) it returns a result carrying the
// SlippageAmbiguous sentinel. A successful decode yields a consumption in [0, 1] — including 0 for
// a victim that set no real bound — or SlippageOutOfRange when the ratio exceeds 1 or is not
// finite, meaning the decoded limit did not bind this swap.
func ComputeVictimSlippage(
	dexInstructions []DexInstructionRef,
	tokenDecimals map[string]int,
	fromAmount, toAmount float64,
	fromToken, toToken string,
) *VictimSlippageResult {
	// Count decodable DEX instructions. If there are multiple, we cannot
	// reliably match which instruction corresponds to which pool in the
	// sandwich, so skip slippage computation entirely.
	var decodedCount int
	var firstInfo *SlippageInfo
	for _, inst := range dexInstructions {
		info := ExtractSlippage(inst.ProgramID, inst.Data)
		if info != nil {
			decodedCount++
			if firstInfo == nil {
				firstInfo = info
			}
		}
	}
	if decodedCount == 0 {
		return nil
	}
	if decodedCount > 1 {
		// Multiple swap instructions in one tx — we cannot tell which limit binds this pool.
		// This is "unmeasured", distinct from a victim that genuinely set no protection (-1).
		return &VictimSlippageResult{
			Utilization: SlippageAmbiguous,
		}
	}

	info := firstInfo
	if info.NoProtection {
		// Only the output-limited decoders set this, and only for min_out == 0: the victim demanded
		// nothing back, so it consumed none of a tolerance it never set. Consumption is 0, a real
		// value that competes in the sandwich's maximum. The realized amount is recorded rather than
		// zeroed — the Python pipeline recomputes consumption from the two stored columns, and
		// (0, 0) is what this path wrote before 2026-08.
		//
		// The input arm is defensive. If an input decoder ever starts setting NoProtection on a zero
		// max-cost, the ratio path already rejects that as a bound the swap must have violated
		// (a zero ceiling on a swap that spent something), and so does the Python side; routing it
		// here instead would score it 0 on one side and void it on the other.
		if info.LimitType != LimitTypeOutput {
			return outOfRange(info, 0, fromAmount)
		}
		return &VictimSlippageResult{
			LimitType:    info.LimitType,
			LimitAmount:  0,
			ActualAmount: toAmount,
			Utilization:  0,
			DexName:      info.DexName,
		}
	}

	// Convert raw limit amount to float64 using token decimals, then form the consumption ratio.
	// Direction (identical to the Python pipeline's recomputation from the stored raw amounts):
	//   input-limited  (max cost)   -> actual / limit
	//   output-limited (min output) -> limit / actual
	var limitFloat, actualFloat, utilization float64
	switch info.LimitType {
	case LimitTypeInput:
		// Limit is on input side (max cost in fromToken)
		decimals := getDecimals(tokenDecimals, fromToken)
		limitFloat = float64(info.LimitAmount) / math.Pow10(decimals)
		actualFloat = fromAmount
		if limitFloat <= 0 || actualFloat <= 0 {
			// A max-cost ceiling of zero is not a loose bound — it is a bound the swap must have
			// violated, since the victim spent something. Likewise a swap that spent nothing is not
			// a measurement of anything. Either way the decode disagrees with the realized swap and
			// the victim's true consumption is unknown. Reachable via a decoder that leaves
			// NoProtection false on a zero limit (pump.fun `buy` never sets it).
			return outOfRange(info, limitFloat, actualFloat)
		}
		utilization = actualFloat / limitFloat
	case LimitTypeOutput:
		// Limit is on output side (min output in toToken)
		decimals := getDecimals(tokenDecimals, toToken)
		limitFloat = float64(info.LimitAmount) / math.Pow10(decimals)
		actualFloat = toAmount
		if actualFloat <= 0 {
			return outOfRange(info, limitFloat, actualFloat)
		}
		if limitFloat < 0 {
			return outOfRange(info, limitFloat, actualFloat)
		}
		// limitFloat == 0 is reachable when a decoder leaves NoProtection false on a zero min_out;
		// the ratio is then 0, which is the same measurement the NoProtection path returns.
		utilization = limitFloat / actualFloat
	default:
		return nil
	}

	// Only the ceiling rejects. A ratio above 1 (or a non-finite one) says the decoded limit did not
	// bind this swap, so the victim's consumption is unknown and the sandwich cannot be scored. A
	// ratio at or near 0 is a victim that set no real protection — known, and admitted. The slack on
	// the ceiling covers float rounding on a fill at exactly the limit; see the constant block in
	// slippage.go.
	if math.IsNaN(utilization) || math.IsInf(utilization, 0) ||
		utilization < SlippageMinAdmissible ||
		utilization > SlippageMaxAdmissible*(1+SlippageCeilingTolerance) {
		return outOfRange(info, limitFloat, actualFloat)
	}
	utilization = math.Min(utilization, SlippageMaxAdmissible)

	return &VictimSlippageResult{
		LimitType:    info.LimitType,
		LimitAmount:  limitFloat,
		ActualAmount: actualFloat,
		Utilization:  utilization,
		DexName:      info.DexName,
	}
}

// outOfRange builds the SlippageOutOfRange result. It always carries the decimal-scaled limit and
// actual amounts: the Python pipeline recomputes consumption from exactly those two stored columns,
// so a path that emitted the sentinel while zeroing the amounts would make the two codebases
// disagree about which victims are anomalous.
func outOfRange(info *SlippageInfo, limitFloat, actualFloat float64) *VictimSlippageResult {
	return &VictimSlippageResult{
		LimitType:    info.LimitType,
		LimitAmount:  limitFloat,
		ActualAmount: actualFloat,
		Utilization:  SlippageOutOfRange,
		DexName:      info.DexName,
	}
}

// DexInstructionRef is a lightweight reference to a DEX instruction for slippage computation.
type DexInstructionRef struct {
	ProgramID string
	Data      []byte
}

// getDecimals returns decimals for a token, defaulting to 9 (SOL standard).
func getDecimals(tokenDecimals map[string]int, token string) int {
	if d, ok := tokenDecimals[token]; ok {
		return d
	}
	return 9 // default to SOL decimals
}
