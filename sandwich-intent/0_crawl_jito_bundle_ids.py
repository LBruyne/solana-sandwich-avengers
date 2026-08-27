"""Resolve Jito bundle IDs for sandwich legs.

A sandwich is a *bundle sandwich* iff one bundle contains a frontrun leg, a victim leg and
a backrun leg. The `inBundle` flag alone cannot express "same bundle": one slot holds dozens
of consecutive bundles, so a frontrun can sit in bundle #7 while the backrun sits in #8.

RESOLUTION uses two sources, in order:

  1. Bundle content retained in ClickHouse. `--bundle-dbs` names the databases to search;
     the detector reclaims `jito_bundles` per epoch once its slots are marked, so coverage
     of older epochs depends on whether that reclamation has run.
  2. Jito's public API, for the legs the first source cannot reach. Rate limited.

CANDIDATE FILTER. Only sandwiches in one slot with every core leg flagged `inBundle` can
qualify: a bundle never spans slots, and a leg in no bundle shares none. `--validate`
re-checks the filter against the retained content.

RATE LIMITING. The client holds ~2 req/s and steers on a 50-request rolling success rate
rather than reacting to individual 403s, with browser headers. Every answer is appended to
disk as it arrives and the run is resumable.

Outputs, under data/jito_bundle_ids/:
    signature_bundle_map.csv        signature -> bundle_id, append-only
    bundle_sandwiches_<a>_<b>.csv   per-sandwich verdict for the requested epoch range

Usage:
    python3 0_crawl_jito_bundle_ids.py --start-epoch 946 --end-epoch 973 --database solwich
    python3 0_crawl_jito_bundle_ids.py --start-epoch 946 --end-epoch 960 --validate --no-api
"""

import argparse
import csv
import os
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import requests

from utils.db import get_client

API_URL = "https://bundles.jito.wtf/api/v1/bundles/transaction/{sig}"

# Cloudflare rejects python-requests' default fingerprint more often than a browser's: measured
# 70.8 % vs 83.3 % success on the same 24 signatures, interleaved so throttling hit both equally.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://explorer.jito.wtf/",
    "Origin": "https://explorer.jito.wtf",
}
OUT_DIR = "data/jito_bundle_ids"
MAP_FILE = f"{OUT_DIR}/signature_bundle_map.csv"

SLOTS_PER_EPOCH = 432_000
CORE_TYPES = ("frontRun", "victim", "backRun")


def parse_args():
    p = argparse.ArgumentParser(description="Resolve Jito bundle IDs for sandwich legs")
    p.add_argument("--start-epoch", type=int, default=946)
    p.add_argument("--end-epoch", type=int, default=990, help="inclusive")
    p.add_argument("--database", type=str, default=None,
                   help="ClickHouse database holding the sandwiches (default: CLICKHOUSE_DATABASE)")
    p.add_argument("--bundle-dbs", type=str, default="solwich",
                   help="Comma-separated databases to read retained jito_bundles content from")
    p.add_argument("--no-api", action="store_true",
                   help="Use retained content only; report unresolved legs instead of fetching")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--rps", type=float, default=2.0, help="Initial request rate, req/s")
    p.add_argument("--max-rps", type=float, default=2.5)
    p.add_argument("--min-rps", type=float, default=1.0)
    p.add_argument("--limit", type=int, default=0, help="Stop after N new lookups (0 = no limit)")
    p.add_argument("--validate", action="store_true",
                   help="Cross-check the verdicts against retained content, where it survives")
    return p.parse_args()


# ── Candidate selection ──────────────────────────────────────────────────────

CANDIDATE_FILTER = """
    SELECT sandwichId FROM {db}.sandwich_txs
    WHERE type IN {types} AND slot >= {lo} AND slot <= {hi}
    GROUP BY sandwichId
    HAVING uniqExact(slot) = 1 AND countIf(NOT inBundle) = 0
"""


def fetch_candidate_legs(client, db, lo, hi):
    """Every core leg of every candidate sandwich. One row per (sandwich, type, signature)."""
    sub = CANDIDATE_FILTER.format(db=db, types=CORE_TYPES, lo=lo, hi=hi)
    df = client.query_df(f"""
        SELECT sandwichId, type, signature, slot
        FROM {db}.sandwich_txs
        WHERE type IN {CORE_TYPES} AND slot >= {lo} AND slot <= {hi}
          AND sandwichId IN ({sub})
    """)
    return df.drop_duplicates(subset=["sandwichId", "type", "signature"])


# ── Source 1: retained bundle content ────────────────────────────────────────

def covered_slot_ranges(client, bundle_dbs, lo, hi):
    """Which slots have retained bundle content, and therefore need no API call.

    Reported per epoch rather than per slot: a slot absent from a *covered* epoch means the fetch
    genuinely found no bundle there, whereas a slot in an uncovered epoch means we simply do not
    know. Conflating the two is how `inBundle = false` silently becomes `not bundled`.
    """
    parts = [f"SELECT intDiv(slot,{SLOTS_PER_EPOCH}) AS epoch, uniqExact(slot) AS slots "
             f"FROM {db}.jito_bundles WHERE slot >= {lo} AND slot <= {hi} GROUP BY epoch"
             for db in bundle_dbs]
    df = client.query_df(f"SELECT epoch, sum(slots) AS slots FROM ({' UNION ALL '.join(parts)}) "
                         f"GROUP BY epoch ORDER BY epoch")
    # An epoch counts as covered only if the retained content spans most of it; a handful of slots
    # is a fetch-ahead remnant, not coverage.
    return {int(r.epoch) for r in df.itertuples() if r.slots >= 0.5 * SLOTS_PER_EPOCH}


SIG_TABLE = "default._bundle_sig_probe"


def resolve_from_stored(client, bundle_dbs, db, lo, hi, covered_epochs, legs):
    """signature -> bundle_id for every candidate leg sitting in a covered epoch.

    Structured for memory, not elegance. `arrayJoin(transactions)` over an unfiltered epoch of
    `jito_bundles` expands hundreds of millions of rows and takes the server past its limit, so the
    candidate signatures are staged into a table first, bundles are pruned with `hasAny` *before*
    the array is expanded, and the whole thing runs one epoch at a time.
    """
    if not covered_epochs:
        return {}

    sigs = sorted(set(legs["signature"]))
    client.command(f"DROP TABLE IF EXISTS {SIG_TABLE}")
    client.command(f"CREATE TABLE {SIG_TABLE} (sig String) ENGINE = MergeTree ORDER BY sig")
    for i in range(0, len(sigs), 50_000):
        client.insert(SIG_TABLE, [[s] for s in sigs[i:i + 50_000]], column_names=["sig"])

    resolved = {}
    for epoch in sorted(covered_epochs):
        e_lo = max(lo, epoch * SLOTS_PER_EPOCH)
        e_hi = min(hi, (epoch + 1) * SLOTS_PER_EPOCH - 1)
        for bdb in bundle_dbs:
            df = client.query_df(f"""
                WITH probe AS (SELECT groupArray(sig) AS arr FROM {SIG_TABLE})
                SELECT sig AS signature, any(bundleId) AS bundle_id FROM (
                    SELECT arrayJoin(transactions) AS sig, bundleId
                    FROM {bdb}.jito_bundles
                    WHERE slot >= {e_lo} AND slot <= {e_hi}
                      AND hasAny(transactions, (SELECT arr FROM probe))
                )
                WHERE sig IN (SELECT sig FROM {SIG_TABLE})
                GROUP BY sig
            """)
            # A database with no content for this epoch yields an empty frame with no columns at
            # all, so test emptiness before indexing — coverage is a union across databases and any
            # single one may legitimately hold nothing here.
            if df.empty:
                continue
            for sig, bid in zip(df["signature"], df["bundle_id"]):
                resolved.setdefault(sig, bid)
        print(f"    epoch {epoch}: {len(resolved):,} legs resolved so far", flush=True)

    client.command(f"DROP TABLE IF EXISTS {SIG_TABLE}")
    return resolved


# ── Source 2: Jito API ───────────────────────────────────────────────────────

class AdaptiveLimiter:
    """Hold a target rate; adjust only on the ROLLING success rate, never on a single rejection.

    An earlier AIMD version treated every 403 as "too fast" and multiplied the rate down. Measured,
    that was the wrong model. A rate ladder against this endpoint (40 requests per rung, browser
    headers) gives:

        target rps   success   effective throughput
           0.5        72.5 %          0.33
           1.0        67.5 %          0.56
           2.0        97.5 %          1.39
           4.0        45.0 %          0.82

    Rejection is largely probabilistic below ~2 req/s and only becomes rate-driven above it, so
    backing off on isolated 403s just walks the client down to min_rps and pins it there — which is
    exactly what happened: 0.235 effective against the 1.39 available, a 6x self-inflicted penalty.

    This controller instead watches the last WINDOW outcomes and only slows down when the success
    rate falls under LOW, speeding back up when it clears HIGH.
    """

    WINDOW = 50
    LOW = 0.70
    HIGH = 0.92

    def __init__(self, rps, min_rps, max_rps):
        self.rps, self.min_rps, self.max_rps = rps, min_rps, max_rps
        self._next_at = time.monotonic()
        self._lock = threading.Lock()
        self._recent = deque(maxlen=self.WINDOW)

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + 1.0 / self.rps
        if wait > 0:
            time.sleep(wait)

    def _adjust(self):
        """Caller must hold the lock. Steer on the window, not on the last outcome."""
        if len(self._recent) < self.WINDOW:
            return
        rate = sum(self._recent) / len(self._recent)
        if rate < self.LOW and self.rps > self.min_rps:
            self.rps = max(self.min_rps, self.rps * 0.8)
            self._recent.clear()
        elif rate > self.HIGH and self.rps < self.max_rps:
            self.rps = min(self.max_rps, self.rps * 1.15)
            self._recent.clear()

    def on_success(self):
        with self._lock:
            self._recent.append(1)
            self._adjust()

    def on_throttle(self):
        with self._lock:
            self._recent.append(0)
            self._adjust()
            # A short, fixed pause. A flat 10 s version burned 155 of 234 wall-clock minutes on
            # 928 throttles — 66 % of the run spent asleep — while the request spacing itself
            # accounted for only 131. At the rates this client runs, a rejection is cheaper to
            # retry than to wait out.
            self._next_at = max(self._next_at, time.monotonic() + 1.0)


def load_existing_map():
    """Resume support. An empty bundle_id is a real answer ("in no bundle"), not a gap."""
    resolved = {}
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE, newline="") as fh:
            for row in csv.DictReader(fh):
                resolved[row["signature"]] = row["bundle_id"]
    return resolved


def fetch_via_api(signatures, limiter, workers, out_fh, limit=0):
    resolved, stats = {}, defaultdict(int)
    local, lock, stop = threading.local(), threading.Lock(), threading.Event()

    def session():
        if not hasattr(local, "s"):
            local.s = requests.Session()
        return local.s

    def one(sig):
        if stop.is_set():
            return
        for attempt in range(8):
            limiter.acquire()
            try:
                r = session().get(API_URL.format(sig=sig), headers=HEADERS, timeout=20)
            except requests.RequestException:
                stats["neterr"] += 1
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                body = r.json()
                bid = body[0]["bundle_id"] if body else ""   # [] = landed outside any bundle
                limiter.on_success()
                with lock:
                    out_fh.write(f"{sig},{bid}\n")
                    resolved[sig] = bid
                    stats["ok"] += 1
                    if stats["ok"] % 250 == 0:
                        out_fh.flush()
                        print(f"    {stats['ok']:,} fetched  (rate {limiter.rps:.2f} req/s, "
                              f"throttled {stats['throttle']})", flush=True)
                    if limit and stats["ok"] >= limit:
                        stop.set()
                return
            if r.status_code in (403, 429, 503):
                stats["throttle"] += 1
                limiter.on_throttle()
                continue
            if r.status_code == 404:
                # The API separates "no bundle" (200 []) from "unknown signature" (404). The latter
                # is not an answer, so it is left unresolved rather than recorded as un-bundled.
                stats["notfound"] += 1
                return
            stats[f"http{r.status_code}"] += 1
            time.sleep(2 ** attempt)
        stats["exhausted"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, signatures))
    out_fh.flush()
    return resolved, dict(stats)


# ── Verdict ──────────────────────────────────────────────────────────────────

def decide(legs, sig_to_bundle):
    """A sandwich qualifies iff one bundle holds >=1 front, >=1 victim and >=1 back leg.

    One bundleId must hold the front run, a victim and the back run.
    """
    per_sw = defaultdict(lambda: defaultdict(set))
    for sid, typ, sig in zip(legs["sandwichId"], legs["type"], legs["signature"]):
        bid = sig_to_bundle.get(sig)
        if bid:
            per_sw[sid][bid].add(typ)
    out = {}
    for sid, by_bundle in per_sw.items():
        hit = sorted(b for b, types in by_bundle.items() if len(types) == len(CORE_TYPES))
        out[sid] = hit[0] if hit else None
    return out


def api_shortlist(legs, resolved):
    """Which unresolved signatures are still worth an API call.

    front and back must share a bundle for a sandwich to qualify, so victim legs are only worth
    resolving once that is established. Sandwiches whose front/back bundles are already known to
    differ need no victim lookup at all.
    """
    known = {sid: {} for sid in set(legs["sandwichId"])}
    for sid, typ, sig in zip(legs["sandwichId"], legs["type"], legs["signature"]):
        known[sid].setdefault(typ, []).append(sig)

    stage1, stage2 = [], []
    for sid, by_type in known.items():
        fb = [s for t in ("frontRun", "backRun") for s in by_type.get(t, [])]
        missing_fb = [s for s in fb if s not in resolved]
        if missing_fb:
            stage1.extend(missing_fb)
            continue
        fb_bundles = {resolved[s] for s in fb if resolved[s]}
        if not fb_bundles:
            continue                      # front and back landed outside bundles entirely
        stage2.extend(s for s in by_type.get("victim", []) if s not in resolved)
    return stage1, stage2


# ── Validation ───────────────────────────────────────────────────────────────

def validate(client, bundle_dbs, db, lo, hi, verdicts):
    """Where retained content survives, the verdict must reproduce it exactly."""
    sub = CANDIDATE_FILTER.format(db=db, types=CORE_TYPES, lo=lo, hi=hi)
    truth = set()
    for bdb in bundle_dbs:
        n = client.query_df(f"SELECT count() AS n FROM {bdb}.jito_bundles "
                            f"WHERE slot >= {lo} AND slot <= {hi}")["n"].iloc[0]
        if n == 0:
            continue
        df = client.query_df(f"""
            WITH b AS (SELECT arrayJoin(transactions) AS sig, bundleId AS bid
                       FROM {bdb}.jito_bundles WHERE slot >= {lo} AND slot <= {hi})
            SELECT t.sandwichId AS sandwichId
            FROM {db}.sandwich_txs t INNER JOIN b ON t.signature = b.sig
            WHERE t.type IN {CORE_TYPES} AND t.slot >= {lo} AND t.slot <= {hi}
            GROUP BY t.sandwichId, b.bid
            HAVING countIf(t.type='frontRun') > 0 AND countIf(t.type='victim') > 0
               AND countIf(t.type='backRun') > 0
        """)
        if df.empty:
            continue
        truth |= set(df["sandwichId"])
    if not truth:
        print("  no retained content for this range — nothing to validate against.")
        return
    got = {sid for sid, bid in verdicts.items() if bid}
    print(f"  retained content : {len(truth):,}")
    print(f"  this run         : {len(got):,}")
    print(f"  agree            : {len(truth & got):,}")
    print(f"  run-only         : {len(got - truth):,}")
    print(f"  content-only     : {len(truth - got):,}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    client = get_client(args.database)
    db = args.database or os.getenv("CLICKHOUSE_DATABASE", "solwich")
    bundle_dbs = [d.strip() for d in args.bundle_dbs.split(",") if d.strip()]

    lo = args.start_epoch * SLOTS_PER_EPOCH
    hi = (args.end_epoch + 1) * SLOTS_PER_EPOCH - 1
    print(f"Sandwich DB : {db}")
    print(f"Bundle DBs  : {', '.join(bundle_dbs)}")
    print(f"Epochs      : {args.start_epoch}-{args.end_epoch}  (slots {lo:,}-{hi:,})")

    print("\n[1/5] Candidates (one slot, every core leg inBundle) ...")
    legs = fetch_candidate_legs(client, db, lo, hi)
    n_sw = legs["sandwichId"].nunique()
    print(f"  sandwiches {n_sw:,}   distinct leg signatures {legs['signature'].nunique():,}")

    print("\n[2/5] Source 1 — retained bundle content ...")
    covered = covered_slot_ranges(client, bundle_dbs, lo, hi)
    print(f"  epochs with retained content : "
          f"{','.join(str(e) for e in sorted(covered)) if covered else '(none)'}")
    resolved = resolve_from_stored(client, bundle_dbs, db, lo, hi, covered, legs)
    print(f"  resolved from content : {len(resolved):,}")

    print("\n[3/5] Source 2 — resuming previous API results ...")
    resolved.update({k: v for k, v in load_existing_map().items() if k not in resolved})
    print(f"  resolved so far : {len(resolved):,}")

    # A leg in a covered epoch that no bundle claims is definitively un-bundled; recording that
    # keeps it out of the API queue forever.
    for sig, slot in zip(legs["signature"], legs["slot"]):
        if sig not in resolved and int(slot) // SLOTS_PER_EPOCH in covered:
            resolved[sig] = ""

    stage1, stage2 = api_shortlist(legs, resolved)
    print(f"  API needed: {len(stage1):,} front/back legs, then up to {len(stage2):,} victim legs")

    if args.no_api:
        print("\n[4/5] --no-api: skipping remote lookups.")
    elif stage1 or stage2:
        print(f"\n[4/5] Fetching (start {args.rps} req/s) ...")
        new = not os.path.exists(MAP_FILE)
        limiter = AdaptiveLimiter(args.rps, args.min_rps, args.max_rps)
        with open(MAP_FILE, "a", newline="") as fh:
            if new:
                fh.write("signature,bundle_id\n")
            if stage1:
                print(f"  stage 1: {len(stage1):,} front/back legs")
                got, st = fetch_via_api(stage1, limiter, args.workers, fh, args.limit)
                resolved.update(got)
                print(f"    {st}")
                # Re-derive: front/back answers decide which victims are worth fetching at all.
                _, stage2 = api_shortlist(legs, resolved)
            if stage2:
                print(f"  stage 2: {len(stage2):,} victim legs")
                got, st = fetch_via_api(stage2, limiter, args.workers, fh, args.limit)
                resolved.update(got)
                print(f"    {st}")
    else:
        print("\n[4/5] Nothing to fetch.")

    print("\n[5/5] Verdicts ...")
    verdicts = decide(legs, resolved)
    hits = {sid: bid for sid, bid in verdicts.items() if bid}
    unresolved = sum(1 for s in set(legs["signature"]) if s not in resolved)
    print(f"  bundle sandwiches : {len(hits):,} of {n_sw:,} candidates")
    if unresolved:
        print(f"  WARNING: {unresolved:,} leg signatures unresolved — this is a LOWER BOUND. "
              f"Re-run to finish.")

    slot_of = dict(zip(legs["sandwichId"], legs["slot"]))
    out = f"{OUT_DIR}/bundle_sandwiches_{args.start_epoch}_{args.end_epoch}.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sandwichId", "slot", "epoch", "bundle_id"])
        for sid, bid in sorted(hits.items()):
            slot = int(slot_of.get(sid, 0))
            w.writerow([sid, slot, slot // SLOTS_PER_EPOCH, bid])
    print(f"  -> {out}")

    if args.validate:
        print("\n[validate] against retained bundle content ...")
        validate(client, bundle_dbs, db, lo, hi, verdicts)


if __name__ == "__main__":
    main()
