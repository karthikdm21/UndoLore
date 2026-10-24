# UndoBench: Day 1 manual session log

A small local foundation for Task 6B: a person explicitly records a synthetic part and tray location, then reads the ordered history. **Every entry is a manual human report; it has no visual evidence and does not verify physical state.** This is not a finished UndoBench project or completion of its evidence milestone.

The only kit is `synthetic-block-kit-v1`: ten individually identified imaginary non-powered blocks, P01-P10, and a fixed tray A1-D4. There is no actual kit review or assembly procedure. These identifiers are synthetic fixtures, not instructions for handling a real object.

## Run on Windows PowerShell

Python 3.10+ and its standard library are sufficient. No installs, environment variables, services or API keys are required. From this folder:

```powershell
Set-Location D:\Codex-Projects\UndoBench
python -m undobench demo
```

At the prompt, type `yes` to record synthetic P01 -> A1. Any other input records nothing. The command displays history and deletes its temporary demo database when it exits normally. It ignores `--db`; it never resets or writes the persistent database. Temporary files could remain after an abnormal process/OS termination.

For a persistent local session:

```powershell
python -m undobench inventory
$session = python -m undobench new
python -m undobench confirm $session --part P01 --cell A1 --actor sample-human --request step-1 --revision 0 --confirm
python -m undobench confirm $session --part P02 --cell A2 --actor sample-human --request step-2 --revision 1 --confirm
python -m undobench history $session
```

`--confirm` is a deliberate user assertion, not authentication. Use a synthetic actor label, not personal information. In a later PowerShell window, copy the session ID displayed in history and run `python -m undobench history <session-id>` (replace the placeholder). `new` prints only the ID for scripting.

A repeated request ID with identical fields and original revision returns the existing record. Reusing it with different input fails. A new request must supply the current revision shown in history. Invalid part/cell/actor/request/revision, missing confirmation or unknown session produces an error and nonzero exit status without adding a step. IDs and actor labels accept 1-64 ASCII letters, digits, underscores or hyphens; part/cell values must match the displayed inventory exactly.

## What persists and what resets

- Normal commands store sessions, kit version, UTC creation times, revisions and ordered steps in `data/private/sessions.sqlite3`, resolved relative to this code folder. Each step stores its session, sequence, request ID, part, cell, actor, UTC time and `manual_report` evidence mode. A transaction appends the step and advances the revision together. Records survive command/process restarts.
- `new` starts an empty, separate session; existing sessions remain. Sequence/revision starts at zero and the first step is 1. Request IDs are scoped to a session.
- `python -m undobench --db .\data\private\experiment.sqlite3 new` selects a separate local database. Supply that same `--db` before subsequent subcommands. No state is shared between files.
- The interactive `demo` uses a fresh temporary database each run, retaining nothing after normal exit. The PowerShell `$session` variable lasts only in that shell; the saved session does not reset with it.
- There is no reset/delete command. To reset a persistent database deliberately, stop all commands, back up the file if needed, and delete only the chosen database and any SQLite sidecars. This removes every session in that file. No automatic reset occurs.

History is an append-only report through this CLI. Repeated parts/cells are allowed as historical reports; this slice does not infer current occupancy, detect physical contradictions, support corrections/undo, or recommend assembly actions. Local SQLite is not encrypted or tamper-proof, and anyone with file access can alter it. Do not record personal media or real participant data here.

## Verify

```powershell
python -m unittest discover -s tests -v
python -m compileall -q undobench tests
git diff --check
```

Tests cover ordering, validation with no appended record, explicit confirmation, duplicate/conflicting requests, stale revisions, isolated sessions/databases, transaction rollback, persistence across separate processes, CLI failures, and both demo choices. They use only temporary synthetic data. No prior checks existed; no third-party lint/type-check tools are configured.

The brief proposes Python/SQLite with FastAPI, OpenCV and React for later work. This preliminary slice needs only Python/SQLite. No camera, video, visual model, AI/API, graph, dashboard, frontend, cloud or evaluation claims are implemented. All broader milestones remain outstanding.

## Private files and Git

The existing project briefs, `local-notes/`, `data/private/`, database files, media, virtual environments, `.env` files and caches are ignored. Keep private files in those locations; ignore rules are not a secret scanner. Only code, tests, this README and ignore rules belong in today's local commits. No remote is required.
