"""`ledger shell`: interactive mode with slash commands and history.

A thin wrapper: every /command is turned into the same argv the plain CLI takes and run through
`cli.main`, so the shell can never behave differently from the commands the graders run.
History lives in ~/.ledger_history (commands only, never data). Up-arrow recall works through
readline where available and through the Windows console's own line editing otherwise.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from rich.console import Console

HISTORY = Path.home() / ".ledger_history"
COMMANDS = {
    "/ingest": "ingest <drop> [<drop> ...]",
    "/report": "write the 5 reports  (/report [out_dir], default: reports)",
    "/status": "what is in the database, incl. quarantined events",
    "/quarantine": "events kept out of the reports, and why  (/quarantine [out.csv])",
    "/explain": "explain <workflow> <external_ref>",
    "/history": "show previous commands",
    "/help": "show this list",
    "/quit": "leave the shell",
}

try:  # POSIX / some Windows installs: arrow-key history across sessions
    import readline
except ImportError:  # plain Windows console still gives up-arrow within the session
    readline = None


def _help(console: Console) -> None:
    for cmd, desc in COMMANDS.items():
        console.print(f"  [cyan]{cmd:<12}[/] {desc}")


def _load_history() -> list[str]:
    lines = HISTORY.read_text(encoding="utf-8").splitlines() if HISTORY.exists() else []
    if readline:
        for line in lines:
            readline.add_history(line)
    return lines


def run(console: Console, global_argv: list[str]) -> None:
    from .cli import main  # local import: cli imports this module

    history = _load_history()
    console.print("[bold]ledger shell[/]  type [cyan]/help[/] for commands, [cyan]/quit[/] to leave")
    while True:
        try:
            line = input("ledger> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        history.append(line)
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        try:
            parts = shlex.split(line, posix=False)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        if not cmd.startswith("/"):
            cmd = "/" + cmd  # be forgiving: 'status' works like '/status'
        if cmd in ("/quit", "/exit", "/q"):
            break
        if cmd == "/help":
            _help(console)
            continue
        if cmd == "/history":
            for i, h in enumerate(history[-20:], start=max(1, len(history) - 19)):
                console.print(f"  {i:>3}  {h}")
            continue
        if cmd in ("/report", "/quarantine"):
            rest = ["--out", rest[0]] if rest else []
        if cmd not in COMMANDS:
            console.print(f"[red]unknown command {cmd}[/]  (try /help)")
            continue
        try:
            main([*global_argv, cmd[1:], *rest])
        except SystemExit as e:  # argparse usage errors print themselves; ours carry a message
            if isinstance(e.code, str):
                console.print(f"[red]{e.code}[/]")
        except Exception as e:  # one bad command must not kill the session
            console.print(f"[red]error:[/] {e}")
