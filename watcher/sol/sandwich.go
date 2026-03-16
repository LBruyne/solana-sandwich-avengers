package sol

import (
	"fmt"
	"sort"
	"sync"
	"time"
	"watcher/config"
	"watcher/db"
	"watcher/logger"
	"watcher/types"
	"watcher/utils"
)

var ch db.Database
var crossBlockCache = NewBlockCache(config.CROSS_BLOCK_CACHE_SIZE)

func RunSandwichCmd(startSlot uint64) error {
	ch = db.NewClickhouse()
	defer ch.Close()

	currentSlot, err := GetCurrentSlot()
	if err != nil {
		return fmt.Errorf("failed to get current slot: %w", err)
	}
	logger.SolLogger.Info("Current slot from Solana RPC", "slot", currentSlot)

	if startSlot < currentSlot-config.SOL_FETCH_SLOT_DATA_MAX_GAP || startSlot > currentSlot {
		startSlot = currentSlot - config.SOL_FETCH_SLOT_DATA_MAX_GAP
	}
	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)
	logger.SolLogger.Info("Fetch slot setting adjusts", "start", startSlot, "current_from_rpc", currentSlot)

	// Run a loop to continuously fetch slot data, process sandwiches and store results to DB
	logger.SolLogger.Info("Analyzing slot data start from", "start", startSlot)

	for {
		startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)

		currentSlot, err := GetCurrentSlot()
		if err != nil {
			logger.SolLogger.Error("Failed to get current slot", "err", err)
			continue
		}

		if startSlot < currentSlot-config.SOL_FETCH_SLOT_DATA_MAX_GAP {
			startSlot = currentSlot - config.SOL_FETCH_SLOT_DATA_MAX_GAP
			startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)
			logger.SolLogger.Info("Start slot too old, adjust to", "start", startSlot, "current", currentSlot-config.SOL_FETCH_SLOT_DATA_MAX_GAP)
		}

		numToFetch := config.SOL_FETCH_SLOT_DATA_SLOT_NUM
		if currentSlot-startSlot < config.SOL_FETCH_SLOT_DATA_LATEST_GAP {
			logger.SolLogger.Info("Not enough new slots, sleep and retry after "+config.SOL_FETCH_SLOT_DATA_LONG_INTERVAL.String(), "start", startSlot, "current", currentSlot)
			time.Sleep(config.SOL_FETCH_SLOT_DATA_LONG_INTERVAL)
			continue
		}

		// Fetch blocks
		logger.SolLogger.Info("Fetch slot data (start)", "start", startSlot, "current", currentSlot, "diff", currentSlot-startSlot, "num_to_fetch", numToFetch)
		fetchTimeBefore := time.Now()
		blocks := GetBlocks(startSlot, uint64(numToFetch))
		fetchTime := time.Since(fetchTimeBefore)
		logger.SolLogger.Info("Fetched slot data (done)", "start", startSlot, "num_fetched", len(blocks), "fetch_time", fetchTime.String())

		// Test print block
		// for _, b := range blocks {
		// 	types.PPBlock(b, 5, true)
		// }

		// Process blocks to find sandwiches
		logger.SolLogger.Info("Process slot data (start)", "start", startSlot, "num_fetched", len(blocks))
		timeProcess := time.Now()
		inBlockSandwiches, crossBlockSandwiches := ProcessBlocksForSandwich(blocks)
		logger.SolLogger.Info("Process slot data (done)", "start", startSlot, "num_in_block_sandwiches", len(inBlockSandwiches), "num_cross_block_sandwiches", len(crossBlockSandwiches), "process_time", time.Since(timeProcess).String())

		// Test print sandwiches
		// for _, s := range inBlockSandwiches {
		// 	if s.TokenA != "SOL" {
		// 		continue
		// 	}

		// 	transferFound := false
		// 	for _, tx := range s.FrontRun {
		// 		if tx.Type == "transfer" {
		// 			transferFound = true
		// 			break
		// 		}
		// 	}
		// 	if transferFound {
		// 		logger.SolLogger.Info("Found in-block sandwich with transfer")
		// 		// types.PPInBlockSandwich(i+1, s)
		// 		for _, tx := range s.FrontRun {
		// 			logger.SolLogger.Info("  FrontRun", "tx", tx.Signature, "type", tx.Type, "signers", tx.Signers)
		// 		}
		// 		for _, tx := range s.BackRun {
		// 			logger.SolLogger.Info("  BackRun ", "tx", tx.Signature, "type", tx.Type, "signers", tx.Signers)
		// 		}
		// 	}
		// }
		// for _, s := range crossBlockSandwiches {
		// 	if s.TokenA != "SOL" {
		// 		continue
		// 	}

		// 	transferFound := false
		// 	for _, tx := range s.FrontRun {
		// 		if tx.Type == "transfer" {
		// 			transferFound = true
		// 			break
		// 		}
		// 	}
		// 	if transferFound {
		// 		logger.SolLogger.Info("Found cross-block sandwich with transfer")
		// 		// types.PPCrossBlockSandwich(i+1, s)
		// 		for _, tx := range s.FrontRun {
		// 			logger.SolLogger.Info("  FrontRun", "tx", tx.Signature, "type", tx.Type, "signers", tx.Signers)
		// 		}
		// 		for _, tx := range s.BackRun {
		// 			logger.SolLogger.Info("  BackRun ", "tx", tx.Signature, "type", tx.Type, "signers", tx.Signers)
		// 		}
		// 	}
		// }

		// Save to DB
		logger.SolLogger.Info("Store sandwiches related information to DB (start)")
		timeStore := time.Now()
		if err := StoreSandwichesToDB(ch, inBlockSandwiches, crossBlockSandwiches); err != nil {
			logger.SolLogger.Error("Failed to store sandwiches to DB", "err", err)
		}
		if err := StoreSlotSandwichStatusToDB(ch, blocks, inBlockSandwiches, crossBlockSandwiches); err != nil {
			logger.SolLogger.Error("Failed to store slot sandwich status to DB", "err", err)
		}
		logger.SolLogger.Info("Store sandwiches related information to DB (done)", "store_time", time.Since(timeStore).String())

		// Update next start slot
		startSlot += uint64(numToFetch)
		// Sleep a while
		logger.SolLogger.Info("Sleeping for "+config.SOL_FETCH_SLOT_DATA_SHORT_INTERVAL.String(), "next_start", startSlot)
		time.Sleep(config.SOL_FETCH_SLOT_DATA_SHORT_INTERVAL)
	}
}

func ProcessBlocksForSandwich(blocks types.Blocks) (inBlock []*types.InBlockSandwich, crossBlock []*types.CrossBlockSandwich) {
	if len(blocks) == 0 {
		return make([]*types.InBlockSandwich, 0), make([]*types.CrossBlockSandwich, 0)
	}

	var wg sync.WaitGroup
	inCh := make(chan []*types.InBlockSandwich, 1)
	crCh := make(chan []*types.CrossBlockSandwich, 1)
	wg.Add(2)
	go func() {
		defer wg.Done()
		inCh <- ProcessInBlockSandwich(blocks)
	}()
	go func() {
		defer wg.Done()
		crCh <- ProcessCrossBlockSandwich(blocks)
	}()

	inBlock = <-inCh
	crossBlock = <-crCh

	wg.Wait()

	return
}

func ProcessInBlockSandwich(blocks types.Blocks) []*types.InBlockSandwich {
	// Process in-block sandwiches in parallel
	parallel := config.SOL_PROCESS_IN_BLOCK_SANDWICH_PARALLEL_NUM
	blocksQueue := make(chan *types.Block, len(blocks))
	sandwichesCh := make(chan []*types.InBlockSandwich)

	var processWg sync.WaitGroup

	// Initialize blocks queue
	go func() {
		for _, b := range blocks {
			blocksQueue <- b
		}
		// Close channel after all blocks are sent
		close(blocksQueue)
	}()

	processWg.Add(parallel)
	for range parallel {
		go func() {
			defer processWg.Done()
			// Worker goroutine to process blocks after all blocks are sent
			for b := range blocksQueue {
				// Process in-block sandwiches
				sandwiches := FindInBlockSandwiches(b)
				// Send found sandwiches to channel
				sandwichesCh <- sandwiches
			}
		}()
	}

	// Close sandwiches channel when all processing goroutines are done
	go func() {
		processWg.Wait()
		close(sandwichesCh)
	}()

	// Collect sandwiches from channel
	sandwiches := make([]*types.InBlockSandwich, 0)
	for sandwich := range sandwichesCh {
		sandwiches = append(sandwiches, sandwich...)
	}

	sort.Slice(sandwiches, func(i, j int) bool {
		if sandwiches[i].Slot != sandwiches[j].Slot {
			return sandwiches[i].Slot < sandwiches[j].Slot
		}
		return sandwiches[i].Timestamp.Before(sandwiches[j].Timestamp)
	})

	return sandwiches
}

func FindInBlockSandwiches(b *types.Block) []*types.InBlockSandwich {
	finder := &InBlockSandwichFinder{
		Txs:             b.Txs,
		AmountThreshold: config.INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD,
	}

	// timeFind := time.Now()
	finder.Find()
	// logger.SolLogger.Info("Find in-block sandwiches", "slot", b.Slot, "num_txs", len(b.Txs), "num_sandwiches", len(finder.Sandwiches), "time_cost", time.Since(timeFind).String())
	return finder.Sandwiches
}

func ProcessCrossBlockSandwich(blocks types.Blocks) []*types.CrossBlockSandwich {
	if len(blocks) == 0 {
		return make([]*types.CrossBlockSandwich, 0)
	}

	// Update cache with newly fetched blocks.
	for _, b := range blocks {
		if b != nil {
			crossBlockCache.Put(b)
		}
	}

	// Build unique slot->block map from cache.
	all := crossBlockCache.AllBlocks()
	slotToBlock := make(map[uint64]*types.Block, len(all))
	for _, b := range all {
		if b != nil {
			slotToBlock[b.Slot] = b
		}
	}
	if len(slotToBlock) == 0 {
		return make([]*types.CrossBlockSandwich, 0)
	}

	// Sort all cached blocks by slot.
	sorted := make(types.Blocks, 0, len(slotToBlock))
	for _, b := range slotToBlock {
		sorted = append(sorted, b)
	}
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].Slot < sorted[j].Slot
	})

	// Record slots in current batch.
	newSlots := make(map[uint64]struct{}, len(blocks))
	var latestNewSlot uint64
	hasLatestNewSlot := false
	for _, b := range blocks {
		if b != nil {
			newSlots[b.Slot] = struct{}{}
			if !hasLatestNewSlot || b.Slot > latestNewSlot {
				latestNewSlot = b.Slot
				hasLatestNewSlot = true
			}
		}
	}
	if len(newSlots) == 0 {
		return make([]*types.CrossBlockSandwich, 0)
	}

	leaderCache := make(map[uint64]string, len(sorted))
	leaderKnown := make(map[uint64]bool, len(sorted))
	getLeader := func(slot uint64) (string, bool) {
		if known, ok := leaderKnown[slot]; ok {
			return leaderCache[slot], known
		}

		l, err := crossBlockCache.GetSlotLeader(slot)
		if err != nil {
			leaderKnown[slot] = false
			leaderCache[slot] = ""
			logger.SolLogger.Warn("GetSlotLeader failed", "slot", slot, "err", err)
			return "", false
		}
		leaderKnown[slot] = true
		leaderCache[slot] = l
		return l, true
	}

	type leaderRun struct {
		startIdx int
		endIdx   int // exclusive
		hasNew   bool
	}
	type crossWindow struct {
		blocks   types.Blocks
		leader   string
		runStart uint64
		runEnd   uint64
	}

	// Stage 1: split into same-leader continuous runs.
	runs := make([]leaderRun, 0, len(sorted))
	windows := make([]crossWindow, 0)
	if len(sorted) > 0 {
		runStart := 0
		hasNewInRun := false
		if _, ok := newSlots[sorted[0].Slot]; ok {
			hasNewInRun = true
		}

		for i := 1; i <= len(sorted); i++ {
			endRun := i == len(sorted)
			if !endRun {
				prev, curr := sorted[i-1], sorted[i]
				prevLeader, prevKnown := getLeader(prev.Slot)
				currLeader, currKnown := getLeader(curr.Slot)
				// End run if slots are not continuous, leader unknown, or leader changed.
				if curr.Slot != prev.Slot+1 || !prevKnown || !currKnown || prevLeader != currLeader {
					endRun = true
				}
			}

			if endRun {
				runs = append(runs, leaderRun{startIdx: runStart, endIdx: i, hasNew: hasNewInRun})
				runStart = i
				if i < len(sorted) {
					_, hasNewInRun = newSlots[sorted[i].Slot]
				}
				continue
			}

			if _, ok := newSlots[sorted[i].Slot]; ok {
				hasNewInRun = true
			}
		}

		// Stage 2: choose runs to check this round.
		for runIdx, r := range runs {
			run := sorted[r.startIdx:r.endIdx]
			runStartSlot := run[0].Slot
			runEndSlot := run[len(run)-1].Slot

			// Check runs that are newly fetched OR closed by a newer run in this batch.
			// This covers missing-slot gaps (e.g. runEnd+1 is skipped/cleaned), where
			// the next fetched run starts at runEnd+k (k > 1).
			runClosedByNewerRun := runIdx < len(runs)-1 && runs[runIdx+1].hasNew
			shouldCheck := r.hasNew || runClosedByNewerRun
			if !shouldCheck {
				continue
			}

			runLeader, runLeaderKnown := getLeader(runStartSlot)
			if !runLeaderKnown {
				runLeader = "UNKNOWN"
			}
			isTailRun := runIdx == len(runs)-1
			if isTailRun && hasLatestNewSlot && runEndSlot == latestNewSlot {
				logger.SolLogger.Info("Defer tail leader run for next time", "run_start", runStartSlot, "run_end", runEndSlot, "common_leader", runLeader)
				continue
			}

			window := make(types.Blocks, len(run))
			copy(window, run)

			windows = append(windows, crossWindow{blocks: window, leader: runLeader, runStart: runStartSlot, runEnd: runEndSlot})
		}
	}

	if len(windows) == 0 {
		return make([]*types.CrossBlockSandwich, 0)
	}

	result := make([]*types.CrossBlockSandwich, 0)
	resultSandwichIDSet := make(map[string]bool)
	parallel := config.SOL_PROCESS_CROSS_BLOCK_SANDWICH_PARALLEL_NUM
	if parallel <= 1 {
		parallel = 1
	}
	if parallel > len(windows) {
		parallel = len(windows)
	}

	windowsQueue := make(chan crossWindow, len(windows))
	for _, w := range windows {
		windowsQueue <- w
	}
	close(windowsQueue)

	var mu sync.Mutex
	var processWg sync.WaitGroup
	processWg.Add(parallel)
	for range parallel {
		go func() {
			defer processWg.Done()
			for w := range windowsQueue {
				logger.SolLogger.Info("Checking cross-block sandwich window", "run_start", w.runStart, "run_end", w.runEnd, "common_leader", w.leader)
				found := FindCrossBlockSandwiches(w.blocks)
				if len(found) == 0 {
					continue
				}
				mu.Lock()
				for _, s := range found {
					if _, ok := resultSandwichIDSet[s.SandwichID]; !ok {
						result = append(result, s)
						resultSandwichIDSet[s.SandwichID] = true
					}
				}
				mu.Unlock()
			}
		}()
	}
	processWg.Wait()

	sort.Slice(result, func(i, j int) bool {
		if result[i].Slot != result[j].Slot {
			return result[i].Slot < result[j].Slot
		}
		return result[i].Timestamp.Before(result[j].Timestamp)
	})

	return result
}

func FindCrossBlockSandwiches(blocks types.Blocks) []*types.CrossBlockSandwich {
	finder := NewCrossBlockSandwichFinder(blocks, config.CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD)

	finder.Find()
	return finder.Sandwiches
}

func getSlotLeaderFromDB(slot uint64) (string, error) {
	if ch == nil {
		return "", fmt.Errorf("db is not initialized")
	}
	leader, err := ch.QuerySlotLeader(slot)
	if err != nil {
		return "", fmt.Errorf("QuerySlotLeader failed: %w", err)
	}
	return leader, nil
}

func StoreSandwichesToDB(ch db.Database, inBlockSandwiches []*types.InBlockSandwich, crossBlockSandwiches []*types.CrossBlockSandwich) error {
	if len(inBlockSandwiches) > 0 {
		if err := ch.InsertInBlockSandwiches(inBlockSandwiches); err != nil {
			return fmt.Errorf("failed to insert in-block sandwiches to DB: %w", err)
		}
		logger.SolLogger.Info("Inserted in-block sandwiches to DB", "num", len(inBlockSandwiches))

		sandwichTxToInsert := make([]*types.SandwichTx, 0)
		for _, s := range inBlockSandwiches {
			for _, tx := range s.FrontRun {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.Victims {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.Adverse {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.BackRun {
				tx.SandwichTimestamp = s.Timestamp
			}
			sandwichTxToInsert = append(sandwichTxToInsert, s.FrontRun...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.Victims...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.Adverse...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.BackRun...)
		}
		if err := ch.InsertSandwichTxs(sandwichTxToInsert); err != nil {
			return fmt.Errorf("failed to insert in-block sandwich txs to DB: %w", err)
		}
		logger.SolLogger.Info("Inserted in-block sandwich txs to DB", "num", len(sandwichTxToInsert))
	}

	if len(crossBlockSandwiches) > 0 {
		if err := ch.InsertCrossBlockSandwiches(crossBlockSandwiches); err != nil {
			return fmt.Errorf("failed to insert cross-block sandwiches to DB: %w", err)
		}
		logger.SolLogger.Info("Inserted cross-block sandwiches to DB", "num", len(crossBlockSandwiches))

		sandwichTxToInsert := make([]*types.SandwichTx, 0)
		for _, s := range crossBlockSandwiches {
			for _, tx := range s.FrontRun {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.Victims {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.Adverse {
				tx.SandwichTimestamp = s.Timestamp
			}
			for _, tx := range s.BackRun {
				tx.SandwichTimestamp = s.Timestamp
			}
			sandwichTxToInsert = append(sandwichTxToInsert, s.FrontRun...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.Victims...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.Adverse...)
			sandwichTxToInsert = append(sandwichTxToInsert, s.BackRun...)
		}
		if err := ch.InsertSandwichTxs(sandwichTxToInsert); err != nil {
			return fmt.Errorf("failed to insert cross-block sandwich txs to DB: %w", err)
		}
		logger.SolLogger.Info("Inserted cross-block sandwich txs to DB", "num", len(sandwichTxToInsert))
	}

	return nil
}

func StoreSlotSandwichStatusToDB(ch db.Database, blks types.Blocks, inBlockSandwiches []*types.InBlockSandwich, crossBlockSandwiches []*types.CrossBlockSandwich) error {
	// DO NOT store sandwich tx count now!
	// Map slot to number of sandwich txs
	// slotToSandwichTxCount := make(map[uint64]uint64)
	// slotToSandwichCount := make(map[uint64]uint64)
	// slotToSandwichVictimCount := make(map[uint64]uint64)
	// In-block sandwiches
	// for _, s := range inBlockSandwiches {
	// 	slotToSandwichTxCount[s.Slot] += uint64(len(s.FrontRun) + len(s.BackRun))
	// 	slotToSandwichCount[s.Slot] += 1
	// 	slotToSandwichVictimCount[s.Slot] += uint64(len(s.Victims))
	// }
	// // Cross-block sandwiches
	// for _, s := range crossBlockSandwiches {
	// 	slotToSandwichTxCount[s.Slot] += uint64(len(s.FrontRun) + len(s.BackRun))
	// 	slotToSandwichCount[s.Slot] += 1
	// 	slotToSandwichVictimCount[s.Slot] += uint64(len(s.Victims))
	// }

	statuses := make([]*types.SlotTxsStatus, 0, len(blks))
	for _, blk := range blks {
		statuses = append(statuses, &types.SlotTxsStatus{
			Slot:            blk.Slot,
			TxFetched:       true,
			TxCount:         uint64(len(blk.Txs)),
			ValidTxCount:    blk.ValidTxCount,
			SandwichFetched: true,
			// SandwichTxCount:         slotToSandwichTxCount[blk.Slot],
			// SandwichCount:           slotToSandwichCount[blk.Slot],
			// SandwichVictimCount:     slotToSandwichVictimCount[blk.Slot],
			SandwichInBundleChecked: false,
		})
	}

	if err := ch.InsertSlotTxs(statuses); err != nil {
		return fmt.Errorf("InsertSlotTxsStatus failed: %w", err)
	}

	return nil
}
