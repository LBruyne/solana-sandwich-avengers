package jito

import (
	"sync"
	"sync/atomic"
	"time"
	"sandwich-detector/config"
	"sandwich-detector/db"
	"sandwich-detector/logger"
	"sandwich-detector/types"
	"sandwich-detector/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

// fetchSlotsParallel fetches Jito bundles for the given slots concurrently (one HTTP GET per slot).
// It skips slots already present (dedup via QuerySlotBundleBySlot — cheap local query, matters on
// resume), returns the parsed bundles plus one slot_bundles status per successfully-fetched slot,
// and the slots that hit a genuine (non-"Bundle not found") error so the caller can retry them.
func fetchSlotsParallel(ch db.Database, slots []uint64, parallel int) (types.JitoBundles, []*types.SlotBundlesStatus, []uint64) {
	var mu sync.Mutex
	allBundles := make(types.JitoBundles, 0, len(slots)*48)
	statuses := make([]*types.SlotBundlesStatus, 0, len(slots))
	var failed []uint64

	sem := make(chan struct{}, parallel)
	var wg sync.WaitGroup
	for _, slot := range slots {
		wg.Add(1)
		sem <- struct{}{}
		go func(s uint64) {
			defer wg.Done()
			defer func() { <-sem }()

			if n, err := ch.QuerySlotBundleBySlot(s); err == nil && n > 0 {
				return // already fetched
			}
			bundles, err := GetBundlesBySlot(s)
			if err != nil {
				logger.JitoLogger.Error("GetBundlesBySlot failed", "slot", s, "err", err)
				mu.Lock()
				failed = append(failed, s)
				mu.Unlock()
				return
			}
			valid := make(types.JitoBundles, 0, len(bundles))
			txCount := uint64(0)
			for _, b := range bundles {
				ts, perr := time.Parse(time.RFC3339, b.Timestamp)
				if perr != nil {
					logger.JitoLogger.Warn("Failed to parse timestamp, skipping bundle", "bundleId", b.BundleId, "err", perr)
					continue
				}
				valid = append(valid, &types.JitoBundle{
					Slot:              b.Slot,
					BundleId:          b.BundleId,
					Timestamp:         ts,
					Tippers:           b.Tippers,
					LandedTipLamports: b.LandedTipLamports,
					Transactions:      b.TxSignatures,
				})
				txCount += uint64(len(b.TxSignatures))
			}
			status := &types.SlotBundlesStatus{Slot: s, BundleFetched: true, BundleCount: uint64(len(valid)), BundleTxCount: txCount}
			mu.Lock()
			allBundles = append(allBundles, valid...)
			statuses = append(statuses, status)
			mu.Unlock()
		}(slot)
	}
	wg.Wait()
	return allBundles, statuses, failed
}

// RunJitoCmd fetches Jito bundles by slot starting from startSlot, and stores them in the database. Also scan sandwichTxs to mark inBundle.
// When endSlot > 0 the run is BOUNDED (backfill): the fetch task stops once past endSlot and the
// mark task stops once everything up to endSlot is marked, so RunJitoCmd returns instead of blocking
// forever. endSlot == 0 keeps the original unbounded (live) behavior.
func RunJitoCmd(startSlot, endSlot uint64, runFetchBundle bool, runSyncInBundle bool, fetchAhead bool) error {
	// Initialize db
	ch := db.NewClickhouse()
	defer ch.Close()

	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)

	logger.JitoLogger.Info("Starting Jito bundle fetcher", "start_slot", startSlot, "end_slot", endSlot)

	var runWg sync.WaitGroup

	// fetchDone lets the mark task know the fetch task has finished writing every slot up to
	// endSlot, so it doesn't terminate prematurely while fetch is still lagging behind. In mark-only
	// mode (no fetch task) it is pre-set so mark relies on the sandwich-frontier condition alone.
	var fetchDone atomic.Bool
	// fetchedThrough is the highest slot fetch has CONTIGUOUSLY written. The mark task drops an
	// epoch's jito_bundles partition only once fetchedThrough has passed that epoch's end, so a
	// throttled fetch lagging near an epoch boundary can never re-insert into a partition mark
	// already dropped. Mark-only mode assumes fetch already ran, so it is pre-set to the end.
	var fetchedThrough atomic.Uint64
	if !runFetchBundle {
		fetchDone.Store(true)
		fetchedThrough.Store(endSlot)
	}

	// Task 1: fetch bundles by slot, from startSlot — parallel in batches (one HTTP GET per slot is
	// too slow serially to keep up with the sandwich frontier).
	if runFetchBundle {
		runWg.Add(1)
		go func(start uint64) {
			defer runWg.Done()
			defer fetchDone.Store(true)
			s := start
			var ceiling uint64 // max slot we are allowed to fetch (sandwich frontier - safe lag)
			var failed []uint64
			contiguousEnd := start // highest slot+1 that is contiguously fully fetched (no gaps)
			for {
				if endSlot > 0 && s > endSlot {
					break
				}
				// Refresh the ceiling from the sandwich frontier so we never fetch slots the Jito API
				// hasn't indexed yet (and, in a bounded run, never past the detected range).
				// fetch-ahead: for a bounded HISTORICAL range every slot is already Jito-indexed, so
				// skip the frontier query and fetch straight through to endSlot (used to pre-fetch an
				// epoch range before sandwich detection reaches it).
				if fetchAhead {
					ceiling = endSlot
				} else if s >= ceiling {
					maxSw, err := ch.QueryMaxSandwichCheckedSlot()
					if err != nil {
						logger.JitoLogger.Error("QueryMaxSandwichCheckedSlot failed", "err", err)
						time.Sleep(config.JITO_CHECK_SANDWICH_INTERVAL)
						continue
					}
					if maxSw > config.JITO_FETCH_BUNDLE_SAFE_LAG {
						ceiling = maxSw - config.JITO_FETCH_BUNDLE_SAFE_LAG
					}
				}
				if s > ceiling {
					time.Sleep(config.JITO_CHECK_SANDWICH_INTERVAL)
					continue
				}

				batchEnd := s + uint64(config.JITO_FETCH_BATCH_NUM)
				if batchEnd > ceiling+1 {
					batchEnd = ceiling + 1
				}
				if endSlot > 0 && batchEnd > endSlot+1 {
					batchEnd = endSlot + 1
				}
				slots := make([]uint64, 0, batchEnd-s)
				for x := s; x < batchEnd; x++ {
					slots = append(slots, x)
				}
				// Fetch the batch, retrying its failures in-place with exponential backoff. Completing
				// each batch fully before advancing is what makes the mark task's mid-run
				// delete-after-mark DROP PARTITION safe: once fetch has passed an epoch, that epoch is
				// fully fetched, so a later retry can't re-insert into a partition mark already dropped.
				remaining := slots
				backoff := config.JITO_FETCH_RETRY_BACKOFF
				totalBundles := 0
				for attempt := 0; attempt < config.JITO_FETCH_MAX_ATTEMPTS && len(remaining) > 0; attempt++ {
					if attempt > 0 {
						logger.JitoLogger.Warn("Jito 403/transient on batch, backing off", "remaining", len(remaining), "backoff", backoff.String())
						time.Sleep(backoff)
						backoff *= 2
					}
					bundles, statuses, batchFailed := fetchSlotsParallel(ch, remaining, config.JITO_FETCH_PARALLEL_NUM)
					if err := ch.InsertJitoBundles(bundles); err != nil {
						logger.JitoLogger.Error("InsertJitoBundles failed", "err", err)
					}
					if err := ch.InsertSlotBundles(statuses); err != nil {
						logger.JitoLogger.Error("InsertSlotBundles failed", "err", err)
					}
					totalBundles += len(bundles)
					remaining = batchFailed
				}
				failed = append(failed, remaining...)
				// Advance the contiguous-fetched frontier only when this batch fully succeeded AND it
				// abuts the previous one (no gap from an earlier deferred batch). This is what mark's
				// delete-after-mark gates on.
				if len(remaining) == 0 && s == contiguousEnd {
					contiguousEnd = batchEnd
					fetchedThrough.Store(batchEnd - 1)
				}
				logger.JitoLogger.Info("Fetched bundle batch", "start", s, "end", batchEnd-1, "slots", len(slots), "bundles", totalBundles, "unrecovered", len(remaining))
				s = batchEnd
			}

			// One retry pass for slots that hit a genuine (non-404) error, so a transient blip does
			// not leave a permanent gap that would block their inBundle marking.
			if len(failed) > 0 {
				logger.JitoLogger.Warn("Retrying failed bundle slots", "count", len(failed))
				bundles, statuses, stillFailed := fetchSlotsParallel(ch, failed, config.JITO_FETCH_PARALLEL_NUM)
				_ = ch.InsertJitoBundles(bundles)
				_ = ch.InsertSlotBundles(statuses)
				if len(stillFailed) > 0 {
					logger.JitoLogger.Error("Bundle slots still failing after retry", "count", len(stillFailed))
				}
			}
			// Everything up to endSlot is now written (the deferred slots have been re-fetched), so
			// let mark drop through to the final epoch.
			if endSlot > 0 {
				fetchedThrough.Store(endSlot)
			}
			logger.JitoLogger.Info("Fetch reached end slot, stopping", "end_slot", endSlot)
		}(startSlot)
	}

	// Task 2: scan sandwich txs to mark inBundle
	if runSyncInBundle {
		runWg.Add(1)
		go func() {
			defer runWg.Done()
			// delete-after-mark bookkeeping: highest epoch whose jito_bundles partition has been dropped.
			epochOf := func(slot uint64) uint64 { return slot / 432000 }
			lastDroppedEpoch := epochOf(startSlot) // epoch containing startSlot has not been dropped yet
			if lastDroppedEpoch > 0 {
				lastDroppedEpoch-- // so the first fully-marked epoch (>= start epoch) can be dropped
			}
			dropThrough := func(epoch uint64) { // drop all epoch partitions in (lastDroppedEpoch, epoch]
				// Never drop an epoch fetch hasn't contiguously finished — otherwise a throttled fetch
				// still writing that epoch's tail would re-insert into a just-dropped partition.
				if ft := fetchedThrough.Load(); ft > 0 {
					if maxFetched := epochOf(ft+1); maxFetched >= 1 && epoch > maxFetched-1 {
						epoch = maxFetched - 1
					}
				}
				for e := lastDroppedEpoch + 1; e <= epoch; e++ {
					if err := ch.DropJitoBundlesEpochPartition(e); err != nil {
						logger.JitoLogger.Warn("DropJitoBundlesEpochPartition failed", "epoch", e, "err", err)
					} else {
						logger.JitoLogger.Info("Dropped jito_bundles epoch partition (delete-after-mark)", "epoch", e)
					}
				}
				if epoch > lastDroppedEpoch {
					lastDroppedEpoch = epoch
				}
			}

			for {
				// Find the first (oldest) slot in sandwich_txs, that has already checked sandwich, but not yet checked inBundle and bundles have been fetched.
				slots, err := ch.QuerySlotsToCheckInBundle(config.JITO_MARK_IN_BUNDLE_SLOT_NUM, config.JITO_MARK_IN_BUNDLE_SAFE_LAG)
				if err != nil {
					logger.JitoLogger.Error("QuerySlotsToCheckInBundle failed", "err", err)
					time.Sleep(config.JITO_MARK_IN_BUNDLE_SANDWICH_TX_INTERVAL)
					continue
				}
				if len(slots) == 0 || slots[0] < config.MIN_START_SLOT {
					// Bounded run: terminate once the sandwich backfill has advanced past
					// endSlot + mark safe-lag (so every slot <= endSlot is within the mark gate)
					// and nothing is left to mark. Gating on fetchDone is essential: without it, mark
					// can see a transiently-empty result while fetch still lags, quit early, and drop
					// partitions that fetch then re-inserts into. Drop any remaining marked epochs first.
					if endSlot > 0 && fetchDone.Load() {
						maxSw, mErr := ch.QueryMaxSandwichCheckedSlot()
						if mErr == nil && maxSw >= endSlot+config.JITO_MARK_IN_BUNDLE_SAFE_LAG {
							dropThrough(epochOf(endSlot))
							logger.JitoLogger.Info("Mark reached end slot, all marked, stopping", "end_slot", endSlot, "max_sandwich", maxSw)
							return
						}
					}
					logger.JitoLogger.Info("No slot needs sandwich check, sleep and retry", "sleep", config.JITO_MARK_IN_BUNDLE_SANDWICH_TX_INTERVAL.String())
					time.Sleep(config.JITO_MARK_IN_BUNDLE_SANDWICH_TX_INTERVAL)
					continue
				}
				logger.JitoLogger.Info("Checking sandwich_txs in bundle", "slot_start", slots[0], "slot_end", slots[len(slots)-1], "num_slots", len(slots))

				// For that slot, query all bundle txs
				bundleTxsMap, err := ch.QueryBundleTxsBySlots(slots)
				if err != nil {
					logger.JitoLogger.Error("QueryBundleTxsBySlots failed", "err", err)
					continue
				}
				// For that slot, query all sandwich txs
				swTxsMap, err := ch.QuerySandwichTxsBySlots(slots)
				if err != nil {
					logger.JitoLogger.Error("QuerySandwichTxsBySlots failed", "err", err)
					continue
				}

				results := make([]types.JitoBundleMarkResult, 0, len(slots))
				var mu sync.Mutex
				var wg sync.WaitGroup
				sem := make(chan struct{}, config.JITO_MARK_IN_BUNDLE_PARALLEL_NUM)

				for _, slot := range slots {
					wg.Add(1)
					sem <- struct{}{} // acquire
					go func(slot uint64) {
						defer wg.Done()
						defer func() { <-sem }() // release

						bundleTxs := bundleTxsMap[slot]
						swTxs := swTxsMap[slot]
						if len(bundleTxs) == 0 || len(swTxs) == 0 {
							return
						}
						hit := intersectTxs(bundleTxs, swTxs)
						if len(hit) > 0 {
							mu.Lock()
							results = append(results, types.JitoBundleMarkResult{Slot: slot, Hits: hit})
							mu.Unlock()
						}
						// logger.JitoLogger.Info("Checked slot", "slot", slot, "bundle_txs", len(bundleTxs), "sandwich_txs", len(swTxs), "hits", len(hit))
					}(slot)
				}
				wg.Wait()

				if len(results) > 0 {
					logger.JitoLogger.Info("Batch updating inBundle flags", "num_slots_with_sandwich", len(results), "total_hits", func() int {
						total := 0
						for _, r := range results {
							total += len(r.Hits)
						}
						return total
					}())
					if err := ch.UpdateSandwichTxsInBundle(results); err != nil {
						logger.JitoLogger.Error("UpdateSandwichTxsInBundle failed", "err", err)
						continue
					}
				}

				if err := ch.UpdateSlotTxsCheckInBundle(slots, true); err != nil {
					logger.JitoLogger.Error("UpdateSlotTxsCheckInBundle failed", "err", err)
					continue
				}
				logger.JitoLogger.Info("Finished batch check", "start_slot", slots[0], "end_slot", slots[len(slots)-1], "num_slots", len(slots))

				// delete-after-mark: every epoch strictly below the one we just marked into is now
				// fully checked (marking is ascending and gated behind the fetch frontier), so its
				// raw bundles can be dropped. Fetch is always >= JITO_FETCH_BUNDLE_SAFE_LAG slots
				// ahead, so this never races the fetch task writing higher epochs.
				markedEpoch := epochOf(slots[len(slots)-1])
				if markedEpoch > 0 {
					dropThrough(markedEpoch - 1)
				}
			}
		}()
	}

	// Bounded run (endSlot>0): return when both tasks finish. Unbounded (live, endSlot==0): the
	// goroutines loop forever, so this blocks like the original select{}.
	runWg.Wait()
	return nil
}

func intersectTxs(txsA, txsB []string) []string {
	setA := MapSet.NewSet[string]()
	for _, tx := range txsA {
		setA.Add(tx)
	}
	setB := MapSet.NewSet[string]()
	for _, tx := range txsB {
		setB.Add(tx)
	}
	intersection := setA.Intersect(setB)
	return intersection.ToSlice()
}
