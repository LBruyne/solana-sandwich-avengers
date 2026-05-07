package types

// SlotLeader represents a mapping of a slot to its leader (validator)
type SlotLeader struct {
	Slot   uint64 `ch:"slot"`
	Leader string `ch:"leader"`
}

type SlotLeaders []*SlotLeader

// SlotTxsStatus represents the status of transactions in a specific slot
type SlotTxsStatus struct {
	Slot                    uint64 `ch:"slot"`
	TxFetched               bool   `ch:"txFetched"`
	TxCount                 uint64 `ch:"txCount"`
	ValidTxCount            uint64 `ch:"validTxCount"`
	SandwichFetched         bool   `ch:"sandwichFetched"`
	SandwichCount           uint64 `ch:"sandwichCount"`
	SandwichVictimCount     uint64 `ch:"sandwichVictimCount"`
	SandwichTxCount         uint64 `ch:"sandwichTxCount"`
	SandwichInBundleChecked bool   `ch:"sandwichInBundleChecked"`
}

// SlotBundlesStatus represents the status of bundles in a specific slot
type SlotBundlesStatus struct {
	Slot          uint64 `ch:"slot"`
	BundleFetched bool   `ch:"bundleFetched"`
	BundleCount   uint64 `ch:"bundleCount"`
	BundleTxCount uint64 `ch:"bundleTxCount"`
}
