package sol

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/types"
	"sandwich-detector/utils"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	bin "github.com/gagliardetto/binary"
	"github.com/gagliardetto/solana-go"
	"github.com/mr-tron/base58"
	"github.com/spf13/viper"
)

var SolanaRpcURL string

// GetSolanaRpcURL resolves the RPC endpoint: an explicit override (backfill sets it), then
// Chainstack (the paid archival default), then the self-hosted node, then Helius.
func GetSolanaRpcURL() string {
	if SolanaRpcURL != "" {
		return SolanaRpcURL
	}
	if u := buildChainstackURL(); u != "" {
		return u
	}
	if rpc := viper.GetString("sol.rpc"); rpc != "" {
		return rpc
	}
	return buildHeliusURL()
}

// AccountRpcURL overrides the endpoint for current-state account queries (getMultipleAccounts).
var AccountRpcURL string

// GetAccountRpcURL resolves the endpoint for CURRENT-STATE account queries (getMultipleAccounts owner
// lookups). getMultipleAccounts is slot-independent, so it does NOT need the archival endpoint; it
// uses the self-hosted node (sol.rpc) — fast, free, unmetered — falling back to Helius, then the main
// archival URL. This avoids the archival provider's getMultipleAccounts method-throttling that stalls
// a sustained backfill.
func GetAccountRpcURL() string {
	if AccountRpcURL != "" {
		return AccountRpcURL
	}
	if rpc := viper.GetString("sol.rpc"); rpc != "" {
		return rpc
	}
	if u := buildHeliusURL(); u != "" {
		return u
	}
	return GetSolanaRpcURL()
}

// buildChainstackURL joins the Chainstack base URL (config sol.rpc-chainstack) with the API key
// from the environment (.env CHAINSTACK_API_KEY) — the key is the URL path segment, so it lives in
// .env rather than config.yaml. Returns "" when either half is missing.
func buildChainstackURL() string {
	base := viper.GetString("sol.rpc-chainstack")
	key := viper.GetString("CHAINSTACK_API_KEY")
	if base == "" || key == "" || key == "YOUR-API-KEY" {
		return ""
	}
	return strings.TrimRight(base, "/") + "/" + key
}

// buildHeliusURL joins the Helius base URL (config sol.rpc-helius) with the API key from the
// environment (.env HELIUS_RPC_API_KEY). Returns "" when no key is configured.
func buildHeliusURL() string {
	key := viper.GetString("HELIUS_RPC_API_KEY")
	if key == "" || key == "YOUR-API-KEY" {
		return ""
	}
	base := viper.GetString("sol.rpc-helius")
	if base == "" {
		base = "https://mainnet.helius-rpc.com"
	}
	return strings.TrimRight(base, "/") + "/?api-key=" + key
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

// rpcCallCounts tracks requests per RPC method since process start (one count per
// send-attempt group; internal transport retries on failure are not counted). Lets a
// long backfill report its actual RPC usage for quota accounting.
var rpcCallCounts sync.Map // method -> *atomic.Uint64

func countRpcCall(method string) {
	c, _ := rpcCallCounts.LoadOrStore(method, new(atomic.Uint64))
	c.(*atomic.Uint64).Add(1)
}

// RpcCallCountSnapshot returns the accumulated per-method RPC request counts.
func RpcCallCountSnapshot() map[string]uint64 {
	snap := make(map[string]uint64)
	rpcCallCounts.Range(func(k, v any) bool {
		snap[k.(string)] = v.(*atomic.Uint64).Load()
		return true
	})
	return snap
}

func CallRpc(method string, params []interface{}) (interface{}, error) {
	return callRpcOnURL(GetSolanaRpcURL(), method, params)
}

// callRpcOnURL is CallRpc against an explicit endpoint. Lets slot-independent, current-state methods
// (getMultipleAccounts owner lookups) target the self-hosted node while archival getBlock stays on
// the paid archival endpoint — needed because the archival provider method-throttles
// getMultipleAccounts under sustained backfill load (getBlock is unaffected).
func callRpcOnURL(url string, method string, params []interface{}) (interface{}, error) {
	req := SolanaRpcRequest{
		Jsonrpc: "2.0",
		ID:      "1",
		Method:  method,
		Params:  params,
	}

	backoff := config.RPC_RATE_LIMIT_BACKOFF
	for {
		rpcLimiter.acquire() // throttle to the configured RPS in backfill; no-op in live mode
		countRpcCall(method)

		var resp SolanaRpcResponse
		err := utils.PostUrlResponseWithRetry(url, req, &resp, config.DefaultRetryTimes, logger.SolLogger)
		if err != nil {
			// Throttling (HTTP 429): wait and retry with exponential backoff up to the cap.
			if isRateLimited(err) && backoff <= config.RPC_RATE_LIMIT_BACKOFF_MAX {
				logger.SolLogger.Warn("RPC throttled, backing off", "method", method, "backoff", backoff.String())
				time.Sleep(backoff)
				backoff *= 2
				continue
			}
			return nil, fmt.Errorf("RPC %s failed: %w", method, err)
		}
		if resp.Error != nil {
			return nil, fmt.Errorf("RPC %s returned error: %d %s", method, resp.Error.Code, resp.Error.Message)
		}
		return resp.Result, nil
	}
}

// isRateLimited reports whether an RPC error was an HTTP 429 (too many requests).
func isRateLimited(err error) bool {
	return err != nil && strings.Contains(err.Error(), "status 429")
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

	parallel := FetchParallelism
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

// FetchRewards, when set, requests block rewards so GetBlock can resolve the slot leader
// (the Fee reward recipient). Backfill turns this on to populate leaders for ranges where the
// slot_leaders table is empty; live mode leaves it off since getSlotLeaders feeds that table.
var FetchRewards bool

// FetchParallelism is the worker count GetBlocks uses. Backfill raises it to
// BACKFILL_FETCH_PARALLEL_NUM (the paid archival RPC absorbs it); live keeps the default.
var FetchParallelism = config.SOL_FETCH_SLOT_DATA_PARALLEL_NUM

func GetBlock(slot uint64) (*types.Block, error) {
	params := []interface{}{
		slot,
		map[string]interface{}{
			"encoding":                       "base64",
			"maxSupportedTransactionVersion": 0,
			"transactionDetails":             "full", // include full txs
			"rewards":                        FetchRewards,
			"commitment":                     "finalized",
		},
	}

	raw, err := CallRpc("getBlock", params)
	if err != nil {
		// Normalize well-known error patterns so caller can branch on them
		msg := err.Error()
		// Skipped/missing slot. Self-hosted nodes report "...ledger jump to recent snapshot";
		// archival RPCs (Helius) report code -32009 "...missing in long-term storage". Both mean
		// the slot has no block and must not be retried.
		if regexp.MustCompile(`Slot \d+ was skipped, or missing due to ledger jump to recent snapshot`).MatchString(msg) ||
			regexp.MustCompile(`Slot \d+ was skipped, or missing in long-term storage`).MatchString(msg) {
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
		Leader:       parseLeaderFromRewards(obj),
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

	// Build combined accounts (static + loaded writable + loaded readonly) for instruction account resolution
	combinedAccounts := buildCombinedAccounts(meta, accountKeys)

	// Parse meta data to get Balance changes & ATA owners & token decimals
	ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, tokenDecimals, err := parseBalancesDelta(meta, accountKeys)
	if err != nil {
		return nil, fmt.Errorf("parseTransactionMetaData failed: %w", err)
	}

	// Extract DEX instruction data from top-level and inner instructions
	dexInstructions := parseDexInstructions(tx.Message.Instructions, programs, combinedAccounts, meta)

	// Check if innerInstructions was null (RPC doesn't support extended metadata)
	_, innerPresent := meta["innerInstructions"].([]any)
	innerInstructionsNil := !innerPresent && meta["innerInstructions"] == nil

	// Build Transaction
	return &types.Transaction{
		// Inside
		IsFailed:             isFailed,
		IsVote:               isVote,
		Fee:                  feeLamport,
		Signature:            signature,
		AccountKeys:          accountKeys,
		Signers:              signer,
		Programs:             programs,
		OwnerBalanceChanges:  ownerBalanceChanges,
		OwnerPreBalances:     ownerPreBalances,
		OwnerPostBalances:    ownerPostBalances,
		AtaOwner:             ataOwner,
		DexInstructions:      dexInstructions,
		InnerInstructionsNil: innerInstructionsNil,
		TokenDecimals:        tokenDecimals,
	}, nil
}

// buildCombinedAccounts builds the full account list: static keys + loaded writable + loaded readonly.
// This combined list is needed for resolving instruction account indices in versioned transactions.
func buildCombinedAccounts(meta map[string]any, accountKeys []string) []string {
	loaded, _ := meta["loadedAddresses"].(map[string]any)
	var wr, ro []any
	if loaded != nil {
		wr, _ = loaded["writable"].([]any)
		ro, _ = loaded["readonly"].([]any)
	}
	accounts := make([]string, 0, len(accountKeys)+len(wr)+len(ro))
	accounts = append(accounts, accountKeys...)
	for _, v := range wr {
		if s, ok := v.(string); ok {
			accounts = append(accounts, s)
		}
	}
	for _, v := range ro {
		if s, ok := v.(string); ok {
			accounts = append(accounts, s)
		}
	}
	return accounts
}

// parseDexInstructions extracts instruction data for known DEX programs from both
// top-level instructions and inner instructions (CPI calls).
func parseDexInstructions(topLevelInsts []solana.CompiledInstruction, programs []string, combinedAccounts []string, meta map[string]any) []types.DexInstruction {
	var result []types.DexInstruction

	// Top-level instructions
	for i, inst := range topLevelInsts {
		programID := programs[i]
		if !utils.IsLabeledDexPrograms(programID) {
			continue
		}
		accounts := make([]string, 0, len(inst.Accounts))
		for _, idx := range inst.Accounts {
			if int(idx) < len(combinedAccounts) {
				accounts = append(accounts, combinedAccounts[int(idx)])
			}
		}
		dataCopy := make([]byte, len(inst.Data))
		copy(dataCopy, inst.Data)
		result = append(result, types.DexInstruction{
			ProgramID: programID,
			Data:      dataCopy,
			Accounts:  accounts,
			IsInner:   false,
			ParentIdx: -1,
		})
	}

	// Inner (CPI) instructions from meta. Under encoding=base64 these arrive compiled
	// ({programIdIndex, accounts:[indices], data:<base58>}); under jsonParsed they carry
	// resolved {programId, accounts:[addresses]}. Handle both so the same path works
	// regardless of the RPC encoding, resolving indices against combinedAccounts.
	innerInsts, _ := meta["innerInstructions"].([]any)
	for _, group := range innerInsts {
		groupMap, ok := group.(map[string]any)
		if !ok {
			continue
		}
		parentIdx := int(groupMap["index"].(float64))
		instructions, _ := groupMap["instructions"].([]any)
		for _, inst := range instructions {
			instMap, ok := inst.(map[string]any)
			if !ok {
				continue
			}
			// Fully-parsed instructions (SPL transfers, etc.) carry no raw data to decode.
			if _, hasParsed := instMap["parsed"]; hasParsed {
				continue
			}

			programID, _ := instMap["programId"].(string)
			if programID == "" {
				if idx, ok := instMap["programIdIndex"].(float64); ok && int(idx) < len(combinedAccounts) {
					programID = combinedAccounts[int(idx)]
				}
			}
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

			// Accounts are either resolved addresses (jsonParsed) or indices (base64).
			rawAccounts, _ := instMap["accounts"].([]any)
			accounts := make([]string, 0, len(rawAccounts))
			for _, a := range rawAccounts {
				switch v := a.(type) {
				case string:
					accounts = append(accounts, v)
				case float64:
					if int(v) < len(combinedAccounts) {
						accounts = append(accounts, combinedAccounts[int(v)])
					}
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
func parseBalancesDelta(meta map[string]any, accountKeys []string) (map[string]map[string]types.AtaAmounts, map[string]map[string]float64, map[string]map[string]float64, map[string]string, map[string]int, error) {
	if meta == nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("nil meta")
	}
	// Read balances
	postBalances, ok := meta["postBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, nil, fmt.Errorf("invalid postBalances")
	}
	postTokenBalances, ok := meta["postTokenBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, nil, fmt.Errorf("invalid postTokenBalances")
	}
	preBalances, ok := meta["preBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, nil, fmt.Errorf("invalid preBalances")
	}
	preTokenBalances, ok := meta["preTokenBalances"].([]interface{})
	if !ok {
		return nil, nil, nil, nil, nil, fmt.Errorf("invalid preTokenBalances")
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
			return nil, nil, nil, nil, nil, fmt.Errorf("unexpected loaded writable account type: %T", v)
		}
	}
	for _, v := range ro {
		if s, ok := v.(string); ok {
			accounts = append(accounts, s)
		} else {
			return nil, nil, nil, nil, nil, fmt.Errorf("unexpected loaded readonly account type: %T", v)
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
	tokenDecimals := make(map[string]int)
	// SOL always has 9 decimals
	tokenDecimals[utils.SOL] = 9
	tokenDecimals[utils.WSOL] = 9
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
		amountRaw, _ := strconv.ParseUint(amount, 10, 64) // SPL raw amounts are u64; Atoi overflows high-supply tokens to 0
		preb := float64(amountRaw) / math.Pow10(decimals)

		tokenDecimals[tokenAddr] = decimals
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
		amountRaw, _ := strconv.ParseUint(amount, 10, 64)
		postb := float64(amountRaw) / math.Pow10(decimals)

		// Record decimals here too: a token first appearing in postTokenBalances (e.g. a freshly
		// created ATA receiving a memecoin) is absent from the pre loop, and would otherwise
		// fall back to the 9-decimal default when its slippage limit is converted.
		tokenDecimals[tokenAddr] = decimals

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

	return ownerBalanceChanges, ownerPreBalances, ownerPostBalances, ataOwner, tokenDecimals, nil
}

// GetMultipleAccountOwners queries the on-chain owner program for a batch of
// addresses using the getMultipleAccounts RPC method. Returns a map from
// address → owner program ID. Addresses that don't exist on-chain are omitted.
// Maximum 100 addresses per call (Solana RPC limit).
func GetMultipleAccountOwners(addresses []string) (map[string]string, error) {
	if len(addresses) == 0 {
		return nil, nil
	}

	const batchSize = 100
	result := make(map[string]string, len(addresses))

	for start := 0; start < len(addresses); start += batchSize {
		end := start + batchSize
		if end > len(addresses) {
			end = len(addresses)
		}
		batch := addresses[start:end]

		// Build params: [["addr1","addr2",...], {"commitment":"finalized","encoding":"base64"}]
		addrList := make([]interface{}, len(batch))
		for i, a := range batch {
			addrList[i] = a
		}
		params := []interface{}{
			addrList,
			map[string]string{
				"commitment": "finalized",
				"encoding":   "base64",
			},
		}

		// Route to the current-state account endpoint (self-hosted), not the archival URL: the
		// archival provider method-throttles getMultipleAccounts under sustained load.
		raw, err := callRpcOnURL(GetAccountRpcURL(), "getMultipleAccounts", params)
		if err != nil {
			return result, fmt.Errorf("getMultipleAccounts failed: %w", err)
		}
		// A malformed-but-200 response (nil result, short value array) must be an ERROR, not
		// silence: the caller negative-caches addresses missing from the result, so treating a
		// bad response as authoritative absence would poison the owner cache for a whole batch.
		if raw == nil {
			return result, fmt.Errorf("getMultipleAccounts returned no result for %d addresses", len(batch))
		}
		obj, ok := raw.(map[string]any)
		if !ok {
			return result, fmt.Errorf("unexpected getMultipleAccounts result type: %T", raw)
		}
		values, ok := obj["value"].([]any)
		if !ok {
			return result, fmt.Errorf("unexpected value type: %T", obj["value"])
		}
		if len(values) != len(batch) {
			return result, fmt.Errorf("getMultipleAccounts returned %d values for %d addresses", len(values), len(batch))
		}

		for i, v := range values {
			if v == nil {
				continue // account does not exist
			}
			acct, ok := v.(map[string]any)
			if !ok {
				continue
			}
			owner, _ := acct["owner"].(string)
			if owner != "" {
				result[batch[i]] = owner
			}
		}
	}

	return result, nil
}
