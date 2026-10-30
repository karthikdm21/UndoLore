# Evidence metadata boundary (schema 1)

Run `python -m undobench validate-manifest data/fixtures/synthetic_manifest.json` from the repository root. This command reads JSON only, returns a normalized validation report, and creates no session or database. Errors return exit code 2. No source path is opened, video decoded, hash checked, human review authenticated or visual correctness established.

The included manifest is entirely synthetic. Its zero hash, source path, review actor and action labels are test data. No corresponding video exists. The output retains `synthetic: true`, `media_status: not_inspected`, `review_status: human_review_declared_only` and `physical_verification: unknown`. Even a manifest declaring real data receives the same unverified statuses; validation alone cannot turn it into supported evidence.

JSON input is bounded to 256 KiB and rejects duplicate keys, unexpected fields and unsupported schema versions. The fixture documents required fields. A kit has 10-15 unique stable part IDs. Source metadata carries a source ID, relative portable path, declared SHA256, capture segment, duration in source PTS, and positive integer rational timebase numerator/denominator. Intervals use integer PTS and satisfy `0 <= start < end <= duration`. Derived display milliseconds use `PTS * numerator * 1000 // denominator` (round down); original PTS remains intact. No nominal FPS conversion is used.

Evidence IDs link an interval to a source, inventory part and tray cell. These remain labels, not observed facts. Separate disassembly and reassembly graphs each declare a version, explicit review boolean and reviewer label. Nodes carry IDs, action text, part IDs, prerequisite IDs and evidence IDs. Validation rejects duplicate/missing IDs, unknown or mismatched parts, missing evidence, self dependencies and cycles. Review declarations do not prove the edges were reviewed against a physical kit. This module provides no planner, eligibility computation or session confirmation bypass.

Next integration must verify source files/hashes/timebases and availability, then link explicit user review to persisted session events without converting missing or ambiguous evidence into confirmation. Actual reviewed before/after conditions, correction invalidation and unknown-prerequisite blocking remain required. Schema 1 checks metadata only and does not complete media ingestion or the brief's evidence milestone.


## Persistent references and byte availability

`python -m undobench import-evidence SESSION path/to/manifest.json --media-root path/to/private/media` validates and stores an immutable manifest snapshot against an existing matching-kit session. It prints a bundle ID (SHA256 of canonical manifest JSON). Use `python -m undobench evidence-status SESSION BUNDLE` to reload it read-only and recheck files. Put `--db` before the subcommand when using a custom database. The default fixture is synthetic and intentionally has no corresponding video.

Imports add one `evidence_imports` table to the existing local SQLite database, containing session/bundle IDs, manifest JSON, private media-root path, import time and initial byte-check statuses. Same-session identical manifests replay the saved import; rebinding that bundle to another root is rejected. Changed manifests form new bundles, preserving earlier snapshots. No steps, confirmations or session revisions change. Different sessions cannot read each other's bundle references. Data survives process restarts; deleting its database removes it under the existing reset policy.

Source paths resolve inside the selected root, including resolved-link containment checks. Hashing reads files in 1 MiB chunks. Status is `missing`, `not_a_file`, `outside_media_root`, `unreadable`, `hash_mismatch` or `bytes_match`. Retrieval returns initial and freshly checked statuses; prior matches never replace current failures. A match proves only byte identity against the supplied hash at check time. It does not decode a video or establish its actual timebase, duration, usability, human review or physical correctness. Source metadata still says `declared_not_decoded`, and evidence remains `not_confirmed`. File changes after a check require another check.

Real kit session configuration, actual media probing/playback, evidence-linked confirmations and graph planning remain separate work. Current session creation still uses the existing synthetic kit; manifests for a different kit are rejected rather than silently relabeled. Keep real manifests, media roots and databases under private local storage. No actual video or user data is committed.


## Explicit manual label reviews

After importing a bundle, `review-status` returns its separate review revision (initially zero):

```powershell
python -m undobench review-status SESSION BUNDLE
python -m undobench review-evidence SESSION BUNDLE --evidence synthetic-label --decision unclear --actor sample-reviewer --reason "Synthetic ambiguity" --request review-1 --review-revision 0 --confirm
```

Replace session/bundle placeholders with IDs from an existing import. `--db` still goes before the command. Decisions are `accept_label`, `unclear` and `reject_label`. Every append requires actor, nonblank reason (at most 300 characters; no controls), a new request ID, current bundle review revision and deliberate `--confirm`. Accepting a label requires current source bytes to match the immutable manifest's declared hash. Unclear/reject can record uncertainty even if media is missing. The app does not play or decode the video; review means the person reports a decision about that reference, not that the app verified the scene or authenticated the reviewer.

The new additive `evidence_reviews` SQLite table stores immutable ordered decisions with session, bundle, evidence ID, request ID, actor, reason, UTC time and source-byte status at review. The reference resolves back to original PTS/timebase, part and cell in the saved manifest. Identical retries return the original event even after media changes; they are historical replays. Conflicting retries and stale revisions reject without appending. Request IDs and review revisions are scoped to a session/bundle. A later decision appends instead of deleting earlier reviews. Session steps/revisions are unaffected, and imports retain `not_confirmed` status.

`review-status` reads a consistent database snapshot without creating/migrating tables, preserves all review events and rechecks source files. Latest accepted labels show `needs_review` when the source is currently missing/mismatched/unreadable; no history is erased. Restored matching original bytes resolve to the same recorded assertion. These are historical label assertions only: physical state remains unknown, timebase remains declared/undecoded, and no session step confirmation, graph eligibility or visual evaluation is performed. Keep labels and reasons synthetic until actual reviewed kit/media is available. Confirm/correct/reconciliation integration and real timestamped playback remain outstanding.


## Confirm a manual report with a reviewed reference

```powershell
python -m undobench confirm-reviewed SESSION BUNDLE --evidence synthetic-label --review-revision 1 --part P01 --cell A1 --actor sample-human --request linked-1 --revision 0 --confirm
```

Use IDs from a matching session/import and current revisions from history/review-status. This command requires the latest label decision to be accept_label, the current bundle review revision, matching source bytes and matching part/cell. It also requires deliberate confirmation and the current session revision. Unreviewed/unclear/rejected labels, missing/changed media, stale state and cross-session references cannot create this record. These checks, step append, permanent link and session revision advance occur in one SQLite transaction. Source checks describe bytes at check time; files can change afterward.

The additive step_evidence table links the numbered manual report to its immutable session/bundle/evidence/review references and synthetic flag. Original PTS/timebase/graph versions remain available via evidence-status and review-status for that bundle. History and resume display the historical manual review link. Neither command claims current source support; use review-status to recheck it. All physical locations remain unknown. The report retains manual_report evidence mode because byte identity and a human assertion do not establish decoded or visually verified evidence.

Identical retries return the original linked record even if later reviews or files change; this is historical replay, not new acceptance. A request ID cannot switch between ordinary confirm and confirm-reviewed or change its reference. Plain confirm remains available for clearly manual reports, with no evidence attached. A correction does not inherit or validate the original review link automatically; the old link remains in history. Graph eligibility, dependent correction invalidation, real media playback/probing and kit-specific session configuration remain outstanding. Existing databases/history work without the new table until the first linked confirmation; read-only resume never migrates it. Persistence/reset follows the existing session database policy.
