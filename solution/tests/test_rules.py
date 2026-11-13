"""Lead rules, event linking, effective disposition and buckets, through the real ingest + derive path."""
import json
import sqlite3

import pytest

from conftest import MOTOR_HEADER, ev


def lead_rows(world):
    return {r["external_ref"]: r for r in world.conn.execute("SELECT * FROM leads")}


# ---------------------------------------------------------------- leads
def test_rejection_reason_is_first_failure_in_schema_order(world):
    csv = MOTOR_HEADER + (
        ",12345,POL1,,,\n"            # name missing AND phone invalid -> name comes first in schema
        "Asha,12345,POL2,,,\n"        # invalid phone
        "Asha,,POL3,,,\n"             # missing phone
        "Asha,9876543210,,,,\n"       # missing ref
        ",,,,,\n"                     # blank row: skipped and not counted
        "Ravi,9876543211,POL5,,,\n"   # valid -> row 5, not 6
        "Ravi,98765,POL6,,,\n"
    )
    world.ingest(world.drop("d1", {("motor_renewal", "batch_001"): csv}))
    rej = world.conn.execute("SELECT row_number, reason FROM rejections ORDER BY row_number").fetchall()
    assert [tuple(r) for r in rej] == [
        (1, "missing_required:customer_name"), (2, "invalid_phone"), (3, "missing_required:phone"),
        (4, "missing_required:policy_number"), (6, "invalid_phone")]
    f = world.conn.execute("SELECT rows_total FROM ingested_files").fetchone()
    assert f["rows_total"] == 6


def test_aliases_extra_columns_warnings_and_defaults(world):
    csv = ("Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\n"
           "Hindi,Asha,POL1,+91 98765 43210,31/02/2026,twelve,VIP\n"
           "TA,Ravi,POL2,09876543211,01-Dec-2026,\"₹12,000\",\n")
    res = world.ingest(world.drop("d1", {("motor_renewal", "batch_001"): csv}))
    assert res[0].warnings == 3  # bad language, bad date, bad premium on row 1
    rows = lead_rows(world)
    a1, a2 = json.loads(rows["POL1"]["attrs_json"]), json.loads(rows["POL2"]["attrs_json"])
    assert a1["preferred_language"] is None and a1["policy_expiry"] is None  # default applied at read time
    assert a2 == {"policy_number": "POL2", "policy_expiry": "2026-12-01", "premium_amount": "12000.00",
                  "preferred_language": "ta"}


def test_duplicates_merge_last_write_wins_keep_first_batch(world, keys):
    world.ingest(world.drop("d1", {("motor_renewal", "batch_001"):
                                   MOTOR_HEADER + "Asha,9876543210,POL1,2026-12-01,1000,hi\n"}))
    world.ingest(world.drop("d2", {("motor_renewal", "batch_003"):
                                   MOTOR_HEADER + "Asha K,+919876543210,POL1,,2000,\n"}))
    rows = lead_rows(world)
    r = rows["POL1"]
    attrs = json.loads(r["attrs_json"])
    assert r["batch_id"] == "batch_001"
    assert attrs["premium_amount"] == "2000.00"          # overwritten
    assert attrs["policy_expiry"] == "2026-12-01"        # null did not overwrite
    assert attrs["preferred_language"] == "hi"
    assert keys.decrypt(r["pii_enc"], f"motor_renewal:{r['phone_hmac']}")["customer_name"] == "Asha K"
    merged = world.conn.execute("SELECT SUM(rows_merged) FROM ingested_files").fetchone()[0]
    assert merged == 1


def test_no_plaintext_pii_in_database(world):
    world.ingest(world.drop("d1", {("motor_renewal", "batch_001"):
                                   MOTOR_HEADER + "Zebediah Quux,9876543210,POL1,,,\n"},
                            [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z")]))
    world.conn.close()
    blob = b"".join(p.read_bytes() for p in world.root.glob("t.db*"))  # db + any WAL left behind
    for needle in (b"Zebediah", b"9876543210"):
        assert needle not in blob


# ---------------------------------------------------------------- events
LEAD = {("motor_renewal", "batch_001"): MOTOR_HEADER + "Asha,9876543210,POL1,,,\n"}


def test_malformed_duplicate_orphan_unknown(world):
    events = [
        '{"event_id": "e1", "event_ty',                               # malformed
        '{"no_id": true}',                                            # malformed
        ev("e2", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "call.initiated", "c1", "2026-10-01T10:00:00Z"),     # duplicate
        ev("e3", "call.initiated", "c9", "2026-10-01T10:00:00Z", phone="9000000000"),  # never uploaded
        ev("e4", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="RENEWD"),  # unknown
        ev("e5", "disposition.override", "c9", "2026-10-02T10:00:00Z", phone=None,
           disposition_code="renewed"),                               # override on an orphan call
    ]
    res = world.ingest(world.drop("d1", LEAD, events))
    ev_res = [r for r in res if r.kind == "events"][0]
    assert (ev_res.total, ev_res.malformed, ev_res.duplicates) == (7, 2, 1)
    st = world.state()
    assert sorted(e.event_id for e in st.orphans) == ["e3", "e5"]
    assert [e.event_id for e in st.unknown_dispositions] == ["e4"]


def test_every_quarantined_event_has_a_reason(world):
    events = [
        ev("q1", "call.initiated", "c9", "2026-10-01T10:00:00Z", phone="9000000000"),   # valid, never uploaded
        ev("q2", "call.initiated", "c8", "2026-10-01T10:00:00Z", phone="N/A"),          # unusable phone
        ev("q3", "disposition.override", "c9", "2026-10-02T10:00:00Z", phone=None, disposition_code="renewed"),
        ev("q4", "call.initiated", None, "2026-10-01T10:00:00Z"),                       # no call_id
        ev("q5", "call.initiated", "c7", "2026-10-01T10:00:00Z", wf="no_such_workflow"),
    ]
    world.ingest(world.drop("d1", LEAD, events))
    assert world.state().orphan_reasons == {
        "q1": "no_matching_lead", "q2": "invalid_or_missing_phone", "q3": "override_on_unlinked_call",
        "q4": "missing_call_id_or_timestamp", "q5": "unknown_workflow"}


def test_effective_disposition_latest_override_wins_and_originals_stay(world):
    events = [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30"),
        ev("e2", "call.connected", "c1", "2026-10-01T10:00:10+05:30"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00+05:30", disposition_code="callback_requested"),
        ev("e4", "disposition.override", "c1", "2026-10-03T10:00:00+05:30", phone=None,
           disposition_code="not_interested"),
        ev("e5", "disposition.override", "c1", "2026-10-02 10:00:00", phone=None,   # earlier, naive IST
           disposition_code="renewed"),
    ]
    world.ingest(world.drop("d1", LEAD, events))
    st = world.state()
    lead = next(iter(st.leads.values()))
    assert lead.calls[0].effective == "not_interested"
    assert lead.bucket == "completed"
    assert world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5  # nothing edited away


def test_set_only_latest_wins(world):
    events = [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
              ev("e2", "disposition.set", "c1", "2026-10-01T10:09:00Z", disposition_code="renewed"),
              ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="callback_requested")]
    world.ingest(world.drop("d1", LEAD, events))
    assert next(iter(world.state().leads.values())).calls[0].effective == "renewed"


@pytest.mark.parametrize("events,expected", [
    ([], "yet_to_reach_out"),
    ([ev("a", "call.connected", "c1", "2026-10-01T10:00:00Z")], "yet_to_reach_out"),  # no initiated: not a call
    ([ev("a", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
      ev("b", "call.telephony_failed", "c1", "2026-10-01T10:00:05Z")], "in_progress_telephony_failed"),
    ([ev("a", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
      ev("b", "call.telephony_failed", "c1", "2026-10-01T10:00:05Z"),
      ev("c", "call.initiated", "c2", "2026-10-02T10:00:00Z")], "in_progress_dialed"),
    ([ev("a", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
      ev("b", "call.telephony_failed", "c1", "2026-10-01T10:00:05Z"),
      ev("c", "disposition.set", "c1", "2026-10-01T10:01:00Z", disposition_code="wrong_number")], "completed"),
    ([ev("a", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
      ev("c", "disposition.set", "c1", "2026-10-01T10:01:00Z", disposition_code="callback_requested")],
     "in_progress_dialed"),
])
def test_bucket_first_match(world, events, expected):
    world.ingest(world.drop("d1", LEAD, events))
    assert next(iter(world.state().leads.values())).bucket == expected


def test_event_before_its_lead_counts_once_lead_arrives(world):
    world.ingest(world.drop("d1", {}, [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z")]))
    assert len(world.state().orphans) == 1
    world.ingest(world.drop("d2", LEAD, []))
    st = world.state()
    assert st.orphans == [] and next(iter(st.leads.values())).bucket == "in_progress_dialed"


def test_ledger_is_append_only(world):
    world.ingest(world.drop("d1", LEAD, [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z")]))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        world.conn.execute("UPDATE events SET disposition_code='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        world.conn.execute("DELETE FROM events")
