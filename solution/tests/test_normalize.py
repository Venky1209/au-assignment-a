import pytest

from ledger import normalize as n


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210", "+919876543210",
])
def test_phone_all_six_formats(raw):
    assert n.phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad leading digit
    "022-2345-6789",      # landline: 11 digits, strip 0 -> starts with 2
    "N/A", "", "+1 415 555 0134",
    "919876543210 1",     # 13 digits
])
def test_phone_invalid(raw):
    assert n.phone(raw) is None


@pytest.mark.parametrize("raw,out", [
    ("2026-12-01", "2026-12-01"), ("01/12/2026", "2026-12-01"), ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"), ("01-dec-2026", "2026-12-01"), ("05/03/2026", "2026-03-05"),  # day-first
])
def test_date_valid(raw, out):
    assert n.date_value(raw) == out


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "soon", "TBD", "1/2/2026", "01-Foo-2026", "2026/12/01"])
def test_date_invalid(raw):
    assert n.date_value(raw) is None


@pytest.mark.parametrize("raw,out", [
    ("12000", "12000.00"), ("₹12,000", "12000.00"), ("Rs. 4500", "4500.00"), ("Rs4500", "4500.00"),
    ("rs 4500", "4500.00"), ("INR 1234.5", "1234.50"), ("inr1,234.567", "1234.57"), ("0", "0.00"),
    ("1234.565", "1234.57"),  # half-up, not banker's rounding
])
def test_decimal_valid(raw, out):
    assert n.decimal_value(raw) == out


@pytest.mark.parametrize("raw", ["-500", "N/A", "-", "twelve thousand", "₹", "$100", "1.2.3"])
def test_decimal_invalid(raw):
    assert n.decimal_value(raw) is None


def test_enum_case_insensitive_and_lowercased():
    assert n.enum_value(" HI ", ["en", "hi"]) == "hi"
    assert n.enum_value("Hindi", ["en", "hi"]) is None


def test_header_normalisation():
    assert n.header("  Mobile__No ") == "mobile no"
    assert n.header("Policy   No") == "policy no"
    assert n.header("preferred_language") == "preferred language"


def test_timestamps_three_forms_are_the_same_instant():
    z = n.timestamp("2026-10-03T04:31:00Z")
    off = n.timestamp("2026-10-03T10:01:00+05:30")
    naive = n.timestamp("2026-10-03 10:01:00")  # naive means IST
    assert z == off == naive
    assert n.timestamp("garbage") is None and n.timestamp(None) is None
