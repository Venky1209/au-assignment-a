"""The shell is a thin wrapper over the CLI: it must run the same commands and survive bad input."""
import builtins

from rich.console import Console

from conftest import MOTOR_HEADER
from ledger import shell


def test_shell_runs_commands_and_survives_errors(world, monkeypatch, tmp_path, capsys):
    drop = world.drop("d1", {("motor_renewal", "batch_001"): MOTOR_HEADER + "Asha,9876543210,POL1,,,\n"}, [])
    env = tmp_path / ".env"
    lines = iter([f"/ingest {drop}", "/ingest nowhere", "/bogus", "status", "/explain motor_renewal",
                  "/explain motor_renewal POL1", "/history", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(lines))
    monkeypatch.setattr(shell, "HISTORY", tmp_path / "hist")

    shell.run(Console(width=120), ["--db", str(tmp_path / "s.db"), "--env", str(env)])

    out = capsys.readouterr().out
    assert "not a folder" in out and "unknown command /bogus" in out
    assert "leads  motor_renewal: 1" in out
    assert "motor_renewal / POL1" in out
    assert (tmp_path / "hist").read_text().splitlines()[0].startswith("/ingest")
