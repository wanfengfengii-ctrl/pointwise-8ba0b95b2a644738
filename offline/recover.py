from collections import defaultdict, deque

from inventory.rows import add_row_changes
from offline.catalog import get_received_package
from offline.render import render_rows
from publication.package import PackageEvidence, decode_package
from storage import EvidenceError, NotFoundError, require_text


def recover_revision_rows(evidence: PackageEvidence, revision):
    """Find an inventory and replay the route from that inventory to the request."""
    require_text(revision, 'revision')
    if revision not in evidence.revisions:
        raise NotFoundError(f'revision {revision!r} not declared in this package')

    anchors = {record.revision: record.rows for record in evidence.inventories}
    routes = defaultdict(list)
    for delta in evidence.deltas:
        # The attached sign describes replay toward the request after the search.
        routes[delta.source].append((delta.target, delta.changes, -1))
        routes[delta.target].append((delta.source, delta.changes, 1))

    pending = deque([revision])
    back_to_request = {revision: None}
    while pending:
        current = pending.popleft()
        if current in anchors:
            counts = anchors[current].copy()
            step = back_to_request[current]
            while step is not None:
                current, changes, direction = step
                counts = add_row_changes(counts, changes, direction)
                if any(count < 0 for count in counts.values()):
                    raise EvidenceError(f'negative row count at revision {current!r}')
                step = back_to_request[current]
            return render_rows(counts)
        for neighbor, changes, direction in routes[current]:
            if neighbor not in back_to_request:
                back_to_request[neighbor] = (current, changes, direction)
                pending.append(neighbor)
    raise EvidenceError(f'revision {revision!r} has no inventory in this package')


def recover(db, package_id, revision):
    blob = get_received_package(db, package_id)
    return recover_revision_rows(decode_package(blob), revision)
