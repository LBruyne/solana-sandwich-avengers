package dex

import (
	"math"
)

// VictimSlippageResult holds the computed slippage utilization for a victim transaction.
type VictimSlippageResult struct {
	LimitType    string
	LimitAmount  float64 // limit converted to float64 with decimals
	ActualAmount float64 // actual cost or output from balance deltas
	Utilization  float64 // 0-1 ratio, or SlippageNoProtection
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
// Returns nil if no DEX instruction can be decoded, or if multiple decodable
// instructions exist (multi-swap tx where we can't match instruction to pool).
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
		// Multiple swap instructions in one tx — treat as no slippage protection
		return &VictimSlippageResult{
			Utilization: SlippageNoProtection,
		}
	}

	info := firstInfo
	if info.NoProtection {
		return &VictimSlippageResult{
			LimitType:    info.LimitType,
			LimitAmount:  0,
			ActualAmount: 0,
			Utilization:  SlippageNoProtection,
			DexName:      info.DexName,
		}
	}

	// Convert raw limit amount to float64 using token decimals
	var limitFloat, actualFloat, utilization float64
	switch info.LimitType {
	case LimitTypeInput:
		// Limit is on input side (max cost in fromToken)
		decimals := getDecimals(tokenDecimals, fromToken)
		limitFloat = float64(info.LimitAmount) / math.Pow10(decimals)
		actualFloat = fromAmount
		if limitFloat > 0 {
			utilization = actualFloat / limitFloat
		}
	case LimitTypeOutput:
		// Limit is on output side (min output in toToken)
		decimals := getDecimals(tokenDecimals, toToken)
		limitFloat = float64(info.LimitAmount) / math.Pow10(decimals)
		actualFloat = toAmount
		if actualFloat > 0 {
			utilization = limitFloat / actualFloat
		}
	default:
		return nil
	}

	return &VictimSlippageResult{
		LimitType:    info.LimitType,
		LimitAmount:  limitFloat,
		ActualAmount: actualFloat,
		Utilization:  utilization,
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
