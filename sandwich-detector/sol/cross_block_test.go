package sol

import (
	"fmt"
	"testing"
	"time"

	"sandwich-detector/config"
	"sandwich-detector/db"
	"sandwich-detector/types"
)

var crossSlot uint64

func init() {
	crossSlot = 370209993 // Replace a fresh slot id
	crossBlockCache = NewBlockCache(config.CROSS_BLOCK_CACHE_SIZE)
}

func TestFindCrossBlockSandwiches(t *testing.T) {
	ch = db.NewClickhouse()
	defer ch.Close()

	tt1 := time.Now()
	blks := GetBlocks(crossSlot, config.SOL_FETCH_SLOT_DATA_SLOT_NUM)
	fmt.Printf("GetBlocks time cost: %s\n", time.Since(tt1).String())
	if len(blks) == 0 {
		t.Fatalf("GetBlocks returned no blocks")
	}

	tt2 := time.Now()
	res := ProcessCrossBlockSandwich(blks)
	fmt.Printf("ProcessCrossBlockSandwich time cost: %s\n", time.Since(tt2).String())

	fmt.Printf("Detected %d cross-block sandwiches in slot=%d\n\n", len(res), crossSlot)
	for i, s := range res {
		types.PPCrossBlockSandwich(i+1, s)
	}
}
