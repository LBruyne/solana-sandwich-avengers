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

		logger.SolLogger.Info("Running cmd sandwich, starting sandwich monitoring...", "start", sandwichStart)

		if err := sol.RunSandwichCmd(sandwichStart); err != nil {
			logger.SolLogger.Error("Error running Sandwich command", "error", err)
		}
	},
}
