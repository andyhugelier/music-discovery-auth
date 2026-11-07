"""Private, offline Spotify export import. No API calls or raw device/location storage."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys


ROOT = Path(__file__).resolve().parent
APPLICATION_ID = 0x4D445350
FILE_PATTERN = re.compile(r"Streaming_History_(Audio|Video)_[0-9]{4}(?:_[0-9]+)?\.json\Z")
MAX_FILE_BYTES = 64 * 1024 * 1024
SCHEMA = """
CREATE TABLE batches (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, label TEXT NOT NULL,
 imported_utc TEXT NOT NULL, file_count INTEGER NOT NULL,
 rows_seen INTEGER NOT NULL, new_events INTEGER NOT NULL
);
CREATE TABLE files (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, sha256 TEXT NOT NULL,
 media TEXT NOT NULL, basename TEXT NOT NULL, row_count INTEGER NOT NULL,
 UNIQUE(account,sha256,media)
);
CREATE TABLE events (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, fingerprint TEXT NOT NULL,
 occurrence INTEGER NOT NULL, media TEXT NOT NULL, kind TEXT NOT NULL,
 ended_utc TEXT NOT NULL, ms_played INTEGER NOT NULL,
 title TEXT, artist TEXT, album TEXT, track_uri TEXT,
 episode_uri TEXT, episode_name TEXT, episode_show TEXT,
 audiobook_uri TEXT, audiobook_title TEXT, chapter_uri TEXT, chapter_title TEXT,
 reason_start TEXT, reason_end TEXT,
 shuffle INTEGER, skipped INTEGER, offline INTEGER, incognito INTEGER,
 UNIQUE(account,fingerprint,occurrence)
);
CREATE INDEX event_account_time ON events(account,ended_utc);
CREATE TABLE origins (
 file_id INTEGER NOT NULL REFERENCES files(id), row_number INTEGER NOT NULL,
 event_id INTEGER NOT NULL REFERENCES events(id), PRIMARY KEY(file_id,row_number)
);
CREATE TABLE batch_files (
 batch_id INTEGER NOT NULL REFERENCES batches(id), file_id INTEGER NOT NULL REFERENCES files(id),
 PRIMARY KEY(batch_id,file_id)
);
"""
TEXT_FIELDS = {
    "title": "master_metadata_track_name", "artist": "master_metadata_album_artist_name",
    "album": "master_metadata_album_album_name", "track_uri": "spotify_track_uri",
    "episode_uri": "spotify_episode_uri", "episode_name": "episode_name",
    "episode_show": "episode_show_name", "audiobook_uri": "audiobook_uri",
    "audiobook_title": "audiobook_title", "chapter_uri": "audiobook_chapter_uri",
    "chapter_title": "audiobook_chapter_title", "reason_start": "reason_start",
    "reason_end": "reason_end",
}


class ImportFailure(Exception):
    """Only fixed messages; never propagate raw input, JSON snippets or OS errors."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_row(row, media):
    if not isinstance(row, dict):
        raise ImportFailure("Expected a JSON object for each history row.")
    ts = row.get("ts")
    if not isinstance(ts, str) or len(ts) > 64:
        raise ImportFailure("Missing or invalid stream-end timestamp.")
    try:
        date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if date.utcoffset() is None:
            raise ValueError()
        ended = date.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ImportFailure("Invalid timezone-aware stream-end timestamp.") from None
    milliseconds = row.get("ms_played")
    if type(milliseconds) is not int or not 0 <= milliseconds <= 2 ** 63 - 1:
        raise ImportFailure("Missing or invalid milliseconds played.")
    result = {"media": media, "ended_utc": ended, "ms_played": milliseconds}
    for target, source in TEXT_FIELDS.items():
        value = row.get(source)
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise ImportFailure("Invalid music/context metadata field.")
        result[target] = value
    for field, prefix in (("track_uri", "track"), ("episode_uri", "episode"),
                          ("audiobook_uri", "audiobook"), ("chapter_uri", "audiobook_chapter")):
        uri = result[field]
        if uri and not re.fullmatch(r"spotify:" + prefix + r":[A-Za-z0-9]{22}", uri):
            raise ImportFailure("Unsupported Spotify identifier; no file was committed.")
    for target, source in (("shuffle", "shuffle"), ("skipped", "skipped"),
                           ("offline", "offline"), ("incognito", "incognito_mode")):
        value = row.get(source)
        if value is not None and type(value) is not bool:
            raise ImportFailure("Invalid nullable playback flag.")
        result[target] = None if value is None else int(value)
    if result["track_uri"] or (result["title"] and result["artist"]):
        kind = "music"
    elif result["episode_uri"] or result["episode_name"]:
        kind = "podcast"
    elif result["audiobook_uri"] or result["chapter_uri"] or result["audiobook_title"]:
        kind = "audiobook"
    else:
        kind = "unknown"
    result["kind"] = kind
    # Allowlist above excludes IP, username, country, device and unrelated raw fields.
    fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=True,
                                            separators=(",", ":")).encode()).hexdigest()
    return result, fingerprint


def source_files(directory):
    path = Path(directory)
    if not path.is_dir():
        raise ImportFailure("The source must be an extracted export directory.")
    files = sorted(p for p in path.iterdir() if FILE_PATTERN.fullmatch(p.name))
    if not files or len(files) > 256:
        raise ImportFailure("Expected 1–256 supported streaming-history JSON files.")
    return files


def load_file(path):
    match = FILE_PATTERN.fullmatch(path.name)
    if not match:
        raise ImportFailure("Unsupported export filename.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            raise ImportFailure("Source is not a regular file within the 64 MB limit.")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ImportFailure("Source exceeds the 64 MB file limit.")
    try:
        rows = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ImportFailure("Unreadable history JSON; no file was committed.") from None
    if not isinstance(rows, list):
        raise ImportFailure("Expected a JSON list of history rows.")
    return match[1].lower(), hashlib.sha256(raw).hexdigest(), rows


def inspect(directory):
    totals, years, flags = Counter(), Counter(), Counter()
    first = last = None
    files, seen = [], set()
    for path in source_files(directory):
        media, digest, rows = load_file(path)
        duplicate = (media, digest) in seen
        seen.add((media, digest))
        files.append({"file": path.name, "rows": len(rows), "duplicate_payload": duplicate})
        for row in rows:
            event, _ = normalize_row(row, media)
            if duplicate:
                continue
            totals[media + "_" + event["kind"]] += 1
            ended = event["ended_utc"]
            years[ended[:4]] += 1
            first, last = min(first or ended, ended), max(last or ended, ended)
            if event["kind"] == "music" and media == "audio":
                for name in ("skipped", "incognito"):
                    flags[name + "_" + str(event[name])] += 1
                flags["zero_ms"] += int(event["ms_played"] == 0)
    return {"files": files, "distinct_payload_files": len(seen), "rows_by_kind": dict(totals),
            "rows": sum(totals.values()), "utc_range": [first, last],
            "rows_by_year": dict(sorted(years.items())), "audio_music_flags": dict(flags),
            "coverage": "Files supplied in this delivery; account lifetime completeness is not independently verified."}


@contextmanager
def database_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImportFailure("Another Spotify import holds this database lock.") from None
        yield
    finally:
        os.close(fd)


def open_database(path, readonly=False):
    path = Path(path)
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise ImportFailure("History database must be a private regular file (mode 600).")
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    app = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not readonly and not tables and app == version == 0:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA application_id=%d" % APPLICATION_ID)
        connection.execute("PRAGMA user_version=1")
    elif app != APPLICATION_ID or version != 1:
        connection.close()
        raise ImportFailure("Choose a separate, supported Spotify history database.")
    if not readonly:
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def import_directory(connection, directory, account="personal", label="delivery"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", account):
        raise ImportFailure("Use a local account alias of 1–64 letters, digits, dots, underscores or hyphens.")
    if not isinstance(label, str) or not 1 <= len(label) <= 128:
        raise ImportFailure("Delivery label must contain 1–128 characters.")
    paths = source_files(directory)
    occurrence, seen = Counter(), set()
    rows_seen = new_events = file_count = 0
    # Whole delivery commits atomically. A malformed later file rolls back earlier files.
    with connection:
        batch = connection.execute("INSERT INTO batches(account,label,imported_utc,file_count,rows_seen,new_events) "
                                   "VALUES(?,?,?,0,0,0)", (account, label, utc_now())).lastrowid
        for path in paths:
            media, digest, rows = load_file(path)
            if (media, digest) in seen:
                continue
            seen.add((media, digest))
            file_count += 1
            connection.execute("INSERT OR IGNORE INTO files(account,sha256,media,basename,row_count) VALUES(?,?,?,?,?)",
                               (account, digest, media, path.name, len(rows)))
            file_id = connection.execute("SELECT id FROM files WHERE account=? AND sha256=? AND media=?",
                                         (account, digest, media)).fetchone()[0]
            connection.execute("INSERT INTO batch_files VALUES(?,?)", (batch, file_id))
            for index, row in enumerate(rows, 1):
                event, fingerprint = normalize_row(row, media)
                occurrence[fingerprint] += 1
                ordinal = occurrence[fingerprint]
                columns = list(event)
                values = [account, fingerprint, ordinal] + [event[k] for k in columns]
                cursor = connection.execute("INSERT OR IGNORE INTO events(account,fingerprint,occurrence," +
                    ",".join(columns) + ") VALUES(" + ",".join("?" for _ in values) + ")", values)
                new_events += cursor.rowcount
                event_id = connection.execute("SELECT id FROM events WHERE account=? AND fingerprint=? AND occurrence=?",
                                               (account, fingerprint, ordinal)).fetchone()[0]
                connection.execute("INSERT OR IGNORE INTO origins VALUES(?,?,?)", (file_id, index, event_id))
                rows_seen += 1
        connection.execute("UPDATE batches SET file_count=?,rows_seen=?,new_events=? WHERE id=?",
                           (file_count, rows_seen, new_events, batch))
    return {"batch_id": batch, "files": file_count, "rows_seen": rows_seen,
            "new_events": new_events, "previously_seen_occurrences": rows_seen - new_events}


def summary(connection, account="personal"):
    coverage = [dict(row) for row in connection.execute(
        "SELECT media,kind,COUNT(*) AS records,MIN(ended_utc) AS first_end_utc,MAX(ended_utc) AS last_end_utc "
        "FROM events WHERE account=? GROUP BY media,kind", (account,))]
    stats = dict(connection.execute(
        "SELECT COUNT(*) AS records,COUNT(DISTINCT track_uri) AS distinct_track_uris,"
        "SUM(ms_played) AS milliseconds_played,SUM(ms_played=0) AS zero_ms_records,"
        "SUM(skipped=1) AS skipped_true,SUM(skipped IS NULL) AS skipped_unknown,"
        "SUM(incognito=1) AS private_session_records FROM events WHERE account=? AND media='audio' AND kind='music'",
        (account,)).fetchone())
    return {"account_alias": account, "coverage": coverage, "audio_music": stats,
            "batches": connection.execute("SELECT COUNT(*) FROM batches WHERE account=?", (account,)).fetchone()[0],
            "scope": "Supplied export files only; audio/video and music/nonmusic remain separate.",
            "interpretation": "Stream-end UTC and milliseconds played are source observations, not full listens, exact starts, completion percentages or likes. Skips are not dislikes.",
            "deduplication": "Same-account semantic fingerprint plus occurrence ordinal; maximum multiplicity across supplied deliveries. Ambiguous collisions remain unresolved across sources."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "Data/spotify-history.sqlite3"))
    parser.add_argument("--account", default="personal", help="Local account alias, not a credential")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect").add_argument("directory")
    load = commands.add_parser("import")
    load.add_argument("directory")
    load.add_argument("--label", default="delivery")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    os.umask(0o077)
    connection = None
    try:
        if args.command == "inspect":
            report = inspect(args.directory)
        elif args.command == "status":
            connection = open_database(args.database, readonly=True)
            report = summary(connection, args.account)
        else:
            with database_lock(args.database):
                connection = open_database(args.database)
                result = import_directory(connection, args.directory, args.account, args.label)
                report = {"import": result, "history": summary(connection, args.account)}
        print(json.dumps(report, indent=2))
        return 0
    except ImportFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError):
        print("Spotify import could not complete; check source format, private storage and database access. No raw error details were logged.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Import interrupted; the incomplete delivery was rolled back. Retry the same command.", file=sys.stderr)
        return 130
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
