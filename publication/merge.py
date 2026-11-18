"""V2 exchange documents for merging two correction records onto one base ledger.

The chosen exchange layout is the *inline* layout: the common fields only, with
``left`` and ``right`` holding ordered ``[operation_id, rows]`` arrays.  The same
operation identifier on both sides carries the same normalized delta and is
executed exactly once.
"""

from dataclasses import dataclass

from inventory.rows import (RowCounts, add_row_changes, normalize_delta_rows,
                            normalize_inventory_rows, ordered_rows)
from publication.package import decode_package
from storage import (EvidenceError, InputError, PackageError, decode_json,
                     encode_json, members, require_text)

MERGED_REVISION = 'merged'
MAX_PACKAGE_ID = 2 ** 63 - 1
V2_FIELDS = frozenset(('version', 'partition', 'base_package_id', 'base_revision',
                       'base_rows', 'left', 'right', 'order'))


@dataclass(frozen=True)
class MergeOperation:
    operation_id: str
    changes: RowCounts


@dataclass(frozen=True)
class MergeDocument:
    partition: str
    base_package_id: int
    base_revision: str
    base_rows: RowCounts
    left: tuple[MergeOperation, ...]
    right: tuple[MergeOperation, ...]
    order: tuple[str, ...]


# ---------------------------------------------------------------------------
# Business input handling (publish side)
# ---------------------------------------------------------------------------

def _record(values, label):
    operations = []
    seen = set()
    for item in members(values, label):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise InputError(f'{label} operation must contain an identifier and rows')
        operation_id = require_text(item[0], 'operation identifier')
        if operation_id in seen:
            raise InputError(f'duplicate operation {operation_id!r} in {label}')
        seen.add(operation_id)
        operations.append(MergeOperation(operation_id, normalize_delta_rows(item[1])))
    return tuple(operations)


def _shared_deltas(left, right):
    left_map = {operation.operation_id: operation.changes for operation in left}
    right_map = {operation.operation_id: operation.changes for operation in right}
    for operation_id in sorted(left_map.keys() & right_map.keys()):
        if left_map[operation_id] != right_map[operation_id]:
            raise InputError(f'operation {operation_id!r} has different normalized deltas')
    return left_map, right_map


def normalize_records(left, right):
    """Normalize both ordered records and require shared identifiers to agree."""
    try:
        left_operations = _record(left, 'left')
        right_operations = _record(right, 'right')
        _shared_deltas(left_operations, right_operations)
    except EvidenceError:
        raise
    except InputError as exc:
        raise EvidenceError(f'invalid merge records: {exc}') from exc
    return left_operations, right_operations


# ---------------------------------------------------------------------------
# Execution search
# ---------------------------------------------------------------------------

def find_execution(base_rows, left, right):
    """Return a legal execution order of the two records, or ``None``.

    A state ``(i, j)`` means the first ``i`` left and first ``j`` right
    operations have been executed.  The account at that state is the base
    ledger plus the deltas of the union of both prefixes, so it does not depend
    on the path taken.  Every transition applies one whole operation and must
    leave all full-row multiplicities nonnegative.
    """
    n, m = len(left), len(right)
    left_index = {operation.operation_id: index for index, operation in enumerate(left)}
    right_index = {operation.operation_id: index for index, operation in enumerate(right)}
    if len(left_index) != n or len(right_index) != m:
        raise EvidenceError('duplicate operation identifier')

    start = (0, 0)
    links = {start: None}
    stack = [(0, 0, base_rows)]
    while stack:
        i, j, counts = stack.pop()
        if i == n and j == m:
            order = []
            state = (i, j)
            while links[state] is not None:
                parent, operation_id = links[state]
                order.append(operation_id)
                state = parent
            order.reverse()
            return tuple(order)

        moves = []
        if i < n:
            operation = left[i]
            shared_at = right_index.get(operation.operation_id)
            if shared_at is None:
                moves.append((i + 1, j, operation))
            elif shared_at == j:
                moves.append((i + 1, j + 1, operation))
        if j < m:
            operation = right[j]
            shared_at = left_index.get(operation.operation_id)
            if shared_at is None:
                moves.append((i, j + 1, operation))
            # A shared operation is advanced once, from the left candidate above.

        for next_i, next_j, operation in moves:
            state = (next_i, next_j)
            if state in links:
                continue
            candidate = add_row_changes(counts, operation.changes, 1)
            if any(count < 0 for count in candidate.values()):
                # The account of a state is path independent, so this state
                # can never be entered legally.
                links[state] = False
                continue
            links[state] = ((i, j), operation.operation_id)
            stack.append((next_i, next_j, candidate))
    return None


def validate_execution(base_rows, left, right, order):
    """Validate a declared execution order; return the resulting account."""
    left_map = {operation.operation_id: operation.changes for operation in left}
    right_map = {operation.operation_id: operation.changes for operation in right}
    union = set(left_map) | set(right_map)
    if len(order) != len(set(order)) or set(order) != union:
        raise InputError('execution order must cover every operation exactly once')
    left_ids = [operation.operation_id for operation in left]
    right_ids = [operation.operation_id for operation in right]
    if [operation_id for operation_id in order if operation_id in left_map] != left_ids:
        raise InputError('execution order does not project to the left record order')
    if [operation_id for operation_id in order if operation_id in right_map] != right_ids:
        raise InputError('execution order does not project to the right record order')

    counts = base_rows
    for operation_id in order:
        changes = left_map.get(operation_id)
        if changes is None:
            changes = right_map[operation_id]
        counts = add_row_changes(counts, changes, 1)
        if any(count < 0 for count in counts.values()):
            raise InputError(f'operation {operation_id!r} makes a row count negative')
    return counts


def merged_rows(document: MergeDocument) -> RowCounts:
    """Replay the signed execution order over the delivered base ledger."""
    changes = {operation.operation_id: operation.changes
               for operation in (*document.left, *document.right)}
    counts = document.base_rows
    for operation_id in document.order:
        counts = add_row_changes(counts, changes[operation_id], 1)
    return counts


# ---------------------------------------------------------------------------
# V2 encoding and decoding
# ---------------------------------------------------------------------------

def encode_merge_document(document: MergeDocument) -> bytes:
    payload = {
        'version': 2,
        'partition': document.partition,
        'base_package_id': document.base_package_id,
        'base_revision': document.base_revision,
        'base_rows': ordered_rows(document.base_rows),
        'left': [[operation.operation_id, ordered_rows(operation.changes)]
                 for operation in document.left],
        'right': [[operation.operation_id, ordered_rows(operation.changes)]
                  for operation in document.right],
        'order': list(document.order),
    }
    return encode_json(payload).encode('utf-8')


def _array(value, label):
    if not isinstance(value, list):
        raise InputError(f'{label} must be a JSON array')
    return value


def _decode_record(value, label):
    operations = []
    seen = set()
    for entry in _array(value, label):
        if not isinstance(entry, list) or len(entry) != 2:
            raise InputError(f'{label} operation must be a two-element array')
        operation_id = require_text(entry[0], 'operation identifier')
        if operation_id in seen:
            raise InputError(f'duplicate operation {operation_id!r} in {label}')
        seen.add(operation_id)
        operations.append(MergeOperation(operation_id,
                                         normalize_delta_rows(_array(entry[1], 'operation rows'))))
    return tuple(operations)


def _decode_order(value):
    order = []
    seen = set()
    for entry in _array(value, 'order'):
        operation_id = require_text(entry, 'operation identifier')
        if operation_id in seen:
            raise InputError(f'duplicate operation {operation_id!r} in order')
        seen.add(operation_id)
        order.append(operation_id)
    return tuple(order)


def _decode_merge_document(document):
    if not isinstance(document, dict):
        raise InputError('package must be a JSON object')
    if document.keys() != V2_FIELDS:
        raise InputError('unexpected V2 package fields')
    version = document['version']
    if type(version) is not int or version != 2:
        raise InputError('unsupported V2 package version')
    base_package_id = document['base_package_id']
    if type(base_package_id) is not int or not 1 <= base_package_id <= MAX_PACKAGE_ID:
        raise InputError('base package number must be a positive 63-bit integer')
    partition = require_text(document['partition'], 'partition')
    base_revision = require_text(document['base_revision'], 'base revision')
    base_rows = normalize_inventory_rows(_array(document['base_rows'], 'base rows'))
    left = _decode_record(document['left'], 'left')
    right = _decode_record(document['right'], 'right')
    _shared_deltas(left, right)
    order = _decode_order(document['order'])
    validate_execution(base_rows, left, right, order)
    return MergeDocument(partition, base_package_id, base_revision, base_rows, left, right, order)


def decode_merge_document(blob) -> MergeDocument:
    if not isinstance(blob, bytes):
        raise PackageError('package must be UTF-8 bytes')
    try:
        document = decode_json(blob.decode('utf-8'))
        return _decode_merge_document(document)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PackageError(f'invalid V2 package: {exc}') from exc


def decode_exchange(blob):
    """Validate and decode either a V1 package or a V2 merge document."""
    if not isinstance(blob, bytes):
        raise PackageError('package must be UTF-8 bytes')
    try:
        document = decode_json(blob.decode('utf-8'))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PackageError(f'invalid package: {exc}') from exc
    if isinstance(document, dict) and type(document.get('version')) is int \
            and document['version'] == 2:
        try:
            return _decode_merge_document(document)
        except (ValueError, RecursionError) as exc:
            raise PackageError(f'invalid V2 package: {exc}') from exc
    return decode_package(blob)
