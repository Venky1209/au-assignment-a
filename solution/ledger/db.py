"""SQLite schema.

Design rule: ingest validates, normalises and appends. It never aggregates and never decides
whether an event is an orphan. Linking and every metric are derived at report time from the
current state, so "drop_1 then drop_2" and "everything at once" give identical reports by
construction, and an early orphan starts counting the moment its lead arrives.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per input file. A file is ingested in a single transaction together with its row
-- here, so it is either fully in or not at all; re-ingesting the same content is a no-op.
CREATE TABLE IF NOT EXISTS ingested_files (
    sha256       TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL UNIQUE,      -- ingest order
    kind         TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id  TEXT,
    batch_id     TEXT,
    source_path  TEXT NOT NULL,
    rows_total   INTEGER NOT NULL DEFAULT 0,   -- leads: non-blank rows; events: non-empty lines
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    rows_merged  INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    malformed    INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Current merged state of each lead. Identity = (workflow_id, blind index of canonical phone).
CREATE TABLE IF NOT EXISTS leads (
    workflow_id  TEXT NOT NULL,
    phone_hmac   TEXT NOT NULL,
    batch_id     TEXT NOT NULL,               -- batch of the first valid row, never changes
    external_ref TEXT NOT NULL,
    phone_last4  TEXT NOT NULL,               -- deliberate partial leak, same as the masked output
    attrs_json   TEXT NOT NULL,               -- non-PII normalised fields (null = missing/invalid)
    pii_enc      TEXT NOT NULL,               -- AES-GCM of all pii=true fields
    first_file_seq INTEGER NOT NULL REFERENCES ingested_files(seq),
    PRIMARY KEY (workflow_id, phone_hmac)
);

CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    file_seq    INTEGER NOT NULL REFERENCES ingested_files(seq),
    PRIMARY KEY (file_seq, row_number)
);

-- The interaction ledger. Append-only: nothing ever UPDATEs or DELETEs these rows (enforced by
-- triggers). Overrides are new rows, never edits. Raw phone is not stored, only its blind index.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL UNIQUE,     -- global arrival order, used as a tie-breaker
    file_seq     INTEGER NOT NULL REFERENCES ingested_files(seq),
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  INTEGER,                     -- unix seconds (UTC); NULL if unparseable
    phone_hmac   TEXT,                        -- NULL if absent or not a valid Indian mobile
    disposition_code TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_call ON events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit BEGIN/COMMIT below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(DDL)
    return conn


def check_key(conn: sqlite3.Connection, fingerprint: str) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='hmac_fp'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('hmac_fp', ?)", (fingerprint,))
    elif row["value"] != fingerprint:
        raise RuntimeError("this database was built with a different LEDGER_HMAC_KEY; "
                           "phones would no longer match. Use the original .env or a fresh --db.")


def next_seq(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()[0]
