"""Idempotency and incremental equivalence on freshly generated data (a seed other than 42)."""
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO
from ledger import db, derive
from ledger.ingest import ingest_path
from ledger.report import write_all

FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen") / "data"
    subprocess.run([sys.executable, str(REPO / "assignment-a-data/tools/generate_data.py"),
                    "--seed", "1234", "--scale", "small", "--out", str(out)], check=True, capture_output=True)
    return out


def run(tmp: Path, name: str, keys, cat, *paths: Path) -> dict[str, bytes]:
    conn = db.connect(tmp / f"{name}.db")
    db.check_key(conn, keys.fingerprint())
    for p in paths:
        ingest_path(conn, keys, cat, p)
    write_all(conn, cat, derive.build(conn, cat), tmp / name)
    return {f: (tmp / name / f).read_bytes() for f in FILES}


def test_incremental_equals_all_at_once_equals_rerun(tmp_path, keys, cat, data):
    stepwise = run(tmp_path, "stepwise", keys, cat, data / "drop_1", data / "drop_2")
    at_once = run(tmp_path, "at_once", keys, cat, data)  # folder of drops, name order
    rerun = run(tmp_path, "rerun", keys, cat, data / "drop_1", data / "drop_1", data / "drop_2",
                data / "drop_2", data / "drop_1")
    for f in FILES:
        assert stepwise[f] == at_once[f], f
        assert stepwise[f] == rerun[f], f


def test_report_is_deterministic_across_runs(tmp_path, keys, cat, data):
    a = run(tmp_path, "a", keys, cat, data)
    b = run(tmp_path, "b", keys, cat, data)
    assert a == b
