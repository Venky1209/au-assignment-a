"""Derived facts, computed from the current database state at report time.

Pipeline (each step only sees what the previous step kept):
  1. link    : event -> lead via (workflow_id, phone blind index); override -> lead via its call
  2. orphans : everything that did not link (counted, excluded from every report)
  3. unknown : linked disposition.* whose code is not in the catalogue (counted, ignored)
  4. calls   : only calls with a linked call.initiated count
  5. effective disposition, buckets
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .normalize import IST
from .schema import Catalog

DISPOSITION_TYPES = ("disposition.set", "disposition.override")


@dataclass
class Event:
    event_id: str
    seq: int
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at: int | None
    phone_hmac: str | None
    disposition_code: str | None
    payload: dict


@dataclass
class Call:
    workflow_id: str
    call_id: str
    lead_key: tuple[str, str]
    initiated_at: int | None = None
    connected: bool = False
    telephony_failed: bool = False
    events: list[Event] = field(default_factory=list)
    effective: str | None = None          # effective disposition code
    effective_from: Event | None = None   # the event that decided it (for `explain`)

    @property
    def counted(self) -> bool:
        return self.initiated_at is not None

    @property
    def ist_date(self) -> str:
        return datetime.fromtimestamp(self.initiated_at, IST).date().isoformat()


@dataclass
class Lead:
    workflow_id: str
    phone_hmac: str
    batch_id: str
    external_ref: str
    phone_last4: str
    calls: list[Call] = field(default_factory=list)  # counted calls only, by initiated time
    bucket: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.workflow_id, self.phone_hmac)


@dataclass
class State:
    leads: dict[tuple[str, str], Lead]
    calls: dict[tuple[str, str], Call]
    orphans: list[Event]
    unknown_dispositions: list[Event]
    orphan_reasons: dict[str, str] = field(default_factory=dict)  # event_id -> why it is quarantined


def orphan_reason(e: Event, cat: Catalog) -> str:
    """Why an event could not be linked. Checked in this order; the first match is the reason."""
    if not e.call_id or e.occurred_at is None:
        return "missing_call_id_or_timestamp"
    if e.event_type == "disposition.override":
        return "override_on_unlinked_call"
    if e.workflow_id not in cat.workflows:
        return "unknown_workflow"
    if e.phone_hmac is None:
        return "invalid_or_missing_phone"
    return "no_matching_lead"


def _order(e: Event):
    """Latest-wins ordering: by occurred_at, ties broken by arrival order."""
    return (e.occurred_at if e.occurred_at is not None else -1, e.seq)


def build(conn: sqlite3.Connection, cat: Catalog) -> State:
    leads = {
        (r["workflow_id"], r["phone_hmac"]): Lead(r["workflow_id"], r["phone_hmac"], r["batch_id"],
                                                  r["external_ref"], r["phone_last4"])
        for r in conn.execute("SELECT workflow_id, phone_hmac, batch_id, external_ref, phone_last4 FROM leads")
    }
    events = [Event(r["event_id"], r["seq"], r["event_type"], r["workflow_id"], r["call_id"], r["occurred_at"],
                    r["phone_hmac"], r["disposition_code"], json.loads(r["payload_json"]))
              for r in conn.execute("SELECT * FROM events ORDER BY seq")]

    # 1. link by phone. An event with no timestamp or no call_id cannot be placed on a call -> orphan.
    linked: dict[str, tuple[str, str]] = {}
    for e in events:
        if e.event_type == "disposition.override" or e.occurred_at is None or not e.call_id:
            continue
        k = (e.workflow_id, e.phone_hmac)
        if e.phone_hmac and k in leads:
            linked[e.event_id] = k

    # a call belongs to the lead of its call.initiated; failing that, of its first linked event
    call_lead: dict[tuple[str, str], tuple[str, str]] = {}
    for e in events:
        if e.event_id in linked:
            ck = (e.workflow_id, e.call_id)
            if e.event_type == "call.initiated":
                call_lead[ck] = linked[e.event_id]
            else:
                call_lead.setdefault(ck, linked[e.event_id])

    # overrides have no phone: they link through their call
    for e in events:
        if e.event_type == "disposition.override" and e.occurred_at is not None and e.call_id:
            ck = (e.workflow_id, e.call_id)
            if ck in call_lead:
                linked[e.event_id] = call_lead[ck]

    orphans = [e for e in events if e.event_id not in linked]
    reasons = {e.event_id: orphan_reason(e, cat) for e in orphans}

    # 2-4. group linked events into calls; drop unknown dispositions
    calls: dict[tuple[str, str], Call] = {}
    unknown: list[Event] = []
    for e in events:
        if e.event_id not in linked:
            continue
        ck = (e.workflow_id, e.call_id)
        if e.event_type in DISPOSITION_TYPES and (e.workflow_id, e.disposition_code) not in cat.dispositions:
            unknown.append(e)
            continue
        c = calls.setdefault(ck, Call(e.workflow_id, e.call_id, call_lead[ck]))
        c.events.append(e)
        if e.event_type == "call.initiated":
            c.initiated_at = e.occurred_at if c.initiated_at is None else min(c.initiated_at, e.occurred_at)
        elif e.event_type == "call.connected":
            c.connected = True
        elif e.event_type == "call.telephony_failed":
            c.telephony_failed = True

    # 5. effective disposition: latest override, else latest set. Originals are never touched.
    for c in calls.values():
        overrides = [e for e in c.events if e.event_type == "disposition.override"]
        sets = [e for e in c.events if e.event_type == "disposition.set"]
        winner = max(overrides, key=_order) if overrides else (max(sets, key=_order) if sets else None)
        if winner:
            c.effective, c.effective_from = winner.disposition_code, winner

    for c in calls.values():
        if c.counted:
            leads[c.lead_key].calls.append(c)
    for lead in leads.values():
        lead.calls.sort(key=lambda c: (c.initiated_at, c.call_id))
        lead.bucket = bucket(lead, cat)

    return State(leads, calls, orphans, unknown, reasons)


def is_terminal(cat: Catalog, wf: str, code: str | None) -> bool:
    return code is not None and cat.dispositions[(wf, code)].is_terminal


def is_success(cat: Catalog, wf: str, code: str | None) -> bool:
    return code is not None and cat.dispositions[(wf, code)].is_success


def bucket(lead: Lead, cat: Catalog) -> str:
    """First match wins."""
    if not lead.calls:
        return "yet_to_reach_out"
    if any(is_terminal(cat, lead.workflow_id, c.effective) for c in lead.calls):
        return "completed"
    if all(c.telephony_failed for c in lead.calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"
