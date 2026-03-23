package jito

import (
	"sort"
	"time"
	"watcher/config"
	"watcher/db"
	"watcher/logger"
	"watcher/types"
	"watcher/utils"
)

// RunJitoCmd2 fetches recent Jito bundles in a loop and stores them in the database.
func RunJitoCmd2() error {
	// Initialize db
	ch := db.NewClickhouse()
	defer ch.Close()
	cache := utils.NewBundleCache()

	// First load existing bundles from DB into cache to avoid re-insertion/overlap
	latestBundleIds, err := ch.QueryLatestBundleIds(config.JITO_RECENT_FETCH_LIMIT)
	if err != nil {
		logger.JitoLogger.Error("Failed to query existing bundles", "err", err)
	} else {
		for _, b := range latestBundleIds {
			cache.Add(b)
		}
	}

	// Fetch recent bundles in a loop
	limit := config.JITO_RECENT_FETCH_LIMIT
	// firstQuery := true
	for {
		// Actual fetch recent bundles from Jito API
		bundles, err := GetRecentBundles(limit)
		if err != nil {
			logger.JitoLogger.Error("GetRecentBundles failed", "err", err)
			time.Sleep(config.JITO_RECENT_FETCH_INTERVAL)
			continue
		}
		actualFetched := len(bundles)

		if actualFetched == 0 {
			logger.JitoLogger.Warn("No bundles fetched", "limit", limit)
			time.Sleep(config.JITO_RECENT_FETCH_INTERVAL)
			continue
		}
		logger.JitoLogger.Info("Fetched recent bundles", "count", actualFetched, "current_limit", limit, "first_bundle_id", bundles[0].BundleId, "last_bundle_id", bundles[actualFetched-1].BundleId)

		// Parse timestamps and filter out invalid bundles
		validBundles := make(types.JitoBundles, 0, len(bundles))
		for _, b := range bundles {
			ts, err := time.Parse(time.RFC3339, b.Timestamp)
			if err != nil {
				logger.JitoLogger.Warn("Failed to parse timestamp, skipping bundle", "bundleId", b.BundleId, "err", err)
				continue
			}
			validBundles = append(validBundles, &types.JitoBundle{
				BundleId:          b.BundleId,
				Timestamp:         ts,
				Tippers:           b.Tippers,
				LandedTipLamports: b.LandedTipLamports,
				Transactions:      b.Transactions,
			})
		}

		if len(validBundles) == 0 {
			time.Sleep(config.JITO_RECENT_FETCH_INTERVAL)
			continue
		}

		// Sort the bundles by timestamp ascending
		sort.Slice(validBundles, func(i, j int) bool {
			return validBundles[i].Timestamp.Before(validBundles[j].Timestamp)
		})

		// Insert new bundles into DB
		toInsert := make(types.JitoBundles, 0, len(validBundles))
		for _, b := range validBundles {
			if cache.Has(b.BundleId) {
				continue
			}
			cache.Add(b.BundleId)
			toInsert = append(toInsert, b)
		}

		if len(toInsert) > 0 {
			err := ch.InsertJitoBundles(toInsert)
			if err != nil {
				logger.JitoLogger.Error("Insert JitoBundles failed", "err", err)
			} else {
				logger.JitoLogger.Info("Inserted bundles", "count", len(toInsert))
			}
		}

		// Dynamic limit adjustment
		actualInsertNum := len(toInsert)
		overlapNum := actualFetched - actualInsertNum
		// Report summary
		logger.JitoLogger.Info("Summary of Jito bundle query",
			"current_limit", limit,
			"cached_size", cache.Len(),
			"fetched", actualFetched,
			"overlap_with_before", overlapNum,
			"actual_insert", actualInsertNum,
			"next_limit", limit,
		)

		time.Sleep(config.JITO_RECENT_FETCH_INTERVAL)
	}
}
