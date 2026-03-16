package utils

import (
	MapSet "github.com/deckarep/golang-set/v2"
)

// Units
const (
	SOL_UNIT = 1e9 // 1 SOL = 10^9 lamports

	EPSILON = 1e-4 // Infinite small value for float comparison
)

func HasString(slice []string, str string) bool {
	for _, s := range slice {
		if s == str {
			return true
		}
	}
	return false
}

// SignersOverlap returns true if two signer sets share at least one common element.
// Used for multi-signer comparison in sandwich detection.
func SignersOverlap(a, b MapSet.Set[string]) bool {
	return !a.Intersect(b).IsEmpty()
}

// FloatRound rounds a float64 to a specified number of decimal places.
// e.g. FloatRound(3.14159, 2) => 3.14
func FloatRound(x float64, precision int) float64 {
	pow := 1.0
	for i := 0; i < precision; i++ {
		pow *= 10
	}
	return float64(int(x*pow+0.5)) / pow
}

// AlignSlotToStep aligns slot to the next boundary divisible by step.
// Example: AlignSlotToStep(101, 4) => 104.
func AlignSlotToStep(slot, step uint64) uint64 {
	if step == 0 {
		return slot
	}
	rem := slot % step
	if rem == 0 {
		return slot
	}
	return slot + (step - rem)
}
