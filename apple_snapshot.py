"""Read-only, resumable Apple Music metadata snapshots with explicit coverage."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib import error, parse, request

from collector import collector_lock
from music_auth import NoRedirect, developer_token, read_private, retry_after_seconds, validate_token
from music_config import load_settings


APPLICATION_ID = 0x4D444150
BASE_SECTIONS = {
    "storefront": ("account", "/v1/me/storefront"),
    "library_songs": ("library", "/v1/me/library/songs?limit=100"),
    "favorite_song_flags": ("explicit_favorite", "/v1/me/library/songs?extend=inFavorites&limit=100"),
    "library_albums": ("library", "/v1/me/library/albums?limit=100"),
    "library_artists": ("library", "/v1/me/library/artists?limit=100"),
    "library_playlists": ("playlist_membership", "/v1/me/library/playlists?limit=100"),
    "recently_added": ("library", "/v1/me/library/recently-added?limit=10"),
    "recent_tracks": ("observation", "/v1/me/recent/played/tracks?limit=10"),
    "heavy_rotation": ("aggregate", "/v1/me/history/heavy-rotation?limit=10"),
    "replay": ("aggregate", "/v1/me/music-summaries?filter%5Byear%5D=latest&views=top-artists,top-albums,top-songs"),
    "recommendations": ("recommendation", "/v1/me/recommendations?limit=10"),
}
SCHEMA = """
CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at REAL NOT NULL, completed_at REAL, state TEXT NOT NULL);
CREATE TABLE sections(
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), name TEXT NOT NULL,
 category TEXT NOT NULL, initial_path TEXT NOT NULL, next_path TEXT, state TEXT NOT NULL,
 pages INTEGER NOT NULL DEFAULT 0, items INTEGER NOT NULL DEFAULT 0,
 http_status INTEGER, error_kind TEXT, UNIQUE(run_id,initial_path)
);
CREATE TABLE pages(
 section_id INTEGER NOT NULL REFERENCES sections(id), page INTEGER NOT NULL,
 fetched_at REAL NOT NULL, request_path TEXT NOT NULL, response_json TEXT NOT NULL,
 PRIMARY KEY(section_id,page), UNIQUE(section_id,request_path)
);
CREATE TABLE planned_ratings(run_id INTEGER PRIMARY KEY REFERENCES runs(id));
"""


class SnapshotError(Exception):
    def __init__(self, kind, status=None, retry_after=None):
        super().__init__("Apple snapshot request failed: " + kind)
        self.kind, self.status, self.retry_after = kind, status, retry_after


def safe_path(value, same_endpoint=None):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise SnapshotError("unsafe_pagination")
    parts = parse.urlsplit(value)
    if (parts.scheme and parts.scheme != "https") or (parts.netloc and parts.netloc != "api.music.apple.com") or parts.fragment:
        raise SnapshotError("unsafe_pagination")
    decoded = parse.unquote(parts.path)
    if ".." in decoded.split("/") or "\\" in decoded or "//" in decoded:
        raise SnapshotError("unsafe_pagination")
    personal = re.fullmatch(
        r"/v1/me/(storefront|library/(songs|albums|artists|playlists)(/[^/]+/(tracks|catalog))?"
        r"|library/recently-added|history/heavy-rotation|recent/played/tracks"
        r"|music-summaries(/[^/]+)*|recommendations(/[^/]+/contents)?|ratings/(library-songs|songs))",
        decoded,
    )
    catalog = re.fullmatch(r"/v1/catalog/[a-z]{2}/[a-z-]+(/[A-Za-z0-9._~-]+)*", decoded)
    if not personal and not catalog:
        raise SnapshotError("unsafe_pagination")
    allowed = {"limit", "offset", "l", "include", "extend", "views", "ids", "filter[year]"}
    query = parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(key not in allowed for key, _ in query):
        raise SnapshotError("unsafe_pagination")
    if same_endpoint is not None and parts.path != parse.urlsplit(same_endpoint).path:
        raise SnapshotError("unsafe_pagination")
    return parts.path + ("?" + parse.urlencode(sorted(query), safe=",") if query else "")


def continuation_path(value, previous):
    """Use Apple's offset, carrying options that next links may omit."""
    value = safe_path(value, previous)
    parts = parse.urlsplit(value)
    options = dict(parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, item in parse.parse_qsl(parse.urlsplit(previous).query, keep_blank_values=True):
        if key != "offset":
            options.setdefault(key, item)
    return safe_path(parts.path + ("?" + parse.urlencode(options, safe=",") if options else ""))


def resume_path(connection, section):
    path = section["next_path"]
    if section["pages"]:
        # Older exports lost page options after page one. Recover only options
        # from the first successful request, so rejected initial limits stay absent.
        first = connection.execute("SELECT request_path FROM pages WHERE section_id=? ORDER BY page LIMIT 1",
                                   (section["id"],)).fetchone()
        if first:
            path = continuation_path(path, first[0])
    return path


def sanitize(value, secrets=(), depth=0):
    """Preserve source structures, stripping credentials, URLs and media payloads."""
    if depth > 20:
        raise SnapshotError("response_depth")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lower = key.lower()
            if (len(key) > 128 or any(word in lower for word in ("token", "secret", "authorization", "cookie", "signature"))
                    or lower in {"url", "href", "artwork", "images", "previews", "editorialnotes"}):
                continue
            if key == "next":
                if item:
                    result[key] = safe_path(item)
            else:
                result[key] = sanitize(item, secrets, depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize(item, secrets, depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) > 32768:
            raise SnapshotError("response_string_size")
        if "https://" in value or "http://" in value or any(secret and secret in value for secret in secrets):
            return "[redacted]"
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise SnapshotError("response_value")


class Client:
    def __init__(self, settings):
        settings.validate(require_apple=True)
        self.settings, self.developer, self.issued, self.user, self.last = settings, None, 0, None, None

    def get(self, path):
        path = safe_path(path)
        for attempt in range(3):
            if self.last is not None:
                time.sleep(max(0, 1 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            if self.developer is None or time.time() - self.issued > 3000:
                self.developer = developer_token(settings=self.settings)
                self.issued = time.time()
            self.user = validate_token(read_private(Path(self.settings.credential_dir) / "music-user-token"))
            try:
                req = request.Request("https://api.music.apple.com" + path,
                    headers={"Authorization": "Bearer " + self.developer, "Music-User-Token": self.user,
                             "Accept": "application/json", "User-Agent": "MusicDiscoverySnapshot/1.0"})
                with request.build_opener(NoRedirect()).open(req, timeout=20) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise SnapshotError("response_size")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise SnapshotError("response_shape")
                return sanitize(payload, (self.developer, self.user))
            except error.HTTPError as exc:
                retry = retry_after_seconds(exc.headers.get("Retry-After")) if exc.headers else None
                failure = SnapshotError("rate_limit" if exc.code == 429 else "http", exc.code, retry)
                retryable = exc.code == 429 or exc.code >= 500
            except (error.URLError, OSError, TimeoutError):
                failure, retryable = SnapshotError("network"), True
            except (ValueError, UnicodeError):
                raise SnapshotError("response_json") from None
            delay = failure.retry_after or 2 ** (attempt + 1)
            if not retryable or attempt == 2 or delay > 60:
                raise failure from None
            time.sleep(delay)


def open_store(path, readonly=False):
    path = Path(path)
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    identity = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not readonly and not tables and identity == version == 0:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA application_id=%d" % APPLICATION_ID)
        connection.execute("PRAGMA user_version=1")
    elif identity != APPLICATION_ID or version != 1:
        connection.close()
        raise SnapshotError("wrong_database")
    if not readonly:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def queue(connection, run, name, category, path):
    path = safe_path(path)
    connection.execute("INSERT OR IGNORE INTO sections(run_id,name,category,initial_path,next_path,state) VALUES(?,?,?,?,?,'pending')",
                       (run, name, category, path, path))


def begin(connection, fresh=False):
    run = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    with connection:
        if run and run["state"] == "partial" and not fresh:
            connection.execute("UPDATE sections SET state='pending' WHERE run_id=? AND state='error'", (run["id"],))
            return run["id"]
        run = connection.execute("INSERT INTO runs(started_at,state) VALUES(?,'partial')", (time.time(),)).lastrowid
        for name, (category, path) in BASE_SECTIONS.items():
            queue(connection, run, name, category, path)
    return run


def nested_envelopes(value):
    if isinstance(value, dict):
        if isinstance(value.get("next"), str) and value["next"]:
            yield value["next"]
        for key, item in value.items():
            if key != "next":
                yield from nested_envelopes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_envelopes(item)


def save_page(connection, section, path, payload):
    if connection.execute("SELECT 1 FROM pages WHERE section_id=? AND request_path=?", (section["id"], path)).fetchone():
        raise SnapshotError("pagination_loop")
    next_path = continuation_path(payload["next"], path) if payload.get("next") else None
    if next_path == path or (next_path and connection.execute(
            "SELECT 1 FROM pages WHERE section_id=? AND request_path=?", (section["id"], next_path)).fetchone()):
        raise SnapshotError("pagination_loop")
    with connection:
        connection.execute("INSERT INTO pages VALUES(?,?,?,?,?)", (section["id"], section["pages"] + 1,
            time.time(), path, json.dumps(payload, ensure_ascii=True, sort_keys=True)))
        connection.execute("UPDATE sections SET pages=pages+1,items=items+?,next_path=?,state=?,http_status=200,error_kind=NULL WHERE id=?",
                           (len(payload["data"]), next_path, "pending" if next_path else "complete", section["id"]))
        for index, resource in enumerate(payload["data"]):
            if not isinstance(resource, dict):
                raise SnapshotError("resource_shape")
            identifier, kind = resource.get("id"), resource.get("type")
            if not isinstance(identifier, str) or not isinstance(kind, str):
                raise SnapshotError("resource_identity")
            encoded = parse.quote(identifier, safe="")
            if section["name"] == "library_playlists" and kind == "library-playlists":
                queue(connection, section["run_id"], "playlist_tracks", "playlist_membership",
                      "/v1/me/library/playlists/" + encoded + "/tracks?limit=100")
            if kind == "personal-recommendation":
                queue(connection, section["run_id"], "recommendation_contents", "recommendation",
                      "/v1/me/recommendations/" + encoded + "/contents?limit=25")
            for nested in nested_envelopes(resource):
                queue(connection, section["run_id"], "embedded_pagination", section["category"], nested)


def plan_ratings(connection, run):
    base = connection.execute("SELECT state FROM sections WHERE run_id=? AND name='library_songs'", (run,)).fetchone()
    if not base or base[0] != "complete" or connection.execute("SELECT 1 FROM planned_ratings WHERE run_id=?", (run,)).fetchone():
        return
    identifiers = set()
    for row in connection.execute("SELECT p.response_json FROM pages p JOIN sections s ON s.id=p.section_id "
                                  "WHERE s.run_id=? AND s.name='library_songs'", (run,)):
        identifiers.update(item["id"] for item in json.loads(row[0])["data"] if item.get("type") == "library-songs")
    identifiers = sorted(identifiers)
    with connection:
        for start in range(0, len(identifiers), 25):
            query = parse.urlencode({"ids": ",".join(identifiers[start:start + 25])})
            queue(connection, run, "known_library_song_ratings", "explicit_rating", "/v1/me/ratings/library-songs?" + query)
        connection.execute("INSERT INTO planned_ratings VALUES(?)", (run,))


def collect(connection, client, max_pages=1000, fresh=False):
    run = begin(connection, fresh)
    for _ in range(max_pages):
        plan_ratings(connection, run)
        # Fair page scheduling also catches up never-started sections in old runs.
        section = connection.execute("SELECT * FROM sections WHERE run_id=? AND state='pending' ORDER BY pages,id LIMIT 1", (run,)).fetchone()
        if not section:
            break
        try:
            path = resume_path(connection, section)
            try:
                payload = client.get(path)
            except SnapshotError as exc:
                # Some endpoint limits vary; retry once using that endpoint's default.
                parts = parse.urlsplit(path)
                pairs = parse.parse_qsl(parts.query)
                if exc.status != 400 or not any(key == "limit" for key, _ in pairs):
                    raise
                path = safe_path(parts.path + ("?" + parse.urlencode([(key, value) for key, value in pairs if key != "limit"])
                                               if any(key != "limit" for key, _ in pairs) else ""))
                payload = client.get(path)
            save_page(connection, section, path, payload)
            print("Saved snapshot page: section=%s; page=%d; items=%d; section_items=%d" %
                  (section["name"], section["pages"] + 1, len(payload["data"]), section["items"] + len(payload["data"])), flush=True)
        except SnapshotError as exc:
            unavailable = exc.status in (400, 403, 404, 405, 501)
            with connection:
                connection.execute("UPDATE sections SET state=?,http_status=?,error_kind=? WHERE id=?",
                    ("unavailable" if unavailable else "error", exc.status, exc.kind, section["id"]))
            print("Snapshot section incomplete: section=%s; category=%s; http_status=%s" %
                  (section["name"], exc.kind, exc.status), flush=True)
            if exc.status == 401 or (section["name"] == "storefront" and exc.status == 403) or exc.kind in ("rate_limit", "network"):
                break
    plan_ratings(connection, run)
    pending = connection.execute("SELECT COUNT(*) FROM sections WHERE run_id=? AND state IN ('pending','error')", (run,)).fetchone()[0]
    unavailable = connection.execute("SELECT COUNT(*) FROM sections WHERE run_id=? AND state='unavailable'", (run,)).fetchone()[0]
    state = "partial" if pending else "complete_with_unavailable" if unavailable else "complete"
    with connection:
        connection.execute("UPDATE runs SET state=?,completed_at=? WHERE id=?", (state, None if pending else time.time(), run))
    return state


def summary(connection):
    run = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        return {"state": "not_started"}
    coverage = [dict(row) for row in connection.execute(
        "SELECT name,category,state,COUNT(*) AS sections,SUM(pages) AS pages,SUM(items) AS items "
        "FROM sections WHERE run_id=? GROUP BY name,category,state ORDER BY name,state", (run["id"],))]
    errors = [dict(row) for row in connection.execute("SELECT name,state,http_status,error_kind,COUNT(*) AS sections "
        "FROM sections WHERE run_id=? AND state IN ('error','unavailable') GROUP BY name,state,http_status,error_kind", (run["id"],))]
    favorite_flags, ratings = {}, {}
    def inspect(value, category):
        if isinstance(value, dict):
            attributes = value.get("attributes", {})
            if isinstance(attributes, dict) and isinstance(value.get("id"), str):
                identity = (value.get("type", ""), value["id"])
                if type(attributes.get("inFavorites")) is bool:
                    favorite_flags[identity] = attributes["inFavorites"]
                if category == "explicit_rating" and type(attributes.get("value")) is int and attributes["value"] in (-1, 1):
                    ratings[identity] = attributes["value"]
            for item in value.values():
                inspect(item, category)
        elif isinstance(value, list):
            for item in value:
                inspect(item, category)
    for row in connection.execute("SELECT s.category,p.response_json FROM pages p JOIN sections s ON s.id=p.section_id WHERE s.run_id=?", (run["id"],)):
        inspect(json.loads(row["response_json"]), row["category"])
    return {"run_id": run["id"], "state": run["state"],
        "started_utc": datetime.fromtimestamp(run["started_at"], timezone.utc).isoformat(),
        "coverage": coverage, "incomplete_sections": errors,
        "preference_signals": {"resources_with_favorite_flag": len(favorite_flags),
            "favorited_resources": sum(favorite_flags.values()),
            "known_song_likes": sum(value == 1 for value in ratings.values()),
            "known_song_dislikes": sum(value == -1 for value in ratings.values())},
        "ratings_scope": "Known library song IDs only; not all historical likes/dislikes.",
        "favorites_scope": "Source favorite flags where returned; full favorites coverage is not assumed.",
        "history_scope": "Library membership, snapshots and aggregates are not individual play events."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--database", default="/app/data/apple-music-snapshots.sqlite3")
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    args = parser.parse_args(argv)
    os.umask(0o077)
    connection = None
    try:
        if not 1 <= args.max_pages <= 100000:
            raise SnapshotError("invalid_page_limit")
        if args.command == "status":
            connection = open_store(args.database, readonly=True)
            print(json.dumps(summary(connection), indent=2))
            return 0
        settings = load_settings(args.config)
        client = Client(settings)
        with collector_lock(args.database):
            connection = open_store(args.database)
            state = collect(connection, client, args.max_pages, args.fresh)
            print(json.dumps(summary(connection), indent=2))
            return 1 if state == "partial" else 0
    except KeyboardInterrupt:
        print("Stopped. Saved snapshot pages remain available for resume.", file=sys.stderr)
        return 1
    except Exception:
        print("Snapshot could not finish. Check configuration, credential access, storage permissions and the database lock; no exception details were logged.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
