package sol

import (
	"strings"
	"sandwich-detector/sol/dex"
	"sandwich-detector/types"

	"github.com/spf13/viper"
)

// dexNameByProgram maps an AMM/DEX program ID to a normalized short exchange name, used to classify
// the pool a sandwich attacked. Covers every program in programs.yaml's labeled_dex, including
// proprietary AMMs (SolFi/BisonFi/...) that have no slippage decoder — so the pool is still
// classified for distribution analysis even when its slippage can't be read.
var dexNameByProgram = map[string]string{
	dex.PumpFunProgram:       "pumpfun",
	dex.PumpFunAMMProgram:    "pumpfun_amm",
	dex.RaydiumV4Program:     "raydium_v4",
	dex.RaydiumCPMMProgram:   "raydium_cpmm",
	dex.RaydiumCLMMProgram:   "raydium_clmm",
	dex.MeteoraDBCProgram:    "meteora_dbc",
	dex.MeteoraDAMMv2Program: "meteora_damm_v2",
	dex.MeteoraDLMMProgram:   "meteora_dlmm",
	dex.WhirlpoolProgram:     "whirlpool",
	dex.OrcaTokenSwapV1:      "orca",
	dex.OrcaTokenSwapV2:      "orca",
	dex.PancakeSwapProgram:   "pancakeswap",
	// labeled_dex programs without a slippage decoder
	"routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS":  "raydium_route",
	"Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_pools",
	"Dooar9JkhdZ7J3LHN3A7YCuoGRUggXhQaG4kijfLGU2j": "stepn_dooar",
	"REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2":  "byreal",
	// PropAMM (proprietary market makers — captured but slippage not decodable)
	"SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF":  "solfi",
	"BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi": "bisonfi",
	"9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp": "humidifi",
	"fUSioN9YKKSa3CUC2YUc4tPkHJ5Y6XW1yz8y6F7qWz9":  "fusion",
	"TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH":  "tessera",
	"goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE":  "goonfi",
	"ALPHAQmeA7bjrVuccPsYPiCvsi428SNwte66Srvs4pHA": "alphaq",
	"obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y":  "obric",
	"ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY":  "zerofi",
}

// dexNameForProgram maps a program ID to a normalized exchange name, using the explicit table first
// and the labeled_dex label as a fallback so a newly-added DEX is never silently unclassified.
func dexNameForProgram(programID string) string {
	if name, ok := dexNameByProgram[programID]; ok {
		return name
	}
	if label := viper.GetString("labeled_dex." + strings.ToLower(programID)); label != "" {
		return normalizeDexLabel(label)
	}
	return ""
}

// classifySandwichDex determines the exchange a sandwich attacked. The sandwiched pool is identified
// via balance deltas, whose "pool" account is often a shared vault authority (e.g. Meteora/Raydium
// pool authorities) whose owner isn't a DEX program — so a pool-owner lookup misses those. Instead
// we read the DEX program directly from the FRONT-run's instructions: the front is a clean single
// swap on the sandwiched pool, so it names exactly one exchange. Falls back to the pool account's
// owner program when the front carries no recognizable DEX instruction.
func classifySandwichDex(frontTx *types.Transaction, poolAddr string) string {
	if names := dexNamesFromTx(frontTx); len(names) > 0 {
		return names[0]
	}
	return classifyPoolByOwner(poolAddr)
}

// dexNamesFromTx returns the distinct normalized DEX names among a tx's DEX instructions (top-level
// + inner CPI), in first-seen order. A clean single swap yields exactly one.
func dexNamesFromTx(tx *types.Transaction) []string {
	if tx == nil {
		return nil
	}
	seen := make(map[string]struct{})
	var out []string
	for _, di := range tx.DexInstructions {
		name := dexNameForProgram(di.ProgramID)
		if name == "" {
			continue
		}
		if _, dup := seen[name]; dup {
			continue
		}
		seen[name] = struct{}{}
		out = append(out, name)
	}
	return out
}

// classifyPoolByOwner resolves the exchange from the pool account's on-chain owner program. Works
// when the balance-delta "pool" is the AMM's state account (pump.fun / Raydium CLMM / Whirlpool),
// but not when it is a shared vault authority; used only as a fallback to the instruction-based path.
func classifyPoolByOwner(pool string) string {
	if pool == "" {
		return ""
	}
	if name := dexNameForProgram(pool); name != "" {
		return name
	}
	if owner, cached := accountOwnerCache.Get(pool); cached && owner != "" {
		return dexNameForProgram(owner)
	}
	return ""
}

// normalizeDexLabel turns a human labeled_dex value ("Meteora DAMM v2") into a stable token
// ("meteora_damm_v2") for the fallback path.
func normalizeDexLabel(label string) string {
	label = strings.ToLower(strings.TrimSpace(label))
	var b strings.Builder
	prevUnderscore := false
	for _, r := range label {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			prevUnderscore = false
		default:
			if !prevUnderscore && b.Len() > 0 {
				b.WriteByte('_')
				prevUnderscore = true
			}
		}
	}
	return strings.Trim(b.String(), "_")
}
