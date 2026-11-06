# Codex Project Instructions

You are assisting with a personal Apple Music discovery application running on a Synology NAS.

- Read `PROJECT_CONTEXT.md` before beginning substantial work.
- Inspect existing files before proposing changes.
- Favor simple, maintainable Python implementations suitable for a low-memory NAS.
- Keep secrets out of source control, logs, generated artifacts, and assistant responses.
- Do not read or print credential contents unless strictly necessary and explicitly authorized.
- Never commit or push credentials.
- Use SQLite for initial persistent storage unless there is a compelling reason otherwise.
- Preserve data across container recreation.
- Prefer Docker Compose and reproducible builds over manual package installation.
- Provide clear, incremental implementation steps.
- Run appropriate tests before recommending deployment.
- Ask before destructive operations, external deployments, or changes to production infrastructure.
- Distinguish between verified behavior, assumptions, and proposed architecture.
- Do not assume Apple Music exposes complete listening history or reliable timestamps.
- Do not automatically push changes to GitHub or deploy to the NAS.
