# sandwich-detector

A Go service that ingests Solana blocks in near real time, detects sandwich
attacks (in-block and cross-block), enriches them with Jito bundle metadata,
and writes everything to ClickHouse.

The service is split into four CLI subcommands that are intended to run as
long-lived background processes against a single ClickHouse database. Each
subcommand owns one piece of the pipeline; they can be run independently and
will pick up from the latest state stored in the database.

## Pipeline

```
Solana RPC ──► leader   ──► slot_leaders
           ──► sandwich ──► sandwiches, sandwich_txs, slot_txs
                              │
Jito API ──► jito ────────────┴──► jito_bundles, slot_bundles, sandwich_txs.inBundle
```

| Subcommand | Reads from         | Writes to                                                    |
|------------|--------------------|--------------------------------------------------------------|
| `leader`   | Solana RPC         | `slot_leaders`                                               |
| `sandwich` | Solana RPC         | `sandwiches`, `sandwich_txs`, `slot_txs`                     |
| `jito`     | Jito API + DB      | `jito_bundles`, `slot_bundles`, `sandwich_txs.inBundle`      |
| `reset`    | (none)             | drops all tables                                             |

The database schema is in [`../db/create_tables/`](../db/create_tables/).
Tables are created automatically on the first run.

## Requirements

- Go 1.24
- ClickHouse server (tested against the Debian package, default ports)
- A Solana JSON-RPC endpoint that supports `getBlock` with full transaction
  details. The public `mainnet-beta` endpoint is rate-limited and not viable
  for continuous detection; a paid or self-hosted RPC is recommended.
- Jito Block Engine bundles API (`https://bundles.jito.wtf`, public)

## Install ClickHouse (Ubuntu)

Follow the official guide at
<https://clickhouse.com/docs/install/debian_ubuntu>. A minimal install:

```bash
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
ARCH=$(dpkg --print-architecture)
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=${ARCH}] https://packages.clickhouse.com/deb stable main" \
    | sudo tee /etc/apt/sources.list.d/clickhouse.list
sudo apt-get update && sudo apt-get install -y clickhouse-server clickhouse-client
sudo service clickhouse-server start
```

Create the database used by the detector (default name `solwich`):

```sql
clickhouse-client -q "CREATE DATABASE solwich"
```

Tables are created by the detector on first run; you do not need to apply
the SQL files manually.

## Configuration

Two files in this directory, both gitignored:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

`.env` holds ClickHouse credentials:

```
CLICKHOUSE_ADDR=localhost:9000
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=solwich
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=
```

`config.yaml` holds RPC and Jito endpoints:

```yaml
jito:
  bundles-url: https://bundles.jito.wtf/api/v1/bundles
sol:
  rpc: https://solana-mainnet.core.chainstack.com/<your-key>   # primary (live + backfill)
  rpc-archival:                                                # optional backfill override
```

`sol.rpc` is the primary endpoint for both live and backfill; use an archival,
rate-limit-friendly RPC (we use Chainstack Core — archival + paid). `sol.rpc-archival`
optionally overrides the backfill endpoint; if neither is archival, backfill falls back
to `HELIUS_RPC_API_KEY`. Detection throughput is bounded by RPC quality.

Detection thresholds, parallelism, and timing intervals are compile-time
constants in [`config/config.go`](config/config.go); see the [Tuning](#tuning)
section.

## Build

```bash
./scripts/build.sh        # produces ./sandwich-detector
# equivalent to: go mod tidy && go build -o sandwich-detector .
```

## Subcommands

All subcommands accept `-s <slot>` to set the starting slot and `-t` to
silence stdout (logs still go to `./logs/`). The starting slot must be
`>= MIN_START_SLOT` (currently 400000000 in
[`config/config.go`](config/config.go)). The ceiling is the most recent slot
that public RPC and Jito APIs still expose (about 3 hours of block history,
about 0.5 month of leader schedule).

### `leader`

Polls `getSlotLeaders` and writes one row per slot into `slot_leaders`. On
startup it resumes from the last slot already in the database. If the input
`-s` is more than `SOL_FETCH_SLOT_LEADER_MAX_GAP` slots behind the chain
tip, it skips forward to the tip rather than backfilling forever.

```bash
./sandwich-detector leader -s 400000000
```

### `sandwich`

The detection loop. Each iteration:

1. Fetches `SOL_FETCH_SLOT_DATA_SLOT_NUM` blocks via `getBlock`.
2. Runs in-block detection in parallel, one finder per block.
3. Runs cross-block detection over a sliding cache of
   `CROSS_BLOCK_CACHE_SIZE` blocks, restricted to leader-contiguous runs.
4. Inserts results into `sandwiches`, `sandwich_txs`, and `slot_txs`.

```bash
./sandwich-detector sandwich -s 400000000
```

The loop runs forever. If `-s` is older than
`SOL_FETCH_SLOT_DATA_MAX_GAP` slots, it is bumped forward to the oldest
slot the RPC still serves.

### `jito`

Two background tasks:

- **fetch**: walks `slot_bundles` from `-s` upward, calls the Jito
  `bundles/slot/<n>` endpoint, and writes bundles into `jito_bundles`. The
  fetch ceiling is the sandwich detection frontier minus
  `JITO_FETCH_BUNDLE_SAFE_LAG`, so bundles are only fetched for slots whose
  sandwich data is already finalized in the database.
- **mark**: scans `sandwich_txs` whose slot has been bundle-fetched but not
  yet `inBundle`-checked, intersects signatures with `jito_bundles.transactions`,
  and updates `sandwich_txs.inBundle` in batches.

```bash
./sandwich-detector jito -s 400000000                       # both tasks
./sandwich-detector jito -s 400000000 --fetch-bundle-only   # fetch only
./sandwich-detector jito -s 400000000 --sync-in-bundle      # mark only
```

`jito` should generally start at a slot less than or equal to the `sandwich`
loop's start slot, so that bundles arrive before the inBundle marker tries
to use them.

### `reset`

Drops every table the detector creates. Tables are recreated on the next
run. Destructive; there is no confirmation prompt on the binary itself
(the wrapper script `scripts/reset.sh` adds one).

```bash
./sandwich-detector reset
```

## Recommended startup sequence

For a fresh database:

```bash
# 1. Sync slot leaders. Cheap; safe to run continuously.
./scripts/leader.sh 400000000

# 2. Start sandwich detection. Heavy; this is the main worker.
./scripts/sandwich.sh 400000000

# 3. Start Jito enrichment after sandwich detection has produced output.
./scripts/jito.sh 400000000
```

All three scripts call `nohup` and write to `./logs/`. They can be tailed
with `./scripts/logs.sh <component>` and stopped with `./scripts/stop.sh`.

## Tuning

All knobs live in [`config/config.go`](config/config.go). Notable ones:

| Constant                                     | Default | Effect                                           |
|----------------------------------------------|--------:|--------------------------------------------------|
| `MIN_START_SLOT`                             |   4×10⁸ | Hard floor on `-s`                               |
| `PER_LEADER_SLOT`                            |       4 | Solana leader rotation length                    |
| `SOL_FETCH_SLOT_DATA_SLOT_NUM`               |       8 | Blocks per fetch round                           |
| `SOL_FETCH_SLOT_DATA_PARALLEL_NUM`           |       8 | Parallel `getBlock` workers                      |
| `SOL_PROCESS_IN_BLOCK_SANDWICH_PARALLEL_NUM` |       8 | Parallel in-block finders                        |
| `CROSS_BLOCK_CACHE_SIZE`                     |      64 | Sliding window for cross-block detection         |
| `INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD`     |      10 | % tolerance between front-run and back-run sizes |
| `SANDWICH_AMOUNT_SOL_TOLERANCE`              |     0.1 | SOL fee/dust tolerance (lamports)                |
| `SANDWICH_FRONTRUN_MAX_GAP`                  |     500 | Max position gap for multi-front sandwiches      |
| `SANDWICH_BACKRUN_MAX_GAP`                   |     500 | Same, back-run side                              |
| `JITO_MARK_IN_BUNDLE_SAFE_LAG`               |    2000 | Lag behind sandwich frontier before marking      |

Detection precision is sensitive to the amount-diff threshold and SOL
tolerance; the defaults are calibrated against the comparison sets in
`sandwich-intent/`.

## Tests

Tests are integration-style and require a live Solana RPC.

```bash
go test ./...
go test ./sol -run TestFindInBlockSandwichesBySlot -v
```

`sol/testdata/` contains JSON fixtures (sandwiches and victim swaps) used by
the deterministic regression tests in `sol/sandbox_test.go`.

## Logs

Each subcommand writes rotating files under `./logs/` (50 MB per file, old
files are kept on disk):

```
detector_<timestamp>_global.log        framework + DB
detector_<timestamp>_<cmd>_sol.log     sandwich / leader RPC + detection
detector_<timestamp>_<cmd>_jito.log    jito API + bundle marking
```

## Layout

```
cmd/        Cobra subcommand definitions (sandwich, jito, slot, reset)
config/     compile-time constants
db/         ClickHouse adapter + Database interface
jito/       Jito API client + RunJitoCmd (fetch + mark inBundle)
logger/     rotating slog handlers per subsystem
sol/        block fetcher, in-block + cross-block detection, victim slippage
sol/dex/    DEX-specific instruction decoders (Raydium, Whirlpool, Meteora,
            PumpFun, PancakeSwap) used for slippage extraction
types/      shared types (Block, Transaction, Sandwich, JitoBundle, Slot*)
utils/      LRU caches, RPC helpers, program lists
scripts/    wrappers (build, run, stop, logs, reset)
```
