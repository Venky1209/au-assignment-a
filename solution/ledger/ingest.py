"""Ingest a drop: lead CSVs, then event JSONL files.

Every file runs in one transaction with its `ingested_files` row, keyed by content SHA-256:
  * crash mid-file  -> transaction rolls back, file is retried in full next run
  * same file again -> skipped, so no counter is double-counted
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db, normalize
from .crypto import Keys
from .schema import Catalog, Field, Workflow


@dataclass
class FileResult:
    path: str
    kind: str
    skipped: bool
    total: int = 0
    rejected: int = 0
    merged: int = 0
    warnings: int = 0
    malformed: int = 0
    duplicates: int = 0


# --------------------------------------------------------------------------- drops
def discover(path: Path) -> list[tuple[str, Path, str | None]]:
    """-> [(kind, file, workflow_id)] in processing order.

    `path` is either one drop (has leads/ or events/) or a folder of drops, processed in name order,
    which is how "ingest everything at once" works.
    """
    if not path.is_dir():
        raise SystemExit(f"{path}: not a folder (expected a drop like data/drop_1, or data/)")
    drops =[path] if (path / "leads").is_dir() or (path / "events").is_dir() else \
        sorted(p for p in path.iterdir() if p.is_dir())
    out = []
    for drop in drops:
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
                for f in sorted(wf_dir.glob("*.csv")):
                    out.append(("leads", f, wf_dir.name))
        events_dir = drop / "events"
        if events_dir.is_dir():
            for f in sorted(events_dir.glob("*.jsonl")):
                out.append(("events", f, None))
    return out


def ingest_path(conn: sqlite3.Connection, keys: Keys, cat: Catalog, path: Path) -> list[FileResult]:
    files = discover(path)
    if not files:
        raise SystemExit(f"no leads/ or events/ found under {path}")
    return [ingest_file(conn, keys, cat, kind, f, wf) for kind, f, wf in files]


def ingest_file(conn, keys: Keys, cat: Catalog, kind: str, path: Path, workflow_id: str | None) -> FileResult:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM ingested_files WHERE sha256=?", (sha,)).fetchone():
        return FileResult(str(path), kind, skipped=True)
    if kind == "leads" and workflow_id not in cat.workflows:
        raise SystemExit(f"{path}: no schema for workflow '{workflow_id}'")

    conn.execute("BEGIN IMMEDIATE")
    try:
        seq = db.next_seq(conn, "ingested_files")
        batch_id = path.stem if kind == "leads" else None
        conn.execute("INSERT INTO ingested_files (sha256, seq, kind, workflow_id, batch_id, source_path) "
                     "VALUES (?,?,?,?,?,?)", (sha, seq, kind, workflow_id, batch_id, str(path)))
        text = data.decode("utf-8-sig")
        if kind == "leads":
            res = _ingest_leads(conn, keys, cat.workflows[workflow_id], text, batch_id, seq)
        else:
            res = _ingest_events(conn, keys, text, seq)
        res.path = str(path)
        conn.execute("UPDATE ingested_files SET rows_total=?, rows_rejected=?, rows_merged=?, field_warnings=?, "
                     "malformed=?, duplicates=? WHERE seq=?",
                     (res.total, res.rejected, res.merged, res.warnings, res.malformed, res.duplicates, seq))
        conn.execute("COMMIT")
        return res
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- leads
def parse_lead_row(wf: Workflow, colmap: dict[str, int], cells: list[str]) -> tuple[str | None, dict, int]:
    """Validate one non-blank row. -> (rejection_reason | None, values, warnings).

    Fields are walked in schema order; the first failure is the single rejection reason.
    """
    values: dict[str, str | None] = {}
    warnings = 0
    for f in wf.fields:
        idx = colmap.get(f.name)
        raw = cells[idx].strip() if idx is not None and idx < len(cells) else ""
        if raw == "":
            if f.required:
                return f"missing_required:{f.name}", {}, 0
            values[f.name] = None
            continue
        v = _normalise(f, raw)
        if v is None:
            if f.type == "phone":
                return "invalid_phone", {}, 0
            if f.required:  # not reachable with the given schemas; see SUBMISSION.md
                return f"invalid:{f.name}", {}, 0
            warnings += 1
        values[f.name] = v
    return None, values, warnings


def _normalise(f: Field, raw: str) -> str | None:
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalize.phone(raw)
    if f.type == "date":
        return normalize.date_value(raw)
    if f.type == "decimal":
        return normalize.decimal_value(raw)
    if f.type == "enum":
        return normalize.enum_value(raw, f.values)
    raise ValueError(f"unknown field type {f.type!r}")


def _ingest_leads(conn, keys: Keys, wf: Workflow, text: str, batch_id: str, file_seq: int) -> FileResult:
    res = FileResult("", "leads", skipped=False)
    reader = csv.reader(text.splitlines())
    header = next(reader, None) or []
    hmap = wf.header_map()
    colmap: dict[str, int] = {}
    for i, h in enumerate(header):
        f = hmap.get(normalize.header(h))
        if f is not None and f.name not in colmap:  # first matching column wins
            colmap[f.name] = i

    pii_fields = [f.name for f in wf.fields if f.pii]
    phone_f, ref_f = wf.phone_field.name, wf.ref_field.name
    row_number = 0
    for cells in reader:
        if all(c.strip() == "" for c in cells):
            continue  # blank rows are skipped and not counted
        row_number += 1
        res.total += 1
        reason, values, warns = parse_lead_row(wf, colmap, cells)
        if reason:
            res.rejected += 1
            conn.execute("INSERT INTO rejections VALUES (?,?,?,?,?)",
                         (wf.workflow_id, batch_id, row_number, reason, file_seq))
            continue
        res.warnings += warns
        canon = values[phone_f]
        hmac_ = keys.blind_index(canon)
        aad = f"{wf.workflow_id}:{hmac_}"
        pii = {k: values[k] for k in pii_fields}
        attrs = {k: v for k, v in values.items() if k not in pii_fields}

        existing = conn.execute("SELECT attrs_json, pii_enc FROM leads WHERE workflow_id=? AND phone_hmac=?",
                                (wf.workflow_id, hmac_)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO leads VALUES (?,?,?,?,?,?,?,?)",
                         (wf.workflow_id, hmac_, batch_id, attrs[ref_f], canon[-4:],
                          json.dumps(attrs, sort_keys=True), keys.encrypt(pii, aad), file_seq))
        else:
            # merged duplicate: later non-null values overwrite (last write wins); batch_id is kept
            res.merged += 1
            old_attrs = json.loads(existing["attrs_json"])
            old_pii = keys.decrypt(existing["pii_enc"], aad)
            old_attrs.update({k: v for k, v in attrs.items() if v is not None})
            old_pii.update({k: v for k, v in pii.items() if v is not None})
            conn.execute("UPDATE leads SET attrs_json=?, pii_enc=?, external_ref=? "
                         "WHERE workflow_id=? AND phone_hmac=?",
                         (json.dumps(old_attrs, sort_keys=True), keys.encrypt(old_pii, aad),
                          old_attrs[ref_f], wf.workflow_id, hmac_))
    return res


# --------------------------------------------------------------------------- events
def _ingest_events(conn, keys: Keys, text: str, file_seq: int) -> FileResult:
    res = FileResult("", "events", skipped=False)
    seq = db.next_seq(conn, "events")
    for line in text.splitlines():
        if not line.strip():
            continue  # empty lines are not events
        res.total += 1
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            ev = None
        if not isinstance(ev, dict) or not isinstance(ev.get("event_id"), str) or not ev["event_id"]:
            res.malformed += 1
            continue
        ts = normalize.timestamp(ev.get("occurred_at"))
        raw_phone = ev.get("phone")
        canon = normalize.phone(raw_phone) if isinstance(raw_phone, str) else None
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        code = payload.get("disposition_code")
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (event_id, seq, file_seq, event_type, workflow_id, call_id, "
            "occurred_at, phone_hmac, disposition_code, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ev["event_id"], seq, file_seq, ev.get("event_type"), ev.get("workflow_id"), ev.get("call_id"),
             int(ts.timestamp()) if ts else None, keys.blind_index(canon) if canon else None,
             code if isinstance(code, str) else None, json.dumps(payload, sort_keys=True)))
        if cur.rowcount == 0:
            res.duplicates += 1  # event_id already seen, in this drop or an earlier one
        else:
            seq += 1
    return res
