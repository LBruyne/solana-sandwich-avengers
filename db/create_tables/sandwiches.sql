CREATE TABLE IF NOT EXISTS solwich.sandwiches
(
    `sandwichId` String,     -- Hash of frontTx.signature + backTx.signature
    `crossBlock` Bool,
    `crossLeader` Bool DEFAULT false,                       -- front and back under different slot leaders (implies crossBlock)
    `frontLeader` String DEFAULT '',                        -- leader of the first front-run slot
    `backLeader` String DEFAULT '',                         -- leader of the last back-run slot
    `windowStartSlot` UInt64 DEFAULT 0,                     -- detection window bounds
    `windowEndSlot` UInt64 DEFAULT 0,
    `rpcSource` LowCardinality(String) DEFAULT 'live',      -- 'live' (live loop) or 'backfill' (backfill run)
    `slot` UInt64,
    `timestamp` DateTime,

    `tokenA` String,
    `tokenB` String,

    `hasTransfer` Bool,
    `hasFrontInlineTransfer` Bool,
    `hasDirectTransfer` Bool,
    `hasBackInlineTransfer` Bool,
    `signerSame` Bool,
    `ownerSame` Bool,
    `ataSame` Bool,
    `consecutive` Bool,

    `multiFrontRun` Bool,
    `multiBackRun` Bool,
    `multiVictim` Bool,
    `frontCount` UInt16,
    `backCount` UInt16,
    `victimCount` UInt16,
    `adverseCount` UInt16,
    `frontConsecutive` Bool,
    `backConsecutive` Bool,
    `victimConsecutive` Bool,

    `perfect` Bool,
    `relativeDiffB` Float64,
    `profitA` Float64,

    `intentScore` Float64 DEFAULT 0,  -- Intent score for sandwich attack (0-1, higher = more likely intentional)
    `maxSlippageUtilization` Float64 DEFAULT 0  -- Max slippage utilization across all victims (0-1)
)
ENGINE = MergeTree
ORDER BY (slot, timestamp, sandwichId)
SETTINGS index_granularity = 8192;