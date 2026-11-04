# Channel Factory memory

## Current state

- Current phase: Post-Phase-8 operations-console and production-readiness hardening complete.
- Status: the project is a resumable local workflow engine with a secured operations console and auditable stages suitable for PowerShell or n8n orchestration.
- Verification: 40 offline tests, Phase 0 check, and a fresh MockLLM end-to-end acceptance run pass; one expected live-Gemini test skips without credentials.
- Fresh acceptance run: job `062e3e69-335a-481b-b808-59701b721f51` rendered, exported, decoded, and exposed through JSON status in `demo-workspace-review`.
- Latest acceptance run: job `37423435-421a-4238-b9d2-49358797cc44` completed end to end with eSpeak NG TTS in `demo-workspace-run`.
- Live Gemini remains intentionally untested until `GEMINI_API_KEY` and `GEMINI_MODEL` are provided.

## Phase status

- Phase 0: Python 3.13 confirmed; system FFmpeg absent; bundled FFmpeg works; `google-genai` installs/imports; live Gemini skipped.
- Phase 1: scaffold, profiles, prompts, config loader, schema validator, samples, and tests complete.
- Phase 2: SQLite state/history, topic usage, LLM counters, asset ledger, transitions, and dedupe complete.
- Phase 3: LLM port, Gemini adapter, MockLLM, JSON repair, safety pass, retries, and call cap complete.
- Phase 4: terminal approval/edit/reject and MockLLM dry run complete.
- Phase 5: recorded adapter, eSpeak NG default adapter, and optional Piper/SAPI adapters complete.
- Phase 6: FFmpeg zoom/pan slideshow, captions, audio, optional music ducking, lock, and 10-second render complete.
- Phase 7: manual export package complete; optional private YouTube OAuth module is disabled by default.
- Phase 8: end-to-end MockLLM tests, posting-cap enforcement, cleanup, and README complete.

## Decisions and evidence

- Python 3.13 satisfies the 3.11+ target.
- Use official `google-genai`; require env-provided key and model, and never hardcode a model.
- `imageio-ffmpeg` supplies the fallback binary because no system FFmpeg is on `PATH`.
- Use pure-Python TF-IDF cosine to avoid NumPy/scikit-learn for duplicate detection.
- Manual images are mandatory; image-provider automation is disabled.
- All narration, render, and export paths enforce prior approval through persisted states.
- eSpeak NG is the default local TTS: its 9.4 MB Windows wheel generated a valid WAV and completed the TTS demo.
- Windows SAPI failed to initialize a voice on this host, so it remains an unselected adapter.
- Piper runtime installed successfully, but the official voice host returned HTTP 403; it remains optional for user-supplied models.
- The baseline YouTube path is a manual package. API upload is isolated, private, OAuth-based, and disabled.
- SQLite transitions, topic selection, LLM counters, and export-cap reservations are transactional for multi-process safety.
- Export is two-phase and idempotent, with recovery plus SHA-256 manifest validation.
- One render lock is shared across the workspace; FFmpeg timeout removes partial output.
- Narration and optional music join images in the persistent asset/provenance ledger.
- Music is accepted only with a local `.license.json` sidecar using an approved license.
- Machine-readable `list` and `status` commands expose workflow state, next actions, events, and artifacts.
- The FastAPI console binds only to loopback, requires an environment token, uses signed HttpOnly sessions and CSRF, and exposes no approval endpoint.
- SQLite uses numbered migrations through schema v6; recoverable failures, system operations, and cross-process leases are audited.
- Online backups are verified with SQLite quick-check and schema compatibility checks.
- Windows CI covers Python 3.11 and 3.13 with offline tests and a MockLLM end-to-end run.

## Files changed

- Root: `AGENTS.md`, `README.md`, `.env.example`, `.gitignore`, `pyproject.toml`.
- Profiles: `channel_factory/profiles/history_en/` and `kids_stories_en/`.
- Core: `config.py`, `core/schema.py`, `core/state.py`, `core/pipeline.py`.
- Services: `llm.py`, `tts.py`, `assemble.py`, `export.py`, `publish_youtube.py`.
- History/review: `history/store.py`, `history/dedupe.py`, `review/approval.py`.
- Samples/tests: `channel_factory/samples/`, `channel_factory/tests/`.
- Architecture: `docs/ARCHITECTURE.md`.
- Operations: `history/migrations.py`, `web/`, and `.github/workflows/windows-ci.yml`.

## Commands

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m channel_factory phase0-check
.\.venv\Scripts\python.exe -m pytest -ra
.\.venv\Scripts\python.exe -m channel_factory demo --workspace demo-workspace --approve
.\.venv\Scripts\python.exe -m channel_factory demo --workspace demo-workspace-espeak --approve --tts
.\.venv\Scripts\python.exe -m channel_factory list --workspace demo-workspace-review --json
.\.venv\Scripts\python.exe -m channel_factory status 062e3e69-335a-481b-b808-59701b721f51 --workspace demo-workspace-review
$env:CHANNEL_FACTORY_DASHBOARD_TOKEN = "replace-with-a-long-private-token"
.\.venv\Scripts\python.exe -m channel_factory serve --workspace demo-workspace-review --port 8765
.\.venv\Scripts\python.exe -m channel_factory backup --workspace demo-workspace-review create
```

## Known issues

- The live Gemini test has not run because no Gemini credentials/model are configured; it skips automatically.
- Piper voice download is blocked by the current network with HTTP 403; eSpeak NG is the verified fallback.
- eSpeak NG is intelligible but robotic; use recorded audio or a reviewed Piper voice for higher production quality.
- The kids profile is present and validated but intentionally disabled until the history channel is accepted.
- The console is an operational dashboard, not a generic n8n-style drag-and-drop canvas.
- A local scheduler, authenticated webhooks, backup restore, and packaged Windows release are not implemented yet.
- CI is configured in source but cannot be observed until the repository is committed and pushed to GitHub.

## Next steps

- Review the hardened sample package under `demo-workspace-review/outputs/`.
- Add real profile prompts, licensed images, sources, recorded narration, and royalty-free music.
- When ready for Gemini, set both environment variables and run `pytest -m live` before live drafting.
- Use the operations console for health, inspection, backups, and safe post-approval recovery.
- Add a scheduler or n8n webhooks only after their missed-run and idempotency contracts are specified.

## Senior architecture review (2026-11-03)

- Fixed per-connection foreign-key enforcement and atomic status updates.
- Added workflow event history and same-state script-update audit records.
- Added topic reservations with failure release and stale-reservation reclamation.
- Added export reservation, cap serialization, failure rollback, stale recovery, idempotency, and checksum verification.
- Added global render serialization, profile format enforcement, and timeout cleanup.
- Fixed multi-WAV concatenation for clips with different durations.
- Tightened schema validation for empty credits, sources, flags, tags, narration, and image paths.
- Added real workflow commands: `redraft`, `list`, `status`, `narrate`, `render`, and `export`.
- Added narration/music provenance and mandatory music license sidecars.
- Added rotating file logs and broader Gemini transient-error retry handling.

## Operations console review (2026-11-03)

- Added a responsive, high-density operations dashboard with overview metrics, searchable jobs, audit timeline, assets, script inspection, profiles, backups, and system health.
- Added loopback-only FastAPI serving, environment-token authentication, signed sessions, CSRF enforcement, and browser security headers.
- Kept human approval terminal-only; the web API has no approval route.
- Added schema migrations, workflow failure tracking and resolution, verified backups, safe recovery, and Windows CI.
- Browser-verified authenticated desktop and mobile layouts with installed Edge.
- Confirmed the existing review workspace upgrades through schema v5 and retains its exported job.

## Premium Mission Control review (2026-11-03)

- Replaced the conservative dashboard with an Editorial Mission Control design informed by Linear, Grafana, Primer, and Vercel operational patterns.
- Added attention-first health, compact metrics, a six-stage production rail, live activity, capacity and backup posture, sortable/paginated pipeline data, CSV/JSON export, and auto-refresh.
- Added a keyboard command palette, local SVG icon system, tabbed inspector, readiness checklist, structured scene view, provenance view, and incident register.
- Added schema v5 system events and audited successful backup/recovery plus login throttling events.
- Hardened trusted hosts, login throttling, token strength/size, revocable sessions, origin checks, CSP, UUID/path containment, path redaction, recovery leases, and backup resource limits.
- Edge QA passed authenticated desktop/mobile, command palette, inspector tabs, strict CSP, zero browser warnings/errors, and no mobile horizontal overflow.
- Created and verified a schema-v5 backup in `demo-workspace-review/backups/`.

## Scalability and failure-mode review (2026-11-03)

- Added schema v6 indexes and expiring SQLite operation leases for redraft,
  narration, render, recovery, and backup work across independent processes.
- Fixed a concurrent fresh-start WAL/migration race with bounded retry and an
  in-lock migration recheck; an eight-worker startup regression test covers it.
- Replaced the stale render sentinel with a crash-released kernel file lock.
- Made scripts, narration, rendered video, and backups atomic; failed backups
  clean up temporary snapshots and transient leases are removed from backups.
- Dashboard totals now use indexed full-database aggregates rather than the
  latest 500 rows; backup integrity results are cached by immutable file metadata.
- Human approval now freezes script content and identity; asset provenance is
  immutable after rendering, and reserved scene IDs cannot overwrite media credits.
- Added script resource ceilings, finite-duration validation, contained image
  resolution, full-UUID export collision fallback, and per-attempt Gemini quotas.
- Added an operations warning for OneDrive/synced workspaces. The supported
  scaling boundary is one local Windows host with multiple automation processes,
  not shared SQLite across machines.
- Fresh MockLLM acceptance workspace: `demo-workspace-scalability-acceptance`,
  job `3c983b5d-4ad9-4fb1-9857-6e274c98e78b`; export manifest verified.
- Existing `demo-workspace-review` upgraded to schema v6 and a verified schema-v6
  backup was created at `channel-factory-20261103-115141-282750.sqlite3`.

## Support log

- 2026-10-19: Provided a standalone C++ reverse-DP solution for the "Tax Collector's Survival" minimum-initial-HP grid problem; no Channel Factory source files were changed.
- 2026-11-03: Documented transfer to another Windows PC: use 64-bit Python
  3.13, recreate rather than copy `.venv`, install `.[dev]`, verify Phase 0,
  offline tests, and the MockLLM demo, and keep the active workspace outside
  OneDrive. Stop all processes before copying an existing full workspace because
  database-only backups do not contain runs or exported media.
- 2026-11-03: Added root-level `NEW_PC_SETUP_AND_AI_HANDOFF.md` as the
  executable human/AI migration runbook, including prerequisites, fresh and
  migrated workspace paths, online/offline installs, acceptance gates,
  dashboard validation, troubleshooting, and the required final report.
- 2026-11-04: Host environment updated: uninstalled obsolete Python 3.10.5,
  installed 64-bit Python 3.13.17, recreated `.venv`, installed `.[dev]`
  dependencies, and passed all verification gates: `pip check` clean, Phase 0
  check OK, 40 passed / 1 skipped offline tests, and MockLLM end-to-end demo.
  Repository committed and pushed to GitHub remote `Ritik-J19/N8N-Automation-2.git`.

