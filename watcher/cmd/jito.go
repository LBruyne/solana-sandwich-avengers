package cmd

import (
	"fmt"
	"watcher/config"
	"watcher/jito"
	"watcher/logger"

	"github.com/spf13/cobra"
)

var jitoCmd = cobra.Command{
	Use:   "jito",
	Short: "Start monitoring, sycning and storing Jito bundles, including marking sandwiches in Jito bundles",
	Run: func(cmd *cobra.Command, args []string) {
		logger.InitLogs("jito")

		if jitoStart < config.MIN_START_SLOT {
			logger.JitoLogger.Error(fmt.Sprintf("start slot (%d) is below minimum allowed slot (%d)", jitoStart, config.MIN_START_SLOT))
			return
		}

		runFetchBundle := true
		runSyncInBundle := true

		if jitoFetchBundleOnly && jitoSyncInBundle {
			logger.JitoLogger.Error("--fetch-bundle-only and --sync-in-bundle cannot be used together")
			return
		}

		switch {
		case jitoFetchBundleOnly:
			runFetchBundle = true
			runSyncInBundle = false
		case jitoSyncInBundle:
			runFetchBundle = false
			runSyncInBundle = true
		default:
			// Default: run both tasks.
			runFetchBundle = true
			runSyncInBundle = true
		}

		logger.JitoLogger.Info(
			"Running cmd jito",
			"start_slot", jitoStart,
			"run_fetch_bundle", runFetchBundle,
			"run_sync_in_bundle", runSyncInBundle,
		)

		if err := jito.RunJitoCmd(jitoStart, runFetchBundle, runSyncInBundle); err != nil {
			logger.JitoLogger.Error("Error running Jito command", "error", err)
		}
	},
}
