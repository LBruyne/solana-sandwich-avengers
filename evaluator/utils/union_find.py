import hashlib
from collections import defaultdict


class UnionFind:
    """Disjoint-set with path compression and union by rank."""

    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.rank.setdefault(ra, 0)
        self.rank.setdefault(rb, 0)
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def build_attacker_mapping(df_txs):
    """Cluster signers via Union-Find and map each sandwichId to an attacker key.

    Parameters
    ----------
    df_txs : DataFrame
        sandwich_txs with columns: sandwichId, type, primarySigner.
        Only frontRun/backRun/transfer rows are used.

    Returns
    -------
    sandwich_to_attacker : dict[str, str]
        sandwichId -> attacker_key
    attacker_members : dict[str, set[str]]
        attacker_key -> set of signer addresses
    """
    attacker_types = {"frontRun", "backRun", "transfer"}
    atk_txs = df_txs[df_txs["type"].isin(attacker_types)]

    uf = UnionFind()
    sandwich_signers = defaultdict(set)

    for _, row in atk_txs.iterrows():
        sw_id = row["sandwichId"]
        signer = row["primarySigner"]
        if signer:
            sandwich_signers[sw_id].add(signer)

    # Union signers that appear in the same sandwich
    for signers in sandwich_signers.values():
        signers_list = list(signers)
        for i in range(1, len(signers_list)):
            uf.union(signers_list[0], signers_list[i])

    # Build connected components
    components = defaultdict(set)
    all_signers = set()
    for signers in sandwich_signers.values():
        all_signers.update(signers)
    for signer in all_signers:
        root = uf.find(signer)
        components[root].add(signer)

    # Generate deterministic attacker keys
    root_to_key = {}
    attacker_members = {}
    for root, members in components.items():
        key = hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:16]
        root_to_key[root] = key
        attacker_members[key] = members

    # Map sandwichId -> attacker_key
    sandwich_to_attacker = {}
    for sw_id, signers in sandwich_signers.items():
        first_signer = next(iter(signers))
        root = uf.find(first_signer)
        sandwich_to_attacker[sw_id] = root_to_key[root]

    return sandwich_to_attacker, attacker_members
