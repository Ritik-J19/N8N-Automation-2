# Channel Factory repository rules

- Target Windows 10/11 with Python 3.11+ and PowerShell instructions.
- Keep the project free to run, CPU-only, low-memory, and usable without Docker.
- Do not add paid services, GPU requirements, or dependencies over 200 MB without approval.
- Read `docs/MEMORY.md` before work and update it after every completed task.
- Keep `docs/MEMORY.md` current; archive old entries after roughly 200 lines.
- Use `pathlib.Path` for filesystem paths and type hints on public functions.
- Keep ports in protocols and external integrations in service adapters.
- No secrets in code. Load secrets from environment variables or a git-ignored `.env`.
- `GEMINI_MODEL` must be configured; never hardcode a Gemini model name.
- Network tests must be opt-in. The default suite runs without API keys or network access.
- All external calls need timeouts, retries where appropriate, and clear errors.
- Approval is mandatory before narration, rendering, or export.
- Enforce per-channel posting caps and retain disclosure and asset-license records.
- Do not scrape, repost, or accept unlicensed images or music.
- Run the CLI with `python -m channel_factory --help`.
- Run tests with `python -m pytest`.
- Run the MockLLM end-to-end check with `python -m channel_factory demo --workspace demo-workspace --approve`.

