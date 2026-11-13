"""Writes the five report files exactly per the output contract."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .derive import State, is_success
from .normalize import IST
from .schema import Catalog

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(x: bool) -> str:
    return "true" if x else "false"


def write_all(conn: sqlite3.Connection, cat: Catalog, st: State, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    leads = sorted(st.leads.values(), key=lambda l: (l.workflow_id, l.external_ref, l.phone_hmac))

    # 1. batch_summary.csv
    per_batch: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for l in leads:
        per_batch[(l.workflow_id, l.batch_id)][l.bucket] += 1
    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, b, sum(c.values()), *(c[k] for k in BUCKETS)] for (wf, b), c in sorted(per_batch.items())])

    # 2. daily_overview.csv
    day_calls: dict[tuple[str, str], list] = defaultdict(list)
    for l in leads:
        for c in l.calls:
            day_calls[(l.workflow_id, c.ist_date)].append((l, c))
    rows = []
    for (wf, d), items in sorted(day_calls.items()):
        total = len(items)
        connected = sum(1 for _, c in items if c.connected)
        success = len({l.key for l, c in items if is_success(cat, wf, c.effective)})
        rows.append([wf, d, total, connected, f"{connected / total:.4f}", success])
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"], rows)

    # 3. lead_report.csv
    rows = []
    for l in leads:
        last = l.calls[-1] if l.calls else None
        with_disp = [c for c in l.calls if c.effective]
        rows.append([
            l.workflow_id, l.external_ref, l.batch_id, f"+91******{l.phone_last4}", l.bucket,
            len(l.calls), sum(c.connected for c in l.calls),
            datetime.fromtimestamp(last.initiated_at, IST).isoformat() if last else "",
            with_disp[-1].effective if with_disp else "",
            _b(any(is_success(cat, l.workflow_id, c.effective) for c in l.calls)),
        ])
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"], rows)

    # 4. rejections.csv (no PII: only ids, row numbers and reasons are stored)
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [list(r) for r in conn.execute(
                   "SELECT workflow_id, batch_id, row_number, reason FROM rejections "
                   "ORDER BY workflow_id, batch_id, row_number")])

    # 5. data_quality.json
    f = conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN kind='leads' THEN rows_total END), 0)   AS lead_rows_total,
        COALESCE(SUM(rows_rejected), 0)                                AS lead_rows_rejected,
        COALESCE(SUM(rows_merged), 0)                                  AS merged,
        COALESCE(SUM(field_warnings), 0)                               AS warnings,
        COALESCE(SUM(CASE WHEN kind='events' THEN rows_total END), 0)  AS events_total,
        COALESCE(SUM(malformed), 0)                                    AS malformed,
        COALESCE(SUM(duplicates), 0)                                   AS duplicates
        FROM ingested_files""").fetchone()
    reasons = dict(conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason ORDER BY reason"))
    dq = {
        "lead_rows_total": f["lead_rows_total"],
        "lead_rows_rejected": f["lead_rows_rejected"],
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": f["merged"],
        "field_warnings": f["warnings"],
        "events_total": f["events_total"],
        "malformed_events": f["malformed"],
        "duplicate_events_dropped": f["duplicates"],
        "orphan_events": len(st.orphans),
        "unknown_disposition_events": len(st.unknown_dispositions),
    }
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
    return [out / n for n in ("batch_summary.csv", "daily_overview.csv", "lead_report.csv",
                              "rejections.csv", "data_quality.json")]
