# Music Discovery — Project Context

## Project goal

Build a personal, always-on music discovery service that integrates with Apple Music, collects listening history, identifies musical preferences, generates personalized recommendations using AI, and eventually creates Apple Music playlists automatically.

The system should improve recommendations over time using listening behavior and explicit feedback.

## User preferences

- Apple Music is the primary music service.
- The service should run continuously with minimal manual intervention.
- Prefer straightforward, maintainable solutions over unnecessary complexity.
- Keep operating costs low.
- Use VS Code and GitHub for development.
- Avoid using Synology's browser-based terminal for routine work.
- Explain architectural tradeoffs and implement changes incrementally.

## Existing infrastructure

**NAS:** Synology DS220+, DSM 7.4.1, 2 GB RAM.

**Container platform:** Synology Container Manager / Docker Compose.

**Project:** `music-discovery-test`

**NAS project directory:** `/volume1/docker/Music-discovery`

Existing Compose configuration:

```yaml
services:
  music-test:
    image: python:3.12-alpine
    container_name: music-discovery-test
    working_dir: /app
    command: ["sh", "-c", "pip install --no-cache-dir PyJWT cryptography requests && sleep infinity"]
    volumes:
      - ./Secrets:/run/secrets:ro
      - ./Config:/app/config
      - ./Data:/app/data
    mem_limit: 128m
    restart: unless-stopped
```

The container was successfully recreated and started on November 5, 2026.

Verified:
- `/app/data` is mounted and accessible.
- Python packages `PyJWT`, `cryptography`, and `requests` import successfully.
- NAS folder capitalization matters.
- SSH access and Docker inspection were verified by the user. Docker commands require `sudo` for the current SSH account.
- Bind mounts were verified: `Secrets` is read-only; `Config` and `Data` are persistent, writable mounts.
- The container defaults to root; authorization commands run explicitly as UID/GID `1027:100`, matching the private key owner.
- The private key was restricted to mode `600` and remains readable by that runtime user. `Config/private` was created with owner `1027:100` and mode `700`.
- Installed authorization dependencies match the local pins: PyJWT `2.15.1` and cryptography `50.0.2`.

**Important:** This container is currently a test environment, not a functioning listening-history collector.

## Apple Music integration

An Apple Developer Media Services key has been created and its `.p8` private key is stored on the NAS, mounted read-only under `/run/secrets`.

The key belongs to an employer-managed Apple Developer team. The user confirmed on November 5, 2026 that the employer permits the current personal use. A future migration to a personal team is intended; do not block the authorization proof on that migration. Do not copy or expose the private key. Long-term authorization reliability remains to be validated, and migration should be expected to require a new key and user authorization.

A Python script has successfully generated a developer token.

A GitHub Pages site has successfully:
- Authorized the user's personal Apple Music account using MusicKit.
- Retrieved the user's recent plays through the Apple Music API.

Existing GitHub repository:

`https://github.com/andyhugelier/music-discovery-auth`

Existing GitHub Pages site:

`https://andyhugelier.github.io/music-discovery-auth/`

The repository contains an `index.html` authorization/test interface.

### Authorization milestone progress (November 5, 2026)

Verified from the user's terminal output/screenshots:
- Updated local browser authorization succeeded. Setup and sign-in now use separate clicks; the page checks bootstrap-token expiration and shows safe error categories.
- `music_auth.py` was staged at `/app/config/music_auth.py` through the existing bind mount.
- The browser-issued Music User Token was imported through a hidden SSH terminal prompt. The CLI verified Apple access and saved it in `/app/config/private/music-user-token` (owner-only file permissions).
- Import returned 10 recent tracks. A separate `check` command reloaded the saved token, generated a fresh developer JWT, and returned 10 recent tracks without using the browser API.
- The user restarted `music-discovery-test` and repeated `check` successfully: 10 recent tracks returned using the saved token. Authorization persistence across a container restart is verified.
- The Apple `/v1/test` endpoint has a successful empty body; the CLI now handles it without attempting JSON decoding. Regression tests cover this behavior.

Local validation: 15 Python tests and the browser-flow test pass using synthetic credentials and mocked services.

Still unverified at the authorization milestone: recovery after container recreation, NAS reboot, extended unattended operation, and reauthorization after token invalidation. Subsequent collector progress below records persistent runtime configuration and cross-container database verification. The user authorized committing and pushing the authorization milestone on November 5, 2026.

## Current limitations

- NAS authorization and saved collection history survive container recreation, with subsequent scheduled polling verified. Long-term unattended operation and NAS reboot recovery remain to be tested.
- The initial manual handoff and owner-restricted persistence are implemented; automatic reauthorization is not implemented.
- The existing test container installs dependencies at startup; the deployed collector image installs pinned dependencies at build time.
- A live SQLite database now exists on the NAS and has been verified across separate one-off containers.
- Automatic polling is deployed on the NAS, with scheduled successes and healthy status verified. Playlist generation is not implemented.
- Apple Music's recent-plays API may provide incomplete historical coverage and may not provide reliable play timestamps, skip events, or full listening durations. Do not invent these fields.

## Recommended architecture

1. Browser-based Apple Music authorization.
2. Secure transfer of the Music User Token to the NAS.
3. Python collector running on the NAS.
4. SQLite database stored in `/app/data`.
5. Periodic polling of recent plays with deduplication.
6. Recommendation engine combining collected listening data, Apple Music catalog information, and AI.
7. Apple Music playlist creation.
8. Feedback and recommendation-quality improvements.

The architecture is provisional and should be validated before implementation.

## Security requirements

- Never commit private keys, developer tokens, Music User Tokens, or other credentials to GitHub.
- Never display secrets in logs or assistant responses.
- Keep credentials outside public web assets.
- Use appropriate file permissions and avoid exposing the NAS unnecessarily.
- Use GitHub Pages only for browser-side functionality that can safely be public.
- Do not put a private key in browser JavaScript.

## Collector implementation progress (November 5, 2026)

The user requested work on the always-on collector. Local implementation now includes:
- `music_config.py` plus `music_auth.py configure`: persistent nonsecret IDs/paths at `/app/config/private/apple-music.json`, retaining environment compatibility.
- `collector.py`: latest-ten polling every five minutes, ordered observation snapshots with storage deduplication, transactional SQLite persistence in `/app/data`, a process lock, scheduled retries with persisted next-attempt times, safe logs and graceful shutdown.
- Snapshots are not individual play events. Exact play counts and playback times are not inferred. This first version does not paginate or backfill.
- Local `status`, Docker `healthcheck`, and consistent standalone SQLite `backup` commands.
- `Dockerfile`, `.dockerignore`, and `compose.collector.yaml`: a separate nonroot `music-discovery-collector` service using the existing mounts, with no published ports. The existing test service is retained.
- `COLLECTOR.md`: concrete NAS build, one-poll validation, continuous startup, monitoring, backup/restore and rollback procedures.

Docker Desktop was started for local validation: the Linux/amd64 image builds and a synthetic offline smoke test passes under a read-only root filesystem and 128 MB limit (about 58 MB peak process RSS in the short test). Package versions and the tested base-image digest are pinned. All 36 Python tests, the browser-flow test and Compose configuration validation pass. The user authorized committing and pushing this collector milestone after the successful NAS recreation check.

The user approved NAS staging/build and a single live poll. Verified from the user's terminal output:
- Source files were staged and the image built successfully on the NAS.
- Nonsecret configuration was saved at `/app/config/private/apple-music.json`; a fresh helper container fetched ten recent tracks without shell Apple variables.
- `Data` was owned by UID `1027` but had mode `555`. Changing only that directory to mode `700` fixed the write failure; ownership and existing file permissions were retained.
- A single live collector poll saved a ten-track snapshot. A separate temporary container reported `healthy: true`, `successful_polls: 1`, `failed_polls: 0`, and `unique_snapshots: 1`, verifying cross-container persistence of the database.
- The user subsequently approved continuous operation and started `music-discovery-collector`. A later status check reported `healthy: true`, `successful_polls: 5`, `failed_polls: 0`, and `unique_snapshots: 1`, verifying scheduled polling with unchanged responses reusing stored snapshots.
- The user force-recreated only the collector service and checked again after scheduled polling resumed: `healthy: true`, `successful_polls: 9`, `failed_polls: 0`, `unique_snapshots: 1`. Configuration, authorization and database history survived recreation. Initial deployment checks are complete.
- Multi-day resource usage, NAS reboot recovery, live backup/restore and reauthorization after invalidation are not yet verified. The service runs detached and does not depend on the user's SSH session or sudo password cache.

User communication preference: explicitly label any required user action **Your next step** and name the terminal. Otherwise state when no action is needed. Do not imply background work continues after a response ends.

## Immediate next steps

1. Observe multi-day resource usage and collection freshness during the trial; initial deployment and recreation checks have passed.
2. Clean up bootstrap credential copies and validate backup/restore during the trial. Add notifications and scheduled backups in a later iteration.
3. Future changes still require authorization before pushing; the prior authorization milestone is `49ecdd0`, and this collector checkpoint includes the verified NAS deployment results.

The user approved implementation of the collector after the completed authorization milestone. Ask before external deployments or production infrastructure changes. Do not automatically push changes or deploy the collector.
