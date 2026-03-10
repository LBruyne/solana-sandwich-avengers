package sol

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"sync"
	"time"
	"watcher/config"
	"watcher/logger"
	"watcher/types"
	"watcher/utils"

	bin "github.com/gagliardetto/binary"
	"github.com/gagliardetto/solana-go"
	"github.com/spf13/viper"
)

var SolanaRpcURL string

func GetSolanaRpcURL() string {
	if SolanaRpcURL != "" {
		return SolanaRpcURL
	}
	rpc := viper.GetString("sol.rpc")
	if rpc != "" {
		return rpc
	}
	return viper.GetString("sol.rpc-helius")
}

type SolanaRpcRequest struct {
	Jsonrpc string        `json:"jsonrpc"`
	ID      string        `json:"id"`
	Method  string        `json:"method"`
	Params  []interface{} `json:"params"`
}

type SolanaRpcResponse struct {
	Jsonrpc string      `json:"jsonrpc"`
	ID      string      `json:"id"`
	Result  interface{} `json:"result"`
	Error   *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

func CallRpc(method string, params []interface{}) (interface{}, error) {
	url := GetSolanaRpcURL()

	req := SolanaRpcRequest{
		Jsonrpc: "2.0",
		ID:      "1",
		Method:  method,
		Params:  params,
	}

	var resp SolanaRpcResponse
	err := utils.PostUrlResponseWithRetry(url, req, &resp, config.DefaultRetryTimes, logger.SolLogger)
	if err != nil {
		return nil, fmt.Errorf("RPC %s failed: %w", method, err)
	}

	if resp.Error != nil {
		return nil, fmt.Errorf("RPC %s returned error: %d %s", method, resp.Error.Code, resp.Error.Message)
	}

	return resp.Result, nil
}

func GetSlotLeaders(start, limit uint64) (types.SlotLeaders, error) {
	result, err := CallRpc("getSlotLeaders", []interface{}{start, limit})
	if err != nil {
		return nil, err
	}

	leaders, ok := result.([]interface{})
	if !ok {
		return nil, fmt.Errorf("unexpected type for leaders: %T", result)
	}

	res := make(types.SlotLeaders, 0, len(leaders))
	for i, v := range leaders {
		str, ok := v.(string)
		if !ok {
			return nil, fmt.Errorf("unexpected type in leaders array: %T", v)
		}
		res = append(res, &types.SlotLeader{
			Slot:   start + uint64(i),
			Leader: str,
		})
	}

	return res, nil
}

func GetCurrentSlot() (uint64, error) {
	result, err := CallRpc("getSlot", []interface{}{map[string]string{"commitment": "finalized"}})
	if err != nil {
		return 0, err
	}

	slot, ok := result.(float64) // json.Unmarshal default decodes numbers as float64
	if !ok {
		return 0, fmt.Errorf("unexpected type for slot: %T", result)
	}

	return uint64(slot), nil
}

func GetBlocks(startSlot, count uint64) types.Blocks {
	if count == 0 {
		return nil
	}
	endSlot := startSlot + count - 1

	parallel := config.SOL_FETCH_SLOT_DATA_PARALLEL_NUM
	if parallel <= 0 {
		parallel = 1
	}
	maxRetry := config.SOL_FETCH_SLOT_DATA_RETRYS

	slotsQueue := make(chan uint64, count)
	blocksCh := make(chan *types.Block, count)
	var wg sync.WaitGroup

	for s := startSlot; s <= endSlot; s++ {
		slotsQueue <- s
	}
	close(slotsQueue)

	wg.Add(parallel)
	for range parallel {
		go func() {
			defer wg.Done()
			for slotId := range slotsQueue {
				var block *types.Block
				var err error
				for attempt := 0; attempt <= maxRetry; attempt++ {
					block, err = GetBlock(slotId)
					if err == nil {
						if block != nil {
							blocksCh <- block
						}
						break
					}

					errStr := err.Error()
					if errStr == utils.SKIPPED_BLOCK || errStr == utils.CLEANED_BLOCK {
						logger.SolLogger.Warn("getBlock skipped/cleaned, move on", "slot", slotId, "err", err)
						break
					}

					if attempt < maxRetry {
						logger.SolLogger.Warn("retrying getBlock", "slot", slotId, "attempt", attempt+1, "err", err)
						continue
					}

					logger.SolLogger.Warn("getBlock failed after retries, skip", "slot", slotId, "err", err)
				}
			}
		}()
	}

	// Close channels when all workers exit
	go func() {
		wg.Wait()
		close(blocksCh)
	}()

	// Collect results
	blocks := make(types.Blocks, 0, count)
	for b := range blocksCh {
		if b != nil {
			blocks = append(blocks, b)
		}
	}

	// Sort by slot
	sort.Slice(blocks, func(i, j int) bool { return blocks[i].Slot < blocks[j].Slot })
	return blocks
}

func GetBlock(slot uint64) (*types.Block, error) {
	params := []interface{}{
		slot,
		map[string]interface{}{
			"encoding":                       "base64",
			"maxSupportedTransactionVersion": 0,
			"transactionDetails":             "full", // include full txs
			"rewards":                        false,  // rewards not needed here
			"commitment":                     "finalized",
		},
	}

	raw, err := CallRpc("getBlock", params)
	if err != nil {
		// Normalize well-known error patterns so caller can branch on them
		msg := err.Error()
		// e.g. "Slot 123 was skipped, or missing due to ledger jump to recent snapshot"
		if regexp.MustCompile(`^RPC getBlock returned error: \d+ Slot \d+ was skipped, or missing due to ledger jump to recent snapshot$`).MatchString(msg) ||
			regexp.MustCompile(`Slot \d+ was skipped, or missing due to ledger jump to recent snapshot`).MatchString(msg) {
			return nil, fmt.Errorf(utils.SKIPPED_BLOCK)
		}
		// e.g. "Block 123 cleaned up, does not exist on node. First available block: 456"
		if regexp.MustCompile(`Block \d+ cleaned up, does not exist on node\. First available block: \d+`).MatchString(msg) {
			return nil, fmt.Errorf(utils.CLEANED_BLOCK)
		}
		return nil, err
	}

	// Defensive: handle null result (can happen on some nodes)
	if raw == nil {
		return nil, fmt.Errorf(utils.SKIPPED_BLOCK)
	}

	// parseBlkTime := time.Now()
	obj, ok := raw.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("parse block failed, unexpected getBlock result type: %T", raw)
	}

	// Parse blockTime, blockHeight and txs (may be null)
	var ts time.Time
	if bt, ok := obj["blockTime"]; ok && bt != nil {
		switch v := bt.(type) {
		case float64:
			ts = time.Unix(int64(v), 0)
		case int64:
			ts = time.Unix(v, 0)
		case json.Number:
			if sec, e := v.Int64(); e == nil {
				ts = time.Unix(sec, 0)
			}
		default:
			// leave zero time on unknown type
		}
	}
	var height uint64
	if bh, ok := obj["blockHeight"]; ok && bh != nil {
		switch v := bh.(type) {
		case float64:
			height = uint64(v)
		case json.Number:
			if h, e := v.Int64(); e == nil {
				height = uint64(h)
			}
		default:
			// leave zero height on unknown type
		}
	}
	var txsData []any
	if arr, ok := obj["transactions"]; ok && arr != nil {
		if cast, ok := arr.([]any); ok {
			txsData = cast
		} else {
			return nil, fmt.Errorf("parse block failed, unexpected transactions type: %T", arr)
		}
	}

	// Init block model
	b := &types.Block{
		Slot:         slot,
		Timestamp:    ts,
		BlockHeight:  height,
		ValidTxCount: 0,
		Txs:          make([]*types.Transaction, 0, len(txsData)),
	}
	// Parse transactions (each item has meta + transaction{ message{...}, signatures... }; message is base64 per our request)
	for i, txData := range txsData {
		txMap, ok := txData.(map[string]any)
		if !ok {
			logger.SolLogger.Warn("parse block failed, unexpected tx item type", "index", i, "type", fmt.Sprintf("%T", txData))
			continue
		}

		// parseTransactionFromBase64 should:
		//   - decode base64 message
		//   - extract account keys, instructions, program ids, etc.
		//   - build *types.Transaction with RelatedAddrs populated
		tx, err := parseTransactionFromBase64(txMap)
		if err != nil {
			logger.SolLogger.Warn("parse block failed, parseTransactionFromBase64 failed", "slot", slot, "position", i, "err", err)
			continue
		}
		tx.Position = i
		tx.Slot = slot
		tx.Timestamp = b.Timestamp
		tx.PostprocessForFindSandwich()
		b.Txs = append(b.Txs, tx)
		if !tx.IsFailed && !tx.IsVote {
			b.ValidTxCount++
		}
	}
	// logger.SolLogger.Info("Parsed block cost", "slot", slot, "num_txs", len(b.Txs), "num_valid_txs", b.ValidTxCount, "parse_time", time.Since(parseBlkTime).String())
	return b, nil
}

// parseTransactionFromBase64 parses a single transaction item from `getBlock` when encoding="base64".
func parseTransactionFromBase64(txData map[string]any) (*types.Transaction, error) {
	meta, _ := txData["meta"].(map[string]any)
	isFailed := (meta != nil && meta["err"] != nil)

	// "transaction": [<base64>, <encoding>] in base64 mode
	raw, ok := txData["transaction"].([]any)
	if !ok || len(raw) == 0 {
		return nil, fmt.Errorf("unexpected transaction field: %T", txData["transaction"])
	}
	encoded, _ := raw[0].(string)
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, fmt.Errorf("base64 decode transaction failed: %w", err)
	}
	tx, err := solana.TransactionFromDecoder(bin.NewBinDecoder(data))
	if err != nil {
		return nil, fmt.Errorf("decode transaction failed: %w", err)
	}

	// Transaction fee
	fee, ok := meta["fee"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid fee field: %T", meta["fee"])
	}
	feeLamport := uint64(fee)
	// Transaction Id/signature, always the first signature
	signature := tx.Signatures[0].String()
	// Basic keys/signers
	accountKeys := make([]string, 0, len(tx.Message.AccountKeys))
	for _, k := range tx.Message.AccountKeys {
		accountKeys = append(accountKeys, k.String())
	}
	// Multiple signers
	signer := make([]string, 0, tx.Message.Signers().Len())
	for _, s := range tx.Message.Signers() {
		signer = append(signer, s.String())
	}

	// Programs list
	programs := make([]string, len(tx.Message.Instructions))
	for i, inst := range tx.Message.Instructions {
		// ProgramIDIndex are indexes into static account keys (programs are also accounts)
		if int(inst.ProgramIDIndex) < len(accountKeys) {
			programs[i] = accountKeys[int(inst.ProgramIDIndex)]
		}
	}
	// IsVote transaction
	isVote := (len(programs) == 1 && programs[0] == utils.VOTE_PROGRAM)

	// Parse meta data to get Balance changes & ATA owners
	ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, err := parseBalancesDelta(meta, accountKeys)
	if err != nil {
		return nil, fmt.Errorf("parseTransactionMetaData failed: %w", err)
	}

	// Build Transaction
	return &types.Transaction{
		// Inside
		IsFailed:            isFailed,
		IsVote:              isVote,
		Fee:                 feeLamport,
		Signature:           signature,
		AccountKeys:         accountKeys,
		Signers:             signer,
		Programs:            programs,
		OwnerBalanceChanges: ownerBalanceChanges,
		OwnerPreBalances:    ownerPreBalances,
		OwnerPostBalances:   ownerPostBalances,
		AtaOwner:            ataOwner,
	}, nil
}

/*
Balances example:
[

	// Alice's token account has 5 USDC (6 decimals)
	{
	 "accountIndex": 1, // index into combined account keys (static + loaded) to get the token account
	 "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", // token address
	 "owner": "AliceWalletPubkey11111111111111111111111111", //	token account owner address
	 "uiTokenAmount": {
	   "amount": "5000000",	// raw amount in smallest unit (e.g. 5000000 for 5 USDC with 6 decimals)
	   "decimals": 6,
	   "uiAmountString": "5.000000"
	 },
	 "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
	}, ...

]
*/
func parseBalancesDelta(meta map[string]any, accountKeys []string) (map[string]map[string]types.AtaAmounts, map[string]map[string]float64, map[string]map[string]float64, map[string]string, error) {
	if meta == nil {
		return nil, nil, nil, nil, fmt.Errorf("nil meta")
	}
	// Read balances
	postBalances, ok := meta["postBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, fmt.Errorf("invalid postBalances")
	}
	postTokenBalances, ok := meta["postTokenBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, fmt.Errorf("invalid postTokenBalances")
	}
	preBalances, ok := meta["preBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, fmt.Errorf("invalid preBalances")
	}
	preTokenBalances, ok := meta["preTokenBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, fmt.Errorf("invalid preTokenBalances")
	}
	// Read loaded addresses (writable + readonly)
	loaded, _ := meta["loadedAddresses"].(map[string]any)
	var wr, ro []any
	if loaded != nil {
		wr, _ = loaded["writable"].([]any)
		ro, _ = loaded["readonly"].([]any)
	}
	// Combine all accounts
	// Order: static account keys, writable loaded, readonly loaded
	accounts := make([]string, 0, len(accountKeys)+len(wr)+len(ro))
	accounts = append(accounts, accountKeys...)
	for _, v := range wr {
		if s, ok := v.(string); ok {
			accounts = append(accounts, s)
		} else {
			return nil, nil, nil, nil, fmt.Errorf("unexpected loaded writable account type: %T", v)
		}
	}
	for _, v := range ro {
		if s, ok := v.(string); ok {
			accounts = append(accounts, s)
		} else {
			return nil, nil, nil, nil, fmt.Errorf("unexpected loaded readonly account type: %T", v)
		}
	}

	ownerBalanceChanges := make(map[string]map[string]types.AtaAmounts)
	ownerPreBalances := make(map[string]map[string]float64)
	ownerPostBalances := make(map[string]map[string]float64)
	// SOL balance deltas
	for i, acnt := range accounts {
		if i >= len(preBalances) || i >= len(postBalances) {
			continue
		}

		preb := preBalances[i].(float64)
		postb := postBalances[i].(float64)
		deltaSOL := (postb - preb) / utils.SOL_UNIT
		// Skip zero changes
		if deltaSOL == 0 {
			continue
		}

		owner := acnt
		if _, ok := ownerBalanceChanges[owner]; !ok {
			ownerBalanceChanges[owner] = make(map[string]types.AtaAmounts)
		}
		ataAmts := types.NewAtaAmounts()
		ataAmts.AddAtaAmount(acnt, deltaSOL)
		ownerBalanceChanges[owner][utils.SOL] = ataAmts

		if _, ok := ownerPreBalances[owner]; !ok {
			ownerPreBalances[owner] = make(map[string]float64)
		}
		if _, ok := ownerPostBalances[owner]; !ok {
			ownerPostBalances[owner] = make(map[string]float64)
		}
		ownerPreBalances[owner][utils.SOL] += float64(preb) / utils.SOL_UNIT
		ownerPostBalances[owner][utils.SOL] += float64(postb) / utils.SOL_UNIT
	}

	ataOwner := make(map[string]string)
	// SPL token balance deltas
	// Pre token balances
	for _, tokenBalance := range preTokenBalances {
		tokenBalance, ok := tokenBalance.(map[string]any)
		if !ok {
			continue
		}
		ataIdx := int(tokenBalance["accountIndex"].(float64))
		if ataIdx < 0 || ataIdx >= len(accounts) {
			continue
		}
		ataAddr := accounts[ataIdx]
		tokenAddr, _ := tokenBalance["mint"].(string)
		owner := tokenBalance["owner"].(string)
		if owner == "" {
			owner = ataAddr // fallback to self if owner missing
		}
		uiTokenAmount, _ := tokenBalance["uiTokenAmount"].(map[string]any)
		amount, _ := uiTokenAmount["amount"].(string)
		decimals := int(uiTokenAmount["decimals"].(float64))
		amountInt, _ := strconv.Atoi(amount)
		preb := float64(amountInt) / math.Pow10(decimals)

		// Record ATA owner
		ataOwner[ataAddr] = owner
		// Record owner balance change
		if _, ok := ownerBalanceChanges[owner]; !ok {
			ownerBalanceChanges[owner] = make(map[string]types.AtaAmounts)
			ownerBalanceChanges[owner][tokenAddr] = types.NewAtaAmounts()
		}
		bc := ownerBalanceChanges[owner][tokenAddr]
		if bc.Amounts == nil {
			bc.Amounts = make(map[string]float64)
		}
		bc.AddAtaAmount(ataAddr, -preb)
		ownerBalanceChanges[owner][tokenAddr] = bc

		if _, ok := ownerPreBalances[owner]; !ok {
			ownerPreBalances[owner] = make(map[string]float64)
		}
		ownerPreBalances[owner][tokenAddr] += preb
	}

	// Post token balances
	for _, tokenBalance := range postTokenBalances {
		tokenBalance, ok := tokenBalance.(map[string]any)
		if !ok {
			continue
		}
		ataIdx := int(tokenBalance["accountIndex"].(float64))
		if ataIdx < 0 || ataIdx >= len(accounts) {
			continue
		}
		ataAddr := accounts[ataIdx]
		tokenAddr, _ := tokenBalance["mint"].(string)
		owner := tokenBalance["owner"].(string)
		if owner == "" {
			owner = ataAddr // fallback to self if owner missing
		}
		uiTokenAmount := tokenBalance["uiTokenAmount"].(map[string]any)
		amount, _ := uiTokenAmount["amount"].(string)
		decimals := int(uiTokenAmount["decimals"].(float64))
		amountInt, _ := strconv.Atoi(amount)
		postb := float64(amountInt) / math.Pow10(decimals)

		// Record ATA owner
		ataOwner[ataAddr] = owner
		// Record owner balance change
		if _, ok := ownerBalanceChanges[owner]; !ok {
			ownerBalanceChanges[owner] = make(map[string]types.AtaAmounts)
			ownerBalanceChanges[owner][tokenAddr] = types.NewAtaAmounts()
		}
		bc := ownerBalanceChanges[owner][tokenAddr]
		if bc.Amounts == nil {
			bc.Amounts = make(map[string]float64)
		}
		bc.AddAtaAmount(ataAddr, postb)
		ownerBalanceChanges[owner][tokenAddr] = bc

		if _, ok := ownerPostBalances[owner]; !ok {
			ownerPostBalances[owner] = make(map[string]float64)
		}
		ownerPostBalances[owner][tokenAddr] += postb
	}

	return ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, nil
}

// parseTransaction parses a single transaction item from `getBlock` when encoding is JSON (not base64).
// func parseTransaction(result map[string]interface{}) *Transaction {
// 	transactionAttr, _ := safeMap(result["transaction"])
// 	message, _ := safeMap(transactionAttr["message"])
// 	accountKeysInterface, _ := safeSlice(message["accountKeys"])
// 	signaturesAny, _ := safeSlice(transactionAttr["signatures"])
// 	instructionsAny, _ := safeSlice(message["instructions"])
// 	meta, _ := safeMap(result["meta"])

// 	accountKeys := make([]string, len(accountKeysInterface))
// 	for i, v := range accountKeysInterface {
// 		accountKeys[i], _ = v.(string)
// 	}

// 	programs := make([]string, len(instructionsAny))
// 	for i, v := range instructionsAny {
// 		mi, _ := v.(map[string]any)
// 		idx := asInt(mi["programIdIndex"])
// 		if 0 <= idx && idx < len(accountKeys) {
// 			programs[i] = accountKeys[idx]
// 		}
// 	}

// 	// Slot/time
// 	var slot uint64
// 	var timestamp time.Time
// 	if v, ok := result["slot"]; ok {
// 		switch t := v.(type) {
// 		case float64:
// 			slot = uint64(t)
// 		case json.Number:
// 			if n, e := t.Int64(); e == nil {
// 				slot = uint64(n)
// 			}
// 		}
// 	}
// 	if v, ok := result["blockTime"]; ok && v != nil {
// 		switch t := v.(type) {
// 		case float64:
// 			timestamp = time.Unix(int64(t), 0)
// 		case json.Number:
// 			if n, e := t.Int64(); e == nil {
// 				timestamp = time.Unix(n, 0)
// 			}
// 		}
// 	}

// 	// Combined keys for v0
// 	combined := expandCombinedKeys(accountKeys, meta)

// 	// Related addresses
// 	related := utils.NewUnionFind[string]()
// 	for _, k := range accountKeys {
// 		related.Add(k)
// 	}
// 	for _, k := range combined {
// 		related.Add(k)
// 	}

// 	// Strong decode is not available in JSON mode without re-encoding; use index heuristics
// 	for _, v := range instructionsAny {
// 		mi, _ := v.(map[string]any)
// 		inst := liteInstruction{
// 			ProgramIDIndex: asInt(mi["programIdIndex"]),
// 			Accounts:       asIntSlice(mi["accounts"]),
// 			DataB64:        strOrEmpty(mi["data"]),
// 		}
// 		collectByIndexHeuristicsFromLite(inst, accountKeys, combined, related)
// 	}

// 	signature := ""
// 	if len(signaturesAny) > 0 {
// 		signature, _ = signaturesAny[0].(string)
// 	}
// 	signers := accountKeys[:min(len(accountKeys), len(signaturesAny))]

// 	balanceChange, ataOwner := parseAtaBalanceChangeSafe(meta, accountKeys)
// 	out := &Transaction{
// 		Slot:          slot,
// 		Timestamp:     timestamp,
// 		Signature:     signature,
// 		Signer:        firstOrEmpty(signers),
// 		Signers:       signers,
// 		IsFailed:      meta != nil && meta["err"] != nil,
// 		Programs:      FilterPrograms(programs),
// 		AccountKeys:   accountKeys,
// 		BalanceChange: balanceChange,
// 		AtaOwner:      ataOwner,
// 		RelatedAddrs:  related,
// 	}
// 	return out
// }
