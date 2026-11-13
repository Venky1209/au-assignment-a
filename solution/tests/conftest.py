import base64
import json
from pathlib import Path

import pytest

from ledger import db, derive, schema
from ledger.crypto import Keys
from ledger.ingest import ingest_path

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def cat():
    return schema.load()


@pytest.fixture
def keys():
    return Keys(base64.b64decode(base64.b64encode(b"e" * 32)), b"h" * 32)


class World:
    """Builds tiny drops on disk so tests exercise the real ingest path."""

    def __init__(self, root: Path, keys, cat):
        self.root, self.keys, self.cat = root, keys, cat
        self.conn = db.connect(root / "t.db")
        db.check_key(self.conn, keys.fingerprint())

    def drop(self, name: str, leads: dict | None = None, events: list | None = None) -> Path:
        d = self.root / name
        for (wf, batch), text in (leads or {}).items():
            p = d / "leads" / wf / f"{batch}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        if events is not None:
            p = d / "events" / f"events_{name}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n",
                         encoding="utf-8")
        return d

    def ingest(self, path: Path):
        return ingest_path(self.conn, self.keys, self.cat, path)

    def state(self):
        return derive.build(self.conn, self.cat)


@pytest.fixture
def world(tmp_path, keys, cat):
    return World(tmp_path, keys, cat)


def ev(eid, etype, call, at, phone="9876543210", wf="motor_renewal", **payload):
    e = {"event_id": eid, "event_type": etype, "workflow_id": wf, "call_id": call, "occurred_at": at,
         "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return e


MOTOR_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
