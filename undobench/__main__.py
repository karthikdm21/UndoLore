"""Run with python -m undobench; only Python's standard library is needed."""

import argparse
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from .core import CELLS, DISCLAIMER, KIT_ID, PARTS, Store, ValidationError

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data/private/sessions.sqlite3"


def show_history(store, session_id):
    history = store.history(session_id)
    print(DISCLAIMER)
    print(f"Session {session_id} | {history['kit_id']} | revision {history['revision']}")
    if not history["steps"]:
        print("No recorded steps.")
    for step in history["steps"]:
        print(f"{step['sequence']}. place_in_tray {step['part_id']} -> {step['tray_cell']} "
              f"| actor={step['actor']} | {step['recorded_at']} | manual_report")


def demo():
    print(f"Synthetic demonstration: {KIT_ID}; P01-P10; tray A1-D4.")
    print(DISCLAIMER)
    print("Temporary demo: all demo records reset when this command exits.")
    with TemporaryDirectory(prefix="undobench-") as directory:
        with Store(Path(directory) / "demo.sqlite3") as store:
            session = store.create_session()
            answer = input("Record synthetic report P01 -> A1 as demo-human? Type yes: ")
            if answer == "yes":
                store.confirm(session, part_id="P01", tray_cell="A1", actor="demo-human",
                              request_id="demo-step-1", expected_revision=0, confirmed=True)
            else:
                print("Not confirmed; no step recorded.")
            show_history(store, session)


def main(argv=None):
    parser = argparse.ArgumentParser(description=DISCLAIMER)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="local SQLite file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory", help="show the synthetic kit and tray identifiers")
    commands.add_parser("demo", help="one interactive synthetic report; temporary database")
    commands.add_parser("new", help="create a persistent empty session; print its ID")
    history = commands.add_parser("history", help="show ordered manual reports")
    history.add_argument("session")
    confirm = commands.add_parser("confirm", help="append one deliberate manual report")
    confirm.add_argument("session")
    confirm.add_argument("--part", required=True)
    confirm.add_argument("--cell", required=True)
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--request", required=True)
    confirm.add_argument("--revision", required=True, type=int)
    confirm.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            print(KIT_ID)
            for part, label in PARTS.items():
                print(f"{part}: {label}")
            print("Tray cells: " + ", ".join(CELLS))
        elif args.command == "demo":
            demo()
        else:
            with Store(args.db) as store:
                if args.command == "new":
                    print(store.create_session())
                elif args.command == "history":
                    show_history(store, args.session)
                else:
                    step = store.confirm(
                        args.session, part_id=args.part, tray_cell=args.cell,
                        actor=args.actor, request_id=args.request,
                        expected_revision=args.revision, confirmed=args.confirm,
                    )
                    print(f"Recorded/replayed step {step['sequence']}.")
                    show_history(store, args.session)
    except (ValidationError, sqlite3.Error, OSError, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
