from audit.export import fetch_audit
from audit.reconcile import audit
from audit.store import initialize_offline
from inventory.accept import accept_inventory
from offline.accept import import_package
from offline.catalog import get_received_package
from offline.recover import recover
from publication.commit import initialize_source, publish, publish_merge
from publication.receipts import fetch, get_receipt
from revisions.import_delta import accept_delta
from storage import (EvidenceError, NotFoundError, PackageError, StorageError, decode_json)
from tests import LakesideTestCase, X, Y, shipment_counts, wire_blob


class MergeTests(LakesideTestCase):
    def seed_base(self, db, revision='A'):
        revisions = ['A'] if revision == 'A' else ['A', revision]
        initialize_source(db, ['P'], [('P', item) for item in revisions])
        accept_inventory(db, 'P', 'A', 'opening', [(*X, 2), (*Y, 1)])
        deltas = []
        if revision != 'A':
            deltas.append(accept_delta(db, 'P', 'to-a', revision, 'A', [(*X, 1)]))
        return publish(db, 'P', revisions, ['opening'], deltas)

    def test_publish_and_offline_round_trip(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A',
                                   [('a', [(*X, -1)])], [('b', [(*Y, -1), (*X, 1)])])
            blob = fetch(source, merged.package_id)
            self.assertEqual(get_receipt(source, merged.package_id), merged)
            self.assertEqual((merged.partition, merged.revisions, merged.inventory_ids,
                              merged.delta_ids), ('P', ('merged',), (), ()))
            self.assertEqual(shipment_counts(source), (2, 2))
            self.assertEqual(decode_json(fetch(source, base.package_id))['version'], 1)
        document = decode_json(blob)
        self.assertEqual((document['version'], document['base_package_id'],
                          document['base_revision']), (2, base.package_id, 'A'))
        self.assertEqual(document['base_rows'], [[*X, 2], [*Y, 1]])
        self.assertEqual(sorted(document['order']), ['a', 'b'])
        with self.offline() as night:
            initialize_offline(night)
            local = import_package(night, blob)
            self.assertEqual(import_package(night, blob), local)
            self.assertEqual(recover(night, local, 'merged'), [(*X, 2)])
            with self.assertRaises(NotFoundError):
                recover(night, local, 'A')
            result = audit(night, local, 'merged', {'G': 2})
        with self.offline() as night:
            self.assertEqual(get_received_package(night, local), blob)
            self.assertEqual(fetch_audit(night, result['audit_id']), result)
            self.assertEqual(recover(night, local, 'merged'), [(*X, 2)])

    def test_base_revision_from_recorded_source(self):
        with self.source() as source:
            base = self.seed_base(source, revision='B')
            merged = publish_merge(source, base.package_id, 'B',
                                   [('a', [(*X, 1)])], [('b', [(*Y, -1)])])
            document = decode_json(fetch(source, merged.package_id))
            self.assertEqual(document['base_revision'], 'B')
            # B→A adds one X, so the base revision B holds one X and one Y.
            self.assertEqual(document['base_rows'], [[*X, 1], [*Y, 1]])
            blob = fetch(source, merged.package_id)
        with self.offline() as night:
            initialize_offline(night)
            local = import_package(night, blob)
            self.assertEqual(recover(night, local, "merged"), [(*X, 2)])

    def test_any_legal_order_and_shared_execution(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A',
                                   [('s', [(*X, -2), (*X, 1)]), ('a', [(*X, 2)])],
                                   [('s', [(*X, -1)]), ('b', [(*Y, -1)])])
            blob = fetch(source, merged.package_id)
        document = decode_json(blob)
        self.assertEqual(document['order'].count('s'), 1)
        self.assertEqual([op for op in document['order'] if op in ('s', 'a')], ['s', 'a'])
        self.assertEqual([op for op in document['order'] if op in ('s', 'b')], ['s', 'b'])
        with self.offline() as night:
            initialize_offline(night)
            local = import_package(night, blob)
            self.assertEqual(recover(night, local, 'merged'), [(*X, 3)])

    def test_empty_records(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A', [], [])
            blob = fetch(source, merged.package_id)
            self.assertEqual(decode_json(blob)['order'], [])
        with self.offline() as night:
            initialize_offline(night)
            local = import_package(night, blob)
            self.assertEqual(recover(night, local, 'merged'), [(*X, 2), (*Y, 1)])

    def test_rejections_leave_no_entries(self):
        with self.source() as source:
            base = self.seed_base(source)
            bad_requests = [
                (base.package_id, 'A', [('a', [(*X, 1)])], [('a', [(*X, 2)])]),
                (base.package_id, 'A', [('s', [(*X, 1)]), ('t', [(*X, 1)])],
                 [('t', [(*X, 1)]), ('s', [(*X, 1)])]),
                (base.package_id, 'A', [('a', [(*X, 1)]), ('a', [(*X, 1)])], []),
                (base.package_id, 'A', [('', [(*X, 1)])], []),
                (base.package_id, 'A', [('a', [(*X, -3)])], []),
                (base.package_id, 'B', [], []),
                (base.package_id, 'merged', [], []),
                (999, 'A', [], []),
                (True, 'A', [], []),
            ]
            for package_id, revision, left, right in bad_requests:
                with self.subTest(revision=revision, left=left, right=right):
                    with self.assertRaises(EvidenceError):
                        publish_merge(source, package_id, revision, left, right)
            self.assertEqual(shipment_counts(source), (1, 1))

    def test_v2_cannot_be_a_base(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A', [('a', [(*X, -1)])], [])
            with self.assertRaises(EvidenceError):
                publish_merge(source, merged.package_id, 'merged', [], [])
            self.assertEqual(shipment_counts(source), (2, 2))

    def test_corrupt_base_package_is_rejected(self):
        with self.source() as source:
            base = self.seed_base(source)
            source.execute('UPDATE shipments SET blob = ? WHERE package_id = ?',
                           (b'{"version": 1}', base.package_id))
            with self.assertRaises(EvidenceError):
                publish_merge(source, base.package_id, 'A', [], [])
            source.execute('UPDATE shipments SET blob = ? WHERE package_id = ?',
                           (b'not json', base.package_id))
            with self.assertRaises(EvidenceError):
                publish_merge(source, base.package_id, 'A', [], [])
            self.assertEqual(shipment_counts(source), (1, 1))

    def test_merge_receipt_rollback(self):
        with self.source() as source:
            base = self.seed_base(source)
            source.execute('''
                CREATE TRIGGER stop_merge_receipt BEFORE INSERT ON receipts
                WHEN EXISTS (SELECT 1 FROM shipments WHERE package_id = NEW.package_id)
                BEGIN SELECT RAISE(ABORT, 'merge receipt interrupted'); END
            ''')
            with self.assertRaisesRegex(StorageError, 'merge receipt interrupted'):
                publish_merge(source, base.package_id, 'A', [('a', [(*X, -1)])], [])
            self.assertFalse(source.in_transaction)
            self.assertEqual(shipment_counts(source), (1, 1))
        with self.source() as source:
            self.assertEqual(shipment_counts(source), (1, 1))
            source.execute('DROP TRIGGER stop_merge_receipt')
            merged = publish_merge(source, base.package_id, 'A', [('a', [(*X, -1)])], [])
            self.assertEqual(shipment_counts(source), (2, 2))
            self.assertEqual(get_receipt(source, merged.package_id).revisions, ('merged',))

    def test_bad_v2_package(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A',
                                   [('a', [(*X, -1)])], [('b', [(*X, 1)])])
            blob = fetch(source, merged.package_id)
        good = decode_json(blob)
        bad_documents = []
        for field in ('version', 'partition', 'base_package_id', 'base_revision', 'base_rows',
                      'left', 'right', 'order'):
            broken = dict(good)
            broken.pop(field)
            bad_documents.append(broken)
        bad_documents.extend([
            dict(good, version=1),
            dict(good, version=True),
            dict(good, base_package_id=0),
            dict(good, base_package_id=2 ** 63),
            dict(good, base_rows=[[*X, -1]]),
            dict(good, order=['a']),
            dict(good, order=['a', 'b', 'a']),
            dict(good, order=['a', 'b', 'extra']),
            # An empty effective base cannot absorb the removal operation first.
            dict(good, base_rows=[[*X, 0]], order=['a', 'b']),
            dict(good, left=[['b', [(*X, 1)]]]),
            dict(good, right=[['a', [(*X, 5)]]]),
            dict(good, left=[['a', [(*X, -3)]]], order=['a', 'b']),
            dict(good, extra=True),
        ])
        with self.offline() as night:
            initialize_offline(night)
            first = import_package(night, blob)
            for document in bad_documents:
                with self.subTest(document=document), self.assertRaises(PackageError):
                    import_package(night, wire_blob(document))
            self.assertEqual(night.execute('SELECT count(*) FROM received_packages').fetchone(), (1,))
            self.assertEqual(get_received_package(night, first), blob)
            self.assertEqual(recover(night, first, 'merged'), [(*X, 2), (*Y, 1)])

    def test_any_legal_order_is_accepted(self):
        with self.source() as source:
            base = self.seed_base(source)
            merged = publish_merge(source, base.package_id, 'A',
                                   [('a', [(*X, 1)])], [('b', [(*Y, 1)])])
            blob = fetch(source, merged.package_id)
        first = decode_json(blob)
        alternate = dict(first, order=list(reversed(first['order'])))
        self.assertNotEqual(alternate['order'], first['order'])
        with self.offline() as night:
            initialize_offline(night)
            original_id = import_package(night, blob)
            alternate_id = import_package(night, wire_blob(alternate))
            self.assertNotEqual(original_id, alternate_id)
            expected = [(*X, 3), (*Y, 2)]
            self.assertEqual(recover(night, original_id, 'merged'), expected)
            self.assertEqual(recover(night, alternate_id, 'merged'), expected)

    def test_failed_audit_leaves_no_account(self):
        with self.source() as source:
            initialize_source(source, ['P'], [('P', 'A')])
            accept_inventory(source, 'P', 'A', 'opening', [(*X, 1)])
            base = publish(source, 'P', ['A'], ['opening'], [])
            merged = publish_merge(source, base.package_id, 'A', [('a', [(*X, -1)])], [])
            blob = fetch(source, merged.package_id)
        with self.offline() as night:
            initialize_offline(night)
            local = import_package(night, blob)
            for revision in ('A', 'unknown'):
                with self.assertRaises(NotFoundError):
                    audit(night, local, revision)
            self.assertEqual(night.execute('SELECT count(*) FROM audits').fetchone(), (0,))
            result = audit(night, local, 'merged')
            self.assertEqual(result['actual'], {})
