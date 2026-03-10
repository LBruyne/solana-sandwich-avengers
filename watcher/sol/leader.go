package sol

import (
	"fmt"
	"time"
	"watcher/config"
	"watcher/db"
	"watcher/logger"
	"watcher/utils"
)

func RunSlotLeaderCmd(startSlot uint64) error {
	// Initialize db
	ch := db.NewClickhouse()
	defer ch.Close()

	// Fetch last slot leaders recorded in DB
	lastSlotInDB, err := ch.QueryLastSlotLeader()
	if err != nil {
		return fmt.Errorf("failed to query last slot leader: %w", err)
	}
	logger.SolLogger.Info("Last slot leader in DB", "slot", lastSlotInDB)
	// Fetch current slot from Solana RPC
	currentSolanaSlot, err := GetCurrentSlot()
	if err != nil {
		return fmt.Errorf("failed to get current slot: %w", err)
	}
	logger.SolLogger.Info("Current slot from Solana RPC", "slot", currentSolanaSlot)
	logger.SolLogger.Info("Fetch slot leaders:", "start_from_input", startSlot, "last_in_DB", lastSlotInDB, "current_from_rpc", currentSolanaSlot)
	// Use startSlot, lastSlot, currentSlot to determine the starting point:
	// 1. if startSlot < lastSlotInDB, set startSlot = lastSlotInDB + 1
	startSlot = max(startSlot, lastSlotInDB+1)
	startSlot = utils.AlignSlotToStep(startSlot, config.PER_LEADER_SLOT)
	if startSlot > currentSolanaSlot {
		logger.SolLogger.Warn("Start slot is greater than current slot, nothing to do", "start", startSlot, "current", currentSolanaSlot)
		return nil
	}
	// 2. if currentSlot - startSlot < max gap, fetch start from startSlot,
	// otherwise, jump to currentSlot directly
	if startSlot+config.SOL_FETCH_SLOT_LEADER_MAX_GAP > currentSolanaSlot {
		// Sync from startSlot
		logger.SolLogger.Info("Syncing slot leaders", "start", startSlot)
		for {
			currentSlotFromRpc, err := GetCurrentSlot()
			if err != nil {
				logger.SolLogger.Error("Failed to get current slot", "err", err)
				continue
			}

			var limit uint64
			if startSlot+config.SOL_FETCH_SLOT_LEADER_LIMIT >= currentSlotFromRpc {
				limit = currentSlotFromRpc - startSlot + 1
				logger.SolLogger.Info("Fetch slot leaders (reaching current slot),", "start", startSlot, "current", currentSlotFromRpc, "limit", limit)
				leaders, err := GetSlotLeaders(startSlot, limit)
				if err != nil {
					logger.SolLogger.Error("Failed to get slot leaders", "start", startSlot, "limit", limit, "err", err)
					continue
				}

				err = ch.InsertSlotLeaders(leaders)
				if err != nil {
					logger.SolLogger.Error("Failed to insert slot leaders", "err", err)
					continue
				} else {
					logger.SolLogger.Info("Inserted slot leaders", "count", len(leaders), "start", startSlot, "limit", limit)
				}
				startSlot += uint64(len(leaders))
				break
			}

			limit = config.SOL_FETCH_SLOT_LEADER_LIMIT
			logger.SolLogger.Info("Fetch slot leaders", "start", startSlot, "current", currentSlotFromRpc, "limit", limit)
			leaders, err := GetSlotLeaders(startSlot, limit)
			if err != nil {
				logger.SolLogger.Error("Failed to get slot leaders", "start", startSlot, "limit", limit, "err", err)
				continue
			}
			err = ch.InsertSlotLeaders(leaders)
			if err != nil {
				logger.SolLogger.Error("Failed to insert slot leaders", "err", err)
				continue
			} else {
				logger.SolLogger.Info("Inserted slot leaders", "count", len(leaders), "start", startSlot, "limit", limit)
			}
			startSlot += uint64(len(leaders))
			// Sleep a while to avoid hitting rate limit
			time.Sleep(config.SOL_FETCH_SLOT_LEADER_SHORT_INTERVAL)
		}
	} else {
		startSlot = currentSolanaSlot
		logger.SolLogger.Info("Start slot too far behind current slot, start from current slot", "start", startSlot)
	}

	// Sync from currentSlot
	logger.SolLogger.Info("Syncing slot leaders, starting from current slot", "current", currentSolanaSlot)
	for {
		currentSlot, err := GetCurrentSlot()
		if err != nil {
			logger.SolLogger.Error("Failed to get current slot", "err", err)
			continue
		}

		if currentSlot-startSlot < config.SOL_FETCH_SLOT_LEADER_LOWER {
			logger.SolLogger.Info("Not enough new slots, sleep and retry after "+config.SOL_FETCH_SLOT_LEADER_LONG_INTERVAL.String(), "start", startSlot, "current", currentSlot)
			time.Sleep(config.SOL_FETCH_SLOT_LEADER_LONG_INTERVAL)
			continue
		}

		limit := currentSlot - startSlot + 1
		// Limit should not exceed SOL_FETCH_SLOT_LIMIT
		if limit > config.SOL_FETCH_SLOT_LEADER_LIMIT {
			logger.SolLogger.Warn("Slot gap too large, capping limit to SOL_FETCH_SLOT_LIMIT", "calculatedLimit", limit, "cappedLimit", config.SOL_FETCH_SLOT_LEADER_LIMIT)
			limit = config.SOL_FETCH_SLOT_LEADER_LIMIT
		}

		logger.SolLogger.Info("Fetch slot leaders", "start", startSlot, "current", currentSlot, "limit", limit)
		leaders, err := GetSlotLeaders(startSlot, limit)
		if err != nil {
			logger.SolLogger.Error("Failed to get slot leaders", "start", startSlot, "limit", limit, "err", err)
			continue
		}
		err = ch.InsertSlotLeaders(leaders)
		if err != nil {
			logger.SolLogger.Error("Failed to insert slot leaders", "err", err)
			continue
		} else {
			logger.SolLogger.Info("Inserted slot leaders", "count", len(leaders), "start", startSlot, "limit", limit)
		}
		startSlot += uint64(len(leaders))
		// Sleep a while
		logger.SolLogger.Info("Sleeping for "+config.SOL_FETCH_SLOT_LEADER_LONG_INTERVAL.String(), "next_start", startSlot)
		time.Sleep(config.SOL_FETCH_SLOT_LEADER_LONG_INTERVAL)
	}
}
