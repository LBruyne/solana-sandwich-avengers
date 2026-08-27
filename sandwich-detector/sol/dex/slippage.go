package dex

import "encoding/binary"

// Program IDs for supported DEXes (values from programs.yaml)
const (
	PumpFunProgram       = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
	PumpFunAMMProgram    = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
	RaydiumV4Program     = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
	RaydiumCPMMProgram   = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
	MeteoraDBCProgram    = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
	RaydiumCLMMProgram   = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
	MeteoraDAMMv2Program = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
	MeteoraDLMMProgram   = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
	WhirlpoolProgram     = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
	OrcaTokenSwapV1      = "DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1"
	OrcaTokenSwapV2      = "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP"
	PancakeSwapProgram   = "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq"
)

// Slippage analysis constants
const (
	SlippageNoProtection   = float64(-1) // LEGACY: rows written before consumption 0 replaced this sentinel
	SlippageUnsupported    = float64(-2) // DEX has no decoder, so the limit cannot be read
	SlippageMissingInner   = float64(-3) // swap is in innerInstructions but the RPC returned null (data unavailable)
	SlippageAmbiguous      = float64(-4) // multiple swap instructions in one tx — can't match a limit to this pool
	SlippageOutOfRange     = float64(-5) // decoded limit contradicts the realized swap (ratio > 1, or non-finite)
	SlippageAllUnprotected = float64(-6) // SANDWICH level: every victim measured, none set a real bound
	LimitTypeInput         = "input"     // max cost limit (e.g., max_sol_cost)
	LimitTypeOutput        = "output"    // minimum output limit (e.g., minimum_amount_out)

	// The admissible band for a consumption ratio.
	//
	//   0 is admissible and is a measurement: a victim whose pool-level limit is nominal (an
	//     aggregator's 1-raw-unit min_out, or a literal 0) consumed none of a tolerance it never
	//     set. The leg carries consumption 0, competes in the sandwich's maximum, and voids
	//     nothing.
	//
	//   Above 1 is not admissible: the victim would have received less than the minimum it
	//     demanded and the program would have reverted, so the decoded limit is not the one that
	//     bound this swap. That leg is SlippageOutOfRange and the sandwich becomes unscoreable.
	//
	// These bounds are the Go-side statement of the rule the Python pipeline applies when it
	// recomputes consumption from sandwich_txs.slippageLimitAmount / slippageActualAmount; keep
	// the two in step. They live here rather than in the per-DEX decoders, which only read a raw
	// limit out of instruction data and never form a ratio.
	SlippageMinAdmissible = 0.0
	SlippageMaxAdmissible = 1.0

	// SlippageProtectionFloor is NOT the bottom of the admissible band, which is still [0, 1].
	//
	// A sandwich's score is the maximum over its victims AT OR ABOVE this floor
	// (computeMaxSlippageUtilization), so when no victim reaches it the sandwich is not scored
	// rather than scored near 0, and mean_SC is a conditional mean, E[SC | SC >= floor].
	//
	// "Below the floor" covers both "the victim set no bound" and "the bound is in an outer
	// aggregator route, one level up from where fillVictimSlippage decodes".
	//
	// Must equal SC_PROTECTION_FLOOR in
	// sandwich-intent/1_signer_data_preparation_and_summary.py.
	SlippageProtectionFloor = 0.01

	// Relative slack on the ceiling, then a clamp. The limit is one float64(uint64)/Pow10 and the
	// realized amount is a SUM of balance deltas, so a victim filled at exactly its stated
	// minimum can exceed 1.0 by a few ULP. Without the clamp the stored value would be a ratio
	// above 1, which every downstream reader is entitled to treat as impossible.
	SlippageCeilingTolerance = 1e-9

	AnchorDiscriminatorLen = 8 // Anchor programs use 8-byte discriminators
)

// SlippageInfo holds decoded slippage parameters from a DEX swap instruction.
type SlippageInfo struct {
	LimitType    string // "input" or "output"
	LimitAmount  uint64 // raw limit value in smallest unit (lamports, etc.)
	NoProtection bool   // set by the OUTPUT-limited decoders when min_out == 0; input decoders never set it
	DexName      string // human-readable DEX identifier
}

// ExtractSlippage decodes slippage parameters from instruction data for a known DEX program.
// Returns nil if the program is not supported or the instruction cannot be decoded.
func ExtractSlippage(programID string, data []byte) *SlippageInfo {
	switch programID {
	case PumpFunProgram:
		return decodePumpFun(data)
	case PumpFunAMMProgram:
		return decodePumpFunAMM(data)
	case RaydiumV4Program:
		return decodeRaydiumV4(data)
	case RaydiumCPMMProgram:
		return decodeRaydiumCPMM(data)
	case MeteoraDBCProgram:
		return decodeMeteoraDBC(data)
	case RaydiumCLMMProgram:
		return decodeRaydiumCLMM(data)
	case MeteoraDAMMv2Program:
		return decodeMeteoraDAMMv2(data)
	case MeteoraDLMMProgram:
		return decodeMeteoraDLMM(data)
	case WhirlpoolProgram:
		return decodeWhirlpool(data)
	case OrcaTokenSwapV1, OrcaTokenSwapV2:
		return decodeOrcaTokenSwap(data)
	case PancakeSwapProgram:
		return decodePancakeSwap(data)
	default:
		return nil
	}
}

// readU64LE reads a little-endian uint64 from data at the given offset.
// Returns 0, false if there are not enough bytes.
func readU64LE(data []byte, offset int) (uint64, bool) {
	if len(data) < offset+8 {
		return 0, false
	}
	return binary.LittleEndian.Uint64(data[offset : offset+8]), true
}

// matchDiscriminator checks if data starts with the given 8-byte discriminator.
func matchDiscriminator(data []byte, disc [8]byte) bool {
	if len(data) < AnchorDiscriminatorLen {
		return false
	}
	return data[0] == disc[0] && data[1] == disc[1] && data[2] == disc[2] && data[3] == disc[3] &&
		data[4] == disc[4] && data[5] == disc[5] && data[6] == disc[6] && data[7] == disc[7]
}
