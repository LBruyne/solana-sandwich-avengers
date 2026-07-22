package cmd

import (
	"fmt"
	"sandwich-detector/config"
	"sandwich-detector/logger"

	"github.com/spf13/cobra"
)

var RootCmd = &cobra.Command{
	Use:   "sandwich-detector",
	Short: "Detect, enrich, and persist sandwich attacks on Solana",
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		logger.SetConsoleEnabled(!notToStdout)
	},
}

// Flags
var jitoStart uint64
var slotStart uint64
var sandwichStart uint64
var sandwichEnd uint64
var sandwichMode string
var sandwichRPS int
var notToStdout bool
var jitoFetchBundleOnly bool
var jitoSyncInBundle bool

func init() {

	RootCmd.PersistentFlags().BoolVarP(
		&notToStdout,
		"no-stdout",
		"t",
		false,
		"Do not write logs to stdout (terminal) output (default false)",
	)

	jitoCmd.Flags().Uint64VarP(
		&jitoStart,
		"slot",
		"s",
		0,
		fmt.Sprintf("(Optional) starting slot number (>=%d)", config.MIN_START_SLOT),
	)

	jitoCmd.Flags().BoolVar(
		&jitoFetchBundleOnly,
		"fetch-bundle-only",
		false,
		"Only fetch/sync Jito bundles by slot",
	)

	jitoCmd.Flags().BoolVar(
		&jitoSyncInBundle,
		"sync-in-bundle",
		false,
		"Only sync sandwich inBundle marks",
	)

	slotCmd.Flags().Uint64VarP(
		&slotStart,
		"slot",
		"s",
		0,
		fmt.Sprintf("(Optional) starting slot number (>=%d)", config.MIN_START_SLOT),
	)

	sandwichCmd.Flags().Uint64VarP(
		&sandwichStart,
		"slot",
		"s",
		0,
		fmt.Sprintf("(Optional) starting slot number (>=%d)", config.MIN_START_SLOT),
	)
	sandwichCmd.Flags().StringVarP(
		&sandwichMode,
		"mode",
		"m",
		"live",
		"detection mode: 'live' (self-hosted RPC, follows tip) or 'backfill' (Helius archival, bounded range)",
	)
	sandwichCmd.Flags().Uint64VarP(
		&sandwichEnd,
		"end-slot",
		"e",
		0,
		"backfill only: last slot to scan (inclusive); required in backfill mode",
	)
	sandwichCmd.Flags().IntVar(
		&sandwichRPS,
		"rps",
		0,
		fmt.Sprintf("backfill only: max RPC requests/sec (0 = default %d for Helius free tier)", config.HELIUS_DEFAULT_BACKFILL_RPS),
	)

	RootCmd.AddCommand(&resetCmd, &jitoCmd, &slotCmd, &sandwichCmd)
}
