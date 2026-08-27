package sol

import (
	"fmt"
	"sandwich-detector/config"
	"sandwich-detector/db"
	"sandwich-detector/logger"
	"sandwich-detector/types"
	"sandwich-detector/utils"
	"sort"
	"sync"
	"time"
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

		// Process blocks over sliding double-rotation windows and persist. Live streaming defers
		// the tail rotation so a sandwich straddling the not-yet-fetched next rotation is caught.
		processAndStore(blocks, "live", true)

		// Update next start slot
		startSlot += uint64(numToFetch)
		// Sleep a while
		logger.SolLogger.Info("Sleeping for "+config.SOL_FETCH_SLOT_DATA_SHORT_INTERVAL.String(), "next_start", startSlot)
		time.Sleep(config.SOL_FETCH_SLOT_DATA_SHORT_INTERVAL)
	}
}

// leaderRotation is one leader's turn: a maximal run of same-leader blocks whose slots are
// adjacent (small skips tolerated). Sliding windows are built by pairing each rotation with
// its successor so a sandwich straddling two adjacent leaders is caught.
type leaderRotation struct {
	blocks    types.Blocks
	leader    string
	startSlot uint64
	endSlot   uint64
	hasNew    bool // contains a slot fetched in this batch
	slotSet   map[uint64]struct{}
}

// seenSandwichIDs suppresses re-emitting a sandwich that a previous, overlapping window
// already reported (windows slide by one rotation and are re-checked as the frontier grows).
var seenSandwichIDs = NewAMMPoolLRU(config.SEEN_SANDWICH_CACHE_SIZE)

// ProcessBlocksForSandwich detects sandwiches over sliding double-rotation windows and returns
// them as a single list (in-block, same-leader cross-block and cross-leader alike, tagged by
// the CrossBlock/CrossLeader fields). rpcSource labels the data origin. When deferTail is true
// the final rotation is left for the next batch (live streaming, where later slots are still
// arriving); backfill passes false to flush every rotation.
func ProcessBlocksForSandwich(blocks types.Blocks, rpcSource string, deferTail bool) []*types.CrossBlockSandwich {
	if len(blocks) == 0 {
		return nil
	}

	// Slide the cache forward with the newly fetched blocks.
	for _, b := range blocks {
		if b != nil {
			crossBlockCache.Put(b)
		}
	}

	// Snapshot the cache as unique blocks sorted by slot.
	slotToBlock := make(map[uint64]*types.Block)
	for _, b := range crossBlockCache.AllBlocks() {
		if b != nil {
			slotToBlock[b.Slot] = b
		}
	}
	sorted := make(types.Blocks, 0, len(slotToBlock))
	for _, b := range slotToBlock {
		sorted = append(sorted, b)
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Slot < sorted[j].Slot })
	if len(sorted) == 0 {
		return nil
	}

	// Slots fetched in this batch, and the latest one.
	newSlots := make(map[uint64]struct{}, len(blocks))
	var latestNewSlot uint64
	for _, b := range blocks {
		if b != nil {
			newSlots[b.Slot] = struct{}{}
			if b.Slot > latestNewSlot {
				latestNewSlot = b.Slot
			}
		}
	}
	if len(newSlots) == 0 {
		return nil
	}

	// Resolve leaders (memoized) and build a slot->leader map for cross-leader classification.
	leaderCache := make(map[uint64]string, len(sorted))
	leaderKnown := make(map[uint64]bool, len(sorted))
	getLeader := func(slot uint64) (string, bool) {
		if known, ok := leaderKnown[slot]; ok {
			return leaderCache[slot], known
		}
		l, err := crossBlockCache.GetSlotLeader(slot)
		if err != nil {
			leaderKnown[slot], leaderCache[slot] = false, ""
			return "", false
		}
		leaderKnown[slot], leaderCache[slot] = true, l
		return l, true
	}
	leaderBySlot := make(map[uint64]string, len(sorted))
	for _, b := range sorted {
		if l, ok := getLeader(b.Slot); ok {
			leaderBySlot[b.Slot] = l
		}
	}

	rotations := buildLeaderRotations(sorted, newSlots, getLeader)
	if len(rotations) == 0 {
		return nil
	}

	// Build detection windows: rotation i paired with its slot-adjacent successor (double
	// rotation), or alone when there is no adjacent successor. Process a window only when its
	// rotation or successor carries a new slot; defer the tail rotation in live mode.
	type window struct {
		txs     types.Transactions
		leftSet map[uint64]struct{} // left rotation's slots — owns sandwiches whose front sits here
		start   uint64
		end     uint64
	}
	windows := make([]window, 0, len(rotations))
	for i, rot := range rotations {
		hasSucc := i+1 < len(rotations) && rotations[i+1].startSlot <= rot.endSlot+config.PER_LEADER_SLOT
		successorHasNew := hasSucc && rotations[i+1].hasNew
		if !rot.hasNew && !successorHasNew {
			continue
		}
		if deferTail && !hasSucc && rot.endSlot == latestNewSlot {
			logger.SolLogger.Info("Defer tail leader rotation", "start", rot.startSlot, "end", rot.endSlot, "leader", rot.leader)
			continue
		}

		txs := make(types.Transactions, 0, len(rot.blocks)*8)
		for _, b := range rot.blocks {
			txs = append(txs, b.Txs...)
		}
		end := rot.endSlot
		if hasSucc {
			for _, b := range rotations[i+1].blocks {
				txs = append(txs, b.Txs...)
			}
			end = rotations[i+1].endSlot
		}
		windows = append(windows, window{txs: txs, leftSet: rot.slotSet, start: rot.startSlot, end: end})
	}
	if len(windows) == 0 {
		return nil
	}

	// Detect per window in parallel. Keep only sandwiches owned by this window's left rotation
	// (front-run there) so an overlapping same-leader sandwich is emitted exactly once, and drop
	// any sandwichId already reported in an earlier batch.
	parallel := config.SOL_PROCESS_CROSS_BLOCK_SANDWICH_PARALLEL_NUM
	if parallel > len(windows) {
		parallel = len(windows)
	}
	if parallel < 1 {
		parallel = 1
	}

	windowsQueue := make(chan window, len(windows))
	for _, w := range windows {
		windowsQueue <- w
	}
	close(windowsQueue)

	result := make([]*types.CrossBlockSandwich, 0)
	var mu sync.Mutex
	var processWg sync.WaitGroup
	processWg.Add(parallel)
	for range parallel {
		go func() {
			defer processWg.Done()
			for w := range windowsQueue {
				logger.SolLogger.Info("Checking sandwich window", "start", w.start, "end", w.end, "num_txs", len(w.txs))
				finder := NewSandwichFinder(w.txs, leaderBySlot, config.CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, rpcSource, nil)
				finder.Find()
				for _, s := range finder.Sandwiches {
					// Ownership: the window whose left rotation holds the front-run keeps it.
					if _, own := w.leftSet[s.FrontRun[0].Slot]; !own {
						continue
					}
					mu.Lock()
					if !seenSandwichIDs.Contains(s.SandwichID) {
						seenSandwichIDs.Add(s.SandwichID)
						result = append(result, s)
					}
					mu.Unlock()
				}
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

// buildLeaderRotations groups slot-sorted blocks into leader rotations, breaking on a leader
// change, an unknown leader, or a slot gap wider than one leader's span.
func buildLeaderRotations(sorted types.Blocks, newSlots map[uint64]struct{}, getLeader func(uint64) (string, bool)) []leaderRotation {
	rotations := make([]leaderRotation, 0)
	i := 0
	for i < len(sorted) {
		leader, known := getLeader(sorted[i].Slot)
		rot := leaderRotation{leader: leader, startSlot: sorted[i].Slot, slotSet: make(map[uint64]struct{})}
		j := i
		for j < len(sorted) {
			if j > i {
				l, k := getLeader(sorted[j].Slot)
				if !known || !k || l != leader || sorted[j].Slot > sorted[j-1].Slot+config.PER_LEADER_SLOT {
					break
				}
			}
			rot.blocks = append(rot.blocks, sorted[j])
			rot.slotSet[sorted[j].Slot] = struct{}{}
			rot.endSlot = sorted[j].Slot
			if _, ok := newSlots[sorted[j].Slot]; ok {
				rot.hasNew = true
			}
			j++
		}
		rotations = append(rotations, rot)
		i = j
	}
	return rotations
}

// FindInBlockSandwiches runs the unified finder over a single block. Kept for tests and the
// offline scan harness; every result is in-block (CrossBlock=false) since the window is one slot.
func FindInBlockSandwiches(b *types.Block) []*types.CrossBlockSandwich {
	finder := NewSandwichFinder(b.Txs, nil, config.INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "live", nil)
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

// processAndStore runs windowed detection over the batch and persists the sandwiches and the
// per-slot status. Shared by the live and backfill runners.
func processAndStore(blocks types.Blocks, rpcSource string, deferTail bool) {
	timeProcess := time.Now()
	sandwiches := ProcessBlocksForSandwich(blocks, rpcSource, deferTail)
	logger.SolLogger.Info("Processed slot data", "num_blocks", len(blocks), "num_sandwiches", len(sandwiches), "process_time", time.Since(timeProcess).String())

	if err := StoreSandwichesToDB(ch, sandwiches); err != nil {
		logger.SolLogger.Error("Failed to store sandwiches to DB", "err", err)
	}
	if err := StoreSlotSandwichStatusToDB(ch, blocks, sandwiches); err != nil {
		logger.SolLogger.Error("Failed to store slot sandwich status to DB", "err", err)
	}
}

func StoreSandwichesToDB(ch db.Database, sandwiches []*types.CrossBlockSandwich) error {
	if len(sandwiches) == 0 {
		return nil
	}
	if err := ch.InsertSandwiches(sandwiches); err != nil {
		return fmt.Errorf("failed to insert sandwiches to DB: %w", err)
	}
	logger.SolLogger.Info("Inserted sandwiches to DB", "num", len(sandwiches))

	sandwichTxToInsert := make([]*types.SandwichTx, 0)
	for _, s := range sandwiches {
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
		return fmt.Errorf("failed to insert sandwich txs to DB: %w", err)
	}
	logger.SolLogger.Info("Inserted sandwich txs to DB", "num", len(sandwichTxToInsert))
	return nil
}

func StoreSlotSandwichStatusToDB(ch db.Database, blks types.Blocks, sandwiches []*types.CrossBlockSandwich) error {
	// Per-slot sandwich counts are derived from the sandwiches table when needed rather than
	// denormalised here, so slot_txs stays a pure record of what was fetched.
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
