from offline.catalog import save_received_package
from publication.merge import decode_exchange
from storage import atomic


def import_package(db, blob: bytes):
    decode_exchange(blob)
    with atomic(db):
        return save_received_package(db, blob)
