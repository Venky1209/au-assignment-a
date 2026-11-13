"""Command line: `ledger ingest <path>...`, `ledger report --out <dir>`, `ledger explain <wf> <ref>`."""
from __future__ import annotations

import argparse
import csv
import time
from collections import Counter
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from . import db, derive, schema, shell
from .crypto import Keys
from .ingest import ingest_path
from .report import write_all

console = Console()


def _open(args):
    keys = Keys.load(Path(args.env))
    conn = db.connect(args.db)
    db.check_key(conn, keys.fingerprint())
    return conn, keys, schema.load(Path(args.schemas))


def cmd_ingest(args) -> None:
    conn, keys, cat = _open(args)
    t0 = time.perf_counter()
    # compact on purpose: must stay readable in a narrow terminal / screen share
    table = Table(box=None, padding=(0, 1), pad_edge=False, header_style="bold")
    table.add_column("file", overflow="fold", ratio=1)  # only the file name may wrap
    for col in ("rows", "rej", "merged", "warn", "bad", "dup"):
        table.add_column(col, justify="right", no_wrap=True, min_width=max(len(col), 5))
    table.add_column("status", no_wrap=True, min_width=6)
    for p in args.paths:
        for r in ingest_path(conn, keys, cat, Path(p)):
            parts = Path(r.path).parts
            name = f"{parts[-2]}/{Path(r.path).stem}" if r.kind == "leads" else f"events/{Path(r.path).stem}"
            if r.skipped:
                table.add_row(name, "", "", "", "", "", "", "[yellow]skip[/]")
            elif r.kind == "leads":
                table.add_row(name, str(r.total), str(r.rejected), str(r.merged), str(r.warnings), "", "",
                              "[green]ok[/]")
            else:
                table.add_row(name, str(r.total), "", "", "", str(r.malformed), str(r.duplicates), "[green]ok[/]")
    console.print(table)
    console.print("rej=rejected  warn=bad optional field  bad=malformed line  dup=duplicate event  skip=already ingested",
                  style="dim")
    console.print(f"done in {time.perf_counter() - t0:.2f}s -> {args.db}")


def cmd_report(args) -> None:
    conn, _, cat = _open(args)
    t0 = time.perf_counter()
    st = derive.build(conn, cat)
    for p in write_all(conn, cat, st, Path(args.out)):
        console.print(f"wrote {p}")
    console.print(f"{len(st.leads)} leads, {sum(len(l.calls) for l in st.leads.values())} calls, "
                  f"{len(st.orphans)} orphan events in {time.perf_counter() - t0:.2f}s")


def cmd_status(args) -> None:
    """Look inside the database: what was ingested, in what order, and where every event ended up."""
    conn, keys, cat = _open(args)
    console.print(f"[bold]{args.db}[/]  hmac key fingerprint {keys.fingerprint()}")
    t = Table(box=None, padding=(0, 1), header_style="bold", title="ingested files (in order)", title_justify="left")
    for col in ("seq", "kind", "workflow", "batch", "rows", "sha256"):
        t.add_column(col, no_wrap=True)
    for r in conn.execute("SELECT seq, kind, workflow_id, batch_id, rows_total, sha256 FROM ingested_files ORDER BY seq"):
        t.add_row(str(r["seq"]), r["kind"], r["workflow_id"] or "", r["batch_id"] or "", str(r["rows_total"]),
                  r["sha256"][:12])
    console.print(t)
    for r in conn.execute("SELECT workflow_id, COUNT(*) n FROM leads GROUP BY workflow_id ORDER BY workflow_id"):
        console.print(f"leads  {r['workflow_id']}: {r['n']}")
    st = derive.build(conn, cat)
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    malformed = conn.execute("SELECT COALESCE(SUM(malformed),0) FROM ingested_files").fetchone()[0]
    console.print(f"ledger {total} events stored | linked {total - len(st.orphans) - len(st.unknown_dispositions)}"
                  f" | [yellow]quarantined (orphans) {len(st.orphans)}[/] | unknown disposition "
                  f"{len(st.unknown_dispositions)} | malformed lines (not stored) {malformed}")
    console.print("[dim]orphans stay in the ledger and link automatically when their lead arrives; "
                  "see `ledger quarantine`[/]")


def cmd_quarantine(args) -> None:
    """List every quarantined event with the reason, and write it to a CSV ops can work from.

    No PII: the CSV carries ids and reasons only, never the phone (which we do not store anyway).
    """
    conn, _, cat = _open(args)
    st = derive.build(conn, cat)
    rows = sorted(
        [(e.workflow_id or "", e.call_id or "", e.event_id, e.event_type or "", st.orphan_reasons[e.event_id], "")
         for e in st.orphans]
        + [(e.workflow_id or "", e.call_id or "", e.event_id, e.event_type or "", "unknown_disposition",
            e.disposition_code or "") for e in st.unknown_dispositions])
    counts = Counter(r[4] for r in rows)
    t = Table(box=None, padding=(0, 1), header_style="bold", title="quarantined events by reason",
              title_justify="left")
    t.add_column("reason", no_wrap=True)
    t.add_column("events", justify="right")
    t.add_column("what it means")
    meaning = {
        "no_matching_lead": "valid phone, but no valid lead (yet) for it in this workflow",
        "invalid_or_missing_phone": "phone cannot be normalised to an Indian mobile",
        "override_on_unlinked_call": "supervisor override on a call that itself has no lead",
        "missing_call_id_or_timestamp": "cannot be placed on a call",
        "unknown_workflow": "workflow_id has no schema",
        "unknown_disposition": "linked, but the code is not in dispositions.csv (ignored)",
    }
    for reason, n in counts.most_common():
        t.add_row(reason, str(n), meaning.get(reason, ""))
    console.print(t)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["workflow_id", "call_id", "event_id", "event_type", "reason", "disposition_code"])
        w.writerows(rows)
    console.print(f"wrote {len(rows)} rows to {out}")


WELCOME = """[bold]ledger[/]: interaction ledger & reporting pipeline

  ledger ingest <drop> [<drop> ...]   ingest lead CSVs + event JSONL (re-running is a no-op)
  ledger report --out reports         write the 5 report files
  ledger status                       what is in the database, incl. quarantined events
  ledger quarantine [--out file.csv]  events kept out of the reports, and why
  ledger explain <workflow> <ref>     why a lead is in its bucket, event by event
  ledger shell                        interactive mode: /ingest /report /status /explain /history

  options (before the command): --db ledger.db  --env .env  --schemas <dir>
  start here: [cyan]ledger ingest data/drop_1[/]"""


def cmd_explain(args) -> None:
    from .explain import explain
    conn, _, cat = _open(args)
    explain(console, conn, cat, args.workflow_id, args.external_ref)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ledger", description="Interaction ledger & reporting pipeline")
    ap.add_argument("--db", default="ledger.db", help="SQLite file (default: ledger.db)")
    ap.add_argument("--env", default=".env", help="file holding LEDGER_ENC_KEY / LEDGER_HMAC_KEY")
    ap.add_argument("--schemas", default=str(schema.DEFAULT_SCHEMA_DIR), help="workflow schema directory")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="show what is in the database, incl. quarantined events")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("quarantine", help="list events kept out of the reports, with the reason, as CSV")
    p.add_argument("--out", default="reports/quarantine.csv")
    p.set_defaults(fn=cmd_quarantine)

    p = sub.add_parser("ingest", help="ingest one drop, or a folder of drops (processed in name order)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("report", help="write the five report files")
    p.add_argument("--out", default="reports")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("explain", help="show why a lead is in its bucket, event by event")
    p.add_argument("workflow_id")
    p.add_argument("external_ref")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("shell", help="interactive mode with /commands and history")
    p.set_defaults(fn=lambda a: shell.run(console, ["--db", a.db, "--env", a.env, "--schemas", a.schemas]))

    args = ap.parse_args(argv)
    if args.cmd is None:
        console.print(WELCOME)
        return
    args.fn(args)


if __name__ == "__main__":
    main()
