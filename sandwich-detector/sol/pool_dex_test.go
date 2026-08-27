package sol

import (
	"sandwich-detector/sol/dex"
	"sandwich-detector/types"
	"testing"
)

func TestClassifySandwichDexFromInstructions(t *testing.T) {
	// The front-run's DEX instruction names the exchange, even when the balance-delta "pool" is a
	// shared vault authority whose owner isn't a DEX program (Meteora/Raydium).
	front := &types.Transaction{
		DexInstructions: []types.DexInstruction{{ProgramID: dex.MeteoraDAMMv2Program}},
	}
	if got := classifySandwichDex(front, "someVaultAuthority"); got != "meteora_damm_v2" {
		t.Errorf("front-instruction classify = %q, want meteora_damm_v2", got)
	}

	// PropAMM front (no decoder) is still classified from its labeled_dex instruction.
	propFront := &types.Transaction{
		DexInstructions: []types.DexInstruction{{ProgramID: "SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF"}},
	}
	if got := classifySandwichDex(propFront, ""); got != "solfi" {
		t.Errorf("propamm front classify = %q, want solfi", got)
	}

	// No recognizable DEX instruction -> fall back to the pool account owner.
	saved := accountOwnerCache
	accountOwnerCache = NewAccountOwnerLRU(100)
	defer func() { accountOwnerCache = saved }()
	accountOwnerCache.Put("poolState", dex.WhirlpoolProgram)
	empty := &types.Transaction{}
	if got := classifySandwichDex(empty, "poolState"); got != "whirlpool" {
		t.Errorf("owner fallback classify = %q, want whirlpool", got)
	}
	if got := classifySandwichDex(empty, "unknownPool"); got != "" {
		t.Errorf("unresolved classify = %q, want empty", got)
	}
}

func TestDexNamesFromTx(t *testing.T) {
	// Multi-hop: distinct DEXs in first-seen order, de-duplicated.
	tx := &types.Transaction{DexInstructions: []types.DexInstruction{
		{ProgramID: dex.PumpFunAMMProgram},
		{ProgramID: dex.PumpFunAMMProgram}, // dup (event self-CPI would share program)
		{ProgramID: dex.MeteoraDLMMProgram},
	}}
	got := dexNamesFromTx(tx)
	if len(got) != 2 || got[0] != "pumpfun_amm" || got[1] != "meteora_dlmm" {
		t.Errorf("dexNamesFromTx = %v, want [pumpfun_amm meteora_dlmm]", got)
	}
}

func TestNormalizeDexLabel(t *testing.T) {
	cases := map[string]string{
		"Meteora DAMM v2":  "meteora_damm_v2",
		"Raydium CPMM":     "raydium_cpmm",
		"SolFi V2":         "solfi_v2",
		"  Byreal: CLMM  ": "byreal_clmm",
		"Pump.fun AMM":     "pump_fun_amm",
	}
	for in, want := range cases {
		if got := normalizeDexLabel(in); got != want {
			t.Errorf("normalizeDexLabel(%q) = %q, want %q", in, got, want)
		}
	}
}
