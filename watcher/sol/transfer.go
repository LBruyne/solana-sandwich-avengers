package sol

import (
	"watcher/types"

	MapSet "github.com/deckarep/golang-set/v2"
)

const (
	transferSideFront = "front"
	transferSideBack  = "back"
)

type Transfer struct {
	Tx          *types.Transaction
	TxIdx       int
	Side        string
	IsInline    bool
	SourceOwner string
	SinkOwner   string
	Token       string
	Amount      float64
}

func getTxByIndex(txs types.Transactions, idx int) *types.Transaction {
	if idx < 0 || idx >= len(txs) {
		return nil
	}
	return txs[idx]
}

func collectFrontOwnersByToken(entries []PoolEntry, txs types.Transactions, token string) MapSet.Set[string] {
	owners := MapSet.NewSet[string]()
	for _, entry := range entries {
		if entry.SinkOwner != "" {
			owners.Add(entry.SinkOwner)
			continue
		}

		tx := getTxByIndex(txs, entry.TxIdx)
		if tx == nil {
			continue
		}
		for owner, bc := range tx.OwnerBalanceChanges {
			if bc[token].TotalAmount > 0 {
				owners.Add(owner)
			}
		}
	}
	return owners
}

func collectBackOwnersByToken(entries []PoolEntry, txs types.Transactions, token string) MapSet.Set[string] {
	owners := MapSet.NewSet[string]()
	for _, entry := range entries {
		if entry.SourceOwner != "" {
			owners.Add(entry.SourceOwner)
			continue
		}

		tx := getTxByIndex(txs, entry.TxIdx)
		if tx == nil {
			continue
		}
		for owner, bc := range tx.OwnerBalanceChanges {
			if bc[token].TotalAmount < 0 {
				owners.Add(owner)
			}
		}
	}
	return owners
}

func collectInlineTransfers(entries []PoolEntry, txs types.Transactions, side string) []*Transfer {
	evidences := make([]*Transfer, 0)
	for _, entry := range entries {
		if !entry.HasInlineTransfer || entry.SourceOwner == "" || entry.SinkOwner == "" {
			continue
		}

		tx := getTxByIndex(txs, entry.TxIdx)
		if tx == nil {
			continue
		}

		token := entry.ExpenseToken
		if token == "" {
			continue
		}

		amount := tx.GetOwnerBalanceChange(entry.SinkOwner, token)
		if amount <= 0 {
			continue
		}

		evidences = append(evidences, &Transfer{
			Tx:          tx,
			TxIdx:       entry.TxIdx,
			Side:        side,
			IsInline:    true,
			SourceOwner: entry.SourceOwner,
			SinkOwner:   entry.SinkOwner,
			Token:       token,
			Amount:      amount,
		})
	}
	return evidences
}

func collectDirectTransfers(
	txs types.Transactions,
	startIdx int,
	endIdx int,
	token string,
	fromOwners MapSet.Set[string],
	toOwners MapSet.Set[string],
	side string,
) []*Transfer {
	if startIdx < 0 {
		startIdx = 0
	}
	if endIdx > len(txs) {
		endIdx = len(txs)
	}
	if startIdx >= endIdx {
		return make([]*Transfer, 0)
	}

	evidences := make([]*Transfer, 0)
	for i := startIdx; i < endIdx; i++ {
		tx := txs[i]
		if tx == nil || tx.IsFailed || tx.IsVote {
			continue
		}
		if tx.RelatedPools.Cardinality() != 0 {
			continue
		}

		hasFrom := false
		hasTo := false
		amount := 0.0
		sourceOwner := ""
		sinkOwner := ""
		for owner, bc := range tx.OwnerBalanceChanges {
			delta := bc[token].TotalAmount
			if delta < 0 && fromOwners.Contains(owner) {
				hasFrom = true
				amount += -delta
				if sourceOwner == "" {
					sourceOwner = owner
				}
			}
			if delta > 0 && toOwners.Contains(owner) {
				hasTo = true
				if sinkOwner == "" {
					sinkOwner = owner
				}
			}
		}

		if !hasFrom || !hasTo || amount <= 0 {
			continue
		}

		evidences = append(evidences, &Transfer{
			Tx:          tx,
			TxIdx:       i,
			Side:        side,
			IsInline:    false,
			SourceOwner: sourceOwner,
			SinkOwner:   sinkOwner,
			Token:       token,
			Amount:      amount,
		})
	}

	return evidences
}

func sumTransferAmount(evidences []*Transfer) float64 {
	total := 0.0
	for _, evidence := range evidences {
		if evidence == nil || evidence.Amount <= 0 {
			continue
		}
		total += evidence.Amount
	}
	return total
}

func sumFrontInlineBridgeAmount(
	evidences []*Transfer,
	token string,
	frontOwners MapSet.Set[string],
	backOwners MapSet.Set[string],
) float64 {
	total := 0.0
	for _, evidence := range evidences {
		if evidence == nil || evidence.Token != token || evidence.Amount <= 0 {
			continue
		}
		if evidence.SinkOwner == "" || !backOwners.Contains(evidence.SinkOwner) {
			continue
		}
		if evidence.SourceOwner != "" && frontOwners.Cardinality() > 0 && !frontOwners.Contains(evidence.SourceOwner) {
			continue
		}
		total += evidence.Amount
	}
	return total
}

func makeTransferSandwichTx(sandwichId string, evidence *Transfer, tokenB string) *types.SandwichTx {
	if evidence == nil || evidence.Tx == nil || evidence.Token == "" || evidence.Amount <= 0 {
		return nil
	}

	stx := &types.SandwichTx{
		SandwichID:  sandwichId,
		Transaction: *evidence.Tx,
		Type:        "transfer",
		SandwichTxTokenInfo: types.SandwichTxTokenInfo{
			FromToken:  evidence.Token,
			ToToken:    evidence.Token,
			FromAmount: evidence.Amount,
			ToAmount:   evidence.Amount,
			OwnersOfB:  []string{},
		},
		InBundle: false,
	}

	if evidence.Token == tokenB {
		stx.SandwichTxTokenInfo.OwnersOfB = appendUniqueString(stx.SandwichTxTokenInfo.OwnersOfB, evidence.SourceOwner)
		stx.SandwichTxTokenInfo.OwnersOfB = appendUniqueString(stx.SandwichTxTokenInfo.OwnersOfB, evidence.SinkOwner)
	}

	return stx
}

func appendUniqueString(items []string, value string) []string {
	if value == "" {
		return items
	}
	for _, item := range items {
		if item == value {
			return items
		}
	}
	return append(items, value)
}
