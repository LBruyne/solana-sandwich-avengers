CREATE TABLE IF NOT EXISTS solwich.jito_bundles
(
    `bundleId` String,
    `slot` UInt64,
    `timestamp` DateTime,
    `tippers` Array(String),
    `transactions` Array(String),
    `landedTipLamports` UInt64
)
ENGINE = MergeTree
ORDER BY (slot, bundleId)
SETTINGS index_granularity = 8192