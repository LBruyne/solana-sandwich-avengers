package cmd

import (
	"fmt"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/sol"

	"github.com/spf13/cobra"
)

var slotCmd = cobra.Command{
	Use:   "leader",
	Short: "Start monitoring, syncing and storing Slot information",
	Run: func(cmd *cobra.Command, args []string) {
		logger.InitLogs("leader")

		if slotStart < config.MIN_START_SLOT {
			logger.SolLogger.Error(fmt.Sprintf("start slot (%d) is below minimum allowed slot (%d)", slotStart, config.MIN_START_SLOT))
			return
		}
		// if endSlot > 0 && endSlot < config.MIN_START_SLOT {
		// 	logger.SolLogger.Error(fmt.Sprintf("end slot (%d) is below minimum allowed slot (%d)", endSlot, config.MIN_START_SLOT))
		// 	return
		// }
		// if endSlot > 0 && startSlot > endSlot {
		// 	logger.SolLogger.Error(fmt.Sprintf("start slot (%d) cannot be greater than end slot (%d)", startSlot, endSlot))
		// 	return
		// }

		logger.SolLogger.Info("Running cmd leader, starting slot information monitoring...", "start", slotStart)

		if err := sol.RunSlotLeaderCmd(slotStart); err != nil {
			logger.SolLogger.Error("Error running Slot command", "error", err)
		}
	},
}
