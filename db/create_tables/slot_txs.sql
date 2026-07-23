CREATE TABLE IF NOT EXISTS solwich.slot_txs
(
    `slot` UInt64,
    `txFetched` Bool,
    `txCount` UInt64,
    `validTxCount` UInt64,
    `sandwichFetched` Bool,
    `sandwichCount` UInt64,
    `sandwichTxCount` UInt64,
    `sandwichVictimCount` UInt64,
    `sandwichInBundleChecked` Bool
)
ENGINE = ReplacingMergeTree
PARTITION BY intDiv(slot, 432000)   -- one partition per epoch
PRIMARY KEY slot
ORDER BY slot
SETTINGS index_granularity = 8192