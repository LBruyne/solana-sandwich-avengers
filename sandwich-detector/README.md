# sandwich-detector

A Go service that ingests Solana blocks in near real time, detects sandwich
attacks (in-block, same-leader cross-block, and cross-leader), enriches them
with Jito bundle metadata, and writes everything to ClickHouse.

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

The database schema is in [`../db/create_tables/`](../db/create_tables/); the
authoritative copy is `CreateTables` in [`db/clickhouse.go`](db/clickhouse.go).
Both the database and its tables are created automatically on the first run.

One column is reserved rather than populated: `sandwiches.ataSame` is always
`false`. The detector links a sandwich's two legs by signer and by token-account
owner (`signerSame`, `ownerSame`), and computes no ATA-level comparison.

## Quick start

From a clean machine to a running detector:

```bash
# 0. Prerequisites: Go 1.24 and a running ClickHouse server (see below).
cd sandwich-detector

# 1. Configuration. Both files are gitignored; fill in your own endpoints and keys.
cp .env.example .env                 # ClickHouse credentials + RPC API keys
cp config.example.yaml config.yaml   # RPC and Jito base URLs

# 2. Build.
./scripts/build.sh                   # produces ./sandwich-detector

# 3. Verify the build without touching the network.
go test ./...                        # the whole suite is offline

# 4. Run. The database and all tables are created automatically on first start.
./scripts/leader.sh   400000000      # slot leaders (cheap, run continuously)
./scripts/sandwich.sh 400000000      # detection (the main worker)
./scripts/jito.sh     400000000      # Jito enrichment, once detection has output
```

The binary reads `config.yaml`, `.env` and `programs.yaml` from its **working
directory**, so run it from `sandwich-detector/` (the `scripts/` wrappers `cd`
there for you). It exits immediately with an actionable message if either
config file is missing or ClickHouse is unreachable.

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

Create the database used by the detector (default name `solwich`; set via
`CLICKHOUSE_DATABASE` in `.env`). The detector also bootstraps it automatically
on startup, so this step is optional:

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

`.env` holds ClickHouse credentials and RPC API keys:

```
CLICKHOUSE_ADDR=localhost:9000
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=solwich
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=

CHAINSTACK_API_KEY=YOUR-API-KEY   # joins sol.rpc-chainstack (default RPC, live + backfill)
HELIUS_RPC_API_KEY=YOUR-API-KEY   # joins sol.rpc-helius (backfill fallback)
```

`config.yaml` holds RPC and Jito endpoints:

```yaml
jito:
  bundles-url: https://bundles.jito.wtf/api/v1/bundles
sol:
  rpc-chainstack: https://solana-mainnet.core.chainstack.com  # default (live + backfill)
  rpc-helius: https://mainnet.helius-rpc.com                  # backfill fallback
  rpc:                                                        # optional self-hosted node, live fallback
```

config.yaml holds **base URLs only** — the API keys live in `.env` (`CHAINSTACK_API_KEY`
joins `rpc-chainstack` as the URL path; `HELIUS_RPC_API_KEY` joins `rpc-helius` as
`?api-key=`), so no keyed URL is ever written to config or logs. Resolution order —
live: Chainstack → self-hosted → Helius; backfill: Chainstack → Helius (the self-hosted
node keeps only ~6h of ledger and is never used for backfill).

Detection thresholds, parallelism, and timing intervals are compile-time
constants in [`config/config.go`](config/config.go); see the [Tuning](#tuning)
section.

## Build

```bash
./scripts/build.sh        # produces ./sandwich-detector
# equivalent to: go mod tidy && go build -o sandwich-detector .
```

## Subcommands

All subcommands except `reset` accept `-s <slot>` to set the starting slot;
`-t` silences stdout on every subcommand (logs still go to `./logs/`). The starting slot must be
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

Sandwich detection. It has two modes via `--mode` (default `live`):

**live** — the streaming loop. Each iteration:

1. Fetches a batch of blocks via `getBlock` (`SOL_FETCH_SLOT_DATA_SLOT_NUM`).
2. Builds sliding **double-rotation** windows (each leader rotation paired with its
   slot-adjacent successor) over a `CROSS_BLOCK_CACHE_SIZE`-block cache.
3. Runs one **unified** finder per window — in-block, same-leader cross-block, and
   cross-leader all come out of the same pass (two tiers: same-leader claims legs first).
4. Inserts results into `sandwiches`, `sandwich_txs`, and `slot_txs`.

```bash
./sandwich-detector sandwich -s 400000000                    # live, follows the tip
```

**backfill** — scans a bounded `[start, end]` range on an archival RPC (Chainstack by
default) and exits when done, using larger batch/worker counts (`BACKFILL_FETCH_*`) and
resolving leaders from block rewards:

```bash
./sandwich-detector sandwich --mode backfill -s <start> -e <end> [--rps <n>]
# --rps 0 (default) = no throttle (fine for paid RPC); set e.g. 10 for Helius free tier
```

The live loop runs forever; if `-s` is older than `SOL_FETCH_SLOT_DATA_MAX_GAP` slots it is
bumped forward to the oldest slot the RPC still serves.

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

Drops every table the detector creates; they are recreated empty on the next
run, so this discards every detection result. It asks for confirmation and
accepts only the database name typed back. Pass `--yes` to skip the prompt in
scripts.

```bash
./sandwich-detector reset          # prompts for the database name
./sandwich-detector reset --yes    # non-interactive
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
| `SANDWICH_AMOUNT_SOL_TOLERANCE`              |     0.1 | SOL-tokenB `perfect`-match tolerance (used as %) |
| `SANDWICH_FRONTRUN_MAX_GAP`                  |     500 | Max position gap for multi-front sandwiches      |
| `SANDWICH_BACKRUN_MAX_GAP`                   |     500 | Same, back-run side                              |
| `JITO_MARK_IN_BUNDLE_SAFE_LAG`               |    2000 | Lag behind sandwich frontier before marking      |

`INBLOCK_SANDWICH_AMOUNT_DIFF_THRESHOLD` and `SANDWICH_AMOUNT_SOL_TOLERANCE`
are the two that move the detected set most.

## Tests

The default suite runs with **no network access at all**. Every test that needs
a live endpoint is gated behind an environment variable and skips when it is
unset, so `go test ./...` is reproducible on a machine with no RPC credentials.

```bash
go test ./...                              # offline suite (live tests auto-skip)
go test ./sol -run TestSandwichFromJSON -v # deterministic fixture regression test
```

`sol/testdata/` holds the fixtures: `sandwiches/*.json` and `victims/*.json` drive
the regression tests in `sol/sandbox_test.go` (`TestSandwichFromJSON`,
`TestVictimSlippage`), and `account_owners.json` freezes the account -> owner-program
mappings that a few fixtures would otherwise resolve through `getMultipleAccounts`.
Delete that file and re-run with `SOL_TEST_RPC=<url>` to regenerate it.

Environment variables that unlock live tests, all optional:

| Variable                    | Unlocks                                              |
|-----------------------------|------------------------------------------------------|
| `SOL_TEST_RPC=<url>`        | pool-owner lookups instead of the frozen fixture      |
| `SOL_REAL_TEST=1` + `SOL_REAL_RPC=<url>` | live Solana RPC tests                    |
| `PARITY_RPC=<url>` (+ `PARITY_SLOT`)     | finder determinism on a real slot        |
| `WINDOW_TEST=1` + `PARITY_RPC=<url>`     | end-to-end windowed detection            |
| `SCAN_RPC=<url>`            | the ad-hoc slot-scan helpers                          |
| `JITO_REAL_TEST=1`          | live Jito bundles API                                 |
| `PERF_TEST=1`               | detection throughput benchmark                        |

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
cmd/        Cobra subcommand definitions (sandwich, jito, leader, reset)
config/     compile-time constants
db/         ClickHouse adapter + Database interface
jito/       Jito API client + RunJitoCmd (fetch + mark inBundle)
logger/     rotating slog handlers per subsystem
sol/        block fetcher, unified windowed sandwich detection
            (in-block / cross-block / cross-leader), victim slippage, poolDex
sol/dex/    DEX-specific instruction decoders (Raydium, Whirlpool, Meteora,
            PumpFun, PancakeSwap) used for slippage extraction
types/      shared types (Block, Transaction, Sandwich, JitoBundle, Slot*)
utils/      LRU caches, RPC helpers, program lists
scripts/    wrappers (build, run, stop, logs, reset)
```
