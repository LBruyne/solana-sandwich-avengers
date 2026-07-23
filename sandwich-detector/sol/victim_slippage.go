package sol

import (
	"sandwich-detector/sol/dex"
	"sandwich-detector/types"
)

// fillVictimSlippage computes slippage utilization for a victim SandwichTx.
// Used by the unified SandwichFinder for victim legs.
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
	// The DEX is recorded once per sandwich in PoolDex (set in makeSandwichTx), not here.
}

// computeMaxSlippageUtilization summarizes a sandwich by the slippage utilization of its
// tightest measurable victim.
//
//   - If any victim has a real utilization in [0,1], return the maximum — that victim is the
//     binding constraint and shows how far the attacker pushed the price.
//   - Otherwise every victim is unmeasured, so return the reason, kept distinct (v2 no longer
//     collapses MissingInner into Unsupported): MissingInner(-3) > Ambiguous(-4) >
//     Unsupported(-2) > NoProtection(-1).
func computeMaxSlippageUtilization(victims []*types.SandwichTx) float64 {
	maxReal := -1.0
	sawReal := false
	var sawMissingInner, sawAmbiguous, sawUnsupported, sawNoProtection bool
	for _, v := range victims {
		if v == nil {
			continue
		}
		switch u := v.SlippageUtilization; {
		case u >= 0:
			sawReal = true
			if u > maxReal {
				maxReal = u
			}
		case u == dex.SlippageMissingInner:
			sawMissingInner = true
		case u == dex.SlippageAmbiguous:
			sawAmbiguous = true
		case u == dex.SlippageUnsupported:
			sawUnsupported = true
		default: // SlippageNoProtection
			sawNoProtection = true
		}
	}
	switch {
	case sawReal:
		return maxReal
	case sawMissingInner:
		return dex.SlippageMissingInner
	case sawAmbiguous:
		return dex.SlippageAmbiguous
	case sawUnsupported:
		return dex.SlippageUnsupported
	case sawNoProtection:
		return dex.SlippageNoProtection
	default:
		return dex.SlippageNoProtection
	}
}
