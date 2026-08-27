package sol

import (
	"sandwich-detector/sol/dex"
	"sandwich-detector/types"
)

// fillVictimSlippage computes slippage utilization for a victim SandwichTx.
// Used by the unified SandwichFinder for victim legs.
//
// sandwichedDexProgram is the DEX program of the sandwiched pool (from the front-run's clean swap).
// Slippage is scoped to THAT program's instructions so a multi-hop/aggregator victim's limit is read
// off the sandwiched pool, never off some other pool its route happens to touch. When the sandwiched
// pool's DEX has no decoder (sandwichedDexProgram == ""), the victim's protection on that pool is
// unmeasurable → Unsupported, rather than borrowing a decodable leg from a different pool.
func fillVictimSlippage(stx *types.SandwichTx, orig *types.Transaction, entry PoolEntry, sandwichedDexProgram string) {
	if len(orig.DexInstructions) == 0 {
		if orig.InnerInstructionsNil {
			stx.SlippageUtilization = dex.SlippageMissingInner
		} else {
			stx.SlippageUtilization = dex.SlippageUnsupported
		}
		return
	}
	if sandwichedDexProgram == "" {
		// Sandwiched pool's DEX has no slippage decoder — cannot measure this victim's protection.
		stx.SlippageUtilization = dex.SlippageUnsupported
		return
	}
	refs := make([]dex.DexInstructionRef, 0, len(orig.DexInstructions))
	for _, inst := range orig.DexInstructions {
		if inst.ProgramID != sandwichedDexProgram {
			continue // only the sandwiched pool's DEX; ignore other legs of a multi-hop route
		}
		refs = append(refs, dex.DexInstructionRef{ProgramID: inst.ProgramID, Data: inst.Data})
	}
	if len(refs) == 0 {
		// The sandwiched pool's DEX is decodable in general, but this victim tx carries no visible
		// instruction on it (e.g. the swap is inside an unavailable CPI) — treat as unmeasurable.
		if orig.InnerInstructionsNil {
			stx.SlippageUtilization = dex.SlippageMissingInner
		} else {
			stx.SlippageUtilization = dex.SlippageUnsupported
		}
		return
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

// computeMaxSlippageUtilization summarizes a sandwich by the slippage consumption of its tightest
// victim — but only when EVERY victim is measurable.
//
//   - If any victim is anomalous (a negative sentinel, or a value outside [0, 1]), the whole
//     sandwich is anomalous and carries no value: return the highest-priority sentinel seen. A
//     maximum taken over the measurable subset is only a lower bound on the true maximum, so
//     reporting it would silently understate the sandwich and would be indistinguishable from a
//     sandwich whose victims were all measured.
//   - Otherwise return the maximum over the victims — that victim is the binding constraint and
//     shows how far the attacker pushed the price.
//
// A victim that set no real protection is NOT anomalous — we know exactly what happened to it — but
// it is also not evidence about the attacker, so it is excluded from the maximum rather than
// competing in it at 0. When EVERY victim is in that state the sandwich carries
// SlippageAllUnprotected and is not scored at all. See SlippageProtectionFloor in slippage.go.
//
// SlippageNoProtection(-1) is legacy, written per leg for a zero limit, and is read here as the 0
// it stood for, which now means "below the floor" and is excluded.
//
// Sentinel priority, most to least specific:
// MissingInner(-3) > Ambiguous(-4) > Unsupported(-2) > OutOfRange(-5) > AllUnprotected(-6).
//
// Downstream analysis should recompute from the two raw amount columns on sandwich_txs rather
// than reading sandwiches.maxSlippageUtilization, which may carry an older rule on old rows.
func computeMaxSlippageUtilization(victims []*types.SandwichTx) float64 {
	maxProtected := -1.0
	sawProtected, sawAny := false, false
	var sawMissingInner, sawAmbiguous, sawUnsupported, sawOutOfRange bool
	for _, v := range victims {
		if v == nil {
			continue
		}
		sawAny = true
		u := v.SlippageUtilization
		if u == dex.SlippageNoProtection {
			u = 0 // legacy per-leg sentinel for a zero limit, i.e. a consumption of 0
		}
		switch {
		case u >= dex.SlippageProtectionFloor && u <= dex.SlippageMaxAdmissible:
			sawProtected = true
			if u > maxProtected {
				maxProtected = u
			}
		case u >= dex.SlippageMinAdmissible && u < dex.SlippageProtectionFloor:
			// Measured, and measured to be a victim with no tolerance to consume. Not an anomaly,
			// and not a contribution to the maximum.
		case u == dex.SlippageMissingInner:
			sawMissingInner = true
		case u == dex.SlippageAmbiguous:
			sawAmbiguous = true
		case u == dex.SlippageUnsupported:
			sawUnsupported = true
		default:
			// SlippageOutOfRange, plus any non-sentinel value outside [0, 1] (which
			// ComputeVictimSlippage no longer produces, but a stale/hand-built SandwichTx could).
			sawOutOfRange = true
		}
	}
	switch {
	case sawMissingInner:
		return dex.SlippageMissingInner
	case sawAmbiguous:
		return dex.SlippageAmbiguous
	case sawUnsupported:
		return dex.SlippageUnsupported
	case sawOutOfRange:
		return dex.SlippageOutOfRange
	case sawProtected:
		return maxProtected
	case sawAny:
		// Every victim measured, none of them protected. Not 0.
		return dex.SlippageAllUnprotected
	default:
		// No victim legs at all. Unreachable from the finder (a sandwich requires >=1 victim).
		return dex.SlippageUnsupported
	}
}
