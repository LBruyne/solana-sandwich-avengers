package cmd

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"sandwich-detector/db"
	"sandwich-detector/logger"

	"github.com/spf13/cobra"
)

var resetCmd = cobra.Command{
	Use:   "reset",
	Short: "Drop every table the detector creates (destructive)",
	Long: "Drops jito_bundles, slot_bundles, slot_leaders, slot_txs, sandwiches and sandwich_txs\n" +
		"in the configured database. Tables are recreated empty on the next run, so this discards\n" +
		"every detection result. Asks for confirmation unless --yes is given.",
	Run: func(cmd *cobra.Command, args []string) {
		ch := db.NewClickhouse()
		defer ch.Close()

		// A mistyped command here throws away however many months of detection the database
		// holds, and the tables come back empty on the next start with no sign anything was
		// lost. Confirm against the database name so the operator sees which one they are about
		// to clear.
		if !resetAssumeYes && !confirmReset(ch.DatabaseName()) {
			logger.GlobalLogger.Info("Reset aborted.")
			return
		}

		logger.GlobalLogger.Info("Dropping tables in database...")
		if err := ch.DropTables(); err != nil {
			logger.GlobalLogger.Error("Failed to drop tables", "err", err)
			os.Exit(1)
		}
		logger.GlobalLogger.Info("Done.")
	},
}

// confirmReset prompts on the terminal and accepts only the exact database name. A bare "yes"
// is too easy to type into the wrong terminal.
func confirmReset(dbName string) bool {
	fmt.Printf("This will DROP every sandwich-detector table in database %q.\n", dbName)
	fmt.Printf("Type the database name to confirm: ")
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil {
		return false
	}
	return strings.TrimSpace(line) == dbName
}
