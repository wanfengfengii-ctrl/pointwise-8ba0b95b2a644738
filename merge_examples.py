import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from audit.export import fetch_audit
from audit.reconcile import audit
from audit.store import initialize_offline
from demo import report
from inventory.accept import accept_inventory
from offline.accept import import_package
from offline.catalog import get_received_package
from offline.recover import recover
from publication.commit import initialize_source, publish
from publication.receipts import fetch, get_receipt
from revisions.import_delta import accept_delta
from storage import EvidenceError, NotFoundError, PackageError, StorageError, decode_json, encode_json, open_database
from tests import shipment_counts

check = TestCase()


def seed_base(source, case):
    revision = case["base_revision"]
    revisions = ["A"] if revision == "A" else ["A", revision]
    initialize_source(source, ["P"], [("P", item) for item in revisions])
    accept_inventory(source, "P", "A", "opening", case.get("anchor_rows", case["base_rows"]))
    deltas = []
    if revision != "A":
        deltas.append(accept_delta(source, "P", "to-opening", revision, "A", case["reverse_delta"]))
    return publish(source, "P", revisions, ["opening"], deltas)


def check_fixtures(cases):
    for case in cases.values():
        with open_database(":memory:") as source:
            base = seed_base(source, case)
            blob = fetch(source, base.package_id)
            check.assertEqual(get_receipt(source, base.package_id), base)
        with open_database(":memory:") as night:
            initialize_offline(night)
            received = import_package(night, blob)
            check.assertEqual(recover(night, received, case["base_revision"]),
                              [tuple(row) for row in case["base_rows"]])
    print("V1 fixture bases: " + ", ".join(cases))


def reject_corrupt(night, name, blob):
    wrong_orders = [["a", "b", "c"]] if name == "backtrack" else [
        ["a", "s", "s", "b"], ["a", "b"], ["s", "a", "b"]
    ]
    bad_documents = [dict(decode_json(blob), order=wrong) for wrong in wrong_orders]
    bad_documents.append(dict(decode_json(blob), base_package_id=True))
    if name == "shared":
        bad = decode_json(blob)
        if "operations" in bad:
            bad["operations"]["unclaimed"] = []
        else:
            bad["right"][0][1] = []
        bad_documents.append(bad)
    for bad in bad_documents:
        with check.assertRaises(PackageError):
            import_package(night, encode_json(bad).encode("utf-8"))
    check.assertEqual(night.execute("SELECT count(*) FROM received_packages").fetchone(), (1,))


def run_case(name, case, verify):
    from publication.commit import publish_merge

    with TemporaryDirectory() as directory:
        source_path = Path(directory) / "source.sqlite"
        night_path = Path(directory) / "night.sqlite"
        with open_database(source_path) as source:
            base = seed_base(source, case)
            base_blob = fetch(source, base.package_id)
            args = (base.package_id, case["base_revision"], case["left"], case["right"])
            if "order" not in case:
                with check.assertRaises(EvidenceError):
                    publish_merge(source, *args)
                check.assertEqual(shipment_counts(source), (1, 1))
                check.assertEqual(fetch(source, base.package_id), base_blob)
                return
            if verify and name == "backtrack":
                source.execute(
                    "CREATE TRIGGER stop_merge_receipt BEFORE INSERT ON receipts "
                    "WHEN EXISTS (SELECT 1 FROM shipments WHERE package_id = NEW.package_id) "
                    "BEGIN SELECT RAISE(ABORT, \"merge receipt interrupted\"); END"
                )
                with check.assertRaisesRegex(StorageError, "merge receipt interrupted"):
                    publish_merge(source, *args)
        with open_database(source_path) as source:
            check.assertEqual(shipment_counts(source), (1, 1))
            check.assertEqual(get_receipt(source, base.package_id), base)
            check.assertEqual(fetch(source, base.package_id), base_blob)
            source.execute("DROP TRIGGER IF EXISTS stop_merge_receipt")
            merged = publish_merge(source, *args)
            blob = fetch(source, merged.package_id)
            check.assertEqual(get_receipt(source, merged.package_id), merged)
            check.assertEqual((merged.partition, merged.revisions, merged.inventory_ids, merged.delta_ids),
                              ("P", ("merged",), (), ()))
            check.assertEqual(shipment_counts(source), (2, 2))
        source_path.unlink()
        document = decode_json(blob)
        check.assertEqual((document["version"], document["base_package_id"], document["base_revision"]),
                          (2, base.package_id, case["base_revision"]))
        with open_database(night_path) as night:
            initialize_offline(night)
            local = import_package(night, blob)
            check.assertEqual(import_package(night, blob), local)
            if verify:
                reject_corrupt(night, name, blob)
                with check.assertRaises(NotFoundError):
                    audit(night, local, case["base_revision"])
                check.assertEqual(night.execute("SELECT count(*) FROM audits").fetchone(), (0,))
            result = audit(night, local, "merged", {"G": 1})
        with open_database(night_path) as night:
            check.assertEqual(get_received_package(night, local), blob)
            fixed = fetch_audit(night, result["audit_id"])
            check.assertEqual(fixed, result)
            rows = [list(row) for row in recover(night, local, "merged")]
        expected_night = {"rows": case["merged_rows"], "actual": case["actual"],
                          "differences": {"G": case["actual"]["G"] - 1}}
        actual_night = {"rows": rows, "actual": fixed["actual"], "differences": fixed["differences"]}
        report(name, case, case["order"], document["order"], expected_night, actual_night,
               ["publication/commit.py", "offline/accept.py", "offline/recover.py", "audit/reconcile.py"],
               case["explanation"])


def main():
    parser = argparse.ArgumentParser(description="Run the published merge handoff examples")
    parser.add_argument("mode", choices=("fixtures", "verify", "demo"))
    mode = parser.parse_args().mode
    fixture_path = Path(__file__).resolve().parent / "examples" / "merge-cases.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    if mode == "fixtures":
        check_fixtures(cases)
        return
    for name, case in cases.items():
        if mode == "verify" or "order" in case:
            run_case(name, case, verify=mode == "verify")


if __name__ == "__main__":
    main()
