# Channel Factory

Moving to another Windows PC? Start with
[`NEW_PC_SETUP_AND_AI_HANDOFF.md`](NEW_PC_SETUP_AND_AI_HANDOFF.md). It includes
the complete installation, migration, offline acceptance, dashboard, and AI
handoff runbook.

Channel Factory is a free-to-run Windows workflow engine and authenticated local
operations console for drafting and assembling videos with a human approval gate.
A channel is a profile containing its audience,
schema extensions, prompts, topic queue, safety rules, posting cap, and output
settings. The baseline workflow uses manually supplied, licensed images and
recorded audio or local CPU speech.

The tool is a drafting assistant, not an unattended publishing system. No
approval means no narration, render, or export.

## What is implemented

- Profile-driven history and kids-story channels (`kids_stories_en` starts disabled).
- Standalone JSON validation with profile-specific fields and image-license checks.
- Transactional SQLite workflow state, topic reservations, last-30 history,
  workflow event audit, asset credits, LLM call count, approval timestamps, and weekly exports.
- Pure-Python TF-IDF cosine checks for title, narration, and lesson/character overlap.
- Structure-fingerprint warnings and up to three duplicate-regeneration attempts.
- Official Google Gen AI SDK adapter with configurable model, timeout, transient-error backoff,
  JSON repair, and a per-day call limit.
- Deterministic `MockLLM` used by every normal test and the full sample workflow.
- Terminal approve/edit/reject flow; rejection feedback is passed to redrafting.
- Recorded narration and local CPU-only eSpeak NG narration adapters.
- Manual-image validation and FFmpeg slideshow assembly with zoom/pan, caption
  overlays, narration, optional licensed-music ducking, 16:9 or 9:16 output, a workspace render lock,
  configured thread count, and automatic intermediate cleanup.
- Resumable CLI stages with JSON job status for PowerShell or n8n orchestration.
- Authenticated, responsive localhost operations console with job search,
  audit timelines, provenance, failure visibility, safe recovery, profile
  governance, system health, and verified backups.
- Numbered SQLite migrations, a workflow failure ledger, online backup integrity
  checks, and Windows CI for Python 3.11 and 3.13.
- Idempotent manual upload packages containing checksums, video, copy, tags,
  captions, audience/disclosure reminders, sources, and an all-media credit ledger.
- Optional private YouTube upload module, disabled by default.

## Setup (PowerShell)

Python 3.11 or newer is required. This machine currently has Python 3.13.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`imageio-ffmpeg` provides a small FFmpeg fallback when `ffmpeg.exe` is not on
`PATH`. To use a different binary, set `CHANNEL_FACTORY_FFMPEG` to its full path.
No Docker, GPU, local LLM, or local image generator is needed.

Copy `.env.example` to `.env` only as a reference. The CLI reads environment
variables; it does not load or commit secrets automatically.

## Phase 0 check

```powershell
.\.venv\Scripts\python.exe -m channel_factory phase0-check
```

This checks Python, a usable FFmpeg binary, and installation/import of the
official `google-genai` SDK. It intentionally does **not** call Gemini.

TTS evaluation:

| Candidate | Measured Windows result | Voice quality | Decision |
|---|---|---|---|
| eSpeak NG | 9.4 MB wheel; generated a valid WAV and completed the full TTS demo | Intelligible but robotic | Default |
| Piper | Runtime installed; official voice download was blocked here with HTTP 403 | Expected to be more natural, but not honestly measurable without a model | Optional adapter |
| Windows SAPI | No extra install, but the installed synthesizer failed to initialize a voice | Not measurable on this host | Not selected |

eSpeak NG was selected because it is the only candidate that completed a local
sound-generation check on this machine. Piper remains available through
`PiperNarration` when the optional dependency and a voice model are supplied:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[piper]"
.\.venv\Scripts\python.exe -m piper.download_voices --data-dir channel_factory\data\voices en_US-lessac-medium
```

Voice-model hosting may be blocked on managed corporate networks. Voice quality
varies, so listen to a complete draft before publication approval.

## Gemini configuration

Gemini is optional for setup and testing. Set both variables only when ready:

```powershell
$env:GEMINI_API_KEY = "your-key"
$env:GEMINI_MODEL = "the-model-you-selected"
```

The model name is never hardcoded. The live test is clearly marked `live` and
automatically skips when either variable is missing:

```powershell
.\.venv\Scripts\python.exe -m pytest -m live
```

The default test suite and full demo make no network calls and require no key.

## Validate samples

```powershell
.\.venv\Scripts\python.exe -m channel_factory validate channel_factory\samples\history_script.json --profile history_en
.\.venv\Scripts\python.exe -m channel_factory validate channel_factory\samples\kids_script.json --profile kids_stories_en
```

History scripts require `sources[]`, `claims_to_verify[]`, and a valid
`image_credit` on every scene. Allowed image licenses are `public_domain`,
`cc_by`, `cc_by_sa`, `owned`, and `ai_generated`. TTS or any AI-generated image
forces `disclosure_required=true`.

## Offline end-to-end demo

Explicit approval is required even for the sample:

```powershell
.\.venv\Scripts\python.exe -m channel_factory demo --workspace demo-workspace --approve
```

The demo uses `MockLLM`, owned placeholder images, and a generated silent WAV as
sample recorded audio. It drafts, safety-checks, records approval, prepares
narration, renders a 10-second MP4, and creates the manual upload package. To
exercise the verified local eSpeak NG adapter instead, add `--tts`.

Without `--approve`, the command stops after showing and saving the reviewable
draft. That is intentional.

## Local operations console

Set a private token of at least 24 varied characters, then start the loopback-only
console. The token is not stored in source code or browser storage.

```powershell
$env:CHANNEL_FACTORY_DASHBOARD_TOKEN = "replace-with-a-long-private-token"
.\.venv\Scripts\python.exe -m channel_factory serve --workspace demo-workspace-review --port 8765
```

Open `http://127.0.0.1:8765` and enter the token. The console offers operational
visibility and safe recovery for narrated or rendered stages. It deliberately
has no approve action: review and approval remain an explicit terminal interaction.

The premium Mission Control interface includes an attention-first overview,
workflow stage rail, live activity, channel-capacity and backup posture,
sortable/paginated pipeline data, production-readiness checks, tabbed job
inspection, asset provenance, incident history, system health, auto-refresh,
CSV/JSON export, and a keyboard command palette (`Ctrl+K`). It uses local HTML,
CSS, JavaScript, and SVG assets with no CDN or Node.js runtime.

Security controls include trusted-host enforcement, login throttling, revocable
sessions, CSRF plus origin validation, strict CSP, UUID/path containment,
database-backed cross-process operation leases, backup rate/space/retention
limits, and redacted paths.

Create or verify database backups without the console:

```powershell
.\.venv\Scripts\python.exe -m channel_factory backup --workspace demo-workspace-review create
.\.venv\Scripts\python.exe -m channel_factory backup --workspace demo-workspace-review list
.\.venv\Scripts\python.exe -m channel_factory backup --workspace demo-workspace-review verify .\path\to\backup.sqlite3
```

## Normal workflow

Create an offline draft:

```powershell
.\.venv\Scripts\python.exe -m channel_factory draft --workspace channel_factory\data --profile history_en --llm mock
```

Create a live Gemini draft after configuring the two environment variables:

```powershell
.\.venv\Scripts\python.exe -m channel_factory draft --workspace channel_factory\data --profile history_en --llm gemini
```

Review the displayed draft ID:

```powershell
.\.venv\Scripts\python.exe -m channel_factory review VIDEO_ID --workspace channel_factory\data
```

The review shows the script, flags, claims, sources, closest historical video,
similarity scores, and any repeated-structure warning. `edit` opens the JSON in
`$env:EDITOR` (Notepad by default) and revalidates it. `reject` requires a reason.

Continue the approved job as separate resumable stages:

```powershell
.\.venv\Scripts\python.exe -m channel_factory narrate VIDEO_ID --workspace channel_factory\data --mode recorded --audio .\my-narration.wav
.\.venv\Scripts\python.exe -m channel_factory render VIDEO_ID --workspace channel_factory\data --images .\my-images
.\.venv\Scripts\python.exe -m channel_factory export VIDEO_ID --workspace channel_factory\data
```

Use `--mode tts` for local eSpeak NG. A script's declared narration mode must
match the selected adapter. To redraft a rejected job, use `redraft VIDEO_ID`.

Inspect jobs manually or from another orchestrator:

```powershell
.\.venv\Scripts\python.exe -m channel_factory list --workspace channel_factory\data
.\.venv\Scripts\python.exe -m channel_factory list --workspace channel_factory\data --json
.\.venv\Scripts\python.exe -m channel_factory status VIDEO_ID --workspace channel_factory\data
```

`status` returns the current state, allowed next actions, artifact paths, and the
workflow event audit. Only one render may run anywhere in a workspace at a time.

## Scalability and operating limits

Channel Factory scales vertically on one Windows computer. Multiple CLI, PowerShell,
dashboard, or n8n processes may share one workspace: SQLite WAL transactions,
cross-process leases, topic/export reservations, and a kernel-backed render lock
prevent duplicate state changes. Dashboard totals use indexed database aggregates,
so they are not limited to the newest 500 jobs.

It is not a horizontally scaled cloud service. Do not run the same SQLite file from
two computers, a network share, or two OneDrive-synced copies. Keep the active
workspace on a local non-synced disk and copy verified backups after the application
is stopped. If the workload eventually needs many simultaneous render workers,
multiple users, or hundreds of thousands of searchable jobs, the next architecture
step is PostgreSQL, object storage, and a durable work queue—not more SQLite workers.

Scripts are bounded to 60 scenes, one hour of total duration, and 1 MB of JSON to
protect a CPU-only machine from accidental resource exhaustion. Rendering remains
intentionally serialized per workspace because FFmpeg is the heaviest operation.

Optional music requires `track.ext.license.json` beside the audio file:

```json
{"source":"Local royalty-free library","license":"royalty_free","attribution":"Artist and track name"}
```

Allowed music licenses are `royalty_free`, `public_domain`, `cc_by`, `cc_by_sa`,
and `owned`.

## Data and compliance

Runtime files belong under the selected workspace and are git-ignored. Each
draft or later video stores title, topic, summary, lesson, characters, key facts,
full narration, script JSON, structure fingerprint, state, and timestamps in
SQLite. The next unused profile topic is transactionally reserved before drafting,
and the last 30 titles and summaries are included in the drafting prompt.

The weekly posting cap defaults to three per channel and is reserved atomically
during export. Every image, narration track, and optional music track retains its
license and attribution. Exports are retry-safe and include a SHA-256 manifest;
tampered or incomplete packages are not silently accepted. Supply only content
you own or are licensed to use. The project does not scrape images, copy videos,
or download music. Manual images remain required.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for state transitions, component
boundaries, concurrency behavior, and the recommended roadmap toward a visual
n8n-like product.

## YouTube API module

Manual upload is the baseline. `services/publish_youtube.py` is separate,
optional, and disabled until `CHANNEL_FACTORY_YOUTUBE_UPLOAD_ENABLED=true`.
Install it only when required:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[youtube]"
```

Uploads made through an unaudited Google Cloud API project are forced private by
YouTube. A compliance audit is required before public API uploads. The adapter
therefore submits private uploads and sets `selfDeclaredMadeForKids` plus
`containsSyntheticMedia` from the profile/script where the API supports them.
OAuth client secrets are never stored in this repository.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite needs no network or API keys. It includes a real 10-second FFmpeg
render and complete MockLLM export. External calls use bounded retries/timeouts;
errors are logged and surfaced rather than ignored.

## Project layout

```text
channel_factory/
  cli.py
  config.py
  profiles/{history_en,kids_stories_en}/
  core/{schema,pipeline,state}.py
  services/{llm,tts,assemble,export,publish_youtube}.py
  history/{store,dedupe}.py
  review/approval.py
  samples/
  tests/
docs/MEMORY.md
docs/ARCHITECTURE.md
AGENTS.md
```

