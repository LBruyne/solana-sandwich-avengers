package jito

import (
	"fmt"
	"strconv"
	"strings"
	"sandwich-detector/config"
	"sandwich-detector/logger"
	"sandwich-detector/utils"

	"github.com/spf13/viper"
)

var JitoBundleURL string

func GetJitoBundleURL() string {
	if JitoBundleURL != "" {
		return JitoBundleURL
	}

	JitoBundleURL = viper.GetString("jito.bundles-url")
	if JitoBundleURL == "" {
		JitoBundleURL = "https://bundles.jito.wtf/api/v1/bundles"
		logger.JitoLogger.Warn("JitoBundleURL not set in config, using default", "url", JitoBundleURL)
	}

	return JitoBundleURL
}

type RecentBundle struct {
	BundleId          string   `json:"bundleId"`
	Timestamp         string   `json:"timestamp"`
	Tippers           []string `json:"tippers"`
	Transactions      []string `json:"transactions"`
	LandedTipLamports uint64   `json:"landedTipLamports"`
}

type SlotBundle struct {
	BundleId          string   `json:"bundleId"`
	Slot              uint64   `json:"slot"`
	Validator         string   `json:"validator"`
	Tippers           []string `json:"tippers"`
	LandedTipLamports uint64   `json:"landedTipLamports"`
	LandedCu          uint64   `json:"landedCu"`
	BlockIndex        uint     `json:"blockIndex"`
	Timestamp         string   `json:"timestamp"`
	TxSignatures      []string `json:"txSignatures"`
}

func GetRecentBundles(limit int) ([]RecentBundle, error) {
	params := map[string]string{
		"limit":     strconv.Itoa(limit),
		"sort":      "Time",
		"asc":       "false",
		"timeframe": "Week",
	}

	var result []RecentBundle
	err := utils.GetUrlResponseWithRetry(GetJitoBundleURL()+"/recent", params, &result, config.DefaultRetryTimes, logger.JitoLogger)
	if err != nil {
		return nil, fmt.Errorf("GetRecentBundles failed: %w", err)
	}
	return result, nil
}

func GetBundlesBySlot(slot uint64) ([]SlotBundle, error) {
	params := map[string]string{}

	var result []SlotBundle
	// This endpoint is link .../bundles/slot/100000000
	err := utils.GetUrlResponseWithRetry(GetJitoBundleURL()+"/slot/"+strconv.FormatUint(slot, 10), params, &result, config.DefaultRetryTimes, logger.JitoLogger)
	if err != nil {
		if strings.Contains(err.Error(), utils.NO_BUNDLE) {
			return []SlotBundle{}, nil // No bundles for this slot
		}

		return nil, fmt.Errorf("GetBundlesBySlot failed: %w", err)
	}
	return result, nil
}
