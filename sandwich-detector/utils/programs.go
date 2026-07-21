package utils

import (
	"strings"
	"sandwich-detector/config"
	"sandwich-detector/logger"

	"github.com/spf13/viper"
)

const OKX_PROGRAM = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"
const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
const VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"

const SOL = "SOL"
const WSOL = "So11111111111111111111111111111111111111112"

func init() {
	viper.SetConfigName("programs")
	viper.SetConfigType("yaml")
	viper.AddConfigPath(config.ConfigPath)

	if err := viper.MergeInConfig(); err != nil {
		logger.GlobalLogger.Error("Error reading programs.yaml file", "err", err)
	}
}

func IsBuiltInPrograms(name string) bool {
	return viper.IsSet("builtin." + strings.ToLower(name))
}

func IsLabeledDexPrograms(name string) bool {
	return viper.IsSet("labeled_dex." + strings.ToLower(name))
}

func IsLabeledDexPool(name string) bool {
	return viper.IsSet("labeled_pools." + strings.ToLower(name))
}

// IsLabeledAggregator reports whether a program is a known swap aggregator/router
// (Jupiter, OKX, DFlow, ...). Aggregator-routed txs are user swaps, not arbitrage,
// so their presence is an annotation signal rather than a rejection reason.
func IsLabeledAggregator(name string) bool {
	return viper.IsSet("labeled_aggregators." + strings.ToLower(name))
}
