"""Private, resumable Last.fm imports; never log request URLs or upstream bodies."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

from collector import collector_lock
from music_auth import AuthError, NoRedirect, private_directory, read_private, retry_after_seconds, write_private


ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 200
APPLICATION_ID = 0x4D444849
METHODS = {"scrobbles": ("user.getRecentTracks", "recenttracks"),
           "loved": ("user.getLovedTracks", "lovedtracks")}
SCHEMA = """
CREATE TABLE runs (
 id INTEGER PRIMARY KEY, source_user TEXT NOT NULL, kind TEXT NOT NULL,
 cutoff INTEGER NOT NULL, started_at INTEGER NOT NULL, completed_at INTEGER,
 state TEXT NOT NULL DEFAULT 'partial', next_page INTEGER NOT NULL DEFAULT 1,
 expected_total INTEGER, total_pages INTEGER, received INTEGER NOT NULL DEFAULT 0,
 error_kind TEXT
);
CREATE TABLE evidence (
 id INTEGER PRIMARY KEY, source_user TEXT NOT NULL, kind TEXT NOT NULL,
 event_key TEXT NOT NULL, occurrence INTEGER NOT NULL, event_time INTEGER,
 artist TEXT NOT NULL, title TEXT NOT NULL, album TEXT NOT NULL, track_mbid TEXT NOT NULL,
 first_run INTEGER NOT NULL REFERENCES runs(id),
 UNIQUE(source_user, kind, event_key, occurrence)
);
CREATE INDEX evidence_user_time ON evidence(source_user, kind, event_time);
CREATE TABLE run_events (
 run_id INTEGER NOT NULL REFERENCES runs(id), event_key TEXT NOT NULL,
 occurrences INTEGER NOT NULL, PRIMARY KEY(run_id, event_key)
);
CREATE TABLE pages (
 run_id INTEGER NOT NULL REFERENCES runs(id), page INTEGER NOT NULL,
 digest TEXT NOT NULL, received INTEGER NOT NULL, inserted INTEGER NOT NULL,
 PRIMARY KEY(run_id, page), UNIQUE(run_id, digest)
);
"""


class ImportFailure(Exception):
    def __init__(self, message, kind="response", retryable=False, delay=None):
        super().__init__(message)
        self.kind, self.retryable, self.delay = kind, retryable, delay


def validate_user(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ImportFailure("Invalid Last.fm username.", "configuration")
    return value.casefold()


def validate_key(value):
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ImportFailure("Expected a 32-character hexadecimal API key; no credential was saved.", "configuration")
    return value


class Client:
    def __init__(self, key, user):
        self.key, self.user = validate_key(key), validate_user(user)
        self.last_request = None

    def request(self, method, **parameters):
        if method not in {"user.getInfo", "user.getRecentTracks", "user.getLovedTracks"}:
            raise ImportFailure("Only supported read methods are allowed.", "configuration")
        for attempt in range(3):
            if self.last_request is not None:
                time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                return self._request(method, parameters)
            except ImportFailure as error:
                if not error.retryable or attempt == 2:
                    raise
                delay = error.delay or 2 ** (attempt + 1)
                if delay > 60:
                    raise ImportFailure("Last.fm requested a longer retry delay; retry this import later.", "rate_limit") from None
                time.sleep(delay)

    def _request(self, method, parameters):
        query = urllib.parse.urlencode({**parameters, "method": method, "user": self.user,
                                       "api_key": self.key, "format": "json"})
        request = urllib.request.Request("https://ws.audioscrobbler.com/2.0/?" + query,
                                         headers={"User-Agent": "MusicDiscoveryPersonalImporter/1.0",
                                                  "Accept": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ImportFailure("Last.fm response exceeded the size limit.")
            payload = json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = retry_after_seconds(error.headers.get("Retry-After")) if error.headers else None
                raise ImportFailure("Last.fm rate limit reached.", "rate_limit", True, delay) from None
            raise ImportFailure("Last.fm HTTP request failed.", "service", error.code >= 500) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ImportFailure("Cannot reach Last.fm securely.", "network", True) from None
        except (ValueError, UnicodeError):
            raise ImportFailure("Last.fm returned unreadable JSON.") from None
        if not isinstance(payload, dict):
            raise ImportFailure("Last.fm returned an unexpected response.")
        if "error" in payload:
            code = payload["error"]
            if code in (10, 26):
                raise ImportFailure("Last.fm API key was rejected; check or replace it.", "authorization")
            if code == 6:
                raise ImportFailure("Last.fm could not access the requested user.", "account")
            if code in (11, 16, 29):
                raise ImportFailure("Last.fm is temporarily unavailable or rate limited.",
                                    "rate_limit" if code == 29 else "service", True)
            raise ImportFailure("Last.fm rejected the request.", "service")
        return payload

    def profile(self):
        user = self.request("user.getInfo").get("user")
        if not isinstance(user, dict) or str(user.get("name", "")).casefold() != self.user:
            raise ImportFailure("Last.fm returned a different or missing user profile.", "account")
        return {"username": self.user, "reported_scrobbles": integer(user.get("playcount"))}


def integer(value, allow_zero=True):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ImportFailure("Last.fm returned an invalid count or timestamp.")
    value = int(value)
    if value < (0 if allow_zero else 1) or value > 10 ** 12:
        raise ImportFailure("Last.fm count or timestamp is outside the supported range.")
    return value


def text(value, required=False):
    if not isinstance(value, str) or len(value) > 4096 or (required and not value.strip()):
        raise ImportFailure("Last.fm returned invalid track metadata.")
    return value


def parse_page(payload, kind, user, page, cutoff):
    container = payload.get(METHODS[kind][1])
    if not isinstance(container, dict) or not isinstance(container.get("@attr"), dict):
        raise ImportFailure("Last.fm returned invalid pagination metadata.")
    attributes = container["@attr"]
    if str(attributes.get("user", "")).casefold() != user:
        raise ImportFailure("Last.fm pagination belongs to a different account.", "account")
    total, pages = integer(attributes.get("total")), integer(attributes.get("totalPages"))
    if integer(attributes.get("page"), False) != page or pages > 1000000:
        raise ImportFailure("Last.fm returned the wrong page or too many pages.")
    if total > 0 and (pages < page or not pages):
        raise ImportFailure("Last.fm returned inconsistent pagination.")
    resources = container.get("track", [])
    if isinstance(resources, dict):
        resources = [resources]
    if not isinstance(resources, list) or len(resources) > PAGE_SIZE + 1:
        raise ImportFailure("Last.fm returned an invalid track list.")
    tracks = []
    for resource in resources:
        if not isinstance(resource, dict):
            raise ImportFailure("Last.fm returned an invalid track entry.")
        flags = resource.get("@attr", {})
        if isinstance(flags, dict) and str(flags.get("nowplaying", "")).lower() == "true":
            continue
        artist = resource.get("artist", "")
        if isinstance(artist, dict):
            artist = artist.get("#text", artist.get("name", ""))
        album = resource.get("album", "")
        if isinstance(album, dict):
            album = album.get("#text", "")
        date = resource.get("date", {})
        timestamp = integer(date["uts"], False) if isinstance(date, dict) and "uts" in date else None
        if timestamp is not None and timestamp > 253402300799:
            raise ImportFailure("Last.fm timestamp exceeds the supported calendar range.")
        if kind == "scrobbles" and (timestamp is None or timestamp > cutoff):
            raise ImportFailure("Scrobble timestamp is missing or exceeds the fixed import cutoff.")
        tracks.append({"event_time": timestamp, "artist": text(artist, True),
                       "title": text(resource.get("name"), True), "album": text(album),
                       "track_mbid": text(resource.get("mbid", ""))})
    if (total == 0 and tracks) or (total > 0 and not tracks):
        raise ImportFailure("Last.fm count and track list disagree.")
    return tracks, total, pages


def open_history(path, readonly=False):
    if readonly:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not readonly and not tables and application == version == 0:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA application_id=%d" % APPLICATION_ID)
        connection.execute("PRAGMA user_version=1")
    elif application != APPLICATION_ID or version != 1:
        connection.close()
        raise ImportFailure("This is not a supported history-import database; choose a separate file.", "storage")
    if not readonly:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def start_run(connection, user, kind, fresh=False):
    previous = connection.execute("SELECT * FROM runs WHERE source_user=? AND kind=? ORDER BY id DESC LIMIT 1",
                                  (user, kind)).fetchone()
    if previous and previous["state"] == "partial" and not fresh:
        return previous
    now = int(time.time())
    with connection:
        cursor = connection.execute("INSERT INTO runs(source_user, kind, cutoff, started_at) VALUES (?, ?, ?, ?)",
                                    (user, kind, now - 1, now))
    return connection.execute("SELECT * FROM runs WHERE id=?", (cursor.lastrowid,)).fetchone()


def save_page(connection, run, tracks, total, pages):
    if run["expected_total"] is not None and (total != run["expected_total"] or pages != run["total_pages"]):
        raise ImportFailure("Last.fm history changed during pagination. Retry later with --fresh.", "pagination")
    received = run["received"] + len(tracks)
    complete = run["next_page"] >= pages
    if received > total or (complete and received != total):
        raise ImportFailure("Downloaded counts do not match Last.fm; import remains incomplete.", "pagination")
    canonical = json.dumps(tracks, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    if connection.execute("SELECT 1 FROM pages WHERE run_id=? AND digest=?", (run["id"], digest)).fetchone():
        raise ImportFailure("Last.fm repeated a page; import remains incomplete.", "pagination")
    inserted = 0
    with connection:
        occurrences = Counter()
        for track in tracks:
            key = hashlib.sha256(json.dumps(track, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if key not in occurrences:
                old = connection.execute("SELECT occurrences FROM run_events WHERE run_id=? AND event_key=?",
                                         (run["id"], key)).fetchone()
                occurrences[key] = old[0] if old else 0
            occurrences[key] += 1
            inserted += connection.execute(
                "INSERT OR IGNORE INTO evidence(source_user,kind,event_key,occurrence,event_time,artist,title,album,track_mbid,first_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run["source_user"], run["kind"], key, occurrences[key], track["event_time"],
                 track["artist"], track["title"], track["album"], track["track_mbid"], run["id"])).rowcount
        for key, count in occurrences.items():
            connection.execute("INSERT OR REPLACE INTO run_events VALUES (?, ?, ?)", (run["id"], key, count))
        connection.execute("INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
                           (run["id"], run["next_page"], digest, len(tracks), inserted))
        connection.execute(
            "UPDATE runs SET expected_total=?,total_pages=?,received=?,next_page=next_page+1,state=?,completed_at=?,error_kind=NULL WHERE id=?",
            (total, pages, received, "complete" if complete else "partial", int(time.time()) if complete else None, run["id"]))
    return inserted, complete


def import_history(connection, client, kind, max_pages=10, fresh=False):
    run = start_run(connection, client.user, kind, fresh)
    try:
        for _ in range(max_pages):
            parameters = {"page": run["next_page"], "limit": PAGE_SIZE}
            if kind == "scrobbles":
                parameters["to"] = run["cutoff"]
            payload = client.request(METHODS[kind][0], **parameters)
            tracks, total, pages = parse_page(payload, kind, client.user, run["next_page"], run["cutoff"])
            inserted, complete = save_page(connection, run, tracks, total, pages)
            print("Page %d saved: records=%d, new_records=%d, run_records=%d/%d, complete=%s" %
                  (run["next_page"], len(tracks), inserted, run["received"] + len(tracks), total, complete), flush=True)
            if complete:
                return
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
    except ImportFailure as error:
        with connection:
            connection.execute("UPDATE runs SET error_kind=? WHERE id=?", (error.kind, run["id"]))
        raise


def coverage(connection, user):
    result = {"source": "Last.fm", "username": user, "attribution": "https://www.last.fm/user/" + user,
              "evidence": {}, "latest_runs": []}
    for kind in METHODS:
        rows = connection.execute(
            "SELECT COUNT(*) AS records,COUNT(DISTINCT artist) AS artists,"
            "MIN(CASE WHEN event_time>=86400 THEN event_time END) AS first,"
            "MAX(CASE WHEN event_time>=86400 THEN event_time END) AS last,"
            "SUM(CASE WHEN event_time IS NULL THEN 1 ELSE 0 END) AS missing_times,"
            "SUM(CASE WHEN event_time<86400 THEN 1 ELSE 0 END) AS suspicious_times "
            "FROM evidence WHERE source_user=? AND kind=?", (user, kind)).fetchone()
        result["evidence"][kind] = {"records": rows["records"], "distinct_artist_names": rows["artists"],
            "earliest_utc": utc(rows["first"]), "latest_utc": utc(rows["last"]),
            "missing_timestamp_records": rows["missing_times"] or 0,
            "suspicious_epoch_timestamp_records": rows["suspicious_times"] or 0}
        run = connection.execute("SELECT id,state,received,expected_total,next_page,error_kind,cutoff FROM runs "
                                 "WHERE source_user=? AND kind=? ORDER BY id DESC LIMIT 1", (user, kind)).fetchone()
        if run:
            result["latest_runs"].append({"kind": kind, **dict(run)})
    return result


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="andyhugelier")
    parser.add_argument("--private-dir", default=str(ROOT / "Config/private"))
    parser.add_argument("--database", default=str(ROOT / "Data/listening-imports.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("setup", "check", "status", "import-scrobbles", "import-loved"):
        sub = subparsers.add_parser(command)
        if command.startswith("import-"):
            sub.add_argument("--max-pages", type=int, default=10)
            sub.add_argument("--fresh", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    connection = None
    try:
        user = validate_user(args.user)
        if args.command == "status":
            connection = open_history(args.database, readonly=True)
            print(json.dumps(coverage(connection, user), indent=2))
            return 0
        if args.command.startswith("import-") and not 1 <= args.max_pages <= 1000000:
            raise ImportFailure("Choose --max-pages between 1 and 1000000.", "configuration")
        private = private_directory(args.private_dir)
        if args.command == "setup":
            if not sys.stdin.isatty():
                raise ImportFailure("Key setup requires an interactive terminal.", "configuration")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = validate_key(getpass.getpass("Paste Last.fm API key (hidden): "))
            Client(key, user).profile()
            write_private(private, "lastfm-api-key", key)
            print("Last.fm account and API key verified. Key saved privately; no shared secret needed.")
            return 0
        client = Client(read_private(private / "lastfm-api-key"), user)
        if args.command == "check":
            print(json.dumps(client.profile(), indent=2))
            return 0
        with collector_lock(args.database):
            connection = open_history(args.database)
            import_history(connection, client, "scrobbles" if args.command == "import-scrobbles" else "loved",
                           args.max_pages, args.fresh)
            print(json.dumps(coverage(connection, user), indent=2))
        return 0
    except ImportFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except AuthError as error:
        # These are our shared helper's fixed, credential-safe messages.
        print(str(error), file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Hidden input is unavailable. Run setup in your local interactive terminal.", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Local storage operation failed. Check directory ownership, permissions, free space and the database lock.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Stopped. Saved import pages remain available for resume.", file=sys.stderr)
        return 1
    except Exception:
        print("Import operation failed. Check private-file permissions, storage and setup; no exception details were logged.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
