import sqlite3

from inventory.partitions import init_partitions
from inventory.store import init_inventory_store
from publication.merge import (MergeDocument, encode_merge_document, find_execution,
                               normalize_records, validate_execution)
from publication.package import decode_package, encode_package
from publication.receipts import init_receipts, record_merge_receipt, record_receipt
from publication.reconcile import reconcile_evidence
from publication.scope import select_publication_evidence
from revisions.register import register_revision
from revisions.registry import init_revision_registry
from storage import (EvidenceError, InputError, NotFoundError, PackageError, StorageError, atomic,
                     decode_json, require_text)


def init_shipment_store(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS shipments (package_id INTEGER PRIMARY KEY, blob BLOB NOT NULL)
    ''')
    init_receipts(db)


def initialize_source(db, partitions=(), initial_revisions=()):
    with atomic(db):
        init_partitions(db, partitions)
        init_revision_registry(db)
        init_inventory_store(db)
        init_shipment_store(db)
        for partition, revision in initial_revisions:
            register_revision(db, partition, revision)


def commit_shipment(db, evidence):
    """Write qualified evidence and its receipt in the caller's transaction."""
    if not db.in_transaction:
        raise RuntimeError('shipment write requires an active transaction')
    blob = encode_package(evidence)
    cursor = db.execute('INSERT INTO shipments (blob) VALUES (?)', (blob,))
    return record_receipt(db, cursor.lastrowid, evidence)


def publish(db, partition, revision_ids, inventory_ids, delta_ids):
    try:
        with atomic(db):
            evidence = select_publication_evidence(db, partition, revision_ids, inventory_ids, delta_ids)
            reconcile_evidence(evidence)
            return commit_shipment(db, evidence)
    except sqlite3.Error as exc:
        raise StorageError(f'publication transaction failed: {exc}') from exc


def _base_package(db, base_package_id, base_revision):
    if type(base_package_id) is not int or base_package_id <= 0:
        raise EvidenceError('base package number must be a positive integer')
    if base_package_id > 2 ** 63 - 1:
        raise EvidenceError('base package is outside the catalog')
    require_text(base_revision, 'base revision')
    row = db.execute('SELECT blob FROM shipments WHERE package_id = ?', (base_package_id,)).fetchone()
    if row is None:
        raise EvidenceError(f'unknown base package {base_package_id}')
    try:
        document = decode_json(row[0].decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise EvidenceError(f'base package {base_package_id} is unreadable: {exc}') from exc
    if isinstance(document, dict) and type(document.get('version')) is int and document['version'] != 1:
        raise EvidenceError(f'base package {base_package_id} is not a V1 package')
    try:
        evidence = decode_package(row[0])
    except PackageError as exc:
        raise EvidenceError(f'base package {base_package_id} is unreadable: {exc}') from exc
    if base_revision not in evidence.revisions:
        raise EvidenceError(f'revision {base_revision!r} is not declared in the base package')
    return evidence


def _base_rows(evidence, base_revision):
    from offline.recover import recover_revision_rows

    return {tuple(row[:3]): row[3] for row in recover_revision_rows(evidence, base_revision)}


def commit_merge_shipment(db, document: MergeDocument):
    """Write the V2 package and its merge receipt in the caller's transaction."""
    if not db.in_transaction:
        raise RuntimeError('shipment write requires an active transaction')
    blob = encode_merge_document(document)
    cursor = db.execute('INSERT INTO shipments (blob) VALUES (?)', (blob,))
    return record_merge_receipt(db, cursor.lastrowid, document.partition)


def publish_merge(db, base_package_id, base_revision, left, right):
    try:
        with atomic(db):
            base = _base_package(db, base_package_id, base_revision)
            left_operations, right_operations = normalize_records(left, right)
            base_account = _base_rows(base, base_revision)
            order = find_execution(base_account, left_operations, right_operations)
            if order is None:
                raise EvidenceError('the two correction records have no legal execution')
            document = MergeDocument(base.partition, base_package_id, base_revision,
                                     base_account, left_operations, right_operations, order)
            # Re-validate the derived order against the same fixed account.
            validate_execution(base_account, left_operations, right_operations, order)
            return commit_merge_shipment(db, document)
    except sqlite3.Error as exc:
        raise StorageError(f'merge transaction failed: {exc}') from exc
    except EvidenceError:
        raise
    except (InputError, NotFoundError) as exc:
        raise EvidenceError(f'invalid merge request: {exc}') from exc
