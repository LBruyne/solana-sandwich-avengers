package cmd

import (
	"sandwich-detector/db"
	"sandwich-detector/logger"

	"github.com/spf13/cobra"
)

var resetCmd = cobra.Command{
	Use:   "reset",
	Short: "Reset everything",
	Run: func(cmd *cobra.Command, args []string) {
		ch := db.NewClickhouse()
		defer ch.Close()

		// Drop tables
		logger.GlobalLogger.Info("Dropping tables in database...")
		if err := ch.DropTables(); err != nil {
			logger.GlobalLogger.Error("Failed to drop tables", "err", err)
		}
		logger.GlobalLogger.Info("Done.")
	},
}
