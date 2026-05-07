package types

import "time"

// JitoBundle represents a single Jito MEV bundle containing one or more transactions, reading from Jito API
type JitoBundle struct {
	Slot              uint64    `json:"slot" ch:"slot"`
	BundleId          string    `json:"bundleId" ch:"bundleId"`
	Timestamp         time.Time `json:"timestamp" ch:"timestamp"`
	Tippers           []string  `json:"tippers" ch:"tippers"`
	Transactions      []string  `json:"transactions" ch:"transactions"`
	LandedTipLamports uint64    `json:"landedTipLamports" ch:"landedTipLamports"`
}

type JitoBundles []*JitoBundle

type JitoBundleMarkResult struct {
	Slot uint64
	Hits []string
}
