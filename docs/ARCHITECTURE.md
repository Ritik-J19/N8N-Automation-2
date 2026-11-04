# Channel Factory architecture

## Product boundary

Channel Factory is a local workflow engine, CLI, and authenticated operations
console for a human-reviewed video pipeline. Its commands are node-like
orchestration steps, but it is not a generic drag-and-drop workflow editor.
PowerShell, Task Scheduler, or n8n can call the commands and read JSON status
without bypassing the approval gate.

## State model

```text
drafted -> safety_checked -> awaiting_review -> approved -> narrated
                                                        -> rendered
                                                        -> exporting -> exported
drafted/safety_checked/awaiting_review -> rejected -> drafted
exporting -> rendered  (failed package, safe retry)
```

Only legal transitions are accepted. SQLite write locks plus status-checked
updates prevent concurrent processes from advancing the same job inconsistently.
Every state change and script update is recorded in `workflow_events`.

## Components

- `core/schema.py`: profile-aware script and disclosure invariants.
- `core/pipeline.py`: use-case orchestration and approval/state gates.
- `history/store.py`: transactional persistence, topic/export reservations,
  audit events, recoverable failures, backups, history, rate counters, and asset provenance.
- `history/migrations.py`: ordered, transactional SQLite schema migrations.
- `services/llm.py`: LLM port, offline mock, Gemini adapter, retry policy, and
  duplicate regeneration.
- `services/tts.py`: recorded, eSpeak NG, optional Piper, and SAPI adapters.
- `services/assemble.py`: manual-asset checks, music licensing, global render
  lock, FFmpeg invocation, captions, and temporary-file cleanup.
- `services/export.py`: two-phase posting-cap reservation, package generation,
  provenance ledger, checksum manifest, recovery, and idempotency.
- `review/approval.py`: terminal review/edit/reject boundary.
- `cli.py`: human commands and machine-readable orchestration surface.
- `web/`: loopback-only FastAPI control plane and self-contained Mission Control UI.

## Concurrency and recovery

- Topic reservations prevent two workers from drafting the same queue item.
- Daily LLM counters and workflow transitions use immediate SQLite transactions.
- Database-backed expiring leases serialize redraft, narration, render, recovery,
  and backup operations across CLI, dashboard, PowerShell, and n8n processes.
- A kernel-backed workspace lock enforces one FFmpeg render at a time and releases
  automatically if a process is killed.
- Export uses `rendered -> exporting -> exported`; failures return to `rendered`.
- Export retries reuse a valid matching package and reject checksum mismatches.
- Abandoned export reservations may be recovered after 15 minutes; abandoned
  topic reservations are reclaimed after one hour.
- Narration, render, redraft, and export exceptions are retained in a failure
  ledger and resolved automatically after a successful retry.
- SQLite online backups are integrity-checked and schema-version-checked.
- Backup creation is atomic, excludes transient leases from the snapshot, and
  removes partial snapshots after failure.
- Per-job recovery leases reject duplicate render/export recovery requests.
- Backup creation is serialized, rate-limited, disk-space checked, and bounded
  by a non-destructive retention cap.
- Scripts and media outputs are atomically materialized so the dashboard never
  treats a half-written file as a completed artifact.
- Human approval freezes the script; rendered provenance cannot be rewritten.

## Security and compliance

- Secrets come only from environment variables and `.env` is git-ignored.
- Gemini model selection is environment-provided and live tests are opt-in.
- Approval is persisted before narration, rendering, or export.
- Image, narration, and music provenance is written to the asset ledger.
- Music requires a local license sidecar; no scraper or downloader is active.
- YouTube API upload is private-only, optional, and disabled by default.
- The console uses a signed HttpOnly SameSite cookie, revocable session IDs,
  CSRF and origin checks, restrictive browser security headers, login throttling,
  trusted-host enforcement, and a strong secret loaded from the environment.
- The API intentionally exposes no approval endpoint. Recovery can only advance
  jobs that have already passed the persisted approval gate.
- Route UUID validation, contained path construction, and workspace-relative
  responses limit filesystem traversal and host-path disclosure.

## Operating envelope

The current design is a single-host, multi-process application. SQLite WAL,
indexed aggregate queries, and expiring database leases are appropriate for a
local channel factory with many persisted jobs and a small number of concurrent
automation callers. FFmpeg remains serialized because CPU and memory are the
limiting resources.

The SQLite database must not be actively shared between computers or operated
from simultaneous cloud-sync replicas. A synced workspace is surfaced as an
operations warning. Use a local non-synced workspace and copy verified backups
only after shutdown. Horizontal rendering, multi-user RBAC, or very large search
volumes require a future PostgreSQL/object-storage/durable-queue deployment.

## Mission Control information architecture

- Overview prioritizes attention, production stages, activity, channel capacity,
  verified backups, and the human-approval boundary.
- Pipeline provides server-side search, sorting, pagination, persistent filters,
  safe data export, and keyboard-accessible inspection.
- Job inspection separates readiness, timeline, script, asset provenance, and
  incidents into focused tabs.
- Channels exposes weekly capacity and compliance configuration; Reliability
  combines backups and incidents; System exposes dependencies and safeguards.
- The `Ctrl+K` command palette offers navigation and safe actions only. Approval
  is never available through the palette or web API.

## Remaining roadmap

1. Add an explicitly confirmed, offline restore command with an automatic pre-restore backup.
2. Add static analysis and packaged Windows release smoke tests to CI.
3. Add a local scheduler only after missed-run, sleep/wake, and timezone behavior is specified.
4. Add authenticated webhooks and an n8n workflow template only after the local
   API has stable idempotency keys and request schemas.
5. Add retry/dead-letter operations for failed external publishing, while
   preserving posting caps and manual approval.

