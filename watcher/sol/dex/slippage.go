package dex

import "encoding/binary"

// Program IDs for supported DEXes (values from programs.yaml)
const (
	PumpFunProgram       = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
	PumpFunAMMProgram    = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
	RaydiumV4Program     = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
	RaydiumCPMMProgram   = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
	MeteoraDBCProgram = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
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
	SlippageNoProtection = float64(-1) // utilization value when limit is 0 (no slippage protection)
	SlippageUnsupported  = float64(-2) // utilization value when DEX is not supported
	SlippageMissingInner = float64(-3) // utilization value when DEX instruction is in innerInstructions but RPC returned null
	LimitTypeInput      = "input"     // max cost limit (e.g., max_sol_cost)
	LimitTypeOutput     = "output"    // minimum output limit (e.g., minimum_amount_out)

	AnchorDiscriminatorLen = 8 // Anchor programs use 8-byte discriminators
)

// SlippageInfo holds decoded slippage parameters from a DEX swap instruction.
type SlippageInfo struct {
	LimitType    string // "input" or "output"
	LimitAmount  uint64 // raw limit value in smallest unit (lamports, etc.)
	NoProtection bool   // true if minimum_out == 0 or slippage is extreme
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
