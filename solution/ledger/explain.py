"""`ledger explain <workflow> <external_ref>`: why a lead is where it is, event by event.

Debug tool for ops: every number in the reports can be traced back to the ledger rows behind it.
"""
from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.table import Table

from . import derive
from .normalize import IST
from .schema import Catalog


def _t(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S IST") if ts is not None else "?"


def _bucket_reason(lead: derive.Lead, cat: Catalog) -> str:
    if lead.bucket == "yet_to_reach_out":
        return "no counted calls (a call needs a linked call.initiated)"
    if lead.bucket == "completed":
        c = next(c for c in lead.calls if derive.is_terminal(cat, lead.workflow_id, c.effective))
        return f"call {c.call_id} has terminal effective disposition '{c.effective}'"
    if lead.bucket == "in_progress_telephony_failed":
        return f"all {len(lead.calls)} call(s) hit call.telephony_failed"
    return "has calls, none terminal, not all telephony-failed"


def explain(console: Console, conn, cat: Catalog, workflow_id: str, external_ref: str) -> None:
    st = derive.build(conn, cat)
    matches = [l for l in st.leads.values() if l.workflow_id == workflow_id and l.external_ref == external_ref]
    if not matches:
        console.print(f"[red]no lead {workflow_id} / {external_ref}[/] (rejected rows are in rejections.csv)")
        return
    for lead in matches:
        console.rule(f"{lead.workflow_id} / {lead.external_ref}")
        console.print(f"batch [bold]{lead.batch_id}[/]  phone +91******{lead.phone_last4}  "
                      f"bucket [bold cyan]{lead.bucket}[/]")
        console.print(f"why: {_bucket_reason(lead, cat)}\n")

        lead_calls = [c for c in st.calls.values() if c.lead_key == lead.key]
        lead_calls.sort(key=lambda c: (c.initiated_at is None, c.initiated_at or 0, c.call_id))
        for c in lead_calls:
            status = "counted" if c.counted else "[yellow]NOT counted: no call.initiated[/]"
            disp = c.effective or "-"
            if c.effective_from is not None:
                disp += f"  (from {c.effective_from.event_type} {c.effective_from.event_id})"
            console.print(f"[bold]{c.call_id}[/] {status}  effective: [bold]{disp}[/]")
            t = Table(show_header=True, box=None, padding=(0, 2))
            for col in ("occurred_at", "event", "event_id", "detail"):
                t.add_column(col)
            for e in sorted(c.events, key=derive._order):
                detail = e.disposition_code or ""
                if e is c.effective_from:
                    detail += "  <- wins"
                elif e.event_type in derive.DISPOSITION_TYPES:
                    detail += "  (superseded, kept in ledger)"
                t.add_row(_t(e.occurred_at), e.event_type or "?", e.event_id, detail)
            console.print(t)

        ignored = [e for e in st.unknown_dispositions if (e.workflow_id, e.call_id) in
                   {(c.workflow_id, c.call_id) for c in lead_calls}]
        for e in ignored:
            console.print(f"[yellow]ignored[/] {e.event_type} {e.event_id}: "
                          f"code '{e.disposition_code}' not in catalogue for {e.workflow_id}")
