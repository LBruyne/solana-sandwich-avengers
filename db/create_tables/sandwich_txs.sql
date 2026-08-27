CREATE TABLE IF NOT EXISTS solwich.sandwich_txs
(
    `sandwichId` String,
    `sandwichTimestamp` DateTime,
    `type` String,           -- "frontRun" / "victim" / "backRun" / "transfer" / "adverse"

    `slot` UInt64,
    `position` Int32,
    `timestamp` DateTime,
    `fee` UInt64,
    `signature` String,
    `signers` Array(String),
    `inBundle` Bool,
    `accountKeys` Array(String),
    `programs` Array(String),

    `fromToken` String,
    `toToken` String,
    `fromAmount` Float64,
    `toAmount` Float64,
    `attackerPreBalanceB` Float64,  -- Attacker's tokenB balance before tx
    `attackerPostBalanceB` Float64, -- Attacker's tokenB balance after tx
    `poolPreBalanceB` Float64,      -- Pool's tokenB balance before tx
    `poolPostBalanceB` Float64,     -- Pool's tokenB balance after tx
    `ownersOfB` Array(String), -- Possible attacker owners, i.e., owners of ATAs that hold tokenB in front-run and back-run

    -- Only the last front-run or back-run tx in a multi-front or multi-back sandwich has the total amount
    `fromTotalAmount` Float64,
    `toTotalAmount` Float64,

    -- Only the last back-run txs in a sandwich have the diff
    `diffA` Float64,         -- back.ToTotal - front.FromTotal
    `diffB` Float64,         -- front.ToTotal - back.FromTotal

    -- Slippage fields (meaningful only for victim txs)
    `slippageLimitType` String DEFAULT '',        -- "input" (max cost) / "output" (min output) / "" (unavailable)
    `slippageLimitAmount` Float64 DEFAULT 0,      -- decoded limit value, converted to float64 with decimals
    `slippageActualAmount` Float64 DEFAULT 0,     -- actual cost or output from balance deltas
    `slippageUtilization` Float64 DEFAULT -1,     -- ratio 0-1 (closer to 1 = tighter fit); -1 no protection, -2 unsupported, -3 missing inner, -4 ambiguous (multi-swap)

    -- Exchange the sandwiched pool belongs to, set on every leg (front/back/victim/adverse) from the
    -- front-run's DEX instruction. 100% coverage incl. proprietary AMMs; single DEX field.
    `poolDex` LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY intDiv(slot, 432000)   -- one partition per epoch (enables delete-after-mark DROP PARTITION + confines slot-keyed mutations)
ORDER BY (sandwichTimestamp, sandwichId, timestamp, slot, position)
SETTINGS index_granularity = 8192;