package db

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"
	"sandwich-detector/logger"
	"sandwich-detector/types"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/spf13/viper"
)

type ClickhouseDB struct {
	conn driver.Conn
	db   string              // target database name; all table refs resolve against it as the connection default
	opts *clickhouse.Options // retained to open a bootstrap connection for CREATE DATABASE
}

func NewClickhouse() Database {
	dbName := viper.GetString("CLICKHOUSE_DATABASE")
	if dbName == "" {
		dbName = "solwich"
	}

	opts := &clickhouse.Options{
		Addr: []string{viper.GetString("CLICKHOUSE_ADDR")},
		Auth: clickhouse.Auth{
			Database: dbName,
			Username: viper.GetString("CLICKHOUSE_USERNAME"),
			Password: viper.GetString("CLICKHOUSE_PASSWORD"),
		},
		DialTimeout:  5 * time.Second,
		Compression:  &clickhouse.Compression{Method: clickhouse.CompressionLZ4},
		MaxOpenConns: 10,
	}

	conn, err := clickhouse.Open(opts)
	if err != nil {
		slog.Error("Failed to connect to ClickHouse", "error", err)
	}

	return &ClickhouseDB{conn: conn, db: dbName, opts: opts}
}

// Database interface implementation
func (d *ClickhouseDB) Close() error {
	return d.conn.Close()
}

func (d *ClickhouseDB) EnsureDatabaseExists() error {
	// The main connection's default database is d.db, which the native protocol validates at
	// handshake — so it can't be used to create d.db when it doesn't exist yet (server returns
	// code 81). Run CREATE DATABASE over a short-lived connection against the always-present
	// "default" database instead.
	bootOpts := *d.opts
	bootOpts.Auth.Database = "default"
	boot, err := clickhouse.Open(&bootOpts)
	if err != nil {
		return fmt.Errorf("open bootstrap connection: %w", err)
	}
	defer boot.Close()

	query := fmt.Sprintf("CREATE DATABASE IF NOT EXISTS %s", d.db)
	if err := boot.Exec(context.Background(), query); err != nil {
		return fmt.Errorf("failed to ensure database exists: %w", err)
	}
	logger.GlobalLogger.Info("Database ensured to exist", "database", d.db)
	return nil
}

func (d *ClickhouseDB) CreateTables() error {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS jito_bundles
		(
			bundleId String,
			slot UInt64,
			timestamp DateTime,
			tippers Array(String),
			transactions Array(String),
			landedTipLamports UInt64
		)
		ENGINE = MergeTree
		PARTITION BY intDiv(slot, 432000)
		ORDER BY (slot, bundleId)
		SETTINGS index_granularity = 8192`,

		`CREATE TABLE IF NOT EXISTS slot_bundles
		(
			slot UInt64,
			bundleFetched Bool,
			bundleCount UInt64,
			bundleTxCount UInt64
		)
		ENGINE = ReplacingMergeTree
		PARTITION BY intDiv(slot, 432000)
		PRIMARY KEY slot
		ORDER BY slot
		SETTINGS index_granularity = 8192`,

		`CREATE TABLE IF NOT EXISTS slot_leaders
		(
			slot UInt64,
			leader String
		)
		ENGINE = ReplacingMergeTree
		PARTITION BY intDiv(slot, 432000)
		PRIMARY KEY slot
		ORDER BY slot
		SETTINGS index_granularity = 8192`,

		`CREATE TABLE IF NOT EXISTS slot_txs
		(
			slot UInt64,
			txFetched Bool,
			txCount UInt64,
			validTxCount UInt64,
			sandwichFetched Bool,
			sandwichCount UInt64,
			sandwichTxCount UInt64,
			sandwichVictimCount UInt64,
			sandwichInBundleChecked Bool
		)
		ENGINE = ReplacingMergeTree
		PARTITION BY intDiv(slot, 432000)
		PRIMARY KEY slot
		ORDER BY slot
		SETTINGS index_granularity = 8192`,

		`CREATE TABLE IF NOT EXISTS sandwiches
		(
			sandwichId String,
			crossBlock Bool,
			crossLeader Bool DEFAULT false,
			frontLeader String DEFAULT '',
			backLeader String DEFAULT '',
			windowStartSlot UInt64 DEFAULT 0,
			windowEndSlot UInt64 DEFAULT 0,
			rpcSource LowCardinality(String) DEFAULT 'live',
			slot UInt64,
			timestamp DateTime,

			tokenA String,
			tokenB String,

			hasTransfer Bool,
			hasFrontInlineTransfer Bool,
			hasDirectTransfer Bool,
			hasBackInlineTransfer Bool,
			signerSame Bool,
			ownerSame Bool,
			ataSame Bool,
			consecutive Bool,

			multiFrontRun Bool,
			multiBackRun Bool,
			multiVictim Bool,
			frontCount UInt16,
			backCount UInt16,
			victimCount UInt16,
			adverseCount UInt16,
			frontConsecutive Bool,
			backConsecutive Bool,
			victimConsecutive Bool,

			perfect Bool,
			relativeDiffB Float64,
			profitA Float64,

			intentScore Float64 DEFAULT 0,
			maxSlippageUtilization Float64 DEFAULT 0
		)
		ENGINE = MergeTree
		PARTITION BY intDiv(slot, 432000)
		ORDER BY (slot, timestamp, sandwichId)
		SETTINGS index_granularity = 8192`,

		`CREATE TABLE IF NOT EXISTS sandwich_txs
		(
			sandwichId String,
			sandwichTimestamp DateTime,
			type String,

			slot UInt64,
			position Int32,
			timestamp DateTime,
			fee UInt64,
			signature String,
			signers Array(String),
			inBundle Bool,
			accountKeys Array(String),
			programs Array(String),

			fromToken String,
			toToken String,
			fromAmount Float64,
			toAmount Float64,
			attackerPreBalanceB Float64,
			attackerPostBalanceB Float64,
			poolPreBalanceB Float64,
			poolPostBalanceB Float64,
			ownersOfB Array(String),

			fromTotalAmount Float64,
			toTotalAmount Float64,

			diffA Float64,
			diffB Float64,

			slippageLimitType String DEFAULT '',
			slippageLimitAmount Float64 DEFAULT 0,
			slippageActualAmount Float64 DEFAULT 0,
			slippageUtilization Float64 DEFAULT -1,
			poolDex LowCardinality(String) DEFAULT ''
		)
		ENGINE = MergeTree
		PARTITION BY intDiv(slot, 432000)
		ORDER BY (sandwichTimestamp, sandwichId, timestamp, slot, position)
		SETTINGS index_granularity = 8192`,
	}

	for _, q := range queries {
		if err := d.conn.Exec(context.Background(), q); err != nil {
			return err
		}
		logger.GlobalLogger.Info("Check or create table in DB", "query", q)
	}

	// Keep an existing v2 table in sync when new columns are added to the CREATE above.
	// ADD COLUMN ... DEFAULT is metadata-only in ClickHouse, so this is instant regardless of row count.
	alterQueries := []string{
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS crossLeader Bool DEFAULT false`,
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS frontLeader String DEFAULT ''`,
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS backLeader String DEFAULT ''`,
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS windowStartSlot UInt64 DEFAULT 0`,
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS windowEndSlot UInt64 DEFAULT 0`,
		`ALTER TABLE sandwiches ADD COLUMN IF NOT EXISTS rpcSource LowCardinality(String) DEFAULT 'live'`,
		`ALTER TABLE sandwich_txs ADD COLUMN IF NOT EXISTS poolDex LowCardinality(String) DEFAULT ''`,
	}
	for _, q := range alterQueries {
		if err := d.conn.Exec(context.Background(), q); err != nil {
			logger.GlobalLogger.Warn("ALTER TABLE ADD COLUMN failed", "query", q, "err", err)
		}
	}

	return nil
}

func (d *ClickhouseDB) DropTables() error {
	var dbName string
	if err := d.conn.QueryRow(context.Background(), "SELECT currentDatabase()").Scan(&dbName); err != nil {
		return fmt.Errorf("failed to get current database: %w", err)
	}

	rows, err := d.conn.Query(context.Background(),
		fmt.Sprintf("SHOW TABLES FROM %s", dbName))
	if err != nil {
		return fmt.Errorf("failed to list tables: %w", err)
	}
	defer rows.Close()

	var tables []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			return fmt.Errorf("failed to scan table name: %w", err)
		}
		tables = append(tables, t)
	}

	for _, t := range tables {
		q := fmt.Sprintf("DROP TABLE IF EXISTS %s.%s", dbName, t)
		if err := d.conn.Exec(context.Background(), q); err != nil {
			return fmt.Errorf("failed to drop table %s: %w", t, err)
		}
	}

	return nil
}

func (d *ClickhouseDB) Exec(query string, args ...any) error {
	if err := d.conn.Exec(context.Background(), query, args...); err != nil {
		return err
	}
	return nil
}

func (d *ClickhouseDB) InsertJitoBundles(bundles types.JitoBundles) error {
	if len(bundles) == 0 {
		return nil
	}

	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO jito_bundles")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, bundle := range bundles {
		if err := batch.AppendStruct(bundle); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

func (d *ClickhouseDB) QueryLatestBundleIds(limit uint) ([]string, error) {
	rows, err := d.conn.Query(context.Background(),
		fmt.Sprintf(`SELECT bundleId FROM jito_bundles ORDER BY timestamp DESC LIMIT %d`, limit))
	if err != nil {
		return nil, fmt.Errorf("failed to query latest bundle ids: %w", err)
	}
	defer rows.Close()

	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("failed to scan bundle id: %w", err)
		}
		ids = append(ids, id)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("failed during rows iteration: %w", err)
	}

	return ids, nil
}

func (d *ClickhouseDB) QueryBundleTxsBySlot(slot uint64) ([]string, error) {
	rows, err := d.conn.Query(context.Background(), `SELECT DISTINCT arrayJoin(transactions) FROM jito_bundles WHERE slot = ?`, slot)
	if err != nil {
		return nil, fmt.Errorf("failed to query bundle txs by slot: %w", err)
	}
	defer rows.Close()

	txs := make([]string, 0)
	for rows.Next() {
		var tx string
		if err := rows.Scan(&tx); err != nil {
			return nil, fmt.Errorf("failed to scan tx: %w", err)
		}
		txs = append(txs, tx)
	}
	return txs, rows.Err()
}

func (d *ClickhouseDB) QueryBundleTxsBySlots(slots []uint64) (map[uint64][]string, error) {
	if len(slots) == 0 {
		return nil, nil
	}
	q := fmt.Sprintf(`
		SELECT slot, arrayJoin(transactions) AS tx
		FROM jito_bundles
		WHERE slot IN (%s)
	`, placeholders(len(slots)))

	args := make([]any, len(slots))
	for i, s := range slots {
		args[i] = s
	}

	rows, err := d.conn.Query(context.Background(), q, args...)
	if err != nil {
		return nil, fmt.Errorf("failed to query bundle txs by slots: %w", err)
	}
	defer rows.Close()

	result := make(map[uint64][]string)
	for rows.Next() {
		var slot uint64
		var tx string
		if err := rows.Scan(&slot, &tx); err != nil {
			return nil, err
		}
		result[slot] = append(result[slot], tx)
	}
	return result, rows.Err()
}

func (d *ClickhouseDB) InsertSlotBundles(statuses []*types.SlotBundlesStatus) error {
	if len(statuses) == 0 {
		return nil
	}
	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO slot_bundles")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, s := range statuses {
		if err := batch.AppendStruct(s); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

// DropJitoBundlesEpochPartition removes one epoch's rows from jito_bundles by dropping its
// partition (jito_bundles is PARTITION BY intDiv(slot, 432000)). This is the delete-after-mark
// cleanup: once every slot in an epoch has had its sandwich txs inBundle-checked, the raw bundles
// are no longer needed (the result lives in sandwich_txs.inBundle and the per-slot summary in
// slot_bundles). A partition drop is metadata-only — far cheaper than row-level ALTER DELETE.
func (d *ClickhouseDB) DropJitoBundlesEpochPartition(epoch uint64) error {
	// Partition id is the intDiv(slot,432000) value = the epoch number.
	q := fmt.Sprintf("ALTER TABLE jito_bundles DROP PARTITION %d", epoch)
	if err := d.conn.Exec(context.Background(), q); err != nil {
		return fmt.Errorf("failed to drop jito_bundles partition %d: %w", epoch, err)
	}
	return nil
}

func (d *ClickhouseDB) QuerySlotBundleBySlot(slot uint64) (uint64, error) {
	// Query by slot
	row := d.conn.QueryRow(context.Background(), `SELECT ifNull(max(slot), toUInt64(0)) FROM slot_bundles WHERE slot = ? and bundleFetched = 1 and bundleCount > 0`, slot)
	var this uint64
	if err := row.Scan(&this); err != nil {
		return 0, fmt.Errorf("failed to query slot bundle by slot: %w", err)
	}
	return this, nil
}

func (d *ClickhouseDB) QueryEarliestAndLatestBundleSlot() (uint64, uint64, bool, error) {
	row := d.conn.QueryRow(context.Background(), `SELECT min(slot), max(slot) FROM slot_bundles WHERE bundleFetched = 1`)
	var earliestSlot, latestSlot *uint64
	if err := row.Scan(&earliestSlot, &latestSlot); err != nil {
		return 0, 0, false, fmt.Errorf("failed to query queried earliest and latest bundle slot: %w", err)
	}
	if earliestSlot == nil || latestSlot == nil {
		return 0, 0, false, nil
	}
	return *earliestSlot, *latestSlot, true, nil
}

func (d *ClickhouseDB) InsertSlotTxs(statuses []*types.SlotTxsStatus) error {
	if len(statuses) == 0 {
		return nil
	}
	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO slot_txs")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, s := range statuses {
		if err := batch.AppendStruct(s); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

func (d *ClickhouseDB) UpdateSlotTxsCheckInBundle(slots []uint64, check bool) error {
	if len(slots) == 0 {
		return nil
	}

	ctx := clickhouse.Context(context.Background(), clickhouse.WithSettings(clickhouse.Settings{
		"mutations_sync": 1,
	}))

	q := fmt.Sprintf(`
		ALTER TABLE slot_txs
		UPDATE sandwichInBundleChecked = ?
		WHERE slot IN (%s)
	`, placeholders(len(slots)))

	args := make([]any, 0, len(slots)+1)
	args = append(args, check)
	for _, s := range slots {
		args = append(args, s)
	}
	return d.conn.Exec(ctx, q, args...)
}

func (d *ClickhouseDB) InsertSlotLeaders(leaders types.SlotLeaders) error {
	if len(leaders) == 0 {
		return nil
	}
	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO slot_leaders")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, leader := range leaders {
		if err := batch.AppendStruct(leader); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

func (d *ClickhouseDB) QueryLastSlotLeader() (uint64, error) {
	row := d.conn.QueryRow(context.Background(), "SELECT MAX(slot) from slot_leaders")
	var slot uint64
	if err := row.Scan(&slot); err != nil {
		return 0, fmt.Errorf("failed to query last slot leader: %w", err)
	}
	return slot, nil
}

func (d *ClickhouseDB) QuerySlotLeader(slot uint64) (string, error) {
	row := d.conn.QueryRow(context.Background(), "SELECT leader from slot_leaders WHERE slot = ?", slot)
	var leader string
	if err := row.Scan(&leader); err != nil {
		return "", fmt.Errorf("failed to query slot leader: %w", err)
	}
	return leader, nil
}

func (d *ClickhouseDB) InsertSandwiches(rows []*types.CrossBlockSandwich) error {
	if len(rows) == 0 {
		return nil
	}
	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO sandwiches")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, s := range rows {
		if err := batch.AppendStruct(s); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

func (d *ClickhouseDB) InsertSandwichTxs(sandwichTxs []*types.SandwichTx) error {
	if len(sandwichTxs) == 0 {
		return nil
	}
	batch, err := d.conn.PrepareBatch(context.Background(), "INSERT INTO sandwich_txs")
	if err != nil {
		return fmt.Errorf("failed to prepare batch: %w", err)
	}
	for _, tx := range sandwichTxs {
		if err := batch.AppendStruct(tx); err != nil {
			return fmt.Errorf("failed to append struct: %w", err)
		}
	}
	return batch.Send()
}

func (d *ClickhouseDB) UpdateSandwichTxsInBundle(results []types.JitoBundleMarkResult) error {
	if len(results) == 0 {
		return nil
	}

	ctx := clickhouse.Context(context.Background(), clickhouse.WithSettings(clickhouse.Settings{
		"mutations_sync": 1,
	}))

	const batchSize = 500
	type pair struct {
		slot uint64
		sig  string
	}
	var allPairs []pair
	for _, r := range results {
		for _, sig := range r.Hits {
			allPairs = append(allPairs, pair{slot: r.Slot, sig: sig})
		}
	}

	for i := 0; i < len(allPairs); i += batchSize {
		end := i + batchSize
		if end > len(allPairs) {
			end = len(allPairs)
		}
		batch := allPairs[i:end]

		var pairs []string
		args := make([]any, 0, len(batch)*2)
		for _, p := range batch {
			pairs = append(pairs, "(?, ?)")
			args = append(args, p.slot, p.sig)
		}

		q := fmt.Sprintf(`
			ALTER TABLE sandwich_txs
			UPDATE inBundle = 1
			WHERE (slot, signature) IN (%s)
		`, strings.Join(pairs, ", "))

		if err := d.conn.Exec(ctx, q, args...); err != nil {
			return fmt.Errorf("failed to update batch [%d:%d]: %w", i, end, err)
		}
	}

	return nil
}

func (d *ClickhouseDB) QuerySandwichTxsBySlot(slot uint64) ([]string, error) {
	rows, err := d.conn.Query(context.Background(),
		`SELECT DISTINCT signature FROM sandwich_txs WHERE slot = ?`, slot)
	if err != nil {
		return nil, fmt.Errorf("failed to query sandwich txs by slot: %w", err)
	}
	defer rows.Close()

	txs := make([]string, 0)
	for rows.Next() {
		var tx string
		if err := rows.Scan(&tx); err != nil {
			return nil, fmt.Errorf("failed to scan tx: %w", err)
		}
		txs = append(txs, tx)
	}
	return txs, rows.Err()
}

func (d *ClickhouseDB) QuerySandwichTxsBySlots(slots []uint64) (map[uint64][]string, error) {
	if len(slots) == 0 {
		return nil, nil
	}
	q := fmt.Sprintf(`
		SELECT slot, signature
		FROM sandwich_txs
		WHERE slot IN (%s)
	`, placeholders(len(slots)))

	args := make([]any, len(slots))
	for i, s := range slots {
		args[i] = s
	}

	rows, err := d.conn.Query(context.Background(), q, args...)
	if err != nil {
		return nil, fmt.Errorf("failed to query sandwich txs by slots: %w", err)
	}
	defer rows.Close()

	result := make(map[uint64][]string)
	for rows.Next() {
		var slot uint64
		var sig string
		if err := rows.Scan(&slot, &sig); err != nil {
			return nil, err
		}
		result[slot] = append(result[slot], sig)
	}
	return result, rows.Err()
}

func (d *ClickhouseDB) QueryMaxSandwichCheckedSlot() (uint64, error) {
	row := d.conn.QueryRow(context.Background(), `
		SELECT ifNull(max(slot), toUInt64(0))
		FROM slot_txs
		WHERE sandwichFetched = 1
	`)
	var slot uint64
	if err := row.Scan(&slot); err != nil {
		return 0, fmt.Errorf("failed to query max sandwich checked slot: %w", err)
	}
	return slot, nil
}

func (d *ClickhouseDB) QueryFirstSlotToCheckInBundle() (uint64, error) {
	row := d.conn.QueryRow(context.Background(), `
		SELECT ifNull(min(t.slot), toUInt64(0))
		FROM slot_txs t
		ANY INNER JOIN slot_bundles b USING (slot)
		WHERE t.txFetched = 1 AND t.sandwichFetched = 1 AND t.sandwichInBundleChecked = 0
		  AND b.bundleFetched = 1
	`)
	var slot uint64
	if err := row.Scan(&slot); err != nil {
		return 0, fmt.Errorf("failed to query first slot to check in bundle: %w", err)
	}
	return slot, nil
}

func (d *ClickhouseDB) QuerySlotsToCheckInBundle(limit int, safeLag uint64) ([]uint64, error) {
	rows, err := d.conn.Query(context.Background(), fmt.Sprintf(`
		SELECT slot
		FROM slot_txs AS t
		ANY INNER JOIN slot_bundles AS b USING (slot)
		WHERE t.txFetched = 1
		  AND t.sandwichFetched = 1
		  AND t.sandwichInBundleChecked = 0
		  AND b.bundleCount > 0
		  AND t.slot <= (SELECT max(slot) FROM slot_txs WHERE sandwichFetched = 1) - %d
		ORDER BY slot ASC
		LIMIT %d
	`, safeLag, limit))
	if err != nil {
		return nil, fmt.Errorf("failed to query slots to check in bundle: %w", err)
	}
	defer rows.Close()

	var slots []uint64
	for rows.Next() {
		var slot uint64
		if err := rows.Scan(&slot); err != nil {
			return nil, err
		}
		slots = append(slots, slot)
	}
	return slots, rows.Err()
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	return strings.TrimRight(strings.Repeat("?,", n), ",")
}
