package sol

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
	"watcher/types"
	"watcher/utils"

	"github.com/spf13/viper"
)

func init() {
	// Ensure programs.yaml is found when tests run from the sol/ directory.
	viper.AddConfigPath("..")
	_ = viper.MergeInConfig()
}

// ─────────────────────────────────────────────────────────────────────────────
// Parser: converts Solana RPC getTransaction jsonParsed response → *types.Transaction
// ─────────────────────────────────────────────────────────────────────────────

// parseTransactionFromJSONParsed parses a single transaction from the Solana
// RPC getTransaction response (jsonParsed encoding) into our internal
// *types.Transaction, then calls PostprocessForFindSandwich.
//
// Expected top-level keys in raw:
//
//	{
//	  "transaction": { "signatures": [...], "message": { "accountKeys": [...], ... } },
//	  "meta":        { "err": null, "fee": 5000, "preBalances": [...], "postBalances": [...], ... },
//	  "slot":        405857593,
//	  "blockTime":   1773296064,
//	  "txIdx":       223
//	}
//
// accountKeys may be objects (jsonParsed) or plain strings.
func parseTransactionFromJSONParsed(raw map[string]any) (*types.Transaction, error) {
	meta, _ := raw["meta"].(map[string]any)
	if meta == nil {
		return nil, fmt.Errorf("missing meta")
	}

	txData, _ := raw["transaction"].(map[string]any)
	if txData == nil {
		return nil, fmt.Errorf("missing transaction")
	}

	message, _ := txData["message"].(map[string]any)
	if message == nil {
		return nil, fmt.Errorf("missing message")
	}

	// ── Account keys & signers ──────────────────────────────────────────
	accountKeysRaw, _ := message["accountKeys"].([]any)
	accountKeys := make([]string, 0, len(accountKeysRaw))
	signersList := make([]string, 0)
	for _, ak := range accountKeysRaw {
		switch v := ak.(type) {
		case string:
			accountKeys = append(accountKeys, v)
		case map[string]any:
			pubkey, _ := v["pubkey"].(string)
			accountKeys = append(accountKeys, pubkey)
			if isSigner, _ := v["signer"].(bool); isSigner {
				signersList = append(signersList, pubkey)
			}
		}
	}

	// ── Signature ───────────────────────────────────────────────────────
	sigsRaw, _ := txData["signatures"].([]any)
	signature := ""
	if len(sigsRaw) > 0 {
		signature, _ = sigsRaw[0].(string)
	}

	// ── Fee ─────────────────────────────────────────────────────────────
	fee := uint64(0)
	if f, ok := meta["fee"].(float64); ok {
		fee = uint64(f)
	}

	// ── IsFailed ────────────────────────────────────────────────────────
	isFailed := meta["err"] != nil

	// ── Programs ────────────────────────────────────────────────────────
	instructionsRaw, _ := message["instructions"].([]any)
	programs := make([]string, 0, len(instructionsRaw))
	for _, inst := range instructionsRaw {
		instMap, _ := inst.(map[string]any)
		if instMap == nil {
			continue
		}
		if pid, ok := instMap["programId"].(string); ok {
			programs = append(programs, pid)
		}
	}
	isVote := len(programs) == 1 && programs[0] == utils.VOTE_PROGRAM

	// ── Balance deltas ──────────────────────────────────────────────────
	// In jsonParsed format, accountKeys already includes loaded addresses.
	// Remove loadedAddresses from meta to prevent parseBalancesDelta from
	// double-appending them.
	metaCopy := make(map[string]any)
	for k, v := range meta {
		if k != "loadedAddresses" {
			metaCopy[k] = v
		}
	}
	ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, err :=
		parseBalancesDelta(metaCopy, accountKeys)
	if err != nil {
		return nil, fmt.Errorf("parseBalancesDelta: %w", err)
	}

	// ── Position / slot / timestamp ─────────────────────────────────────
	// Position is assigned externally by loadTxsFromJSONFile based on array order.
	slot := uint64(0)
	if s, ok := raw["slot"].(float64); ok {
		slot = uint64(s)
	}
	var ts time.Time
	if bt, ok := raw["blockTime"].(float64); ok {
		ts = time.Unix(int64(bt), 0)
	}

	tx := &types.Transaction{
		Slot:                slot,
		Timestamp:           ts,
		Fee:                 fee,
		IsFailed:            isFailed,
		IsVote:              isVote,
		Signature:           signature,
		Signers:             signersList,
		AccountKeys:         accountKeys,
		Programs:            programs,
		OwnerBalanceChanges: ownerBalanceChanges,
		OwnerPreBalances:    ownerPreBalances,
		OwnerPostBalances:   ownerPostBalances,
		AtaOwner:            ataOwner,
	}
	tx.PostprocessForFindSandwich()
	return tx, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// File helpers
// ─────────────────────────────────────────────────────────────────────────────

// loadTxsFromJSONFile reads a JSON array of getTransaction responses from path
// and returns parsed, postprocessed Transactions sorted by Position.
func loadTxsFromJSONFile(path string) ([]*types.Transaction, error) {
	data, err := os.ReadFile(path) // #nosec G304 -- test-only utility
	if err != nil {
		return nil, fmt.Errorf("read file: %w", err)
	}

	var rawTxs []map[string]any
	if err := json.Unmarshal(data, &rawTxs); err != nil {
		return nil, fmt.Errorf("unmarshal JSON array: %w", err)
	}

	txs := make([]*types.Transaction, 0, len(rawTxs))
	for i, raw := range rawTxs {
		tx, err := parseTransactionFromJSONParsed(raw)
		if err != nil {
			return nil, fmt.Errorf("parse tx[%d]: %w", i, err)
		}
		tx.Position = i
		txs = append(txs, tx)
	}
	return txs, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────

// TestSandwichFromJSON loads transactions from testdata/txs.json, builds a
// synthetic block, and runs in-block sandwich detection.
//
// Usage:
//  1. Place a JSON array of getTransaction responses in sol/testdata/txs.json
//     (each element should have "transaction", "meta", "slot", "blockTime", "txIdx")
//  2. Run:  go test -v -run TestSandwichFromJSON -timeout 30s ./sol/
func TestSandwichFromJSON(t *testing.T) {
	jsonFiles, err := filepath.Glob(filepath.Join("testdata", "*.json"))
	if err != nil {
		t.Fatalf("glob testdata json files: %v", err)
	}
	if len(jsonFiles) == 0 {
		t.Fatal("no json files found under testdata")
	}

	sort.Strings(jsonFiles)

	for _, jsonFile := range jsonFiles {
		base := filepath.Base(jsonFile)

		t.Run(base, func(t *testing.T) {
			txs, err := loadTxsFromJSONFile(jsonFile)
			if err != nil {
				t.Fatalf("loadTxsFromJSONFile(%s): %v", jsonFile, err)
			}
			if len(txs) == 0 {
				t.Fatalf("no transactions loaded from %s", jsonFile)
			}

			blk := &types.Block{
				Slot:         txs[0].Slot,
				Timestamp:    txs[0].Timestamp,
				Txs:          txs,
				ValidTxCount: uint64(len(txs)),
			}

			res := FindInBlockSandwiches(blk)

			expected := 1
			if strings.HasPrefix(strings.ToLower(base), "fake") {
				expected = 0
			}

			if len(res) != expected {
				t.Logf("file=%s expected=%d got=%d", jsonFile, expected, len(res))
				if len(res) > 0 {
					for i, s := range res {
						types.PPInBlockSandwich(i+1, s)
					}
				} else {
					dumpBucketDebug(txs)
				}
				t.Fatalf("unexpected sandwich count for %s", jsonFile)
			}

			transferRows := 0
			for _, s := range res {
				for _, stx := range s.FrontRun {
					if stx == nil || stx.Type != "transfer" {
						continue
					}
					transferRows++
					if stx.FromToken == "" || stx.ToToken == "" || stx.FromAmount <= 0 || stx.ToAmount <= 0 {
						t.Fatalf("invalid transfer row in frontRun for %s: sig=%s fromToken=%s toToken=%s fromAmount=%.9f toAmount=%.9f", jsonFile, stx.Signature, stx.FromToken, stx.ToToken, stx.FromAmount, stx.ToAmount)
					}
				}
				for _, stx := range s.BackRun {
					if stx == nil || stx.Type != "transfer" {
						continue
					}
					transferRows++
					if stx.FromToken == "" || stx.ToToken == "" || stx.FromAmount <= 0 || stx.ToAmount <= 0 {
						t.Fatalf("invalid transfer row in backRun for %s: sig=%s fromToken=%s toToken=%s fromAmount=%.9f toAmount=%.9f", jsonFile, stx.Signature, stx.FromToken, stx.ToToken, stx.FromAmount, stx.ToAmount)
					}
				}
			}

			if expected > 0 && strings.Contains(strings.ToLower(base), "transfer") && transferRows == 0 {
				t.Fatalf("expected transfer rows for %s, but got none", jsonFile)
			}
		})
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Debug helpers
// ─────────────────────────────────────────────────────────────────────────────

// dumpBucketDebug prints the PoolKey buckets built from the given transactions
// and checks for reverse-bucket existence. Useful when no sandwich is detected.
func dumpBucketDebug(txs types.Transactions) {
	buckets := filterAndBuildTxBuckets(txs, false)
	fmt.Printf("Buckets: %d\n", len(buckets))
	for key, entries := range buckets {
		fmt.Printf("  [%s] income=%s expense=%s  (%d entries)\n",
			key.PoolAddress, key.IncomeToken, key.ExpenseToken, len(entries))
		for _, e := range entries {
			fmt.Printf("    TxIdx=%d Pos=%d Signers=%v Income=%.9f Expense=%.9f\n",
				e.TxIdx, e.Position, e.Signers.ToSlice(), e.IncomeAmt, e.ExpenseAmt)
		}
		revKey := PoolKey{
			PoolAddress:  key.PoolAddress,
			IncomeToken:  key.ExpenseToken,
			ExpenseToken: key.IncomeToken,
		}
		if rev, ok := buckets[revKey]; ok {
			fmt.Printf("    ↔ reverse bucket exists (%d entries)\n", len(rev))
		} else {
			fmt.Printf("    ↔ NO reverse bucket\n")
		}
	}
}
