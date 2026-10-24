"""Local manual reports. No physical or visual verification is performed."""

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import uuid

KIT_ID = "synthetic-block-kit-v1"
PARTS = {f"P{i:02}": f"Synthetic block {i:02}" for i in range(1, 11)}
CELLS = tuple(f"{row}{col}" for row in "ABCD" for col in range(1, 5))
DISCLAIMER = "Manual human report only; no visual evidence or physical verification."


class ValidationError(ValueError):
    """A command was rejected without appending a record."""


def token(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValidationError(f"{field} must be 1-64 ASCII letters, digits, _ or -")
    return value


class Store:
    def __init__(self, path, *, read_only=False):
        path = Path(path)
        if read_only:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                      uri=True, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                kit_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
            );
            CREATE TABLE IF NOT EXISTS steps (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                request_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                tray_cell TEXT NOT NULL,
                actor TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                evidence_mode TEXT NOT NULL CHECK (evidence_mode = 'manual_report'),
                PRIMARY KEY (session_id, sequence),
                UNIQUE (session_id, request_id)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    def create_session(self):
        session_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO sessions (id, kit_id, created_at) VALUES (?, ?, ?)",
            (session_id, KIT_ID, datetime.now(timezone.utc).isoformat()),
        )
        return session_id

    def _session(self, session_id):
        token(session_id, "session_id")
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValidationError("unknown session; create one with new")
        if row["kit_id"] != KIT_ID:
            raise ValidationError("unsupported kit version")
        return row

    def history(self, session_id):
        # A single read transaction gives a consistent revision and history.
        self.db.execute("BEGIN")
        try:
            session = dict(self._session(session_id))
            session["steps"] = [dict(row) for row in self.db.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY sequence", (session_id,)
            )]
            self.db.execute("COMMIT")
            return session
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def resume_summary(self, session_id):
        """Summarize recorded reports, never infer current physical state."""
        history = self.history(session_id)
        latest = {step["part_id"]: step for step in history["steps"]}
        return {
            "session_id": session_id,
            "revision": history["revision"],
            "last_step": history["steps"][-1] if history["steps"] else None,
            "parts": [
                {"part_id": part, "latest_report": latest.get(part),
                 "current_physical_location": "unknown"}
                for part in PARTS
            ],
        }

    def confirm(self, session_id, *, part_id, tray_cell, actor,
                request_id, expected_revision, confirmed):
        token(session_id, "session_id")
        token(request_id, "request_id")
        token(actor, "actor")
        if not isinstance(part_id, str) or part_id not in PARTS:
            raise ValidationError("part_id must be P01-P10 from the synthetic inventory")
        if not isinstance(tray_cell, str) or tray_cell not in CELLS:
            raise ValidationError("tray_cell must be A1-D4 (uppercase)")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError("expected_revision must be a non-negative integer")
        if confirmed is not True:
            raise ValidationError("explicit human confirmation is required (--confirm)")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            session = self._session(session_id)
            previous = self.db.execute(
                "SELECT * FROM steps WHERE session_id = ? AND request_id = ?",
                (session_id, request_id),
            ).fetchone()
            if previous is not None:
                if (previous["part_id"], previous["tray_cell"], previous["actor"],
                    previous["sequence"] - 1) != (part_id, tray_cell, actor, expected_revision):
                    raise ValidationError("request_id already used with different input")
                result = dict(previous)
            else:
                if session["revision"] != expected_revision:
                    raise ValidationError("stale revision; read history before confirming again")
                sequence = expected_revision + 1
                self.db.execute(
                    "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, 'manual_report')",
                    (session_id, sequence, request_id, part_id, tray_cell, actor,
                     datetime.now(timezone.utc).isoformat()),
                )
                self.db.execute("UPDATE sessions SET revision = ? WHERE id = ?",
                                (sequence, session_id))
                result = dict(self.db.execute(
                    "SELECT * FROM steps WHERE session_id = ? AND sequence = ?",
                    (session_id, sequence),
                ).fetchone())
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise
