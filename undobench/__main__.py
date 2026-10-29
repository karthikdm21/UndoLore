"""Run with python -m undobench; only Python's standard library is needed."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from .reviews import DECISIONS, review_evidence, review_status
from .evidence import import_evidence, evidence_status
from .contracts import load_manifest
from .core import CELLS, DISCLAIMER, KIT_ID, PARTS, Store, ValidationError

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data/private/sessions.sqlite3"


def provenance(step):
    if "target_sequence" not in step:
        return ""
    return (f" | correction of {step['session_id']}/{step['target_sequence']}"
            f" | reason={step['reason']}")


def show_history(store, session_id):
    history = store.history(session_id)
    print(DISCLAIMER)
    print(f"Session {session_id} | {history['kit_id']} | revision {history['revision']}")
    if not history["steps"]:
        print("No recorded steps.")
    for step in history["steps"]:
        print(f"{step['sequence']}. place_in_tray {step['part_id']} -> {step['tray_cell']} "
              f"| actor={step['actor']} | {step['recorded_at']} | manual_report" + provenance(step))


def show_resume(store, session_id):
    summary = store.resume_summary(session_id)
    print(DISCLAIMER)
    print(f"Resume summary | session {session_id} | revision {summary['revision']}")
    print("References below use session/step; request IDs identify the original reports.")
    last = summary["last_step"]
    if last is None:
        print("Most recent confirmed report: none (empty session).")
    else:
        print(f"Most recent confirmed report: {session_id}/{last['sequence']} "
              f"| request={last['request_id']} | {last['part_id']} -> {last['tray_cell']} "
              f"| actor={last['actor']} | {last['recorded_at']}" + provenance(last))
    for part in summary["parts"]:
        report = part["latest_report"]
        if report is None:
            print(f"{part['part_id']}: reported tray UNKNOWN (no confirmed report); "
                  "current physical location UNKNOWN.")
        else:
            print(f"{part['part_id']}: last reported tray {report['tray_cell']} "
                  f"[{session_id}/{report['sequence']}; request={report['request_id']}]; "
                  "current physical location UNKNOWN." + provenance(report))
    print("Unconfirmed: movement since each report, current tray occupancy, assembly state "
          "and physical correctness. Manual corrections amend records only; physical reconciliation is unverified.")
    print("No next assembly action is established by this summary.")


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
    resume = commands.add_parser("resume", help="read-only summary of stored manual reports")
    resume.add_argument("session")
    manifest = commands.add_parser("validate-manifest", help="validate evidence/graph metadata only")
    manifest.add_argument("path", type=Path)
    ingest = commands.add_parser("import-evidence", help="store validated metadata; no confirmation")
    ingest.add_argument("session")
    ingest.add_argument("manifest", type=Path)
    ingest.add_argument("--media-root", type=Path, required=True)
    status = commands.add_parser("evidence-status", help="reload metadata and recheck source bytes")
    status.add_argument("session")
    status.add_argument("bundle")
    reviews = commands.add_parser("review-status", help="read manual label reviews and current source status")
    reviews.add_argument("session")
    reviews.add_argument("bundle")
    review = commands.add_parser("review-evidence", help="append a deliberate manual label assertion")
    review.add_argument("session")
    review.add_argument("bundle")
    review.add_argument("--evidence", required=True)
    review.add_argument("--decision", required=True, choices=DECISIONS)
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--request", required=True)
    review.add_argument("--review-revision", required=True, type=int)
    review.add_argument("--confirm", action="store_true")
    for name in ("confirm", "correct"):
        command = commands.add_parser(name, help="append an explicit manual report or correction")
        command.add_argument("session")
        command.add_argument("--part", required=True)
        command.add_argument("--cell", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--request", required=True)
        command.add_argument("--revision", required=True, type=int)
        command.add_argument("--confirm", action="store_true")
        if name == "correct":
            command.add_argument("--target", required=True, help="full session-id/step-number")
            command.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-manifest":
            print(json.dumps(load_manifest(args.path), indent=2))
        elif args.command == "inventory":
            print(KIT_ID)
            for part, label in PARTS.items():
                print(f"{part}: {label}")
            print("Tray cells: " + ", ".join(CELLS))
        elif args.command == "demo":
            demo()
        else:
            with Store(args.db, read_only=args.command in ("resume", "evidence-status", "review-status")) as store:
                if args.command == "review-status":
                    print(json.dumps(review_status(store, args.session, args.bundle), indent=2))
                elif args.command == "review-evidence":
                    record = review_evidence(
                        store, args.session, args.bundle, evidence_id=args.evidence,
                        decision=args.decision, actor=args.actor, reason=args.reason,
                        request_id=args.request, expected_review_revision=args.review_revision,
                        confirmed=args.confirm)
                    print(json.dumps({"review": record, "assertion_type": "manual_label_only",
                                      "physical_verification": "unknown"}, indent=2))
                elif args.command == "import-evidence":
                    print(json.dumps(import_evidence(store, args.session, args.manifest,
                                                     args.media_root), indent=2))
                elif args.command == "evidence-status":
                    print(json.dumps(evidence_status(store, args.session, args.bundle), indent=2))
                elif args.command == "new":
                    print(store.create_session())
                elif args.command == "resume":
                    show_resume(store, args.session)
                elif args.command == "history":
                    show_history(store, args.session)
                else:
                    operation = store.correct if args.command == "correct" else store.confirm
                    extra = {"target": args.target, "reason": args.reason} if args.command == "correct" else {}
                    step = operation(
                        args.session, part_id=args.part, tray_cell=args.cell,
                        actor=args.actor, request_id=args.request,
                        expected_revision=args.revision, confirmed=args.confirm, **extra,
                    )
                    print(f"Recorded/replayed step {step['sequence']}.")
                    show_history(store, args.session)
    except (ValidationError, sqlite3.Error, OSError, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
