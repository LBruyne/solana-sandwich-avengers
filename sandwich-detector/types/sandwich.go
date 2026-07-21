package types

import (
	"fmt"
	"time"
)

type SandwichTxTokenInfo struct {
	FromToken  string  `ch:"fromToken"`
	ToToken    string  `ch:"toToken"`
	FromAmount float64 `ch:"fromAmount"`
	ToAmount   float64 `ch:"toAmount"`

	FromTotalAmount float64 `ch:"fromTotalAmount"` // Only last front-run or back-run tx in a multi-front or multi-back sandwich has the total amount
	ToTotalAmount   float64 `ch:"toTotalAmount"`   // Only last front-run or back-run tx in a multi-front or multi-back sandwich has the total amount

	DiffA                float64  `ch:"diffA"`                // backTx.ToTotal - frontTx.FromTotal
	DiffB                float64  `ch:"diffB"`                // frontTx.ToTotal - backTx.FromTotal
	AttackerPostBalanceB float64  `ch:"attackerPostBalanceB"` // Attacker's tokenB balance after tx
	AttackerPreBalanceB  float64  `ch:"attackerPreBalanceB"`  // Attacker's tokenB balance before tx
	PoolPreBalanceB      float64  `ch:"poolPreBalanceB"`      // Pool's tokenB balance before tx
	PoolPostBalanceB     float64  `ch:"poolPostBalanceB"`     // Pool's tokenB balance after tx
	OwnersOfB            []string `ch:"ownersOfB"`            // Possible attacker owners, i.e., owners of ATAs that hold tokenB in front-run and back-run

	// Slippage utilization fields (meaningful only for victim txs)
	SlippageLimitType    string  `ch:"slippageLimitType"`    // "input" (max cost) / "output" (min output) / "" (unavailable)
	SlippageLimitAmount  float64 `ch:"slippageLimitAmount"`  // decoded limit value, converted to float64 with decimals
	SlippageActualAmount float64 `ch:"slippageActualAmount"` // actual cost or output from balance deltas
	SlippageUtilization  float64 `ch:"slippageUtilization"`  // ratio 0-1 (closer to 1 = tighter fit), -1 = unavailable
	SlippageDexName      string  `ch:"slippageDexName"`      // DEX name (e.g., "pumpfun", "raydium_v4")
}

type SandwichTx struct {
	SandwichID        string    `ch:"sandwichId"`
	SandwichTimestamp time.Time `ch:"sandwichTimestamp"`
	InBundle          bool      `ch:"inBundle"`
	Type              string    `ch:"type"` // frontRun, backRun, victim, transfer, or adverse
	Transaction
	SandwichTxTokenInfo
}

// Sandwich represents a detected sandwich transaction, including front-run, victim(s) and back-run like A->B, A->B, B->A
type Sandwich struct {
	SandwichID        string `ch:"sandwichId"`
	TokenA            string `ch:"tokenA"`
	TokenB            string `ch:"tokenB"`
	CrossBlock        bool   `ch:"crossBlock"`        // whether the sandwich spans multiple slots
	CrossLeader       bool   `ch:"crossLeader"`       // whether front and back fall under different slot leaders (implies CrossBlock)
	FrontLeader       string `ch:"frontLeader"`       // leader of the first front-run slot ("" if unknown)
	BackLeader        string `ch:"backLeader"`        // leader of the last back-run slot ("" if unknown)
	WindowStartSlot   uint64 `ch:"windowStartSlot"`   // first slot of the detection window this sandwich was found in
	WindowEndSlot     uint64 `ch:"windowEndSlot"`     // last slot of the detection window this sandwich was found in
	RpcSource         string `ch:"rpcSource"`         // data source: "live" (self-hosted) or "helius" (archival backfill)
	Consecutive       bool   `ch:"consecutive"`       // whether the sandwich txs are consecutive in the block, i.e., F_last + 1 == V_first and V_last + 1 == B_first
	FrontConsecutive  bool   `ch:"frontConsecutive"`  // whether the front-run txs are consecutive in the block, i.e., F_1 + 1 == F_2, F_2 + 1 == F_3, ...
	BackConsecutive   bool   `ch:"backConsecutive"`   // whether the back-run txs are consecutive in the block, i.e., B_1 + 1 == B_2, B_2 + 1 == B_3, ...
	VictimConsecutive bool   `ch:"victimConsecutive"` // whether the victim txs are consecutive in the block, i.e., V_1 + 1 == V_2, V_2 + 1 == V_3, ...

	MultiFrontRun bool `ch:"multiFrontRun"` // whether there are multiple front-run txs
	MultiBackRun  bool `ch:"multiBackRun"`  // whether there are multiple back-run txs
	MultiVictim   bool `ch:"multiVictim"`   // whether there are multiple victim txs

	SignerSame             bool `ch:"signerSame"` // whether front-run and back-run have the same signer
	HasTransfer            bool `ch:"hasTransfer"`
	HasFrontInlineTransfer bool `ch:"hasFrontInlineTransfer"` // front tx contains inline transfer (source != sink within same tx)
	HasDirectTransfer      bool `ch:"hasDirectTransfer"`      // separate direct transfer tx exists between front and back
	HasBackInlineTransfer  bool `ch:"hasBackInlineTransfer"`  // back tx contains inline transfer (source != sink within same tx)

	OwnerSame bool `ch:"ownerSame"` // whether front-run and back-run have the same owner, i.e., the owner of ATA that holds the toToken in front-run and the fromToken in back-run
	ATASame   bool `ch:"ataSame"`   // whether front-run and back-run have the same ATA that holds the toToken in front-run and the fromToken in back-run

	Perfect       bool    `ch:"perfect"`       // whether the sandwich is perfect, i.e., the amount diff of tokeb Bis exactly the same
	RelativeDiffB float64 `ch:"relativeDiffB"` // The relative amount diff = |backTxs.fromTotalAmount - frontTxs.toTotalAmount| / max(frontTxs.toTotalAmount, backTxs.fromTotalAmount).
	ProfitA       float64 `ch:"profitA"`       // The profit of the sandwich = backTx.toToTalAmount - frontTx.fromTotalAmount

	IntentScore            float64 `ch:"intentScore"`            // Intent score for sandwich attack (0-1, higher = more likely intentional)
	MaxSlippageUtilization float64 `ch:"maxSlippageUtilization"` // Max slippage utilization across all victims (0-1), 0 if unavailable

	AdverseCount uint16        `ch:"adverseCount"`
	FrontCount   uint16        `ch:"frontCount"`
	BackCount    uint16        `ch:"backCount"`
	VictimCount  uint16        `ch:"victimCount"`
	FrontRun     []*SandwichTx `ch:"frontRunTx" json:"frontRunTx"`
	BackRun      []*SandwichTx `ch:"backRunTx" json:"backRunTx"`
	Victims      []*SandwichTx `ch:"victims" json:"victims"`
	Adverse      []*SandwichTx `ch:"adverseTx" json:"adverseTx"`
}

// InBlockSandwich is a detected sandwich transaction, that front-run, victim(s) and back-run are all in the same block
type InBlockSandwich struct {
	Sandwich
	Slot      uint64    `ch:"slot" json:"slot"`
	Timestamp time.Time `ch:"timestamp" json:"timestamp"`
}

type CrossBlockSandwich struct {
	Sandwich
	Slot      uint64    `ch:"slot" json:"slot"`
	Timestamp time.Time `ch:"timestamp" json:"timestamp"`
}

// Pretty print sandwich txs
func ppSandwichTxs(kind string, txs []*SandwichTx) {
	fmt.Printf("  %s (%d):\n", kind, len(txs))
	for i, stx := range txs {
		ti := stx.SandwichTxTokenInfo
		PPTx(i+1, &stx.Transaction, true)
		fmt.Printf("         type=%v\n", stx.Type)
		if stx.Type == "transfer" {
			fmt.Printf("         transfer token=%s amount=%.9f\n", ti.FromToken, ti.FromAmount)
			switch len(ti.OwnersOfB) {
			case 0:
				// No owner linkage available for this transfer evidence.
			case 1:
				fmt.Printf("         owner=%s\n", ti.OwnersOfB[0])
			default:
				fmt.Printf("         owners=%s -> %s\n", ti.OwnersOfB[0], ti.OwnersOfB[1])
			}
			continue
		}
		fmt.Printf("         from=%s amt=%.9f  to=%s amt=%.9f\n",
			ti.FromToken, ti.FromAmount, ti.ToToken, ti.ToAmount)
		fmt.Printf("         poolPostBalanceB=%.9f  poolPreBalanceB=%.9f\n",
			ti.PoolPostBalanceB, ti.PoolPreBalanceB)

		// Statistics
		switch kind {
		case "FrontRun":
			if i == len(txs)-1 { // only last front-run tx has total amount
				fmt.Printf("         totals: fromTotal=%.9f  toTotal=%.9f\n",
					ti.FromTotalAmount, ti.ToTotalAmount)
			}
			fmt.Printf("         attackerPostBalanceB=%.9f  attackerPreBalanceB=%.9f ownersOfB=%v\n",
				ti.AttackerPostBalanceB, ti.AttackerPreBalanceB, ti.OwnersOfB)
		case "BackRun":
			if i == len(txs)-1 { // only last back-run tx has total amount
				fmt.Printf("         totals: fromTotal=%.9f  toTotal=%.9f  diffA=%.9f  diffB=%.9f\n", ti.FromTotalAmount, ti.ToTotalAmount, ti.DiffA, ti.DiffB)
			}
			fmt.Printf("         attackerPostBalanceB=%.9f  attackerPreBalanceB=%.9f ownersOfB=%v\n",
				ti.AttackerPostBalanceB, ti.AttackerPreBalanceB, ti.OwnersOfB)
		case "Victims":
			// No extra info for victims
		case "Adverse":
			// No extra info for adverse txs
		}
	}
}

// Pretty print an in-block sandwich
func PPInBlockSandwich(i int, s *InBlockSandwich) {
	fmt.Printf("==== Sandwich #%d ====\n", i)
	fmt.Printf("slot=%d time=%s\n", s.Slot, s.Timestamp.Format(time.RFC3339))
	fmt.Printf("pair: A=%s  B=%s\n", s.TokenA, s.TokenB)
	fmt.Printf("flags: CrossBlock=%v Consecutive=%v FrontConsec=%v BackConsec=%v VictimConsec=%v\n",
		s.CrossBlock, s.Consecutive, s.FrontConsecutive, s.BackConsecutive, s.VictimConsecutive)
	fmt.Printf("multi: Front=%v Back=%v Victim=%v  SignerSame=%v OwnerSame=%v ATASame=%v HasTransfer=%v\n",
		s.MultiFrontRun, s.MultiBackRun, s.MultiVictim, s.SignerSame, s.OwnerSame, s.ATASame, s.HasTransfer)
	fmt.Printf("quality: Perfect=%v RelativeDiffB=%.9f ProfitA=%.9f\n",
		s.Perfect, s.RelativeDiffB, s.ProfitA)

	ppSandwichTxs("FrontRun", s.FrontRun)
	ppSandwichTxs("Victims", s.Victims)
	if len(s.Adverse) > 0 {
		ppSandwichTxs("Adverse", s.Adverse)
	}
	ppSandwichTxs("BackRun", s.BackRun)
	fmt.Println()
}

func summarizeCrossBlockSpan(s *CrossBlockSandwich) (minSlot, maxSlot uint64, minTime, maxTime time.Time) {
	minSlot = ^uint64(0) // max uint64
	maxSlot = 0
	minTime = time.Unix(1<<62, 0)
	maxTime = time.Unix(0, 0)

	collect := func(txs []*SandwichTx) {
		for _, t := range txs {
			if t == nil {
				continue
			}
			if t.Slot < minSlot {
				minSlot = t.Slot
			}
			if t.Slot > maxSlot {
				maxSlot = t.Slot
			}
			if !t.Timestamp.IsZero() {
				if minTime.IsZero() || t.Timestamp.Before(minTime) {
					minTime = t.Timestamp
				}
				if t.Timestamp.After(maxTime) {
					maxTime = t.Timestamp
				}
			}
		}
	}

	collect(s.FrontRun)
	collect(s.Victims)
	collect(s.Adverse)
	collect(s.BackRun)
	return
}

// Pretty print a cross-block sandwich
func PPCrossBlockSandwich(i int, s *CrossBlockSandwich) {
	fmt.Printf("==== CrossBlock Sandwich #%d ====\n", i)
	minSlot, maxSlot, minTime, maxTime := summarizeCrossBlockSpan(s)
	if minSlot <= maxSlot {
		if !minTime.IsZero() && !maxTime.IsZero() {
			fmt.Printf("slots=[%d..%d] time=[%s .. %s]\n",
				minSlot, maxSlot, minTime.Format(time.RFC3339), maxTime.Format(time.RFC3339))
		} else {
			fmt.Printf("slots=[%d..%d]\n", minSlot, maxSlot)
		}
	}
	fmt.Printf("pair: A=%s  B=%s\n", s.TokenA, s.TokenB)
	fmt.Printf("flags: CrossBlock=%v FrontConsec=%v BackConsec=%v VictimConsec=%v\n",
		s.CrossBlock, s.FrontConsecutive, s.BackConsecutive, s.VictimConsecutive)
	fmt.Printf("multi: Front=%v Back=%v Victim=%v  SignerSame=%v OwnerSame=%v ATASame=%v HasTransfer=%v\n",
		s.MultiFrontRun, s.MultiBackRun, s.MultiVictim, s.SignerSame, s.OwnerSame, s.ATASame, s.HasTransfer)
	fmt.Printf("quality: Perfect=%v RelativeDiffB=%.9f ProfitA=%.9f\n",
		s.Perfect, s.RelativeDiffB, s.ProfitA)

	ppSandwichTxs("FrontRun", s.FrontRun)
	ppSandwichTxs("Victims", s.Victims)
	if len(s.Adverse) > 0 {
		ppSandwichTxs("Adverse", s.Adverse)
	}
	ppSandwichTxs("BackRun", s.BackRun)
	fmt.Println()
}
