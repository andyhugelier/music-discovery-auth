"""Always-on Apple Music observations, not an inferred per-play event ledger."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading
import time

import music_auth
from music_config import ConfigError, DEFAULT_CONFIG, load_settings


LOG = logging.getLogger("music-collector")
ERROR_KINDS = {"authorization", "developer_authorization", "user_authorization",
               "network", "rate_limit", "api", "response", "configuration", "internal"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    position INTEGER NOT NULL,
    apple_type TEXT NOT NULL,
    apple_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, position)
);
CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY,
    observed_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'error')),
    snapshot_id INTEGER REFERENCES snapshots(id),
    item_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    next_attempt_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS poll_runs_completed ON poll_runs(completed_at);
"""


class CollectorError(Exception):
    pass


@contextmanager
def collector_lock(database):
    """One writer per local NAS database, including one-off runs."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorError("Another collector already holds this database's lock.") from None
        yield
    finally:
        os.close(fd)


def open_database(path, readonly=False):
    path = Path(path)
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    else:
        # Create without following symlinks and restrict new database permissions.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            connection.close()
            raise CollectorError("Unsupported database version; do not overwrite the existing database.")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA user_version=1")
    return connection


def normalize_tracks(response):
    """Keep only catalog metadata, avoiding signed URLs and arbitrary payload fields."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise music_auth.AuthError("Unexpected recent-tracks response.", kind="response")
    if len(response["data"]) > 100:
        raise music_auth.AuthError("Unexpected recent-tracks response size.", kind="response")
    tracks = []
    for resource in response["data"]:
        if not isinstance(resource, dict):
            raise music_auth.AuthError("Invalid track resource.", kind="response")
        identifier, resource_type = resource.get("id"), resource.get("type")
        if not all(isinstance(value, str) and 0 < len(value) <= 512
                   for value in (identifier, resource_type)):
            raise music_auth.AuthError("Missing track identity.", kind="response")
        attributes = resource.get("attributes", {})
        if not isinstance(attributes, dict):
            raise music_auth.AuthError("Invalid track attributes.", kind="response")
        metadata = {}
        for field in ("name", "artistName", "albumName", "isrc"):
            value = attributes.get(field)
            if isinstance(value, str):
                metadata[field] = value[:4096]
        duration = attributes.get("durationInMillis")
        if type(duration) is int and duration >= 0:
            metadata["catalogDurationInMillis"] = duration
        tracks.append({"type": resource_type, "id": identifier, "metadata": metadata})
    return tracks


def fetch_tracks(settings):
    settings.validate(require_apple=True)
    directory = music_auth.private_directory(settings.credential_dir)
    user = music_auth.validate_token(music_auth.read_private(directory / "music-user-token"))
    developer = music_auth.developer_token(settings=settings)
    # Validate developer access separately so failures can be classified correctly.
    music_auth.request_api("/v1/test", developer, expect_json=False)
    response = music_auth.request_api("/v1/me/recent/played/tracks?limit=10", developer, user)
    return normalize_tracks(response)


def save_success(connection, tracks, observed, completed, next_attempt):
    payload = json.dumps(tracks, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    with connection:
        inserted = connection.execute("INSERT OR IGNORE INTO snapshots(fingerprint) VALUES (?)",
                                      (fingerprint,)).rowcount
        snapshot = connection.execute("SELECT id FROM snapshots WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
        if inserted:
            connection.executemany(
                "INSERT INTO snapshot_items VALUES (?, ?, ?, ?, ?)",
                [(snapshot, index, track["type"], track["id"], json.dumps(track["metadata"], sort_keys=True))
                 for index, track in enumerate(tracks)],
            )
        connection.execute(
            "INSERT INTO poll_runs(observed_at, completed_at, status, snapshot_id, item_count, next_attempt_at) "
            "VALUES (?, ?, 'success', ?, ?, ?)", (observed, completed, snapshot, len(tracks), next_attempt),
        )
    return bool(inserted)


def save_failure(connection, kind, observed, completed, next_attempt):
    kind = kind if kind in ERROR_KINDS else "internal"
    with connection:
        connection.execute(
            "INSERT INTO poll_runs(observed_at, completed_at, status, error_kind, next_attempt_at) "
            "VALUES (?, ?, 'error', ?, ?)", (observed, completed, kind, next_attempt),
        )


def retry_delay(settings, failures, error):
    delay = min(settings.max_backoff_seconds, settings.poll_seconds * 2 ** min(failures - 1, 16))
    if error.kind in {"authorization", "developer_authorization", "user_authorization", "configuration"}:
        delay = settings.max_backoff_seconds
    if error.retry_after is not None:
        delay = max(delay, error.retry_after)
    return delay


def poll_once(connection, settings, failures=0):
    observed = time.time()
    try:
        tracks = fetch_tracks(settings)
    except (music_auth.AuthError, ConfigError) as error:
        failures += 1
        if isinstance(error, ConfigError):
            error = music_auth.AuthError("Invalid runtime configuration.", kind="configuration")
        kind = error.kind if error.kind in ERROR_KINDS else "internal"
        delay = retry_delay(settings, failures, error)
        completed = time.time()
        save_failure(connection, kind, observed, completed, completed + delay)
        # Never log exception bodies, credentials, or response data.
        LOG.warning("Collection failed: category=%s; retry_in_seconds=%d", kind, delay)
        return failures, delay
    completed = time.time()
    changed = save_success(connection, tracks, observed, completed, completed + settings.poll_seconds)
    LOG.info("Collected recent-track snapshot: items=%d; new_snapshot=%s", len(tracks), changed)
    return 0, settings.poll_seconds


def run(settings, stop, once=False):
    with collector_lock(settings.database_path):
        connection = open_database(settings.database_path)
        try:
            # Honor persisted backoff after a process/container restart.
            last = connection.execute("SELECT next_attempt_at FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
            if last and not once and stop.wait(max(0, last[0] - time.time())):
                return 0
            failures = 0
            while not stop.is_set():
                failures, delay = poll_once(connection, settings, failures)
                if once:
                    return 1 if failures else 0
                if stop.wait(delay):
                    break
            LOG.info("Collector stopped cleanly.")
            return 0
        finally:
            connection.close()


def status(settings):
    connection = open_database(settings.database_path, readonly=True)
    try:
        latest = connection.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
        success = connection.execute("SELECT MAX(completed_at) FROM poll_runs WHERE status='success'").fetchone()[0]
        counts = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM poll_runs GROUP BY status")}
        age = time.time() - success if success is not None else None
        healthy = bool(latest and latest["status"] == "success" and age is not None
                       and -60 <= age <= max(600, 3 * settings.poll_seconds))
        return {"healthy": healthy,
                "last_success_utc": datetime.fromtimestamp(success, timezone.utc).isoformat() if success is not None else None,
                "latest_result": latest["status"] if latest else "not_started",
                "error_kind": latest["error_kind"] if latest else None,
                "successful_polls": counts.get("success", 0), "failed_polls": counts.get("error", 0),
                "unique_snapshots": connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]}
    finally:
        connection.close()


def backup(settings, destination):
    """SQLite online backup includes committed WAL data; never overwrite a backup."""
    path = Path(destination)
    if not path.is_absolute():
        raise CollectorError("Backup destination must be absolute.")
    source = open_database(settings.database_path, readonly=True)
    temporary, target = None, None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".history-backup-", dir=path.parent)
        os.close(fd)
        target = sqlite3.connect(temporary)
        source.backup(target)
        # Produce a standalone backup that does not require WAL/SHM sidecars.
        target.execute("PRAGMA journal_mode=DELETE")
        target.close()
        target = None
        # Publish only a complete backup; link refuses an existing destination.
        os.link(temporary, path)
    finally:
        source.close()
        if target is not None:
            target.close()
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("MUSIC_CONFIG_PATH", DEFAULT_CONFIG))
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("run")
    collect.add_argument("--once", action="store_true")
    subparsers.add_parser("status")
    subparsers.add_parser("healthcheck")
    copy = subparsers.add_parser("backup")
    copy.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    try:
        settings = load_settings(args.config)
        if args.command in {"status", "healthcheck"}:
            result = status(settings)
            if args.command == "status":
                print(json.dumps(result, indent=2))
            return 0 if result["healthy"] else 1
        if args.command == "backup":
            backup(settings, args.output)
            print("SQLite backup completed.")
            return 0
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda signum, frame: stop.set())
        return run(settings, stop, once=args.once)
    except (OSError, sqlite3.Error, CollectorError, ConfigError):
        # Exception text may include filesystem paths or values; do not log it.
        LOG.error("Collector operation failed; check configuration, storage permissions and database lock.")
        return 1
    except Exception:
        LOG.error("Unexpected collector failure; no exception details were logged.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
