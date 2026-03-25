# Program classification for Solana sandwich attack analysis.
# Sources: watcher/programs.yaml, watcher/sol/dex/slippage.go

# --- Builtin programs (always ignore) ---
BUILTIN_PROGRAMS = {
    "11111111111111111111111111111111",                          # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",             # Token Program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",            # Associated Token Account
    "ComputeBudget111111111111111111111111111111",               # Compute Budget
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",             # Token-2022
    "Vote111111111111111111111111111111111111111",               # Vote
    "Stake11111111111111111111111111111111111111",               # Stake
    "Config1111111111111111111111111111111111111",               # Config
    "So11111111111111111111111111111111111111112",               # Wrapped SOL (mint, not program)
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",            # Memo v2
    "Memo1UhkJBfCR6MNBHAdLJFTxj7FQ3qtMRF5eTh5j5r",            # Memo v1
    "BPFLoaderUpgradeab1e11111111111111111111111",              # BPF Loader
    "Ed25519SigVerify111111111111111111111111111",              # Ed25519
    "KeccakSecp256k11111111111111111111111111111",              # Secp256k1
    "SysvarC1ock11111111111111111111111111111111",              # Sysvar Clock
    "SysvarRent111111111111111111111111111111111",              # Sysvar Rent
}

# --- Known aggregators (filter out, not meaningful for program profiling) ---
AGGREGATOR_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",            # Jupiter Aggregator v6
    "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M",          # Jupiter DCA
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma",           # OKX DEX Aggregation Router V2
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH",           # DFlow Aggregator v4
    "AxiomfHaWDemCFBLBayqnEnNwE6b7B2Qz3UmzMpgbMG6",           # Axiom Trading Program 1
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS",            # Raydium AMM Routing
}

# --- Known DEX programs (from slippage.go + programs.yaml) ---
KNOWN_DEX_PROGRAMS = {
    # Major AMMs (slippage-supported)
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",            # Pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",             # Pump.fun AMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",           # Raydium V4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",            # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",            # Raydium CLMM
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",            # Meteora DBC
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",             # Meteora DAMM v2
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",            # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",           # Meteora Pools
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",             # Whirlpools
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",           # Orca Token Swap V2
    "DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1",           # Orca Token Swap V1
    "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq",           # PancakeSwap
    # PropAMM / smaller DEXes
    "SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF",            # SolFi V2
    "BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi",            # BisonFi
    "9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp",           # HumidiFi
    "fUSioN9YKKSa3CUC2YUc4tPkHJ5Y6XW1yz8y6F7qWz9",            # Fusion AMM
    "TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH",            # Tessera V
    "goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE",             # GoonFi V2
    "ALPHAQmeA7bjrVuccPsYPiCvsi428SNwte66Srvs4pHA",            # AlphaQ
    "obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y",             # Obric V2
    "ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY",             # ZeroFi
    "Dooar9JkhdZ7J3LHN3A7YCuoGRUggXhQaG4kijfLGU2j",           # StepN DOOAR
    "REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2",            # Byreal CLMM
}

# All known (non-custom) programs
_ALL_KNOWN = BUILTIN_PROGRAMS | AGGREGATOR_PROGRAMS | KNOWN_DEX_PROGRAMS


def classify_program(program_id: str) -> str:
    """Classify a program as builtin, aggregator, dex, or custom."""
    if program_id in BUILTIN_PROGRAMS:
        return "builtin"
    if program_id in AGGREGATOR_PROGRAMS:
        return "aggregator"
    if program_id in KNOWN_DEX_PROGRAMS:
        return "dex"
    return "custom"


def get_representative_program(tx_programs_lists: list) -> str:
    """Pick the representative program from front/back tx program arrays.

    Filters out builtin and aggregator programs, then returns the least common
    remaining program (custom > dex). If no meaningful program remains, returns "".

    Parameters
    ----------
    tx_programs_lists : list of list[str]
        Each element is the programs array from one front/back tx.
    """
    from collections import Counter

    candidates = Counter()
    for programs in tx_programs_lists:
        for p in programs:
            if p not in BUILTIN_PROGRAMS and p not in AGGREGATOR_PROGRAMS:
                candidates[p] += 1

    if not candidates:
        return ""

    # Prefer custom programs over known DEX; among same type, pick least common
    custom = {p: c for p, c in candidates.items() if p not in KNOWN_DEX_PROGRAMS}
    if custom:
        return min(custom, key=custom.get)

    return min(candidates, key=candidates.get)
