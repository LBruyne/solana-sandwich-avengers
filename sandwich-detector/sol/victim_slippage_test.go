package sol

import (
	"testing"

	"sandwich-detector/sol/dex"
	"sandwich-detector/types"
)

func victim(u float64) *types.SandwichTx {
	return &types.SandwichTx{SandwichTxTokenInfo: types.SandwichTxTokenInfo{SlippageUtilization: u}}
}

// TestComputeMaxSlippageUtilization pins the level-2 rule: a sandwich is summarized by the maximum
// over its victims ONLY when every victim is measurable. A victim whose consumption is UNKNOWN
// poisons the whole sandwich, because a maximum taken over the measurable subset is merely a lower
// bound on the true maximum and would be indistinguishable from a fully measured sandwich.
//
// A victim that set no real protection is NOT unknown, but it is also not evidence about the
// attacker: it is excluded from the maximum rather than competing in it at 0, and a sandwich with
// nothing but such victims carries SlippageAllUnprotected instead of a score. The cases tagged
// "was <x>" record what earlier rules returned, so each change of meaning is visible here.
func TestComputeMaxSlippageUtilization(t *testing.T) {
	tests := []struct {
		name    string
		victims []*types.SandwichTx
		want    float64
	}{
		// ── fully measurable: max over the band ─────────────────────────────────────────────
		{"single_real", []*types.SandwichTx{victim(0.42)}, 0.42},
		{"all_real_takes_max", []*types.SandwichTx{victim(0.42), victim(0.87), victim(0.13)}, 0.87},
		{"boundaries_are_admissible", []*types.SandwichTx{victim(0.0), victim(1.0)}, 1.0},

		// ── an unprotected victim is excluded from the max; it does not poison ──────────────
		{"unprotected_excluded_from_max",
			[]*types.SandwichTx{victim(0.42), victim(0.87), victim(0)}, 0.87},
		{"legacy_sentinel_reads_as_unprotected",
			[]*types.SandwichTx{victim(0.42), victim(0.87), victim(dex.SlippageNoProtection)}, 0.87},
		{"just_below_floor_excluded",
			[]*types.SandwichTx{victim(0.42), victim(0.0099)}, 0.42},
		{"exactly_on_floor_counts",
			[]*types.SandwichTx{victim(dex.SlippageProtectionFloor)}, dex.SlippageProtectionFloor},
		// The case the floor exists for: nothing to take a max over, so no score — NOT 0.
		{"all_unprotected_is_not_zero",
			[]*types.SandwichTx{victim(0), victim(dex.SlippageNoProtection)}, dex.SlippageAllUnprotected},
		{"single_unprotected_victim_is_not_zero",
			[]*types.SandwichTx{victim(0.0)}, dex.SlippageAllUnprotected},

		// ── one victim of unknown consumption poisons the sandwich ──────────────────────────
		{"one_unsupported_poisons", // was 0.87
			[]*types.SandwichTx{victim(0.42), victim(0.87), victim(dex.SlippageUnsupported)},
			dex.SlippageUnsupported},
		{"one_missing_inner_poisons", // was 0.87
			[]*types.SandwichTx{victim(0.87), victim(dex.SlippageMissingInner)},
			dex.SlippageMissingInner},
		{"one_ambiguous_poisons", // was 0.87
			[]*types.SandwichTx{victim(0.87), victim(dex.SlippageAmbiguous)},
			dex.SlippageAmbiguous},
		{"one_out_of_range_poisons", // was 0.87
			[]*types.SandwichTx{victim(0.87), victim(dex.SlippageOutOfRange)},
			dex.SlippageOutOfRange},

		// ── sentinel priority: MissingInner > Ambiguous > Unsupported > OutOfRange ──────────
		{"priority_all_unprotected_is_last",
			[]*types.SandwichTx{victim(0), victim(dex.SlippageOutOfRange)}, dex.SlippageOutOfRange},
		{"priority_missing_inner_over_all",
			[]*types.SandwichTx{victim(0), victim(dex.SlippageOutOfRange),
				victim(dex.SlippageUnsupported), victim(dex.SlippageAmbiguous), victim(dex.SlippageMissingInner)},
			dex.SlippageMissingInner},
		{"priority_ambiguous_over_unsupported",
			[]*types.SandwichTx{victim(dex.SlippageUnsupported), victim(dex.SlippageAmbiguous)},
			dex.SlippageAmbiguous},
		{"priority_unsupported_over_out_of_range",
			[]*types.SandwichTx{victim(dex.SlippageOutOfRange), victim(dex.SlippageUnsupported)},
			dex.SlippageUnsupported},
		{"out_of_range_outranks_a_measurement",
			[]*types.SandwichTx{victim(0.5), victim(dex.SlippageOutOfRange)},
			dex.SlippageOutOfRange},

		// ── in-band values from any era are measurements ────────────────────────────────────
		// Values like 0.0024 and 1.0 are admissible under [0, 1] and must not be re-rejected on
		// read, whatever a narrower band once did with them.
		{"stale_tiny_value_is_excluded_not_rejected",
			[]*types.SandwichTx{victim(0.5), victim(0.0024)}, 0.5},
		{"stale_exactly_one_is_a_measurement", // was SlippageOutOfRange
			[]*types.SandwichTx{victim(0.5), victim(1.0)}, 1.0},
		{"above_one_is_still_an_anomaly",
			[]*types.SandwichTx{victim(0.5), victim(1.0001)}, dex.SlippageOutOfRange},

		// ── degenerate inputs ───────────────────────────────────────────────────────────────
		{"nil_entries_skipped", []*types.SandwichTx{nil, victim(0.42), nil}, 0.42},
		{"no_victims", nil, dex.SlippageUnsupported},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := computeMaxSlippageUtilization(tt.victims); got != tt.want {
				t.Errorf("computeMaxSlippageUtilization = %v, want %v", got, tt.want)
			}
		})
	}
}

// TestComputeMaxSlippageUtilizationOrderIndependent: the result must not depend on the order the
// victims happen to arrive in, since victim order is a detection artefact.
func TestComputeMaxSlippageUtilizationOrderIndependent(t *testing.T) {
	vs := []*types.SandwichTx{victim(0.42), victim(dex.SlippageUnsupported), victim(0.87),
		victim(dex.SlippageNoProtection), victim(0.13)}
	want := computeMaxSlippageUtilization(vs)
	for i := range vs {
		rotated := append(append([]*types.SandwichTx{}, vs[i:]...), vs[:i]...)
		if got := computeMaxSlippageUtilization(rotated); got != want {
			t.Fatalf("rotation by %d changed result: %v != %v", i, got, want)
		}
	}
	if want != dex.SlippageUnsupported {
		t.Fatalf("expected Unsupported (highest-priority anomaly present), got %v", want)
	}
}
