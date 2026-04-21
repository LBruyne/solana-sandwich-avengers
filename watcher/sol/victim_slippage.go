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
// all victim txs in a sandwich.
//
// Returns:
//   - -2 if any victim is Unsupported(-2) or MissingInner(-3): data unavailable
//   - -1 if ALL victims are NoProtection(-1): consumption concept not applicable
//   - max of [0,1] values otherwise (victims with protection are the binding constraint)
func computeMaxSlippageUtilization(victims []*types.SandwichTx) float64 {
	maxUtil := -1.0
	for _, v := range victims {
		if v == nil {
			continue
		}
		if v.SlippageUtilization == dex.SlippageUnsupported || v.SlippageUtilization == dex.SlippageMissingInner {
			return dex.SlippageUnsupported
		}
		if v.SlippageUtilization > maxUtil {
			maxUtil = v.SlippageUtilization
		}
	}
	return maxUtil
}
