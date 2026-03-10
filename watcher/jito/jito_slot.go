package jito

import (
	"fmt"
	"sync"
	"time"
	"watcher/config"
	"watcher/db"
	"watcher/logger"
	"watcher/sol"
	"watcher/types"
	"watcher/utils"

	MapSet "github.com/deckarep/golang-set/v2"
)

// RunJitoCmd fetches Jito bundles by slot starting from startSlot, and stores them in the database. Also scan sandwichTxs to mark inBundle.
func RunJitoCmd(startSlot uint64, runFetchBundle bool, runSyncInBundle bool) error {
	// Initialize db
	ch := db.NewClickhouse()
	defer ch.Close()

	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)

	// Fetch current slot from Solana RPC
	solanaSlot, err := sol.GetCurrentSlot()
	if err != nil {
		return fmt.Errorf("GetCurrentSlot failed: %w", err)
	}
	if startSlot > solanaSlot {
		logger.SolLogger.Warn("Start slot is greater than current slot, nothing to do", "start", startSlot, "current", solanaSlot)
		return nil
	}
	logger.JitoLogger.Info("Starting Jito bundle fetcher", "start_slot", startSlot, "current_remote_slot", solanaSlot)

	// Task 1: fetch bundles by slot, from startSlot
	if runFetchBundle {
		go func(start uint64, solS uint64) {
			s := start
			for {
				// Check if slot s has already been fetched
				n, err := ch.QuerySlotBundleBySlot(s)
				if err != nil {
					logger.JitoLogger.Error("QuerySlotBundleBySlot failed", "slot", s, "err", err)
					continue
				}

				if n > 0 {
					logger.JitoLogger.Info("Slot already fetched, skip", "slot", s)
					s++
					continue
				}

				// Fetch current slot from Solana RPC when approaching head
				if s >= solS {
					cur, err := sol.GetCurrentSlot()
					if err != nil {
						logger.JitoLogger.Error("GetCurrentSlot failed", "err", err)
						continue
					}
					solS = cur
					time.Sleep(config.JITO_CHECK_SANDWICH_INTERVAL)
				}
				if s > solS {
					logger.JitoLogger.Info("Reached current slot, sleep and retry", "slot", s, "current", solS)
					time.Sleep(config.JITO_CHECK_SANDWICH_INTERVAL)
					continue
				}

				bundles, err := GetBundlesBySlot(s)
				if err != nil {
					logger.JitoLogger.Error("GetBundlesBySlot failed", "slot", s, "err", err)
					continue
				}

				// Parse timestamps and filter out invalid bundles
				validBundles := make(types.JitoBundles, 0, len(bundles))
				txCount := uint64(0)
				for _, b := range bundles {
					ts, err := time.Parse(time.RFC3339, b.Timestamp)
					if err != nil {
						logger.JitoLogger.Warn("Failed to parse timestamp, skipping bundle", "bundleId", b.BundleId, "err", err)
						continue
					}
					validBundles = append(validBundles, &types.JitoBundle{
						Slot:              b.Slot,
						BundleId:          b.BundleId,
						Timestamp:         ts,
						Tippers:           b.Tippers,
						LandedTipLamports: b.LandedTipLamports,
						Transactions:      b.TxSignatures,
					})
					txCount += uint64(len(b.TxSignatures))
				}

				// Insert new bundles into DB
				if err := ch.InsertJitoBundles(validBundles); err != nil {
					logger.JitoLogger.Error("InsertJitoBundles failed", "err", err)
				}
				logger.JitoLogger.Info("Inserted bundles", "count", len(validBundles), "slot", s)

				// Update slot_bundle status
				status := types.SlotBundlesStatus{
					Slot:          s,
					BundleFetched: len(validBundles) > 0,
					BundleCount:   uint64(len(validBundles)),
					BundleTxCount: txCount,
				}

				if err := ch.InsertSlotBundles([]*types.SlotBundlesStatus{&status}); err != nil {
					logger.JitoLogger.Error("InsertSlotBundles failed", "slot", s, "err", err)
				} else {
					logger.JitoLogger.Info("Slot bundle status updated", "slot", s, "bundle_count", len(validBundles), "tx_count", txCount)
				}
				s++
			}
		}(startSlot, solanaSlot)
	}

	// Task 2: scan sandwich txs to mark inBundle
	if runSyncInBundle {
		go func() {
			for {
				// Find the first (oldest) slot in sandwich_txs, that has already checked sandwich, but not yet checked inBundle and bundles have been fetched.
				slots, err := ch.QuerySlotsToCheckInBundle(config.JITO_MARK_IN_BUNDLE_SLOT_NUM)
				if err != nil {
					logger.JitoLogger.Error("QuerySlotsToCheckInBundle failed", "err", err)
					time.Sleep(config.JITO_MARK_IN_BUNDLE_SANDWICH_TX_INTERVAL)
					continue
				}
				if len(slots) == 0 || slots[0] < config.MIN_START_SLOT {
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
			}
		}()
	}

	select {}
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
