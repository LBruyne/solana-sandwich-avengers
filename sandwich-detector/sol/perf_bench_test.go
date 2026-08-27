package sol

import (
	"fmt"
	"os"
	"runtime"
	"sandwich-detector/config"
	"sandwich-detector/types"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// Env-gated performance benchmark for the detection pipeline. It fetches a real archival
// corpus once, rebuilds the same sliding double-rotation windows the production
// orchestrator uses, then times (a) bucketize, (b) the bucketed finder, and (c) a naive
// variant of the SAME greedy algorithm whose candidate sets are built by linear scans
// over all swap legs instead of bucket lookups. The naive variant must produce the
// identical sandwich set — it exists purely to quantify what the bucketing buys.
//
//	PERF_TEST=1 PERF_RPC=<archival-url> [PERF_START=410600000] [PERF_SLOTS=400] \
//	  [PERF_ITERS=10] [PERF_NAIVE_ITERS=3] go test ./sol/ -run TestPerfDetection -v
func TestPerfDetection(t *testing.T) {
	if os.Getenv("PERF_TEST") == "" {
		t.Skip("set PERF_TEST=1 PERF_RPC=<archival-url> to run the detection benchmark")
	}
	rpc := os.Getenv("PERF_RPC")
	if rpc == "" {
		t.Fatal("PERF_RPC must point at an archival RPC")
	}
	startSlot := envUint(t, "PERF_START", 410600000)
	numSlots := envUint(t, "PERF_SLOTS", 400)
	iters := int(envUint(t, "PERF_ITERS", 10))
	naiveIters := int(envUint(t, "PERF_NAIVE_ITERS", 3))

	savedURL, savedRewards := SolanaRpcURL, FetchRewards
	defer func() { SolanaRpcURL, FetchRewards = savedURL, savedRewards }()
	SolanaRpcURL = rpc
	FetchRewards = true // leaders from rewards, so rotations can be built

	// The production LRUs (50k owners / 10k pools) are smaller than a multi-hundred-slot corpus's
	// working set; sweeping the corpus repeatedly would thrash them and re-issue getMultipleAccounts
	// inside the timed loops. Swap in corpus-sized caches so timed passes measure pure CPU.
	savedOwnerCache, savedAMMCache := accountOwnerCache, knownAMMPools
	accountOwnerCache = NewAccountOwnerLRU(1 << 20)
	knownAMMPools = NewAMMPoolLRU(1 << 20)
	defer func() { accountOwnerCache, knownAMMPools = savedOwnerCache, savedAMMCache }()

	// ---- Fetch corpus (timed; this is the RPC-bound stage) ----
	fetchStart := time.Now()
	blocks := GetBlocks(startSlot, numSlots)
	fetchWall := time.Since(fetchStart)
	if len(blocks) == 0 {
		t.Fatal("no blocks fetched")
	}
	var totalTxs int
	for _, b := range blocks {
		totalTxs += len(b.Txs)
	}
	t.Logf("corpus: slots=%d blocks=%d txs=%d | fetch(8 workers)=%s = %.1f blocks/s, %.1fms/block",
		numSlots, len(blocks), totalTxs, fetchWall.Round(time.Millisecond),
		float64(len(blocks))/fetchWall.Seconds(), fetchWall.Seconds()*1000/float64(len(blocks)))

	windows := buildBenchWindows(blocks)
	if len(windows) == 0 {
		t.Fatal("no detection windows built")
	}
	leaderBySlot := make(map[uint64]string, len(blocks))
	for _, b := range blocks {
		if b.Leader != "" {
			leaderBySlot[b.Slot] = b.Leader
		}
	}

	// ---- Warm-up: pays the cold prefetchPoolOwners RPC once so timed loops are pure CPU ----
	warmStart := time.Now()
	for _, w := range windows {
		filterAndBuildTxBuckets(w, true)
	}
	t.Logf("warm-up bucketize over %d windows (incl. cold getMultipleAccounts): %s",
		len(windows), time.Since(warmStart).Round(time.Millisecond))

	// ---- Analytical shape: what the buckets look like on real data ----
	var sumEntries, sumKeys, maxBucket int
	var sumScanOps, sumBucketOps float64
	for _, w := range windows {
		buckets := filterAndBuildTxBuckets(w, true)
		s := 0
		for _, es := range buckets {
			s += len(es)
			if len(es) > maxBucket {
				maxBucket = len(es)
			}
		}
		sumEntries += s
		sumKeys += len(buckets)
		sumScanOps += float64(len(buckets)) * float64(s) // naive: O(K·S) scans
		sumBucketOps += float64(s)                       // bucketed: one pass to build
	}
	t.Logf("shape: windows=%d swapLegs(S)=%d buckets(K)=%d avgBucket=%.2f maxBucket=%d | naive scan ops Σ(K·S)=%.0f vs bucketed Σ(S)=%.0f (×%.0f)",
		len(windows), sumEntries, sumKeys, float64(sumEntries)/float64(sumKeys), maxBucket,
		sumScanOps, sumBucketOps, sumScanOps/sumBucketOps)

	// ---- Timed passes ----
	// Guard: the timed loops must be pure CPU. If the owner LRU evicted corpus pools, bucketize
	// would silently re-issue getMultipleAccounts and pollute the numbers.
	rpcBefore := RpcCallCountSnapshot()["getMultipleAccounts"]

	var tBucketize, tFind, tFindPar, tNaive []time.Duration
	var refIDs string
	for it := 0; it < iters; it++ {
		runtime.GC()
		start := time.Now()
		for _, w := range windows {
			filterAndBuildTxBuckets(w, true)
		}
		tBucketize = append(tBucketize, time.Since(start))

		runtime.GC()
		start = time.Now()
		var ids []string
		for _, w := range windows {
			f := NewSandwichFinder(w, leaderBySlot, config.CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "bench", nil)
			f.Find()
			for _, s := range f.Sandwiches {
				ids = append(ids, s.SandwichID)
			}
		}
		tFind = append(tFind, time.Since(start))

		sort.Strings(ids)
		joined := strings.Join(ids, ",")
		if it == 0 {
			refIDs = joined
			t.Logf("bucketed finder: %d sandwiches over %d windows", len(ids), len(windows))
		} else if joined != refIDs {
			t.Fatalf("determinism violation: iteration %d produced a different sandwich set", it)
		}

		// Production shape: the same windows through an 8-worker pool (matches
		// SOL_PROCESS_CROSS_BLOCK_SANDWICH_PARALLEL_NUM in ProcessBlocksForSandwich).
		runtime.GC()
		start = time.Now()
		runWindowsParallel(windows, leaderBySlot, 8)
		tFindPar = append(tFindPar, time.Since(start))
	}
	for it := 0; it < naiveIters; it++ {
		runtime.GC()
		start := time.Now()
		var ids []string
		for _, w := range windows {
			ids = append(ids, naiveFind(w, leaderBySlot)...)
		}
		tNaive = append(tNaive, time.Since(start))

		sort.Strings(ids)
		if strings.Join(ids, ",") != refIDs {
			t.Fatalf("naive/bucketed MISMATCH on iteration %d — the comparison is invalid", it)
		}
	}
	t.Logf("cross-check OK: naive == bucketed, identical sandwich ids on all iterations")

	if d := RpcCallCountSnapshot()["getMultipleAccounts"] - rpcBefore; d > 0 {
		t.Logf("WARNING: %d getMultipleAccounts calls happened DURING timed passes (LRU eviction) — CPU numbers include RPC", d)
	} else {
		t.Logf("guard OK: zero RPC calls during timed passes (pure CPU numbers)")
	}

	nb := float64(len(blocks))
	rep := func(name string, ds []time.Duration) time.Duration {
		med := medianDuration(ds)
		t.Logf("%-22s min=%s median=%s max=%s | per-block(median)=%.3fms | all=%s",
			name, minDuration(ds).Round(time.Microsecond), med.Round(time.Microsecond),
			maxDuration(ds).Round(time.Microsecond), med.Seconds()*1000/nb, fmtDurations(ds))
		return med
	}
	medBucketize := rep("bucketize only", tBucketize)
	medFind := rep("bucketed find (total)", tFind)
	medFindPar := rep("bucketed find (8 wrk)", tFindPar)
	medNaive := rep("naive find (total)", tNaive)
	matchOnly := medFind - medBucketize
	naiveMatchOnly := medNaive - medBucketize
	t.Logf("matching-only (find - bucketize): bucketed=%s naive=%s", matchOnly.Round(time.Microsecond), naiveMatchOnly.Round(time.Microsecond))
	t.Logf("SPEEDUP: total %.1fx, matching-only %.1fx", medNaive.Seconds()/medFind.Seconds(), naiveMatchOnly.Seconds()/matchOnly.Seconds())
	t.Logf("SUMMARY per block: fetch=%.1fms  detection(serial)=%.3fms  detection(8-worker wall)=%.3fms  naive(serial)=%.3fms",
		fetchWall.Seconds()*1000/nb, medFind.Seconds()*1000/nb, medFindPar.Seconds()*1000/nb, medNaive.Seconds()*1000/nb)
}

// runWindowsParallel mirrors the production window-worker pool in ProcessBlocksForSandwich.
func runWindowsParallel(windows []types.Transactions, leaderBySlot map[uint64]string, workers int) {
	queue := make(chan types.Transactions, len(windows))
	for _, w := range windows {
		queue <- w
	}
	close(queue)
	var wg sync.WaitGroup
	wg.Add(workers)
	for range workers {
		go func() {
			defer wg.Done()
			for w := range queue {
				f := NewSandwichFinder(w, leaderBySlot, config.CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "bench", nil)
				f.Find()
			}
		}()
	}
	wg.Wait()
}

// buildBenchWindows rebuilds the production sliding double-rotation windows (rotation i
// paired with its slot-adjacent successor) over a fully-fetched corpus, using each
// block's rewards-derived leader — the same construction ProcessBlocksForSandwich does,
// minus the streaming cache/dedup machinery irrelevant to a closed corpus.
func buildBenchWindows(blocks types.Blocks) []types.Transactions {
	sorted := make(types.Blocks, len(blocks))
	copy(sorted, blocks)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Slot < sorted[j].Slot })

	newSlots := make(map[uint64]struct{}, len(sorted))
	for _, b := range sorted {
		newSlots[b.Slot] = struct{}{}
	}
	getLeader := func(slot uint64) (string, bool) {
		for _, b := range sorted {
			if b.Slot == slot {
				return b.Leader, b.Leader != ""
			}
		}
		return "", false
	}
	rotations := buildLeaderRotations(sorted, newSlots, getLeader)

	windows := make([]types.Transactions, 0, len(rotations))
	for i, rot := range rotations {
		txs := make(types.Transactions, 0, len(rot.blocks)*8)
		for _, b := range rot.blocks {
			txs = append(txs, b.Txs...)
		}
		if i+1 < len(rotations) && rotations[i+1].startSlot <= rot.endSlot+config.PER_LEADER_SLOT {
			for _, b := range rotations[i+1].blocks {
				txs = append(txs, b.Txs...)
			}
		}
		windows = append(windows, txs)
	}
	return windows
}

// naiveFind runs the SAME greedy matcher as SandwichFinder.Find, but models "iterate all
// swap transactions directly": every candidate set (fronts, backs — and via f.buckets,
// victims/adverse) is produced by a linear scan over the full flat list of swap legs,
// instead of a bucket lookup. Iteration order is identical to production (same sorted
// key order, same within-key (slot, position) order), so the output must be identical —
// the caller asserts that. Returns the sandwich ids.
func naiveFind(txs types.Transactions, leaderBySlot map[uint64]string) []string {
	real := filterAndBuildTxBuckets(txs, true)

	// Flat list of all swap legs, ordered by (slot, position, key) — "all swap txs".
	entries := make([]PoolEntry, 0, 256)
	for _, es := range real {
		entries = append(entries, es...)
	}
	sort.Slice(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a.Slot != b.Slot {
			return a.Slot < b.Slot
		}
		if a.Position != b.Position {
			return a.Position < b.Position
		}
		if a.PoolAddress != b.PoolAddress {
			return a.PoolAddress < b.PoolAddress
		}
		if a.IncomeToken != b.IncomeToken {
			return a.IncomeToken < b.IncomeToken
		}
		return a.ExpenseToken < b.ExpenseToken
	})

	scan := func(key PoolKey) []PoolEntry { // the naive cost center: O(S) per candidate set
		res := make([]PoolEntry, 0, 8)
		for _, e := range entries {
			if e.PoolAddress == key.PoolAddress && e.IncomeToken == key.IncomeToken && e.ExpenseToken == key.ExpenseToken {
				res = append(res, e)
			}
		}
		return res
	}

	f := NewSandwichFinder(txs, leaderBySlot, config.CROSSBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD, "bench", nil)
	ids := make([]string, 0, 8)
	for _, key := range sortedBucketKeys(real) {
		frontTxBucket := scan(key)
		if len(frontTxBucket) == 0 {
			continue
		}
		revKey := PoolKey{PoolAddress: key.PoolAddress, IncomeToken: key.ExpenseToken, ExpenseToken: key.IncomeToken}
		backTxBucket := scan(revKey)
		if len(backTxBucket) == 0 {
			continue
		}
		// Victim/adverse collection inside Evaluate reads f.buckets[key]/[revKey]; hand it
		// exactly the scanned slices so those sets also come from linear scans.
		f.buckets = map[PoolKey][]PoolEntry{key: frontTxBucket, revKey: backTxBucket}

		for i := 0; i < len(frontTxBucket); i++ {
			candidateFrontTxEntries := f.collectFrontTxs(frontTxBucket[i], frontTxBucket)
			if len(candidateFrontTxEntries) == 0 {
				continue
			}
			var backTxEntries []PoolEntry
			for k := 1; k <= len(candidateFrontTxEntries); k++ {
				backTxEntries = f.collectBackTxs(candidateFrontTxEntries[:k], backTxBucket)
				if len(backTxEntries) > 0 {
					break
				}
			}
			if len(backTxEntries) == 0 {
				continue
			}
			f.RecordSandwich()
			f.ResetSandwichState()
		}
	}
	for _, s := range f.Sandwiches {
		ids = append(ids, s.SandwichID)
	}
	return ids
}

func envUint(t *testing.T, name string, def uint64) uint64 {
	v := os.Getenv(name)
	if v == "" {
		return def
	}
	n, err := strconv.ParseUint(v, 10, 64)
	if err != nil {
		t.Fatalf("bad %s=%q: %v", name, v, err)
	}
	return n
}

func medianDuration(ds []time.Duration) time.Duration {
	s := append([]time.Duration(nil), ds...)
	sort.Slice(s, func(i, j int) bool { return s[i] < s[j] })
	return s[len(s)/2]
}

func minDuration(ds []time.Duration) time.Duration {
	m := ds[0]
	for _, d := range ds[1:] {
		if d < m {
			m = d
		}
	}
	return m
}

func maxDuration(ds []time.Duration) time.Duration {
	m := ds[0]
	for _, d := range ds[1:] {
		if d > m {
			m = d
		}
	}
	return m
}

func fmtDurations(ds []time.Duration) string {
	parts := make([]string, 0, len(ds))
	for _, d := range ds {
		parts = append(parts, fmt.Sprintf("%dms", d.Milliseconds()))
	}
	return strings.Join(parts, " ")
}
