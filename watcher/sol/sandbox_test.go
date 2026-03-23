package sol

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
	"watcher/sol/dex"
	"watcher/types"
	"watcher/utils"

	"github.com/mr-tron/base58"
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

	// ── DEX instructions ───────────────────────────────────────────────
	dexInstructions := parseDexInstructionsFromJSONParsed(instructionsRaw, meta)

	// Check if innerInstructions was null
	_, innerPresent := meta["innerInstructions"].([]any)
	innerInstructionsNil := !innerPresent && meta["innerInstructions"] == nil

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
	ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, tokenDecimals, err :=
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
		Slot:                 slot,
		Timestamp:            ts,
		Fee:                  fee,
		IsFailed:             isFailed,
		IsVote:               isVote,
		Signature:            signature,
		Signers:              signersList,
		AccountKeys:          accountKeys,
		Programs:             programs,
		DexInstructions:      dexInstructions,
		InnerInstructionsNil: innerInstructionsNil,
		OwnerBalanceChanges:  ownerBalanceChanges,
		OwnerPreBalances:     ownerPreBalances,
		OwnerPostBalances:    ownerPostBalances,
		AtaOwner:             ataOwner,
		TokenDecimals:        tokenDecimals,
	}
	tx.PostprocessForFindSandwich()
	return tx, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// DEX instruction parser for jsonParsed format
// ─────────────────────────────────────────────────────────────────────────────

// parseDexInstructionsFromJSONParsed extracts DEX instruction data from both
// top-level and inner instructions in the jsonParsed RPC format.
// In this format, instructions are either "parsed" (have a "parsed" key) or
// "raw" (have "data" as base58 string + "accounts" as string array).
func parseDexInstructionsFromJSONParsed(topLevelInsts []any, meta map[string]any) []types.DexInstruction {
	var result []types.DexInstruction

	// Top-level instructions
	for _, inst := range topLevelInsts {
		instMap, ok := inst.(map[string]any)
		if !ok {
			continue
		}
		if _, hasParsed := instMap["parsed"]; hasParsed {
			continue
		}
		programID, _ := instMap["programId"].(string)
		if !utils.IsLabeledDexPrograms(programID) {
			continue
		}
		dataStr, _ := instMap["data"].(string)
		if dataStr == "" {
			continue
		}
		dataBytes, err := base58.Decode(dataStr)
		if err != nil {
			continue
		}
		rawAccounts, _ := instMap["accounts"].([]any)
		accounts := make([]string, 0, len(rawAccounts))
		for _, a := range rawAccounts {
			if addr, ok := a.(string); ok {
				accounts = append(accounts, addr)
			}
		}
		result = append(result, types.DexInstruction{
			ProgramID: programID,
			Data:      dataBytes,
			Accounts:  accounts,
			IsInner:   false,
			ParentIdx: -1,
		})
	}

	// Inner instructions from meta
	innerInsts, _ := meta["innerInstructions"].([]any)
	for _, group := range innerInsts {
		groupMap, ok := group.(map[string]any)
		if !ok {
			continue
		}
		parentIdx := 0
		if idx, ok := groupMap["index"].(float64); ok {
			parentIdx = int(idx)
		}
		instructions, _ := groupMap["instructions"].([]any)
		for _, inst := range instructions {
			instMap, ok := inst.(map[string]any)
			if !ok {
				continue
			}
			if _, hasParsed := instMap["parsed"]; hasParsed {
				continue
			}
			programID, _ := instMap["programId"].(string)
			if !utils.IsLabeledDexPrograms(programID) {
				continue
			}
			dataStr, _ := instMap["data"].(string)
			if dataStr == "" {
				continue
			}
			dataBytes, err := base58.Decode(dataStr)
			if err != nil {
				continue
			}
			rawAccounts, _ := instMap["accounts"].([]any)
			accounts := make([]string, 0, len(rawAccounts))
			for _, a := range rawAccounts {
				if addr, ok := a.(string); ok {
					accounts = append(accounts, addr)
				}
			}
			result = append(result, types.DexInstruction{
				ProgramID: programID,
				Data:      dataBytes,
				Accounts:  accounts,
				IsInner:   true,
				ParentIdx: parentIdx,
			})
		}
	}

	return result
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
	jsonFiles, err := filepath.Glob(filepath.Join("testdata", "sandwiches", "*.json"))
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
// Victim slippage tests
// ─────────────────────────────────────────────────────────────────────────────

// TestVictimSlippage loads individual victim transactions from testdata/victims/,
// extracts slippage info from DEX instructions, and validates the computed
// utilization against the expected value encoded in the filename.
//
// Filename convention: victim_{n}_{utilization_pct}.json
// e.g., victim_1_43.json → expected utilization ≈ 43%
//
// Usage:
//
//	go test -v -run TestVictimSlippage -timeout 30s ./sol/
func TestVictimSlippage(t *testing.T) {
	jsonFiles, err := filepath.Glob(filepath.Join("testdata", "victims", "victim_*.json"))
	if err != nil {
		t.Fatalf("glob victim json files: %v", err)
	}
	if len(jsonFiles) == 0 {
		t.Fatal("no victim json files found under testdata/victims")
	}

	sort.Strings(jsonFiles)

	for _, jsonFile := range jsonFiles {
		base := filepath.Base(jsonFile)
		t.Run(base, func(t *testing.T) {
			// Parse expected utilization from filename: victim_{n}_{pct}.json
			expectedPct := parseExpectedUtilizationPct(t, base)

			// Load the single victim tx
			txs, err := loadTxsFromJSONFile(jsonFile)
			if err != nil {
				t.Fatalf("loadTxsFromJSONFile(%s): %v", jsonFile, err)
			}
			if len(txs) != 1 {
				t.Fatalf("expected exactly 1 transaction in %s, got %d", jsonFile, len(txs))
			}
			tx := txs[0]

			// Verify DexInstructions were extracted
			if len(tx.DexInstructions) == 0 {
				t.Fatalf("no DexInstructions extracted from %s", jsonFile)
			}

			// Find the AMM pool to get fromAmount/toAmount/fromToken/toToken
			fromToken, toToken, fromAmount, toAmount := extractSwapAmounts(t, tx, jsonFile)

			// Build DexInstructionRefs and compute slippage
			refs := make([]dex.DexInstructionRef, len(tx.DexInstructions))
			for i, inst := range tx.DexInstructions {
				refs[i] = dex.DexInstructionRef{ProgramID: inst.ProgramID, Data: inst.Data}
			}
			result := dex.ComputeVictimSlippage(refs, tx.TokenDecimals, fromAmount, toAmount, fromToken, toToken)
			if result == nil {
				t.Fatalf("ComputeVictimSlippage returned nil for %s", jsonFile)
			}

			gotPct := result.Utilization * 100

			// Print slippage info for each DEX instruction
			for i, inst := range tx.DexInstructions {
				info := dex.ExtractSlippage(inst.ProgramID, inst.Data)
				if info == nil {
					t.Logf("  DexInstruction[%d] programID=%s → not decoded", i, inst.ProgramID)
				} else {
					t.Logf("  DexInstruction[%d] programID=%s dex=%s limitType=%s limit=%d noProtection=%v isInner=%v",
						i, inst.ProgramID, info.DexName, info.LimitType, info.LimitAmount, info.NoProtection, inst.IsInner)
				}
			}
			t.Logf("  swap: fromToken=%s toToken=%s fromAmount=%.9f toAmount=%.9f",
				fromToken, toToken, fromAmount, toAmount)
			t.Logf("  result: dex=%s limitType=%s limit=%.9f actual=%.9f utilization=%.2f%% expected=%.0f%%",
				result.DexName, result.LimitType, result.LimitAmount, result.ActualAmount, gotPct, expectedPct)

			// NoProtection: expectedPct=-1 means utilization should be SlippageNoProtection (-1)
			if expectedPct == -1 {
				if result.Utilization != dex.SlippageNoProtection {
					t.Fatalf("expected NoProtection (utilization=-1) for %s, got %.4f", jsonFile, result.Utilization)
				}
				return
			}

			// Allow ±2% tolerance for rounding differences
			const tolerancePct = 2.0
			if math.Abs(gotPct-expectedPct) > tolerancePct {
				t.Fatalf("utilization mismatch for %s: got %.2f%%, expected %.0f%% (±%.0f%%)",
					jsonFile, gotPct, expectedPct, tolerancePct)
			}
		})
	}
}

// parseExpectedUtilizationPct extracts the expected utilization percentage from the last
// segment of the filename. e.g. "victim_01_pumpfun_buy_43.json" → 43.0, "victim_12_pumpfun_sell_-1.json" → -1.0 (no protection).
func parseExpectedUtilizationPct(t *testing.T, filename string) float64 {
	t.Helper()
	name := strings.TrimSuffix(filename, ".json")
	parts := strings.Split(name, "_")
	if len(parts) < 3 {
		t.Fatalf("invalid victim filename format %q: expected victim_{n}_{pct}.json", filename)
	}
	tag := parts[len(parts)-1]
	pct, err := strconv.ParseFloat(tag, 64)
	if err != nil {
		t.Fatalf("cannot parse utilization pct from %q: %v", filename, err)
	}
	return pct
}

// extractSwapAmounts finds the AMM pool in a transaction and returns the swap
// direction and amounts from the signer's perspective.
func extractSwapAmounts(t *testing.T, tx *types.Transaction, file string) (fromToken, toToken string, fromAmount, toAmount float64) {
	t.Helper()
	pools := tx.RelatedPools.ToSlice()
	if len(pools) == 0 {
		t.Fatalf("no related pools found in %s", file)
	}

	// Find the first pool that is not a signer (i.e., the AMM pool)
	for _, pool := range pools {
		if utils.HasString(tx.Signers, pool) {
			continue
		}
		amt, ok := tx.RelatedPoolsInfo[pool]
		if !ok {
			continue
		}
		// Pool's income = user's expense (fromToken), pool's expense = user's income (toToken)
		fromToken = amt.IncomeToken
		toToken = amt.ExpenseToken
		fromAmount = math.Abs(amt.IncomeAmt)
		toAmount = math.Abs(amt.ExpenseAmt)
		return
	}

	t.Fatalf("no AMM pool found in %s (pools=%v, signers=%v)", file, pools, tx.Signers)
	return
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
