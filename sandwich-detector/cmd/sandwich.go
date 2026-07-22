package cmd

import (
	"fmt"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/sol"

	"github.com/spf13/cobra"
)

var sandwichCmd = cobra.Command{
	Use:   "sandwich",
	Short: "Start monitoring, syncing and storing Sandwich information",
	Run: func(cmd *cobra.Command, args []string) {
		logger.InitLogs("sandwich")

		if sandwichStart < config.MIN_START_SLOT {
			logger.SolLogger.Error(fmt.Sprintf("start slot (%d) is below minimum allowed slot (%d)", sandwichStart, config.MIN_START_SLOT))
			return
		}

		switch sandwichMode {
		case "backfill":
			if sandwichEnd < sandwichStart {
				logger.SolLogger.Error(fmt.Sprintf("backfill requires --end-slot (%d) >= --slot (%d)", sandwichEnd, sandwichStart))
				return
			}
			logger.SolLogger.Info("Running cmd sandwich in backfill mode", "start", sandwichStart, "end", sandwichEnd, "rps", sandwichRPS)
			if err := sol.RunBackfillCmd(sandwichStart, sandwichEnd, sandwichRPS); err != nil {
				logger.SolLogger.Error("Error running Sandwich backfill", "error", err)
			}
		case "live":
			logger.SolLogger.Info("Running cmd sandwich in live mode", "start", sandwichStart)
			if err := sol.RunSandwichCmd(sandwichStart); err != nil {
				logger.SolLogger.Error("Error running Sandwich command", "error", err)
			}
		default:
			logger.SolLogger.Error(fmt.Sprintf("unknown --mode %q (want 'live' or 'backfill')", sandwichMode))
		}
	},
}
