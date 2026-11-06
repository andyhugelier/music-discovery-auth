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

Still unverified: recovery after container recreation, NAS reboot, extended unattended operation, and reauthorization after token invalidation. The Apple identifiers are currently supplied through Docker exec environment options; durable runtime configuration is still needed for an automatic service. No collector has been deployed. The user authorized committing and pushing the authorization milestone on November 5, 2026.

## Current limitations

- Standalone NAS requests using a persisted Music User Token work after a container restart; recreation recovery and long-term unattended operation remain to be tested.
- The initial manual handoff and owner-restricted persistence are implemented; automatic reauthorization is not implemented.
- The current container installs dependencies at startup.
- No persistent listening-history database exists yet.
- No automatic polling or playlist generation exists yet.
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

## Immediate next steps

1. Verify saved authorization after separately reviewed container recreation; the restart test has passed.
2. Make nonsecret runtime configuration persistent and clean up bootstrap credential copies after testing.
3. Preserve the authorization milestone in Git; the user approved this checkpoint and push. Future pushes still require authorization and may publish GitHub Pages.
4. Plan and implement the minimal Python collector with SQLite persistence after approval of that milestone.
5. Move dependencies into a reproducible image build, add operational health reporting and backup/restore instructions.
6. Review deployment before replacing the test container's `sleep infinity` command.

The user approved work on the authorization proof and persistence milestone. Preserve that scope; ask before external deployments or production infrastructure changes. Do not automatically push changes or deploy the collector.
