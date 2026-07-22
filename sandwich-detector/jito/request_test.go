package jito

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"sandwich-detector/logger"
)

func init() {
	logger.InitLogs("jito_test")
	JitoBundleURL = "https://bundles.jito.wtf/api/v1/bundles"
}

func TestGetRecentBundles(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/recent" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		limit := r.URL.Query().Get("limit")
		if limit == "" {
			t.Errorf("limit not provided")
		}

		bundles := []RecentBundle{
			{
				BundleId:          "testbundle1",
				Timestamp:         "2025-08-23T00:00:00+00:00",
				Tippers:           []string{"tipper1"},
				Transactions:      []string{"tx1"},
				LandedTipLamports: 1000,
			},
			{
				BundleId:          "testbundle2",
				Timestamp:         "2025-08-23T01:00:00+00:00",
				Tippers:           []string{"tipper2"},
				Transactions:      []string{"tx2"},
				LandedTipLamports: 2000,
			},
		}
		_ = json.NewEncoder(w).Encode(bundles)
	}))
	defer ts.Close()

	orig := JitoBundleURL
	defer func() { JitoBundleURL = orig }()
	JitoBundleURL = ts.URL

	bundles, err := GetRecentBundles(2)
	if err != nil {
		t.Fatalf("GetRecentBundles failed: %v", err)
	}

	if len(bundles) != 2 {
		t.Fatalf("expected 2 bundles, got %d", len(bundles))
	}

	if bundles[0].BundleId != "testbundle1" {
		t.Errorf("unexpected first bundle id: %s", bundles[0].BundleId)
	}
}

func TestGetBundlesBySlot(t *testing.T) {
	slot := uint64(320000000)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bundles := []SlotBundle{
			{
				BundleId:          "slotbundle1",
				Slot:              slot,
				Validator:         "validator1",
				Tippers:           []string{"tipperA"},
				LandedTipLamports: 1234,
				LandedCu:          5678,
				BlockIndex:        1,
				Timestamp:         "2025-08-23T02:00:00+00:00",
				TxSignatures:      []string{"txsig1"},
			},
		}
		_ = json.NewEncoder(w).Encode(bundles)
	}))
	defer ts.Close()

	orig := JitoBundleURL
	defer func() { JitoBundleURL = orig }()
	JitoBundleURL = ts.URL

	bundles, err := GetBundlesBySlot(slot)
	if err != nil {
		t.Fatalf("GetBundlesBySlot failed: %v", err)
	}

	if len(bundles) != 1 {
		t.Fatalf("expected 1 bundle, got %d", len(bundles))
	}

	if bundles[0].BundleId != "slotbundle1" {
		t.Errorf("unexpected bundle id: %s", bundles[0].BundleId)
	}
}

func TestGetRecentBundlesRealAPI(t *testing.T) {
	if os.Getenv("JITO_REAL_TEST") == "" {
		t.Skip("set JITO_REAL_TEST=1 to hit the live Jito API")
	}
	JitoBundleURL = "https://bundles.jito.wtf/api/v1/bundles"

	fmt.Println("Testing real /recent API...")

	bundles, err := GetRecentBundles(3)
	if err != nil {
		t.Fatalf("GetRecentBundles failed: %v", err)
	}

	for i, b := range bundles {
		fmt.Printf("[%d] BundleId: %s, Timestamp: %s, #Tx: %d, LandedTipLamports: %d\n",
			i, b.BundleId, b.Timestamp, len(b.Transactions), b.LandedTipLamports)
	}
}

func TestGetBundlesBySlotRealAPI(t *testing.T) {
	if os.Getenv("JITO_REAL_TEST") == "" {
		t.Skip("set JITO_REAL_TEST=1 to hit the live Jito API")
	}
	JitoBundleURL = "https://bundles.jito.wtf/api/v1/bundles"

	// A recent slot to stay inside the Jito API's retention window; override with JITO_TEST_SLOT.
	slot := uint64(418768000)
	if s := os.Getenv("JITO_TEST_SLOT"); s != "" {
		fmt.Sscan(s, &slot)
	}
	fmt.Println("Testing real /slot API for slot:", slot)

	bundles, err := GetBundlesBySlot(slot)
	if err != nil {
		t.Fatalf("GetBundlesBySlot failed: %v", err)
	}

	fmt.Printf("Total bundles for slot %d: %d\n", slot, len(bundles))
	for i, b := range bundles {
		fmt.Printf("[%d] BundleId: %s, Slot: %d, Validator: %s, #Tx: %d, LandedTipLamports: %d\n",
			i, b.BundleId, b.Slot, b.Validator, len(b.TxSignatures), b.LandedTipLamports)
	}
}
