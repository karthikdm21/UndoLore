# UndoLore

UndoBench is the local project and Python package behind this repository.

A local Python/SQLite experiment for recording a person's part-and-tray reports and reviewing them after an interruption. All records are manually confirmed assertions. They do not establish visual or physical correctness.

The current fixture is one synthetic ten-block kit (`P01`-`P10`) with tray cells `A1`-`D4`. No real kit or media is included.

## Implemented

- Separate persistent sessions with numbered reports and validated inputs.
- Explicit confirmation, revision checks and idempotent request retries.
- Read-only resume summaries with report references and unknown physical locations.
- Appended corrections with original-record links, actor, reason and timestamp; earlier reports stay intact.

## Run

Use Python 3.10+ from the repository root. Only the standard library is needed; there are no packages to install or API keys to configure.

```powershell
python -m undobench demo
```

Type `yes` to record the synthetic example. Other input records nothing. This demo uses a temporary database that is removed on normal exit, ignoring `--db`. An abnormal termination may leave temporary files.

A persistent session and one correction, in Windows PowerShell:

```powershell
python -m undobench inventory
$session = python -m undobench new
python -m undobench confirm $session --part P01 --cell A1 --actor sample-human --request step-1 --revision 0 --confirm
python -m undobench correct $session --target "$session/1" --part P02 --cell B2 --reason "Synthetic label transcribed incorrectly" --actor sample-reviewer --request correction-1 --revision 1 --confirm
python -m undobench history $session
python -m undobench resume $session
```

Keep the printed session ID to reopen it in another shell. `--confirm` is a deliberate assertion, not authentication. Use synthetic actor labels and reasons. IDs accept 1-64 ASCII letters, digits, underscores or hyphens; parts and cells must match the inventory. Errors return a nonzero exit code without appending a report.

## Corrections and uncertainty

Each correction requires a full same-session `session-id/step-number` target, a changed part or cell, explicit confirmation and a nonblank reason of at most 300 characters without control characters. Use the current revision displayed by history. To amend a correction, target that correction's step; previously corrected targets cannot be branched.

An identical request ID and input replay the stored result, even after later reports. Different input or command type using the same request ID is rejected. Invalid targets, cross-session targets and stale revisions cannot append records.

Resume cites the latest confirmed event and latest effective report per part. Corrections amend a report at its original historical position: correcting an older report does not override a later placement report. Replacing a mistaken part ID removes that assertion from the effective summary unless another report supports it. History retains the full correction chain and reasons.

Reported cells are historical assertions. Unreported parts and **all current physical locations remain UNKNOWN**. The program does not infer tray occupancy, detect movement, verify physical reconciliation or recommend assembly actions.

## Storage

Normal commands persist sessions, revisions, numbered reports and correction metadata in ignored `data/private/sessions.sqlite3`, relative to the code directory. Reports store request IDs, part/cell IDs, actors, UTC times and `manual_report` evidence mode. Accepted writes and revision changes are transactional.

`new` creates an empty session without resetting existing ones. To select another database, put `--db` before the command, for example `python -m undobench --db ./data/private/experiment.sqlite3 new`; use the same option for subsequent commands. Process restarts do not reset saved data. No summary is stored separately. `resume` opens SQLite read-only and neither creates missing databases nor changes session state.

Older databases gain an additive `corrections` table on their next writable command; original reports are preserved. Read-only resume can also read older databases without migration. There is no reset/delete command. Deliberately deleting a selected database and its SQLite sidecars after stopping commands removes all its sessions; back it up first if needed.

## Test

```powershell
python -m unittest discover -s tests -v
python -m compileall -q undobench tests
git diff --check
```

Tests use temporary synthetic data. They cover validation, isolation, ordering, retries, stale revisions, rollback, correction chains, legacy-database compatibility, read-only enforcement and process reload. A relocated-code test checks restart behavior without claiming a laptop reboot. No third-party lint or type-check tools are configured.

## Limits and next steps

This is a command-line foundation. It has no camera, video evidence, CV model, AI/API integration, frontend, procedure graph or assembly planner. It has not been evaluated with a real kit or participants. SQLite files are neither encrypted nor tamper-proof.

Next: a reproducible synthetic interruption walkthrough that exercises recording, reload, correction and remaining uncertainty. Evidence-linked media and reviewed procedure knowledge would require separate work and validation.

Private briefs, learning notes, runtime databases, media, environments, `.env` files and caches are excluded from Git. Keep personal data out of actor labels and reasons; ignore rules do not detect secrets.
