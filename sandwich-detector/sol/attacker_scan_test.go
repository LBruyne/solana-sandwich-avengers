package sol

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"testing"
	"time"

	"sandwich-detector/logger"
	"sandwich-detector/types"

	"github.com/spf13/viper"
)

// Lightweight output structs (avoid serializing the heavy Transaction maps/sets).
type outTx struct {
	Type                string   `json:"type"`
	Slot                uint64   `json:"slot"`
	Position            int      `json:"position"`
	Signature           string   `json:"signature"`
	Signers             []string `json:"signers"`
	Fee                 uint64   `json:"fee"`
	InBundle            bool     `json:"inBundle"`
	Programs            []string `json:"programs"`
	FromToken           string   `json:"fromToken"`
	ToToken             string   `json:"toToken"`
	FromAmount          float64  `json:"fromAmount"`
	ToAmount            float64  `json:"toAmount"`
	OwnersOfB           []string `json:"ownersOfB"`
	SlippageUtilization float64  `json:"slippageUtilization"`
	PoolDex             string   `json:"poolDex"`
	SlippageLimitType   string   `json:"slippageLimitType"`
}

type outSandwich struct {
	Slot                   uint64  `json:"slot"`
	CrossBlock             bool    `json:"crossBlock"`
	TokenA                 string  `json:"tokenA"`
	TokenB                 string  `json:"tokenB"`
	ProfitA                float64 `json:"profitA"`
	Perfect                bool    `json:"perfect"`
	RelativeDiffB          float64 `json:"relativeDiffB"`
	Consecutive            bool    `json:"consecutive"`
	MultiFrontRun          bool    `json:"multiFrontRun"`
	MultiBackRun           bool    `json:"multiBackRun"`
	MultiVictim            bool    `json:"multiVictim"`
	SignerSame             bool    `json:"signerSame"`
	OwnerSame              bool    `json:"ownerSame"`
	VictimCount            uint16  `json:"victimCount"`
	FrontCount             uint16  `json:"frontCount"`
	BackCount              uint16  `json:"backCount"`
	MaxSlippageUtilization float64 `json:"maxSlippageUtilization"`
	Timestamp              int64   `json:"timestamp"`
	Front                  []outTx `json:"front"`
	Back                   []outTx `json:"back"`
	Victims                []outTx `json:"victims"`
}

func toOutTx(s *types.SandwichTx) outTx {
	return outTx{
		Type:                s.Type,
		Slot:                s.Slot,
		Position:            s.Position,
		Signature:           s.Signature,
		Signers:             s.Signers,
		Fee:                 s.Fee,
		InBundle:            s.InBundle,
		Programs:            s.Programs,
		FromToken:           s.FromToken,
		ToToken:             s.ToToken,
		FromAmount:          s.FromAmount,
		ToAmount:            s.ToAmount,
		OwnersOfB:           s.OwnersOfB,
		SlippageUtilization: s.SlippageUtilization,
		PoolDex:             s.PoolDex,
		SlippageLimitType:   s.SlippageLimitType,
	}
}

func convFront(s *types.Sandwich, slot uint64, cross bool, ts time.Time) outSandwich {
	o := outSandwich{
		Slot: slot, CrossBlock: cross, TokenA: s.TokenA, TokenB: s.TokenB,
		ProfitA: s.ProfitA, Perfect: s.Perfect, RelativeDiffB: s.RelativeDiffB,
		Consecutive: s.Consecutive, MultiFrontRun: s.MultiFrontRun, MultiBackRun: s.MultiBackRun,
		MultiVictim: s.MultiVictim, SignerSame: s.SignerSame, OwnerSame: s.OwnerSame,
		VictimCount: s.VictimCount, FrontCount: s.FrontCount, BackCount: s.BackCount,
		MaxSlippageUtilization: s.MaxSlippageUtilization, Timestamp: ts.Unix(),
	}
	for _, t := range s.FrontRun {
		o.Front = append(o.Front, toOutTx(t))
	}
	for _, t := range s.BackRun {
		o.Back = append(o.Back, toOutTx(t))
	}
	for _, t := range s.Victims {
		o.Victims = append(o.Victims, toOutTx(t))
	}
	return o
}

func setupScan(t *testing.T) {
	logger.SetConsoleEnabled(false)
	cfgDir := os.Getenv("SCAN_CONFIG_DIR")
	if cfgDir == "" {
		cfgDir = ".."
	}
	viper.SetConfigName("programs")
	viper.SetConfigType("yaml")
	viper.AddConfigPath(cfgDir)
	if err := viper.MergeInConfig(); err != nil {
		t.Fatalf("load programs.yaml from %s: %v", cfgDir, err)
	}
	if !viper.IsSet("labeled_dex") {
		t.Fatalf("programs.yaml loaded but labeled_dex missing")
	}
	if rpc := os.Getenv("SCAN_RPC"); rpc != "" {
		SolanaRpcURL = rpc
	} else {
		SolanaRpcURL = "http://64.130.32.137:8899"
	}
}

// TestScanSlotList fetches an explicit list of slots (JSON array in SCAN_SLOTLIST),
// one block at a time (gentle on archival RPCs), runs in-block detection, and writes
// every detected sandwich to SCAN_OUT. Used to profile a specific bot by fetching the
// slots where its program/wallet is active, then filtering output by program in Python.
func TestScanSlotList(t *testing.T) {
	if os.Getenv("SCAN_RUN") != "1" {
		t.Skip("set SCAN_RUN=1 to run")
	}
	setupScan(t)
	raw, err := os.ReadFile(os.Getenv("SCAN_SLOTLIST"))
	if err != nil {
		t.Fatalf("read SCAN_SLOTLIST: %v", err)
	}
	var slots []uint64
	if err := json.Unmarshal(raw, &slots); err != nil {
		t.Fatalf("parse slot list: %v", err)
	}
	outPath := os.Getenv("SCAN_OUT")
	if outPath == "" {
		outPath = "./slotlist_out.jsonl"
	}
	f, err := os.Create(outPath)
	if err != nil {
		t.Fatalf("create out: %v", err)
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	fmt.Printf("RPC=%s slots=%d out=%s\n", SolanaRpcURL, len(slots), outPath)

	var total, ok, fail int
	var failed []uint64
	t0 := time.Now()
	for i, slot := range slots {
		blk, err := GetBlock(slot)
		if err != nil || blk == nil {
			fail++
			failed = append(failed, slot)
			continue
		}
		ok++
		for _, s := range FindInBlockSandwiches(blk) {
			o := convFront(&s.Sandwich, s.Slot, false, s.Timestamp)
			enc.Encode(&o)
			total++
		}
		if (i+1)%50 == 0 {
			fmt.Printf("  %d/%d slots (ok=%d fail=%d) sandwiches=%d (%.0fs)\n", i+1, len(slots), ok, fail, total, time.Since(t0).Seconds())
		}
	}
	// Write the list of slots that failed to fetch so a driver can retry them.
	if fb, err := os.Create(outPath + ".failed"); err == nil {
		json.NewEncoder(fb).Encode(failed)
		fb.Close()
	}
	fmt.Printf("DONE %d slots ok=%d fail=%d sandwiches=%d in %.0fs\n", len(slots), ok, fail, total, time.Since(t0).Seconds())
}

// TestScanAttackers scans a recent slot range on the configured RPC, runs both
// in-block and cross-block sandwich detection, and writes every detected
// sandwich as one JSON line to SCAN_OUT. Controlled by env:
//   SCAN_RPC   (default fast node)
//   SCAN_START (default current-6000)
//   SCAN_SLOTS (default 600)
//   SCAN_BATCH (default 100)
//   SCAN_OUT   (default ./scan_out.jsonl)
func TestScanAttackers(t *testing.T) {
	if os.Getenv("SCAN_RUN") != "1" {
		t.Skip("set SCAN_RUN=1 to run the attacker scan")
	}
	logger.SetConsoleEnabled(false)

	// programs.yaml lives in the parent module dir; the package init() looked in
	// ./ (the sol/ test dir) and failed. Re-merge it from the right place so the
	// DEX program registry (labeled_dex etc.) is populated for swap detection.
	cfgDir := os.Getenv("SCAN_CONFIG_DIR")
	if cfgDir == "" {
		cfgDir = ".."
	}
	viper.SetConfigName("programs")
	viper.SetConfigType("yaml")
	viper.AddConfigPath(cfgDir)
	if err := viper.MergeInConfig(); err != nil {
		t.Fatalf("load programs.yaml from %s: %v", cfgDir, err)
	}
	if !viper.IsSet("labeled_dex") {
		t.Fatalf("programs.yaml loaded but labeled_dex missing")
	}
	rpc := os.Getenv("SCAN_RPC")
	if rpc == "" {
		rpc = "http://64.130.32.137:8899"
	}
	SolanaRpcURL = rpc

	cur, err := GetCurrentSlot()
	if err != nil {
		t.Fatalf("GetCurrentSlot: %v", err)
	}
	getU := func(k string, def uint64) uint64 {
		if v := os.Getenv(k); v != "" {
			n, _ := strconv.ParseUint(v, 10, 64)
			return n
		}
		return def
	}
	start := getU("SCAN_START", cur-6000)
	slots := getU("SCAN_SLOTS", 600)
	batch := getU("SCAN_BATCH", 100)
	outPath := os.Getenv("SCAN_OUT")
	if outPath == "" {
		outPath = "./scan_out.jsonl"
	}
	f, err := os.Create(outPath)
	if err != nil {
		t.Fatalf("create out: %v", err)
	}
	defer f.Close()
	enc := json.NewEncoder(f)

	fmt.Printf("RPC=%s current=%d start=%d slots=%d batch=%d out=%s\n", rpc, cur, start, slots, batch, outPath)

	var total, totalCross int
	t0 := time.Now()
	for off := uint64(0); off < slots; off += batch {
		n := batch
		if off+n > slots {
			n = slots - off
		}
		bs := start + off
		tb := time.Now()
		blocks := GetBlocks(bs, n)
		// Backfill: flush every rotation (deferTail=false).
		sandwiches := ProcessBlocksForSandwich(blocks, "helius", false)
		batchCross := 0
		for _, s := range sandwiches {
			o := convFront(&s.Sandwich, s.Slot, s.CrossBlock, s.Timestamp)
			enc.Encode(&o)
			if s.CrossBlock {
				batchCross++
			}
		}
		total += len(sandwiches)
		totalCross += batchCross
		fmt.Printf("batch [%d..%d] blocks=%d sandwiches=%d cross=%d (%.1fs)\n",
			bs, bs+n-1, len(blocks), len(sandwiches), batchCross, time.Since(tb).Seconds())
	}
	fmt.Printf("DONE total sandwiches=%d (cross-block=%d) in %.1fs\n", total, totalCross, time.Since(t0).Seconds())
}
