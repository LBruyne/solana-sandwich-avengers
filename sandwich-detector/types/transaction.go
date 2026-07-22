package types

import (
	"fmt"
	"math"
	"strings"
	"time"
	"sandwich-detector/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

type Block struct {
	Slot         uint64
	BlockHeight  uint64
	Timestamp    time.Time
	Txs          []*Transaction
	ValidTxCount uint64
	Leader       string // block producer, resolved from the Fee reward when rewards are fetched (backfill)
}

type Blocks []*Block

type Transaction struct {
	Slot      uint64    `json:"slot" ch:"slot"`
	Position  int       `json:"position" ch:"position"` // The position of this transaction in the block
	Timestamp time.Time `json:"timestamp" ch:"timestamp"`
	Fee       uint64    `json:"fee" ch:"fee"` // The fee paid by the signer to process this transaction, in lamports (1 SOL = 10^9 lamports)
	IsFailed  bool
	IsVote    bool

	Signature       string           `json:"signature" ch:"signature"`     // The identifier of this transaction, which is the first signature in Signatures field. A 64 bytes Ed25519 signature, encoded as a base-58 string.
	Signers         []string         `json:"signers" ch:"signers"`         // All signers extracted from the transaction message. Used by all detection logic.
	AccountKeys     []string         `json:"accountKeys" ch:"accountKeys"` // All accounts accessed in this transaction
	Programs        []string         `json:"programs" ch:"programs"`       // All programs invoked in this transaction
	DexInstructions      []DexInstruction `json:"-" ch:"-"` // DEX-related instruction data extracted from top-level and inner instructions
	InnerInstructionsNil bool              `json:"-" ch:"-"` // true if RPC returned null for meta.innerInstructions

	// Account-balance related information
	// In Solana, for each token/WSOL, each user has an Associated Token Account, ATA, which is controlled by the address of the holder (owner).
	// An ATA is derived from the owner's address and the token mint address.
	// A token is labeled by its mint address, e.g.
	// USDC: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1vl,
	// WSOL: So11111111111111111111111111111111111111112,
	// SOL is labeled by "SOL"
	// A owner may have multiple ATAs for the same token
	// A pool is a Solana address that has exact 1 income token and 1 expense token throughout a tx, excluding the signer itself, e.g., WSOL/TESLA pool
	OwnerBalanceChanges map[string]map[string]AtaAmounts `ch:"ownerBalanceChanges" json:"ownerBalanceChanges"` // owner -> token/SOL -> []{ata, delta}, record the balance changes of each owner for each token/SOL in this transaction.
	OwnerPreBalances    map[string]map[string]float64    `ch:"ownerPreBalances" json:"ownerPreBalances"`       // owner -> token/SOL -> pre balance, record the pre-transaction balance of each owner for each token/SOL in this transaction.
	OwnerPostBalances   map[string]map[string]float64    `ch:"ownerPostBalances" json:"ownerPostBalances"`     // owner -> token/SOL -> post balance, record the post-transaction balance of each owner for each token/SOL in this transaction.
	AtaOwner            map[string]string                `ch:"ataOwner" json:"ataOwner"`                       // ata -> owner address, record the owner of each ATA involved in this transaction
	RelatedTokens       MapSet.Set[string]               `ch:"relatedTokens" json:"relatedTokens"`             // records all tokens involved in this transaction
	RelatedPools        MapSet.Set[string]               `ch:"relatedPools" json:"relatedPools"`               // records all pools involved in this transaction
	RelatedPoolsInfo    map[string]PoolAmount            // pool address -> {fromToken, fromAmt, toToken, toAmt}, record the token changes of each related pool
	TokenDecimals       map[string]int                   `json:"-" ch:"-"` // token mint -> decimals, for converting raw amounts to float64
}

// DexInstruction stores raw instruction data for a known DEX program invocation.
type DexInstruction struct {
	ProgramID string   // DEX program address
	Data      []byte   // raw instruction bytes (decoded from base64/base58)
	Accounts  []string // resolved account addresses referenced by this instruction
	IsInner   bool     // true if from meta.innerInstructions
	ParentIdx int      // top-level instruction index this inner instruction belongs to (-1 if top-level)
}

type Transactions []*Transaction

// PostprocessForFindSandwich performs post-processing on the transaction data to prepare it for sandwich detection.
// It extracts and organizes relevant information such as RelatedTokens and RelatedPools.
func (tx *Transaction) PostprocessForFindSandwich() {
	tx.RelatedTokens = MapSet.NewSet[string]()
	tx.RelatedPools = MapSet.NewSet[string]()
	tx.RelatedPoolsInfo = make(map[string]PoolAmount)
	if tx.IsFailed || tx.IsVote {
		return // Skip failed or vote transactions
	}

	// Combine SOL and WSOL
	// Other tokens like jitoSOL, bSOL shold not be combined
	for owner, tokenChanges := range tx.OwnerBalanceChanges {
		solChanges, hasSOL := tokenChanges[utils.SOL]
		if solChanges.Amounts == nil {
			solChanges = NewAtaAmounts()
		}
		wsolChanges, hasWSOL := tokenChanges[utils.WSOL]
		if wsolChanges.Amounts == nil {
			wsolChanges = NewAtaAmounts()
		}
		if !hasWSOL {
			continue
		}

		if hasSOL {
			// Add all WSOL changes to SOL changes
			for ata, amt := range wsolChanges.Amounts {
				solChanges.AddAtaAmount(ata, amt)
			}
			// Replace WSOL with SOL
			tokenChanges[utils.SOL] = solChanges
			delete(tokenChanges, utils.WSOL)
		} else {
			// Rename WSOL to SOL
			tokenChanges[utils.SOL] = wsolChanges
			delete(tokenChanges, utils.WSOL)
		}
		tx.OwnerBalanceChanges[owner] = tokenChanges
	}
	for owner, preBalances := range tx.OwnerPreBalances {
		solPreBalance, hasSOL := preBalances[utils.SOL]
		wsolPreBalance, hasWSOL := preBalances[utils.WSOL]
		if !hasWSOL {
			continue
		}

		if hasSOL {
			preBalances[utils.SOL] = solPreBalance + wsolPreBalance
			delete(preBalances, utils.WSOL)
		} else {
			preBalances[utils.SOL] = wsolPreBalance
			delete(preBalances, utils.WSOL)
		}
		tx.OwnerPreBalances[owner] = preBalances
	}
	for owner, postBalances := range tx.OwnerPostBalances {
		solPostBalance, hasSOL := postBalances[utils.SOL]
		wsolPostBalance, hasWSOL := postBalances[utils.WSOL]
		if !hasWSOL {
			continue
		}

		if hasSOL {
			postBalances[utils.SOL] = solPostBalance + wsolPostBalance
			delete(postBalances, utils.WSOL)
		} else {
			postBalances[utils.SOL] = wsolPostBalance
			delete(postBalances, utils.WSOL)
		}
		tx.OwnerPostBalances[owner] = postBalances
	}

	// Useful things for sandwich detection
	// Collect potentially related tokens
	relatedTokens := MapSet.NewSet[string]()
	for _, tokenChanges := range tx.OwnerBalanceChanges {
		for token := range tokenChanges {
			relatedTokens.Add(token)
		}
	}
	tx.RelatedTokens = relatedTokens
	// Collect potentially related pools (any owner with exactly 1 income token and 1 expense token throughout this tx)
	// Note: signer is also included as a pool-like participant to enable swap structure validation downstream.
	// For signer, the SOL balance is adjusted by removing the transaction fee to isolate swap-related changes.
	feeInSOL := float64(tx.Fee) / utils.SOL_UNIT

	relatedPools := MapSet.NewSet[string]()
	relatedPoolsInfo := make(map[string]PoolAmount)
	for owner, tokenChanges := range tx.OwnerBalanceChanges {
		income := 0
		expense := 0
		poolInfo := PoolAmount{}
		for token, change := range tokenChanges {
			amount := change.GetTotalAmount()

			// For any signer's SOL balance, the RPC-reported delta includes the transaction fee.
			// Add back the fee so that only the swap-related SOL change remains.
			// Then, if the fee-adjusted amount is negligibly small (< EPSILON), treat it
			// as zero so it does not count as an income or expense leg.
			// NOTE: We only apply the EPSILON threshold to signer SOL (where fee
			// adjustment happens). For non-signer amounts (e.g., AMM pool SOL changes),
			// even very small but real amounts (like 50K lamports) must be preserved —
			// filtering them out would prevent the pool from being recognized in
			// RelatedPools.
			if utils.HasString(tx.Signers, owner) && token == utils.SOL {
				amount += feeInSOL
				if math.Abs(amount) < utils.EPSILON {
					continue
				}
			}

			if amount < 0 {
				expense++
				poolInfo.ExpenseToken = token
				poolInfo.ExpenseAmt = amount
			} else {
				income++
				poolInfo.IncomeToken = token
				poolInfo.IncomeAmt = amount
			}
		}

		// A pool-like participant has exactly 1 income token and 1 expense token
		if income == 1 && expense == 1 {
			relatedPools.Add(owner)
			relatedPoolsInfo[owner] = poolInfo
		}
	}
	tx.RelatedPools = relatedPools
	tx.RelatedPoolsInfo = relatedPoolsInfo
}

// PoolAmount represents the token changes of a pool in a transaction.
// A pool has exactly one IncomeToken with positive amount and one OutcomeToken with negative amount.
type PoolAmount struct {
	IncomeToken  string
	ExpenseToken string
	IncomeAmt    float64
	ExpenseAmt   float64
}

// AtaAmounts represents pairs of ATA addresses and their corresponding amounts for a specific token.
type AtaAmounts struct {
	Amounts     map[string]float64 // ata address -> amount
	TotalAmount float64
}

func NewAtaAmounts() AtaAmounts {
	return AtaAmounts{
		Amounts:     make(map[string]float64),
		TotalAmount: 0,
	}
}

func (p *AtaAmounts) AddAtaAmount(ata string, amount float64) {
	// If ata already exists, just update the amount
	for a, v := range p.Amounts {
		if strings.EqualFold(a, ata) {
			p.Amounts[ata] = v + amount
			p.TotalAmount += amount
			return
		}
	}
	// New ata, add to the list
	p.Amounts[ata] = amount
	p.TotalAmount += amount
}

func (p *AtaAmounts) GetTotalAmount() float64 {
	return p.TotalAmount
}

// Pretty Print block and its transactions
func PPBlock(b *Block, txLimit int, txDetails bool) {
	if b == nil {
		return
	}
	ts := "-"
	if !b.Timestamp.IsZero() {
		ts = b.Timestamp.Format(time.RFC3339)
	}
	fmt.Printf("==== Block #%d ====\n", b.Slot)
	fmt.Printf("slot=%d  height=%d  time=%s  txs=%d\n",
		b.Slot, b.BlockHeight, ts, len(b.Txs))

	n := len(b.Txs)
	if txLimit > 0 && txLimit < n {
		n = txLimit
	}
	for i := 0; i < n; i++ {
		PPTx(i, b.Txs[i], txDetails)
	}
	fmt.Println()
}

// Pretty Print a transaction
func PPTx(i int, tx *Transaction, details bool) {
	if tx == nil {
		return
	}
	signerStr := ""
	if len(tx.Signers) > 0 {
		signerStr = shorten(tx.Signers[0], 8)
	}
	fmt.Printf("  -- Tx[%d] slot=%d pos=%d fee=%d sig=%s signer=%s signers=%d\n",
		i, tx.Slot, tx.Position, tx.Fee, tx.Signature, signerStr, len(tx.Signers))
	fmt.Printf("     flags: failed=%v vote=%v  programs=%d  accountKeys=%d\n",
		tx.IsFailed, tx.IsVote, len(tx.Programs), len(tx.AccountKeys))

	// Related tokens
	tokens := tx.RelatedTokens.ToSlice()
	if len(tokens) > 0 {
		fmt.Printf("     relatedTokens: %s\n", strings.Join(tokens, ", "))
	}

	// Related pools
	pools := tx.RelatedPools.ToSlice()
	if len(pools) > 0 {
		fmt.Printf("     relatedPools (%d):\n", len(pools))
		for _, p := range pools {
			if pa, ok := tx.RelatedPoolsInfo[p]; ok {
				dir := fmt.Sprintf("%s -> %s", pa.ExpenseToken, pa.IncomeToken)
				fmt.Printf("       - %s  dir=%s  expense=%.12f  income=%.12f\n",
					p, dir, pa.ExpenseAmt, pa.IncomeAmt)
			} else {
				fmt.Printf("       - %s\n", p)
			}
		}
	}

	if !details {
		return
	}

	// Owner pre/post and delta balance changes
	if len(tx.OwnerBalanceChanges) > 0 {
		fmt.Printf("     ownerBalanceChanges:\n")
		for owner, tokenMap := range tx.OwnerBalanceChanges {
			parts := make([]string, 0, len(tokenMap))
			for token, ataAmounts := range tokenMap {
				preb := tx.OwnerPreBalances[owner][token]
				postb := tx.OwnerPostBalances[owner][token]
				parts = append(parts, fmt.Sprintf("%s: pre=%.12f post=%.12f delta=%.12f", token, preb, postb, ataAmounts.GetTotalAmount()))
			}
			if len(parts) > 0 {
				fmt.Printf("       - %s: %s\n", shorten(owner, 12), strings.Join(parts, "; "))
			}
		}
	}

	if len(tx.AtaOwner) > 0 {
		fmt.Printf("     ataOwner (%d):\n", len(tx.AtaOwner))
		count := 0
		for ata, owner := range tx.AtaOwner {
			fmt.Printf("       - %s -> %s\n", shorten(ata, 12), shorten(owner, 12))
			count++
			if count >= 6 { // limit output
				if len(tx.AtaOwner) > count {
					fmt.Printf("         ... and %d more\n", len(tx.AtaOwner)-count)
				}
				break
			}
		}
	}
}

func shorten(s string, n int) string {
	if n <= 3 || len(s) <= n {
		return s
	}
	return s[:n-3] + "..."
}

// GetOwnerPreBalance returns the pre-transaction balance of a specific token for a given owner.
func (tx *Transaction) GetOwnerPreBalance(owner string, token string) float64 {
	if tx.OwnerPreBalances == nil {
		return 0
	}
	if tokenMap, ok := tx.OwnerPreBalances[owner]; ok {
		return tokenMap[token]
	}
	return 0
}

// GetOwnerPostBalance returns the post-transaction balance of a specific token for a given owner.
func (tx *Transaction) GetOwnerPostBalance(owner string, token string) float64 {
	if tx.OwnerPostBalances == nil {
		return 0
	}
	if tokenMap, ok := tx.OwnerPostBalances[owner]; ok {
		return tokenMap[token]
	}
	return 0
}

// GetOwnerBalanceChange returns the total balance change of a specific token for a given owner.
func (tx *Transaction) GetOwnerBalanceChange(owner string, token string) float64 {
	if tx.OwnerBalanceChanges == nil {
		return 0
	}
	if tokenMap, ok := tx.OwnerBalanceChanges[owner]; ok {
		return tokenMap[token].TotalAmount
	}
	return 0
}
