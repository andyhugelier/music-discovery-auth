"""Reproducible, private taste-group baseline. Does not merge source play events."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

from collector import collector_lock, CollectorError
from music_auth import AuthError, read_private
from lastfm_import import Client, ImportFailure

ROOT = Path(__file__).resolve().parent
APP_ID = 0x4D445450
GROUPS = {
    "electronic": ("Electronic & downtempo", {"electronic", "electronica", "downtempo", "trip-hop", "trip hop", "idm", "ambient", "chillwave", "synthpop"}),
    "indie": ("Indie & alternative", {"alternative", "alternative rock", "indie", "indie rock", "indie pop", "dream pop", "shoegaze"}),
    "jazz": ("Jazz & instrumental", {"jazz", "nu jazz", "jazz fusion", "jazz-funk", "instrumental hip-hop", "instrumental hip hop"}),
    "soul": ("Soul & R&B", {"r&b/soul", "soul", "r&b", "neo-soul", "funk"}),
    "hiphop": ("Hip-hop & beats", {"hip-hop/rap", "hip-hop", "hip hop", "rap", "instrumental hip-hop", "instrumental hip hop"}),
    "folk": ("Folk & acoustic", {"folk", "singer/songwriter", "singer-songwriter", "acoustic", "indie folk", "americana"}),
    "rock": ("Rock & guitar", {"rock", "hard rock", "classic rock", "punk", "punk rock", "metal", "blues"}),
    "dance": ("Dance & rhythm", {"dance", "house", "deep house", "techno", "disco", "afro-beat", "afrobeat"}),
    "pop": ("Pop & melody", {"pop", "synthpop", "indie pop", "art pop"}),
    "classical": ("Classical & composition", {"classical", "modern classical", "contemporary classical", "orchestral"}),
    "world": ("Global sounds", {"worldwide", "world", "african", "latin", "brazilian", "reggae"}),
    "country": ("Country & roots", {"country", "alt-country", "americana", "bluegrass"}),
}
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE rules(kind TEXT NOT NULL,value TEXT NOT NULL,action TEXT NOT NULL,
 PRIMARY KEY(kind,value));
CREATE TABLE tracks(id TEXT PRIMARY KEY,source TEXT NOT NULL,source_id TEXT NOT NULL,
 artist TEXT NOT NULL,artist_key TEXT NOT NULL,title TEXT NOT NULL,album TEXT NOT NULL,
 genres TEXT NOT NULL,signals TEXT NOT NULL,score REAL NOT NULL,
 base_excluded INTEGER NOT NULL,base_reason TEXT NOT NULL,
 excluded INTEGER NOT NULL,exclusion_reason TEXT NOT NULL);
CREATE INDEX track_artist ON tracks(artist_key);
CREATE INDEX track_excluded_score ON tracks(excluded,score DESC);
CREATE TABLE memberships(track_id TEXT REFERENCES tracks(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL,basis TEXT NOT NULL,PRIMARY KEY(track_id,group_id));
CREATE INDEX member_group ON memberships(group_id);
CREATE TABLE artist_features(artist_key TEXT PRIMARY KEY,artist TEXT NOT NULL,
 tags TEXT NOT NULL,state TEXT NOT NULL,fetched_utc TEXT NOT NULL);
CREATE TABLE actions(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,
 value TEXT NOT NULL,created_utc TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
"""


class ProfileError(Exception):
    pass


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise ProfileError("Profile database must be private (mode 600).")
    finally:
        os.close(fd)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    app, version = c.execute("PRAGMA application_id").fetchone()[0], c.execute("PRAGMA user_version").fetchone()[0]
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not tables and app == version == 0:
        c.executescript(SCHEMA)
        c.execute("PRAGMA application_id=%d" % APP_ID)
        c.execute("PRAGMA user_version=1")
    elif app != APP_ID or version != 1:
        c.close()
        raise ProfileError("Choose a separate supported taste-profile database.")
    return c


class SourceConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            fd = getattr(self, "source_lock_fd", None)
            if fd is not None:
                os.close(fd)
                self.source_lock_fd = None


def source_db(path, expected):
    path = Path(path)
    if path.is_symlink():
        raise ProfileError("Source database symlinks are not supported.")
    # Cooperate with the importers' exclusive locks before inspecting WAL state.
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        wal = Path(str(path) + "-wal")
        closed_snapshot = not wal.exists() or wal.stat().st_size == 0
        uri = path.resolve().as_uri() + ("?mode=ro&immutable=1" if closed_snapshot else "?mode=ro")
        c = sqlite3.connect(uri, uri=True, factory=SourceConnection)
        c.source_lock_fd = fd
    except Exception:
        os.close(fd)
        raise
    try:
        if c.execute("PRAGMA application_id").fetchone()[0] != expected or c.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ProfileError("Source database has an unexpected application ID.")
    except Exception:
        c.close()
        raise
    c.row_factory = sqlite3.Row
    return c


def seed_rules(c, directory):
    if c.execute("SELECT 1 FROM meta WHERE key='context_seeded'").fetchone():
        return
    path = Path(directory) / "listening-context-review.json"
    if not path.exists():
        raise ProfileError("Supply the private listening-context-review.json before the first build.")
    value = json.loads(path.read_text())
    for artist in value.get("confirmed_artist_filters", []):
        c.execute("INSERT OR REPLACE INTO rules VALUES('artist',?,'exclude')", (norm(artist),))
    for artist in value.get("explicit_keep_artists", []):
        c.execute("INSERT OR REPLACE INTO rules VALUES('artist',?,'keep')", (norm(artist),))
    for assignment in value.get("assignments", []):
        c.execute("INSERT OR REPLACE INTO rules VALUES('track',?,'exclude')", ("apple:" + assignment["library_id"],))
    for name in ("moana", "frozen", "descendants", "lilo & stitch", "curious george", "kpop demon hunters"):
        c.execute("INSERT OR REPLACE INTO rules VALUES('soundtrack',?,'exclude')", (name,))
    c.execute("INSERT INTO meta VALUES('context_seeded',?)", (now(),))


def excluded_by_rules(rules, track_id, artist, title, album):
    if rules.get(("track", track_id)) == "exclude":
        return "Confirmed track context"
    action = rules.get(("artist", norm(artist)))
    if action == "keep":
        return ""
    if action == "exclude":
        return "Confirmed artist context"
    if re.search(r"\brain sounds?\b|\brainfall sounds?\b", " ".join((artist, title, album)), re.I):
        return "Rain sounds / background"
    album_key = norm(album)
    for (kind, name), rule in rules.items():
        if kind != "soundtrack" or rule != "exclude":
            continue
        pattern = r"\bk[- ]?pop demon hunters\b" if name == "kpop demon hunters" else r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, album_key) and (re.search(r"soundtrack|motion picture|sing-a-longs and lullabies", album_key)
                or album_key in {"frozen", "frozen the best", "lilo & stitch"} or name == "kpop demon hunters"):
            return "Confirmed family soundtrack"
    return ""


def item(source, source_id, artist, title, album="", genres=None, signals=None):
    return {"id": source + ":" + source_id, "source": source, "source_id": source_id,
            "artist": artist or "Unknown artist", "artist_key": norm(artist or "Unknown artist"),
            "title": title or "Unknown track", "album": album or "", "genres": genres or [],
            "signals": signals or {}}


def metadata_key(artist, title, album):
    return hashlib.sha256(json.dumps([norm(artist), norm(title), norm(album)]).encode()).hexdigest()


def apple_tracks(path):
    c = source_db(path, 0x4D444150)
    songs, ratings, coverage = {}, {}, {}
    try:
        run = c.execute("SELECT id,state FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            return [], {"state": "not_started"}
        coverage = {"state": run["state"], "run_id": run["id"]}
        for name, raw in c.execute("SELECT s.name,p.response_json FROM pages p JOIN sections s ON s.id=p.section_id "
                                   "WHERE s.run_id=? AND s.name IN ('library_songs','favorite_song_flags','known_library_song_ratings') "
                                   "ORDER BY s.id,p.page", (run["id"],)):
            for r in json.loads(raw)["data"]:
                if name == "known_library_song_ratings":
                    ratings[r["id"]] = r.get("attributes", {}).get("value")
                elif r.get("type") == "library-songs":
                    songs.setdefault(r["id"], {}).update(r.get("attributes", {}))
        result = []
        for sid, a in songs.items():
            rating = ratings.get(sid)
            result.append(item("apple", sid, a.get("artistName"), a.get("name"), a.get("albumName"),
                               a.get("genreNames", []), {"saved": True, "favorite": a.get("inFavorites"),
                               "rating": rating, "positive": a.get("inFavorites") is True or rating == 1,
                               "negative": rating == -1, "catalog_id": a.get("playParams", {}).get("catalogId")}))
        coverage["library_tracks"] = len(result)
        return result, coverage
    finally:
        c.close()


def lastfm_tracks(path):
    c = source_db(path, 0x4D444849)
    tracks, counters = {}, Counter()
    try:
        if c.execute("SELECT COUNT(DISTINCT source_user) FROM evidence").fetchone()[0] > 1:
            raise ProfileError("This baseline requires a single-account Last.fm source database.")
        for r in c.execute("SELECT * FROM evidence"):
            sid = metadata_key(r["artist"], r["title"], r["album"])
            t = tracks.setdefault(sid, item("lastfm", sid, r["artist"], r["title"], r["album"], signals={
                "scrobbles": 0, "dated_scrobbles": 0, "undated_scrobbles": 0, "loved": False}))
            s = t["signals"]
            counters[r["kind"]] += 1
            if r["kind"] == "loved":
                s["loved"] = True
            else:
                s["scrobbles"] += 1
                s["dated_scrobbles" if r["event_time"] is not None and r["event_time"] >= 86400 else "undated_scrobbles"] += 1
        return list(tracks.values()), dict(counters)
    finally:
        c.close()


def spotify_tracks(path):
    c = source_db(path, 0x4D445350)
    tracks, counters = {}, Counter()
    try:
        if c.execute("SELECT COUNT(DISTINCT account) FROM events").fetchone()[0] > 1:
            raise ProfileError("This baseline requires a single-account Spotify source database.")
        for r in c.execute("SELECT * FROM events WHERE media='audio' AND kind='music'"):
            sid = r["track_uri"] or metadata_key(r["artist"], r["title"], r["album"])
            t = tracks.setdefault(sid, item("spotify", sid, r["artist"], r["title"], r["album"], signals={
                "streams": 0, "nonprivate_nonzero": 0, "at_least_30s": 0, "skipped": 0, "ms_played": 0}))
            s = t["signals"]
            s["streams"] += 1
            counters["audio_music_rows"] += 1
            if r["incognito"] != 1 and r["ms_played"] > 0:
                s["nonprivate_nonzero"] += 1
                s["at_least_30s"] += int(r["ms_played"] >= 30000)
                s["ms_played"] += r["ms_played"]
                s["skipped"] += int(r["skipped"] == 1)
        return list(tracks.values()), dict(counters)
    finally:
        c.close()


def evidence_score(t):
    s = t["signals"]
    if t["source"] == "apple":
        return 0.0 if s.get("negative") else (5.0 if s.get("positive") else 0.05)
    if t["source"] == "lastfm":
        return (5.0 if s.get("loved") else 0) + min(2.0, math.log1p(s["scrobbles"]) / 2)
    return min(2.0, math.log1p(s["at_least_30s"]) / 2)


def rebuild(c, directory):
    directory = Path(directory)
    loaded, coverage = [], {}
    for source, filename, reader in (("apple", "apple-music-snapshots.sqlite3", apple_tracks),
                                      ("lastfm", "listening-imports.sqlite3", lastfm_tracks),
                                      ("spotify", "spotify-history.sqlite3", spotify_tracks)):
        if (directory / filename).exists():
            rows, coverage[source] = reader(directory / filename)
            loaded.extend(rows)
        else:
            coverage[source] = {"state": "missing"}
    if not loaded:
        raise ProfileError("No imported source databases found; profile was not replaced.")
    with c:
        seed_rules(c, directory)
        rules = {(r["kind"], r["value"]): r["action"] for r in c.execute("SELECT * FROM rules")}
        c.execute("DELETE FROM memberships")
        c.execute("DELETE FROM tracks")
        for t in loaded:
            s = t["signals"]
            reason = excluded_by_rules(rules, t["id"], t["artist"], t["title"], t["album"])
            if t["source"] == "apple" and s.get("negative"):
                reason = "Explicit negative song rating"
            if t["source"] == "spotify" and s["nonprivate_nonzero"] == 0:
                reason = "Private-session or zero-playback evidence only"
            c.execute("INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                t["id"], t["source"], t["source_id"], t["artist"], t["artist_key"], t["title"], t["album"],
                json.dumps(t["genres"]), json.dumps(s), evidence_score(t), int(bool(reason)), reason, int(bool(reason)), reason))
        c.execute("INSERT OR REPLACE INTO meta VALUES('coverage',?)", (json.dumps(coverage),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('built_utc',?)", (now(),))
        apply_actions(c)
        refresh_memberships(c)
    return profile(c)


def apply_actions(c):
    c.execute("UPDATE tracks SET excluded=base_excluded,exclusion_reason=base_reason")
    for action in c.execute("SELECT * FROM actions WHERE active=1 ORDER BY id").fetchall():
        if action["kind"] in {"exclude_artist", "keep_artist"}:
            restriction = "" if action["kind"] == "exclude_artist" else " AND base_reason NOT IN ('Explicit negative song rating','Private-session or zero-playback evidence only')"
            c.execute("UPDATE tracks SET excluded=?,exclusion_reason=? WHERE artist_key=?" + restriction,
                      (int(action["kind"] == "exclude_artist"), "Your artist exclusion" if action["kind"] == "exclude_artist" else "", action["target"]))
        elif action["kind"] == "exclude_track":
            c.execute("UPDATE tracks SET excluded=1,exclusion_reason='Your track exclusion' WHERE id=?", (action["target"],))


def refresh_memberships(c):
    c.execute("DELETE FROM memberships")
    features = {r["artist_key"]: [norm(t["name"]) for t in json.loads(r["tags"])]
                for r in c.execute("SELECT * FROM artist_features WHERE state='complete'")}
    artist_genres = defaultdict(Counter)
    rows = c.execute("SELECT id,artist_key,genres,excluded FROM tracks").fetchall()
    for row in rows:
        if not row["excluded"]:
            artist_genres[row["artist_key"]].update(set(norm(g) for g in json.loads(row["genres"]) if norm(g) != "music"))
    memberships = []
    for row in rows:
        own = {norm(g) for g in json.loads(row["genres"])}
        inherited = {g for g, _ in artist_genres[row["artist_key"]].most_common(4)}
        tags = set(features.get(row["artist_key"], []))
        for gid, (_, aliases) in GROUPS.items():
            basis = ("Track genre metadata" if own & aliases else
                     "Last.fm artist tags (broad estimate)" if tags & aliases else
                     "Other library tracks by this artist (broad estimate)" if inherited & aliases else "")
            if basis:
                memberships.append((row["id"], gid, basis))
    c.executemany("INSERT INTO memberships VALUES(?,?,?)", memberships)
    for action in c.execute("SELECT target,value FROM actions WHERE active=1 AND kind='set_groups' ORDER BY id").fetchall():
        if not c.execute("SELECT 1 FROM tracks WHERE id=?", (action["target"],)).fetchone():
            continue
        c.execute("DELETE FROM memberships WHERE track_id=?", (action["target"],))
        c.executemany("INSERT INTO memberships VALUES(?,?,'Your group assignment')",
                      [(action["target"], gid) for gid in json.loads(action["value"])])


def track_view(row):
    return {**{k: row[k] for k in ("id", "source", "artist", "artist_key", "title", "album", "excluded", "exclusion_reason")},
            "signals": json.loads(row["signals"]), "genres": json.loads(row["genres"]),
            "basis": row["basis"] if "basis" in row.keys() else None}


def tracks(c, group=None, query="", excluded=False, offset=0, limit=40):
    args = [int(excluded)]
    where = "t.excluded=?"
    join = ""
    if group:
        if group not in GROUPS:
            raise ProfileError("Unknown group.")
        join = " JOIN memberships m ON m.track_id=t.id"
        where += " AND m.group_id=?"
        args.append(group)
    if query:
        where += " AND (instr(lower(t.artist),lower(?))>0 OR instr(lower(t.title),lower(?))>0)"
        args.extend([query[:200], query[:200]])
    total = c.execute("SELECT COUNT(*) FROM tracks t" + join + " WHERE " + where, args).fetchone()[0]
    selection = "t.*,m.basis" if group else "t.*"
    rows = c.execute("SELECT " + selection + " FROM tracks t" + join + " WHERE " + where +
                     " ORDER BY t.score DESC,t.artist,t.title,t.id LIMIT ? OFFSET ?", args + [min(100, limit), offset]).fetchall()
    items = []
    for row in rows:
        view = track_view(row)
        view["group_ids"] = [r[0] for r in c.execute("SELECT group_id FROM memberships WHERE track_id=? ORDER BY group_id", (row["id"],))]
        items.append(view)
    return {"total": total, "offset": offset, "items": items}


def profile(c):
    meta = {r["key"]: r["value"] for r in c.execute("SELECT * FROM meta")}
    labels = {r["target"]: r["value"] for r in c.execute("SELECT * FROM actions WHERE kind='rename_group' AND active=1 ORDER BY id")}
    counts = {r["group_id"]: r["n"] for r in c.execute("SELECT m.group_id,COUNT(*) AS n FROM memberships m JOIN tracks t ON t.id=m.track_id WHERE t.excluded=0 GROUP BY m.group_id")}
    groups = []
    for gid, (label, _) in GROUPS.items():
        if not counts.get(gid):
            continue
        examples, seen = [], set()
        for r in c.execute("SELECT t.*,m.basis FROM tracks t JOIN memberships m ON m.track_id=t.id WHERE t.excluded=0 AND m.group_id=? ORDER BY t.score DESC,t.artist,t.title", (gid,)):
            if r["artist_key"] in seen:
                continue
            seen.add(r["artist_key"])
            examples.append(track_view(r))
            if len(examples) == 4:
                break
        groups.append({"id": gid, "name": labels.get(gid, label), "tracks": counts[gid], "examples": examples})
    total, excluded = c.execute("SELECT COUNT(*),COALESCE(SUM(excluded),0) FROM tracks").fetchone()
    grouped = c.execute("SELECT COUNT(DISTINCT t.id) FROM tracks t JOIN memberships m ON m.track_id=t.id WHERE t.excluded=0").fetchone()[0]
    feature_counts = dict(c.execute("SELECT state,COUNT(*) FROM artist_features GROUP BY state").fetchall())
    return {"built_utc": meta.get("built_utc"), "coverage": json.loads(meta.get("coverage", "{}")),
            "source_tracks": total, "excluded_source_tracks": excluded, "eligible_source_tracks": total-excluded,
            "grouped_source_tracks": grouped, "unclassified_source_tracks": total-excluded-grouped,
            "artist_enrichment": feature_counts, "groups": groups,
            "group_options": [{"id": gid, "name": labels.get(gid, label)} for gid, (label, _) in GROUPS.items()],
            "method": "Overlapping genre/tag baseline; not learned clusters or mood classification.",
            "identity": "Source tracks remain separate. Matching artist names is provisional; counts are not deduplicated recordings or plays.",
            "last_action": dict(row) if (row := c.execute("SELECT id,kind FROM actions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()) else None}


def act(c, kind, target, value=""):
    if kind not in {"exclude_artist", "keep_artist", "exclude_track", "rename_group", "set_groups", "undo"}:
        raise ProfileError("Unsupported review action.")
    if not isinstance(target, str) or len(target) > 4096 or not isinstance(value, str):
        raise ProfileError("Invalid review input.")
    with c:
        if kind == "undo":
            if not target.isdigit():
                raise ProfileError("Invalid undo action.")
            c.execute("UPDATE actions SET active=0 WHERE id=?", (int(target),))
        else:
            if kind == "rename_group":
                value = value.strip()
                if target not in GROUPS or not 1 <= len(value) <= 70:
                    raise ProfileError("Choose a group name of 1–70 characters.")
            elif kind in {"exclude_track", "set_groups"}:
                if not c.execute("SELECT 1 FROM tracks WHERE id=?", (target,)).fetchone():
                    raise ProfileError("Unknown track.")
                if kind == "set_groups":
                    try:
                        selected = json.loads(value)
                    except ValueError:
                        raise ProfileError("Invalid group selection.") from None
                    if not isinstance(selected, list) or len(selected) > len(GROUPS) or any(not isinstance(g, str) or g not in GROUPS for g in selected):
                        raise ProfileError("Invalid group selection.")
                    value = json.dumps(sorted(set(selected)))
            else:
                target = norm(target)
                if not c.execute("SELECT 1 FROM tracks WHERE artist_key=?", (target,)).fetchone():
                    raise ProfileError("Unknown artist.")
            c.execute("INSERT INTO actions(kind,target,value,created_utc) VALUES(?,?,?,?)", (kind, target, value, now()))
        apply_actions(c)
        refresh_memberships(c)
    return profile(c)


def enrich(c, max_artists=50, client=None):
    if not 1 <= max_artists <= 100:
        raise ProfileError("Choose 1–100 artists for a bounded enrichment batch.")
    seeds = c.execute("SELECT artist_key,MIN(artist) AS artist,MAX(score) AS strength,"
                      "MAX(CASE WHEN source='apple' AND score>=5 THEN 1 ELSE 0 END) AS explicit_apple FROM tracks "
                      "WHERE excluded=0 AND score>0.05 AND artist_key NOT IN (SELECT artist_key FROM artist_features) "
                      "GROUP BY artist_key ORDER BY explicit_apple DESC,strength DESC,artist_key LIMIT ?", (max_artists,)).fetchall()
    if client is None:
        client = Client(read_private(ROOT / "Config/private/lastfm-api-key"), "andyhugelier")
    for index, seed in enumerate(seeds, 1):
        result = client.request("artist.getTopTags", artist=seed["artist"], autocorrect=1)
        top = result.get("toptags")
        if not isinstance(top, dict) or not isinstance(top.get("tag", []), list):
            raise ProfileError("The tag provider returned an unsupported response; cached successes remain saved.")
        tags = []
        for tag in top.get("tag", [])[:30]:
            if not isinstance(tag, dict) or not isinstance(tag.get("name"), str) or len(tag["name"]) > 120:
                raise ProfileError("Invalid tag metadata; cached successes remain saved.")
            if str(tag.get("count", "0")).isdigit() and int(tag.get("count", 0)) > 0:
                tags.append({"name": tag["name"], "weight": min(int(tag["count"]), 100)})
        with c:
            c.execute("INSERT OR REPLACE INTO artist_features VALUES(?,?,?,?,?)", (
                seed["artist_key"], seed["artist"], json.dumps(tags), "complete" if tags else "empty", now()))
        print("Artist metadata cached: %d/%d; tags=%d" % (index, len(seeds), len(tags)), flush=True)
    with c:
        refresh_memberships(c)
    return profile(c)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "Data/taste-profile.sqlite3"))
    parser.add_argument("--data-dir", default=str(ROOT / "Data"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    commands.add_parser("status")
    commands.add_parser("enrich").add_argument("--max-artists", type=int, default=50)
    args = parser.parse_args(argv)
    os.umask(0o077)
    c = None
    try:
        with collector_lock(args.database):
            c = connect(args.database)
            value = rebuild(c, args.data_dir) if args.command == "build" else enrich(c, args.max_artists) if args.command == "enrich" else profile(c)
            print(json.dumps({k: v for k, v in value.items() if k != "groups"}, indent=2))
        return 0
    except (ProfileError, AuthError, CollectorError, ImportFailure) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError):
        print("Profile operation failed; check private storage and source database access. No raw error details logged.", file=sys.stderr)
        return 1
    finally:
        if c is not None:
            c.close()


if __name__ == "__main__":
    raise SystemExit(main())
