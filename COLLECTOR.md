# Always-on collector

NAS image build and the approved one-poll test passed on November 5, 2026.
Persistent configuration and saved data were verified from separate temporary
containers. The user approved and started continuous collection; scheduled
polling and container recreation are verified with nine successful polls, no
failures and healthy status.

## Behavior and data meaning

The collector requests the latest ten recently played tracks every five minutes.
It uses the already verified MusicKit key and persisted Music User Token.
It generates developer JWTs automatically and reloads the user token every poll.
It does not play music, modify the library, or create playlists.

This is a partial observation history, not a complete listening-event ledger.
It stores UTC request-start and completion times, resource identity, ordered
position and selected track metadata. Catalog duration is labeled as such; it is
not time listened. No playback timestamps, skip events or play counts are inferred.
No pagination or historical backfill is performed in this first version. Apple
updates and coverage can lag; an outage or activity beyond the latest ten tracks
can leave gaps that the collector cannot reconstruct.

An unchanged ordered response reuses the same snapshot. Every successful poll
still gets its own row pointing to that snapshot. A → B → A is retained as three
observations referencing two snapshots. Repeated IDs within one response retain
their positions. Never count snapshot items or poll rows as individual plays.
Only selected catalog metadata is retained; raw responses and signed URLs are not.

SQLite schema version 1:

- `poll_runs`: one completed attempt, result, safe error category, observation
  times, next-attempt time, and optional snapshot reference.
- `snapshots`: content hashes for unique ordered responses.
- `snapshot_items`: ordered resource IDs/types and selected metadata per snapshot.

Writes are transactional, use SQLite WAL with full synchronization, and acquire
a process lock so two collectors cannot write concurrently. Store the database
on the NAS's local `Data` mount, not SMB/NFS. There is no automatic data deletion;
monitor disk usage as observations accumulate (about 105,000 polls/year at five
minutes, with snapshot size depending on actual changes).

## Persistent configuration

`/app/config/private/apple-music.json` contains **only** IDs, paths and timing
settings. The existing directory is owned by `1027:100`, mode `700`; `configure`
writes a mode `600` file atomically. Keeping it here avoids needing broader write
access to the parent `Config` directory.

`apple-music.example.json` describes the fields with placeholders; it is not a
ready-to-use configuration. `music_auth.py configure` prompts for the three Apple
settings. Existing settings are retained on Enter; it does not print their values.
Set polling timing by editing this nonsecret JSON privately and restarting the
collector. Poll interval must be 30–86400 seconds; maximum backoff must be at
least the poll interval and no more than 86400 seconds.

Existing `APPLE_TEAM_ID`, `APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY_PATH` environment
variables override the file for backward compatibility. The deployed collector
does not need those variables. `MUSIC_CONFIG_PATH` or `--config PATH` selects a
different configuration file. Config changes require a restart; a replaced user
token is picked up on the next scheduled attempt without restarting.

## Deployment plan (execute only after approval)

Use a **new** `music-discovery-collector` container, retaining the existing
`music-discovery-test` service. No published ports or inbound NAS endpoint are
needed. The new container runs as `1027:100` with a read-only root filesystem,
read-only `Secrets` and `Config` mounts, and a writable `Data` mount. Only the
authorization helper used for enrollment needs a writable `Config` mount.

1. Transfer these source files into `/volume1/docker/Music-discovery`:
   `collector.py`, `music_config.py`, `music_auth.py`, `requirements.txt`,
   `Dockerfile`, `.dockerignore`, and `compose.collector.yaml`. Do not copy secrets
   or the local virtual environment. The Docker build context uses an allowlist
   that excludes everything else, including the existing NAS mounts.
2. Check `Data` ownership/ACLs. Grant UID `1027` write access to that directory,
   retaining any existing data. Do not recursively change shared folders or
   loosen key/token permissions. The key remains mode `600`, and
   `Config/private` remains owned by `1027:100`, mode `700`.
3. On the NAS, build and validate, without starting the collector:

   ```sh
   cd /volume1/docker/Music-discovery
   sudo docker compose -f compose.collector.yaml config --quiet
   sudo docker compose -f compose.collector.yaml build
   ```

   The image targets the DS220+'s `linux/amd64` architecture. Python 3.12 slim
   includes runtime CA certificates; dependencies install only at image build
   time and require binary wheels to avoid a memory-heavy source build on NAS.
   Package versions and the tested base-image digest are pinned; updates require
   a deliberate rebuild and validation.
4. Configure using a temporary helper container. This prompts locally for the
   Team ID, Key ID, and private-key path (`/run/secrets/<your-key-filename>.p8`).
   It uses the existing key; never copy its contents into the prompt:

   ```sh
   sudo docker run --rm -it --user 1027:100 \
     --volume /volume1/docker/Music-discovery/Secrets:/run/secrets:ro \
     --volume /volume1/docker/Music-discovery/Config:/app/config \
     music-discovery-collector:local python /app/music_auth.py configure
   ```

5. Validate without shell Apple variables, then collect **one** observation:

   ```sh
   sudo docker compose -f compose.collector.yaml run --rm --no-deps collector python /app/music_auth.py check
   sudo docker compose -f compose.collector.yaml run --rm --no-deps collector python /app/collector.py run --once
   sudo docker compose -f compose.collector.yaml run --rm --no-deps collector python /app/collector.py status
   ```

   Confirm success before starting continuous operation. A one-off poll bypasses
   saved scheduling; it must not be run concurrently with the continuous process.
6. After reviewing the one-poll result and approving continuous operation:

   ```sh
   sudo docker compose -f compose.collector.yaml up -d
   sudo docker compose -f compose.collector.yaml logs --tail=20 collector
   sudo docker compose -f compose.collector.yaml exec collector python /app/collector.py status
   ```

   Verify another scheduled poll after five minutes, then verify restart and
   recreation recovery using the same mounts. Docker health can take a few minutes
   to settle after startup. There is no NAS reboot or production service change
   required for the first proof.

## Failure handling and monitoring

Network/API failures retry on a schedule starting at the poll interval, doubling
to a one-hour maximum by default. Authorization/configuration failures wait the
maximum delay rather than repeatedly trying credentials. HTTP 429 honors
`Retry-After` seconds or date when supplied, bounded at 24 hours. Each failure
records only a safe category and scheduling times; logs never include exception
text, credentials, upstream bodies or track names. Next-attempt times survive
process/container restarts. A successful poll resets exponential backoff.

`status` reports counts, last success, the latest error category and health.
`healthcheck` is silent and exits nonzero when no successful poll exists, the
latest attempt failed, or success is older than three poll intervals (minimum
ten minutes). Neither command contacts Apple. Health is about collection
freshness, not whether Apple's latest tracks match actual recent listening.

Compose restarts a crashed process; it does **not** restart containers just because
health is unhealthy. There is no email/push notification integration yet. Check
Container Manager health and the status command during the initial trial. Docker
logs rotate at 5 MB, retaining up to three files.

On user-access failure, reauthorize through the browser and import a replacement
token using a helper container with writable `Config`. Do not delete existing
history. Automatic polling will retry on its schedule, or restart it to reload
config (persisted next-attempt delay is still honored).

## Backup, restore and rollback

Create a consistent online SQLite backup with the running container:

```sh
sudo docker compose -f compose.collector.yaml exec collector \
  python /app/collector.py backup --output /app/data/history-backup-YYYYMMDD.sqlite3
```

Choose a new absolute destination each time; existing files are never overwritten.
The result is a standalone SQLite database incorporating committed WAL data.
Do not copy only the live main database file while WAL writes are active.
Move backups to separate storage with appropriate access controls. Back up the
private configuration/credentials separately with encryption/access restrictions;
database backups intentionally do not contain them. No automatic backup schedule
is included in this milestone.

To restore: stop the collector, preserve the current database and WAL/SHM files
together, restore the backup to the configured database path, and restore ownership
`1027:100` and mode `600`. Confirm the backup with `PRAGMA integrity_check` and
`status` before resuming. An old successful poll can legitimately report stale
health until a new poll succeeds. Review restore commands before executing them;
never overwrite live history.

Rollback is `sudo docker compose -f compose.collector.yaml stop collector`.
The existing test container, keys, token and collected database remain on disk.
This does not undo the authorization or delete history.

## Validation and outstanding checks

Local automated tests cover configuration, snapshot semantics, transaction
rollback, locks, token reloads, backoff, stale health, backup/restore, and real
process SIGTERM/restart behavior using mocked Apple access. Existing authorization
and browser tests also pass. `docker compose config` validates the service file.

The Linux/amd64 image built successfully. `tests/container_smoke.py` also passed
inside the image on Python 3.12, with networking disabled, a read-only root
filesystem, no capabilities and a 128 MB limit. It generated synthetic credentials,
signed developer JWTs, collected mocked observations twice, deduplicated storage,
checked health and created a backup. Peak process RSS was about 58 MB in that
short test; this is not a long-term NAS resource measurement.

The user built the image on the NAS, saved configuration and verified Apple
authorization with the actual read-only credential mounts. `Data` initially had
mode `555`; changing only that directory to `700` (owner UID `1027`) resolved the
write failure. A single live poll stored ten tracks. A new temporary container
reported healthy status, one successful poll, zero failed polls and one snapshot,
confirming that the database survived the polling container's removal.

The user then approved and started the continuous collector. A later status check
reported healthy status, five successful polls, zero failed polls and one unique
snapshot, verifying scheduled polling and unchanged-response storage deduplication.
The user then force-recreated the collector and verified subsequent scheduled
polling: nine successful polls, zero failed polls, one snapshot and healthy status.
Configuration, saved authorization and database history survived recreation.
Multi-day memory usage, NAS reboot recovery and live backup/restore remain
**unverified**; the initial deployment checks are complete.
