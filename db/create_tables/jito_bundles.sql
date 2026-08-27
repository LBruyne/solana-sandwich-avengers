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
PARTITION BY intDiv(slot, 432000)   -- one partition per epoch (enables delete-after-mark DROP PARTITION + confines slot-keyed mutations)
ORDER BY (slot, bundleId)
SETTINGS index_granularity = 8192