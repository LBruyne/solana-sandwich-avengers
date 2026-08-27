package cmd

import (
	"fmt"
	"sandwich-detector/config"
	"sandwich-detector/logger"

	"github.com/spf13/cobra"
)

// Bootstrap creates the database and its tables. main installs it; running it from
// PersistentPreRun rather than from main means `--help` and shell completion work without a
// reachable ClickHouse, which is the first thing a new user runs.
var Bootstrap func()

var RootCmd = &cobra.Command{
	Use:   "sandwich-detector",
	Short: "Detect, enrich, and persist sandwich attacks on Solana",
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		logger.SetConsoleEnabled(!notToStdout)
		if Bootstrap != nil {
			Bootstrap()
		}
	},
}

// Flags
var jitoStart uint64
var jitoEnd uint64
var slotStart uint64
var sandwichStart uint64
var sandwichEnd uint64
var sandwichMode string
var sandwichRPS int
var notToStdout bool
var jitoFetchBundleOnly bool
var jitoSyncInBundle bool
var jitoFetchAhead bool
var resetAssumeYes bool

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

	jitoCmd.Flags().Uint64VarP(
		&jitoEnd,
		"end-slot",
		"e",
		0,
		"(Optional) last slot to process; >0 bounds the run (backfill): fetch/mark stop and the process exits when done. 0 = run forever (live). Run the sandwich backfill to end-slot + JITO_MARK_IN_BUNDLE_SAFE_LAG so mark can reach end-slot.",
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

	jitoCmd.Flags().BoolVar(
		&jitoFetchAhead,
		"fetch-ahead",
		false,
		"backfill fetch only: fetch the whole [start,end-slot] range directly instead of trailing the sandwich-detection frontier. Safe for HISTORICAL slots (already Jito-indexed); use to pre-fetch bundles for an epoch range before sandwich detection reaches it. Never use in live mode.",
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
		"detection mode: 'live' (follows the tip) or 'backfill' (bounded [start,end] range on archival RPC)",
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
		"backfill only: max RPC requests/sec (0 = no throttle, fine for paid RPC like Chainstack; set e.g. 10 for rate-limited tiers like Helius free)",
	)

	resetCmd.Flags().BoolVar(
		&resetAssumeYes,
		"yes",
		false,
		"skip the confirmation prompt (for non-interactive use)",
	)

	RootCmd.AddCommand(&resetCmd, &jitoCmd, &slotCmd, &sandwichCmd)
}
