# Database Schema (ClickHouse)

## Overview

The database schema is optimized for high-throughput ingestion of slot-level data and fast analytical joins across sandwiches, transactions, and bundle metadata. All tables use MergeTree-family engines for append-oriented workloads.

## Core Tables

### `sandwiches`

Schema: [db/create_tables/sandwiches.sql](db/create_tables/sandwiches.sql)

- **Primary key**: `(slot, timestamp, sandwichId)`
- **Purpose**: Stores sandwich-level metadata including token pair, cross-block flag, and profitability metrics.
- **Key fields**:
  - `tokenA`, `tokenB`
  - `signerSame`, `ownerSame`, `ataSame`
  - `multiFrontRun`, `multiBackRun`, `multiVictim`
  - `perfect`, `relativeDiffB`, `profitA`

### `sandwich_txs`

Schema: [db/create_tables/sandwich_txs.sql](db/create_tables/sandwich_txs.sql)

- **Primary key**: `(sandwichTimestamp, sandwichId, timestamp, slot, position)`
- **Purpose**: Stores per-transaction sandwich decomposition with direction labels.
- **Key fields**:
  - `sandwichTimestamp` (front-run anchor timestamp for sandwich-level ordering)
  - `type ∈ {frontRun, victim, backRun, transfer, adverse}`
  - `fromToken`, `toToken`, `fromAmount`, `toAmount`
  - `attackerPreBalanceB`, `attackerPostBalanceB`
  - `poolPreBalanceB`, `poolPostBalanceB`
  - `fromTotalAmount`, `toTotalAmount`, `diffA`, `diffB`
  - `inBundle` (Jito bundle membership)

### `slot_txs`

Schema: [db/create_tables/slot_txs.sql](db/create_tables/slot_txs.sql)

- **Primary key**: `slot`
- **Purpose**: Slot-level ingestion bookkeeping (txFetched/sandwichFetched/coverage stats).

### `slot_leaders`

Schema: [db/create_tables/slot_leaders.sql](db/create_tables/slot_leaders.sql)

- **Primary key**: `slot`
- **Purpose**: Captures the scheduled leader for each slot to support validator attribution.

### `jito_bundles`

Schema: [db/create_tables/jito_bundles.sql](db/create_tables/jito_bundles.sql)

- **Primary key**: `timestamp`
- **Purpose**: Stores bundle metadata from the Jito API including `transactions` and `landedTipLamports`.

### `slot_bundles`

Schema: [db/create_tables/slot_bundles.sql](db/create_tables/slot_bundles.sql)

- **Primary key**: `slot`
- **Purpose**: Records per-slot bundle fetching status and total counts.

## Relationships and Query Patterns

- `sandwiches.sandwichId` is the join key to `sandwich_txs.sandwichId`.
- Slot-level joins (`slot_txs`, `slot_leaders`, `slot_bundles`) provide temporal attribution and coverage statistics.
- Bundle membership is expressed via `sandwich_txs.inBundle`, which is derived by intersecting `jito_bundles.transactions` with `sandwich_txs.signature`.

## Design Rationale

- **Append-optimized ingestion**: MergeTree engines support high-frequency inserts from `Watcher`.
- **De-duplication**: Analyst queries use `LIMIT 1 BY sandwichId` to avoid overcounting in append-only tables.
- **Separation of concerns**: Sandwich-level metrics are stored once in `sandwiches`, while transaction-level details remain in `sandwich_txs` for flexible feature engineering.
