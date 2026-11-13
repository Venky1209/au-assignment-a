"""Pure value normalisers. No I/O, no database: every rule from the spec lives here and is unit-tested.

Each normaliser returns the canonical value, or None when the input is invalid.
Callers decide whether "invalid" means a rejection or a field warning.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def header(h: str) -> str:
    """lower-case, trim, then collapse every run of '_' or whitespace into one space."""
    return re.sub(r"[_\s]+", " ", h.lower().strip())


def phone(raw: str) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{2})-(\d{2})-(\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$"), ("d", "mon", "y")),
]


def date_value(raw: str) -> str | None:
    """Day-first date in one of the four accepted formats -> 'YYYY-MM-DD', or None."""
    s = raw.strip()
    for rx, order in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        if "mon" in parts:
            month = _MONTHS.get(parts["mon"].lower())
            if month is None:
                return None
        else:
            month = int(parts["m"])
        try:
            return date(int(parts["y"]), month, int(parts["d"])).isoformat()
        except ValueError:  # 31/02, month 13, ...
            return None
    return None


_DECIMAL = re.compile(r"^(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$", re.IGNORECASE)


def decimal_value(raw: str) -> str | None:
    """Optional ₹/Rs/Rs./INR prefix, commas as thousands separators, >= 0, 2 dp -> '1234.50'."""
    m = _DECIMAL.match(raw.strip())
    if not m:
        return None  # covers negatives, words, 'N/A', '-'
    try:
        d = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def enum_value(raw: str, allowed: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {a.lower() for a in allowed} else None


def timestamp(raw: str) -> datetime | None:
    """'...Z', '...+05:30' or naive (= IST) -> aware datetime, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())  # 3.11+: accepts 'Z' and ' ' separator
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt
