package sol

import (
	"watcher/sol/dex"
	"watcher/types"
)

// fillVictimSlippage computes slippage utilization for a victim SandwichTx.
// Used by both InBlockSandwichFinder and CrossBlockSandwichFinder.
func fillVictimSlippage(stx *types.SandwichTx, orig *types.Transaction, entry PoolEntry) {
	if len(orig.DexInstructions) == 0 {
		if orig.InnerInstructionsNil {
			stx.SlippageUtilization = dex.SlippageMissingInner
		} else {
			stx.SlippageUtilization = dex.SlippageUnsupported
		}
		return
	}
	refs := make([]dex.DexInstructionRef, len(orig.DexInstructions))
	for i, inst := range orig.DexInstructions {
		refs[i] = dex.DexInstructionRef{ProgramID: inst.ProgramID, Data: inst.Data}
	}
	result := dex.ComputeVictimSlippage(
		refs,
		orig.TokenDecimals,
		stx.FromAmount, stx.ToAmount,
		stx.FromToken, stx.ToToken,
	)
	if result == nil {
		// All DexInstructions failed to decode. If innerInstructions was null,
		// the actual swap may be in an unavailable CPI call (e.g. top-level is
		// InitUserVolumeAccumulator, real Buy is via BBRouter in inner instructions).
		if orig.InnerInstructionsNil {
			stx.SlippageUtilization = dex.SlippageMissingInner
		} else {
			stx.SlippageUtilization = dex.SlippageUnsupported
		}
		return
	}
	stx.SlippageLimitType = result.LimitType
	stx.SlippageLimitAmount = result.LimitAmount
	stx.SlippageActualAmount = result.ActualAmount
	stx.SlippageUtilization = result.Utilization
	stx.SlippageDexName = result.DexName
}

// computeMaxSlippageUtilization returns the maximum slippage utilization across
// all victim txs in a sandwich. Only considers normal utilization values (0~1+).
// Special values (-1=NoProtection, -2=Unsupported, -3=MissingInner) are skipped.
// Returns 0 if no valid utilization is found.
func computeMaxSlippageUtilization(victims []*types.SandwichTx) float64 {
	maxUtil := 0.0
	for _, v := range victims {
		if v == nil {
			continue
		}
		// Only consider normal utilization values (≥0)
		if v.SlippageUtilization >= 0 && v.SlippageUtilization > maxUtil {
			maxUtil = v.SlippageUtilization
		}
	}
	return maxUtil
}
