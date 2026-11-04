# Channel Factory: New-PC Setup and AI Handoff

This file is the authoritative handoff for moving Channel Factory to another
Windows PC. After transferring the codebase, tell the AI on that PC:

> Read `AGENTS.md`, `docs/MEMORY.md`, and
> `NEW_PC_SETUP_AND_AI_HANDOFF.md` completely. Then follow the new-PC runbook,
> install only the missing required components, run every offline acceptance
> check, start the local dashboard, and report the exact results. Do not run a
> live Gemini test unless both Gemini environment variables are deliberately
> provided.

## 1. Required outcome

The setup is complete only when all of these conditions are satisfied:

- A supported 64-bit Python is available.
- A new local `.venv` has been created; an environment copied from another PC
  is never reused.
- The project and development dependencies install successfully.
- `python -m channel_factory --help` works.
- Phase 0 finds a usable FFmpeg binary and imports the Google Gen AI SDK.
- `python -m pip check` reports no broken requirements.
- The default offline test suite passes.
- The live Gemini test skips automatically when credentials are absent.
- A fresh MockLLM demo completes drafting, approval, narration, rendering, and
  export.
- The export package has a manifest and a non-empty MP4.
- The authenticated dashboard starts on `127.0.0.1` and reports schema v6.
- Any copied existing workspace is on a local non-synced disk and its database
  backup verifies successfully.

Current expected test result for this handoff:

```text
40 passed, 1 skipped
```

The one expected skip is the opt-in live Gemini test when `GEMINI_API_KEY` and
`GEMINI_MODEL` are not both set.

## 2. Non-negotiable project rules

- Target Windows 10/11 and Python 3.11 or newer.
- Prefer 64-bit Python 3.13 for the closest match to the verified environment.
- Keep the application free to run, CPU-only, and usable without Docker.
- Do not add GPU, local-LLM, paid-service, or large optional dependencies merely
  to complete setup.
- Do not put secrets in source files, Markdown, Git, screenshots, or logs.
- `GEMINI_MODEL` must come from the environment; never hardcode a model name.
- Default tests must remain offline and work without API keys.
- Human approval is mandatory before narration, rendering, or export.
- Do not expose the dashboard on `0.0.0.0`; it is a loopback-only local console.
- Do not share the active SQLite workspace through OneDrive, SharePoint, a
  network drive, or simultaneous copies on two PCs.
- Do not use `git reset --hard`, `git clean`, or other destructive cleanup to
  solve an installation problem.
- Read `AGENTS.md` and `docs/MEMORY.md` before changing code. Update
  `docs/MEMORY.md` after a completed repository task.

## 3. What is and is not required

### Required

- Windows 10 or Windows 11, normally x64.
- Python 3.11+; Python 3.13 x64 is recommended.
- PowerShell 5.1 or PowerShell 7.
- A modern browser such as Edge, Chrome, or Firefox.
- Internet access for the first dependency installation, unless a wheelhouse is
  transferred for offline installation.
- At least 2 GB of free space for setup and temporary files, plus room for media.
- At least 4 GB RAM; 8 GB or more is recommended for comfortable rendering.

The current virtual environment is roughly 334 MiB. Video workspaces can become
much larger than the application itself.

### Not required

- Docker
- Node.js or npm
- React build tooling
- PostgreSQL, Redis, RabbitMQ, or another server
- A GPU or CUDA
- A local LLM
- A separately installed system FFmpeg
- A separately installed eSpeak application
- A Gemini key for MockLLM, tests, or the demo
- n8n
- Visual Studio or Visual Studio Build Tools under normal wheel-based installation

`imageio-ffmpeg` supplies a local FFmpeg binary. `espeakng-loader` supplies the
small local eSpeak NG runtime.

## 4. Recommended paths

Keep both the project and especially the active workspace out of synchronized
folders. Short paths also reduce Windows path-length and quoting problems.

```text
C:\ChannelFactory\                 Project source
C:\ChannelFactoryData\workspace\   Production workspace
C:\ChannelFactoryData\acceptance\  Disposable MockLLM acceptance workspace
```

The source may be placed elsewhere, but the active SQLite workspace must not be
operated from two machines or from simultaneous cloud-sync replicas.

Suggested PowerShell variables for the runbook:

```powershell
$ProjectRoot = "C:\ChannelFactory"
$Workspace = "C:\ChannelFactoryData\workspace"
$AcceptanceWorkspace = "C:\ChannelFactoryData\acceptance"
```

If the project was extracted to another directory, set `$ProjectRoot` to the
actual directory that contains `pyproject.toml`.

## 5. Files to transfer

Transfer at least:

```text
channel_factory\
docs\
.github\
pyproject.toml
README.md
NEW_PC_SETUP_AND_AI_HANDOFF.md
AGENTS.md
.env.example
.gitignore
```

Do not transfer or reuse:

```text
.venv\
__pycache__\
.pytest_cache\
*.pyc
.env
```

The old `.venv` contains machine-specific paths and compiled binaries. Reusing
it commonly causes silent interpreter and DLL mismatches.

If the current work has not been committed and pushed, a Git clone will not
contain untracked project files. Copy the actual files. After the repository is
committed and pushed, Git may be used for future transfers.

## 6. Decide whether this is a fresh or migrated installation

### Fresh installation

Copy only the source code, create a new workspace, and begin with new jobs. No
runtime workspace needs to be transferred.

### Migrating existing jobs and media

A database backup contains workflow records, but it does not contain narration,
images, rendered MP4 files, or export packages. To retain everything, transfer
the complete stopped workspace, including:

```text
channel_factory.sqlite3
runs\
outputs\
backups\
logs\
```

Before copying on the old PC:

1. Create a verified backup.
2. Allow any narration, render, or export operation to finish.
3. Stop the dashboard with `Ctrl+C`.
4. Stop all other Channel Factory processes.
5. Copy the entire workspace only after it is no longer changing.

Backup command on the old PC:

```powershell
.\.venv\Scripts\python.exe -m channel_factory backup `
    --workspace "C:\Path\To\ExistingWorkspace" `
    create
```

Expected backup fields:

```text
integrity: ok
schema_version: 6
```

On the new PC, copy that complete workspace to a local path such as
`C:\ChannelFactoryData\workspace`. Never let the old and new PCs operate the
same synchronized SQLite file. Separate copies may run independently, but their
databases will diverge and cannot be automatically merged.

## 7. AI execution runbook

The AI on the new PC should perform these steps in order and stop to report a
specific blocker only when it cannot safely continue.

### Step 1: Inspect before changing anything

```powershell
Set-Location $ProjectRoot
Get-Location
Get-ChildItem -Force
Get-Content -LiteralPath .\AGENTS.md
Get-Content -LiteralPath .\docs\MEMORY.md
Get-Content -LiteralPath .\pyproject.toml
```

Confirm that `pyproject.toml`, `channel_factory`, and this handoff file exist.
Preserve all user files and uncommitted work.

### Step 2: Inspect the operating system and Python

```powershell
[Environment]::OSVersion.VersionString
$PSVersionTable.PSVersion
Get-CimInstance Win32_OperatingSystem | Select-Object Caption,OSArchitecture,Version
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors
Get-PSDrive -Name C | Select-Object Used,Free
```

Check Python without assuming a launcher is present:

```powershell
py -0p
py -3.13 --version
python --version
```

Use the first working 64-bit Python 3.11, 3.12, or 3.13 installation. Prefer
Python 3.13. Python 3.14+ may work, but it should not be chosen for the first
setup unless the supported versions are unavailable because the project is
currently validated on Python 3.11 and 3.13.

### Step 3: Install Python only if it is missing

Recommended installer:

- Python 3.13.17, Windows installer (64-bit):
  <https://www.python.org/downloads/release/python-31317/>

During installation, keep pip and the Python launcher enabled and select the
option to add Python to PATH. A normal per-user install usually does not need
administrator rights.

The AI should use an official Python download or an organization-approved
software portal. It must not download Python from an unofficial mirror.

After installation, open a new PowerShell process and verify:

```powershell
py -3.13 --version
py -3.13 -m pip --version
```

### Step 4: Create a new virtual environment

```powershell
Set-Location $ProjectRoot
py -3.13 -m venv .venv
```

If `py` is unavailable but a valid `python` command exists:

```powershell
python -m venv .venv
```

Do not activate the environment. Use its interpreter directly, which avoids
PowerShell execution-policy issues:

```powershell
.\.venv\Scripts\python.exe --version
```

### Step 5: Install the required dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

This installs the application plus the default test tools. Do not install the
optional Piper or YouTube groups unless the user explicitly requests them.

Verify package consistency:

```powershell
.\.venv\Scripts\python.exe -m pip check
```

Expected:

```text
No broken requirements found.
```

### Step 6: Verify the command surface

```powershell
.\.venv\Scripts\python.exe -m channel_factory --help
```

Expected commands include:

```text
phase0-check
validate
demo
draft
review
redraft
list
status
narrate
render
export
serve
backup
```

### Step 7: Run Phase 0

```powershell
.\.venv\Scripts\python.exe -m channel_factory phase0-check
```

Confirm:

- Python is supported.
- `ffmpeg_status` is `ok`.
- `usable_ffmpeg` points to either a system or imageio-ffmpeg executable.
- `google_genai_import` is `ok`.
- The Gemini live test is reported as skipped.

`system_ffmpeg` may be empty. That is acceptable when `usable_ffmpeg` points to
the bundled binary.

### Step 8: Run the offline test suite

Before testing, make sure no Gemini credentials are required. The default suite
must remain offline:

```powershell
.\.venv\Scripts\python.exe -m pytest -ra
```

Expected baseline:

```text
40 passed, 1 skipped
```

The suite performs an actual 10-second CPU render. A slower PC may take longer.
Do not treat the live-Gemini skip as a failure.

### Step 9: Run a fresh MockLLM end-to-end acceptance test

Use a dedicated workspace so existing production topics and jobs are untouched:

```powershell
New-Item -ItemType Directory -Path "C:\ChannelFactoryData" -Force

.\.venv\Scripts\python.exe -m channel_factory demo `
    --workspace $AcceptanceWorkspace `
    --approve
```

Confirm that the command:

1. Creates and safety-checks a draft.
2. Records explicit approval.
3. Creates narration.
4. Renders a non-empty MP4.
5. Produces an export package under `outputs`.
6. Produces `manifest.json`, `metadata.json`, `credits.csv`, and `video.mp4`.

Inspect the acceptance artifacts:

```powershell
Get-ChildItem -LiteralPath $AcceptanceWorkspace -Recurse -File |
    Select-Object FullName,Length
```

There must be no zero-byte final MP4.

### Step 10: Validate a migrated workspace, when supplied

If an existing workspace was copied, do not run the demo inside it. Verify it
separately:

```powershell
.\.venv\Scripts\python.exe -m channel_factory backup `
    --workspace $Workspace `
    list

.\.venv\Scripts\python.exe -m channel_factory list `
    --workspace $Workspace `
    --json
```

Verify a particular copied backup if one exists:

```powershell
.\.venv\Scripts\python.exe -m channel_factory backup `
    --workspace $Workspace `
    verify "C:\ChannelFactoryData\workspace\backups\BACKUP_FILE.sqlite3"
```

Inspect a job using the ID returned by `list`:

```powershell
.\.venv\Scripts\python.exe -m channel_factory status VIDEO_ID `
    --workspace $Workspace
```

Schema upgrades are applied automatically when `Pipeline` opens an older
supported database. Do not replace or manually edit the database during startup.

### Step 11: Generate a dashboard token

Generate a fresh token on the new PC instead of transferring an old token:

```powershell
$DashboardToken = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:CHANNEL_FACTORY_DASHBOARD_TOKEN = $DashboardToken
$DashboardToken
```

The user should save this value in a password manager. Do not write it into the
repository or this handoff file. The token must contain at least 24 characters
and at least 8 distinct characters.

### Step 12: Start the dashboard

For a fresh setup:

```powershell
New-Item -ItemType Directory -Path $Workspace -Force

.\.venv\Scripts\python.exe -m channel_factory serve `
    --workspace $Workspace `
    --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

Use the generated dashboard token. Keep the server in the foreground for the
first validation and stop it with `Ctrl+C` afterward.

Confirm:

- `/api/health` responds successfully.
- Login succeeds.
- The dashboard reports schema v6.
- No unexpected browser console error appears.
- The workspace warning is absent for a local non-synced path.
- Approval is not offered in the dashboard; it remains terminal-only.

If port 8765 is occupied, inspect it before choosing another port:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

Use 8766 only when needed. Do not terminate an unknown process merely to free a
preferred port.

## 8. Gemini configuration remains optional

MockLLM, Phase 0, the default tests, and the acceptance demo require no Gemini
key. If Gemini is deliberately enabled later, both variables are required:

```powershell
$env:GEMINI_API_KEY = "user-provided-secret"
$env:GEMINI_MODEL = "user-selected-model"
```

Only then run the opt-in live test:

```powershell
.\.venv\Scripts\python.exe -m pytest -m live -ra
```

Never invent a model name, never hardcode it, and never expose the key in a
report or commit.

## 9. Optional features: do not install by default

### Piper TTS

Install only when the user supplies or approves a local Piper voice model:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[piper]"
```

The verified default remains eSpeak NG. Piper is optional and larger.

### YouTube API upload

Manual upload packages are the baseline. Install YouTube dependencies only when
OAuth and publishing governance are ready:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[youtube]"
```

Keep `CHANNEL_FACTORY_YOUTUBE_UPLOAD_ENABLED=false` unless the user explicitly
enables and configures the private upload path.

### Git

Git is optional for execution. If the repository has been committed and pushed,
use the official Git for Windows installer:

<https://git-scm.com/install/windows>

## 10. Offline or restricted-network installation

If the target PC cannot access PyPI, prepare a wheelhouse on an internet-connected
Windows x64 PC using the same Python minor version.

From the project root on the connected PC:

```powershell
py -3.13 -m venv wheel-builder
.\wheel-builder\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\wheel-builder\Scripts\python.exe -m pip wheel --wheel-dir wheelhouse ".[dev]"
```

Transfer the project and `wheelhouse` directory. On the offline PC:

```powershell
Set-Location $ProjectRoot
py -3.13 -m venv .venv

.\.venv\Scripts\python.exe -m pip install `
    --no-index `
    --find-links ".\wheelhouse" `
    "channel-factory[dev]==0.1.0"
```

Then perform the same `pip check`, Phase 0, test, and MockLLM acceptance steps.

Do not disable TLS verification or add insecure `trusted-host` values to work
around a corporate proxy. Use the organization-approved proxy, internal package
mirror, or wheelhouse method.

## 11. Troubleshooting decision table

### `py` is not recognized

- Open a new PowerShell window after Python installation.
- Try `python --version`.
- Confirm that the Python launcher was selected during installation.
- Use `python -m venv .venv` when `python` is the valid command.

### `pip` is unavailable

```powershell
py -3.13 -m ensurepip --upgrade
py -3.13 -m pip install --upgrade pip
```

### `Activate.ps1` is blocked

Do not change execution policy. Activation is unnecessary. Run
`.\.venv\Scripts\python.exe` directly.

### Package installation shows certificate or proxy errors

- Use the corporate-approved proxy or internal PyPI mirror.
- Ask IT to allow official Python/PyPI package access.
- Use the wheelhouse procedure.
- Do not bypass certificate validation.

### Phase 0 cannot find FFmpeg

Reinstall the bundled provider:

```powershell
.\.venv\Scripts\python.exe -m pip install --force-reinstall "imageio-ffmpeg>=0.6,<0.7"
.\.venv\Scripts\python.exe -m channel_factory phase0-check
```

On managed PCs, endpoint protection may block the FFmpeg executable inside the
virtual environment. Ask IT to review or allowlist the official installed wheel;
do not disable security software.

### eSpeak reports a DLL error

Confirm that Windows and Python have matching x64 architecture, then reinstall:

```powershell
.\.venv\Scripts\python.exe -m pip install --force-reinstall "espeakng-loader>=0.2.4,<0.3"
```

If a Microsoft runtime DLL is genuinely missing, use the official latest x64
Visual C++ Redistributable page:

<https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist/>

### Tests pass except one Gemini skip

That is the expected offline result. Do not create credentials merely to remove
the skip.

### The dashboard rejects the token

Generate a new varied token. Short or repetitive tokens are intentionally
rejected.

### The dashboard session disappears after a restart

That is expected. Sessions are revocable and process-local. Log in again with
the dashboard token.

### Port 8765 is already occupied

Inspect the listener and use another loopback port. Do not expose the application
to the LAN as a shortcut.

### A copied workspace shows OneDrive/sync warnings

Stop the application and move the complete workspace to a local non-synced path.
Do not move only the SQLite file while leaving its `runs` and `outputs` behind.

## 12. Required final report from the new-PC AI

The AI should finish with a concise report containing:

```text
Operating system:
Python version and architecture:
Project path:
Workspace path:
Workspace is local/non-synced: yes/no
Dependency installation result:
pip check result:
Phase 0 Python result:
Phase 0 FFmpeg result:
Google Gen AI SDK import result:
Default pytest result:
Live Gemini test status:
MockLLM acceptance job ID:
Rendered MP4 path and size:
Export package path:
Export manifest present: yes/no
Database schema version:
Copied backup integrity result, if applicable:
Dashboard URL:
Dashboard login verified: yes/no
Any warnings, missing items, or deviations:
```

The AI must distinguish between:

- A required failure that blocks local MockLLM operation.
- An optional component that is intentionally absent.
- The expected skipped live Gemini test.
- A future feature that is not required for this local deployment.

## 13. Human completion checklist

- [ ] Project files are present and `pyproject.toml` is at the root.
- [ ] The transferred `.venv` was not reused.
- [ ] Python 3.11-3.13 x64 is available; 3.13 is preferred.
- [ ] A new `.venv` was created.
- [ ] `pip install -e ".[dev]"` completed.
- [ ] `pip check` reports no broken requirements.
- [ ] CLI help works.
- [ ] Phase 0 finds FFmpeg and imports `google-genai`.
- [ ] Default tests pass with only the expected Gemini skip.
- [ ] The MockLLM demo renders and exports successfully.
- [ ] The active workspace is outside OneDrive and network shares.
- [ ] An existing full workspace was copied only after shutdown.
- [ ] A copied database backup verifies as `ok` at schema v6.
- [ ] A fresh dashboard token was generated and stored privately.
- [ ] The dashboard works only on `127.0.0.1`.
- [ ] Old and new PCs are not sharing one SQLite database.

## 14. Official installation references

- Python 3.13.17 Windows release:
  <https://www.python.org/downloads/release/python-31317/>
- Python on Windows:
  <https://docs.python.org/3.13/using/windows.html>
- pip installation and upgrade guidance:
  <https://pip.pypa.io/en/stable/installation/>
- Git for Windows, optional:
  <https://git-scm.com/install/windows>
- Microsoft Visual C++ Redistributable, troubleshooting only:
  <https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist/>

