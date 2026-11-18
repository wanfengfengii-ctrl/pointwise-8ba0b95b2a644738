from collections import defaultdict, deque

from inventory.rows import add_row_changes
from publication.package import PackageEvidence
from storage import EvidenceError


def reconcile_evidence(evidence: PackageEvidence) -> None:
    """Require one common nonnegative row ledger for the entire selected scope."""
    neighbors = defaultdict(list)
    for delta in evidence.deltas:
        neighbors[delta.source].append((delta.target, delta, 1))
        neighbors[delta.target].append((delta.source, delta, -1))

    accounts = {}
    pending = deque()

    def require_account(revision, candidate, origin):
        if revision in accounts:
            if accounts[revision] != candidate:
                raise EvidenceError(f'{origin} disagrees with the account of revision {revision!r}')
            return
        if any(count < 0 for count in candidate.values()):
            raise EvidenceError(f'{origin} gives revision {revision!r} a negative row count')
        accounts[revision] = candidate
        pending.append(revision)

    for inventory in evidence.inventories:
        require_account(inventory.revision, inventory.rows, f'inventory {inventory.inventory_id!r}')
    while pending:
        revision = pending.popleft()
        for target, delta, direction in neighbors[revision]:
            candidate = add_row_changes(accounts[revision], delta.changes, direction)
            require_account(target, candidate, f'delta {delta.delta_id!r}')

    missing = sorted(set(evidence.revisions) - accounts.keys())
    if missing:
        raise EvidenceError(f'revisions without an inventory: {", ".join(missing)}')
