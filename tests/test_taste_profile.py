import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import apple_snapshot
import lastfm_import
import spotify_import
import taste_profile as taste
import profile_server


def apple_resource(sid, artist="Synthetic Artist", title="Synthetic Track", genres=None, favorite=False):
    return {"id": sid, "type": "library-songs", "attributes": {
        "artistName": artist, "name": title, "albumName": "Synthetic Album",
        "genreNames": genres or ["Electronic", "Jazz"], "inFavorites": favorite}}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "taste.sqlite3"
        self.c = taste.connect(self.path)
        self.directory.joinpath("listening-context-review.json").write_text(json.dumps({
            "confirmed_artist_filters": ["Family Artist"], "explicit_keep_artists": ["Keep Artist"],
            "assignments": [{"library_id": "rain"}]}))
        c = apple_snapshot.open_store(self.directory / "apple-music-snapshots.sqlite3")
        with c:
            run = c.execute("INSERT INTO runs(started_at,state) VALUES(1,'complete')").lastrowid
            apple_snapshot.queue(c, run, "library_songs", "library", "/v1/me/library/songs?limit=100")
            section = c.execute("SELECT * FROM sections WHERE name='library_songs'").fetchone()
        apple_snapshot.save_page(c, section, section["initial_path"], {"data": [
            apple_resource("positive", favorite=True), apple_resource("family", artist="Family Artist"),
            apple_resource("kept", artist="Keep Artist"), apple_resource("rain", artist="Rain Sounds", title="Night Rain"),
            apple_resource("negative", title="Synthetic negative") ]})
        with c:
            apple_snapshot.queue(c, run, "known_library_song_ratings", "explicit_rating", "/v1/me/ratings/library-songs?ids=positive,negative")
            section = c.execute("SELECT * FROM sections WHERE name='known_library_song_ratings'").fetchone()
        apple_snapshot.save_page(c, section, section["initial_path"], {"data": [
            {"id": "positive", "type": "ratings", "attributes": {"value": 1}},
            {"id": "negative", "type": "ratings", "attributes": {"value": -1}}]})
        c.close()

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def build(self):
        return taste.rebuild(self.c, self.directory)

    def test_baseline_overlaps_and_favorite_rating_are_one_signal(self):
        profile = self.build()
        self.assertEqual(profile["source_tracks"], 5)
        self.assertEqual(profile["excluded_source_tracks"], 3)
        self.assertEqual(profile["eligible_source_tracks"], 2)
        row = self.c.execute("SELECT * FROM tracks WHERE id='apple:positive'").fetchone()
        self.assertEqual(row["score"], 5.0)
        self.assertTrue(json.loads(row["signals"])["positive"])
        self.assertEqual({g["id"] for g in profile["groups"]}, {"electronic", "jazz"})
        self.assertTrue(all(g["tracks"] == 2 for g in profile["groups"]))

    def test_review_actions_undo_and_names_survive_regeneration(self):
        self.build()
        taste.act(self.c, "rename_group", "electronic", "My synthetic group")
        taste.act(self.c, "exclude_artist", "Synthetic Artist")
        profile = self.build()
        self.assertEqual(profile["excluded_source_tracks"], 4)
        self.assertEqual(next(g["name"] for g in profile["groups"] if g["id"] == "electronic"), "My synthetic group")
        action = profile["last_action"]["id"]
        profile = taste.act(self.c, "undo", str(action))
        self.assertEqual(profile["excluded_source_tracks"], 3)
        self.assertEqual(taste.tracks(self.c, query="Night Rain")["total"], 0)
        self.assertEqual(taste.tracks(self.c, query="Night Rain", excluded=True)["total"], 1)

    def test_context_album_matching_not_substrings_or_performer_bans(self):
        rules = {("soundtrack", "frozen"): "exclude", ("artist", "keep artist"): "keep"}
        self.assertFalse(taste.excluded_by_rules(rules, "x", "Ordinary Artist", "Song", "Frozen Brass - Africa & Latin America"))
        self.assertTrue(taste.excluded_by_rules(rules, "x", "Ordinary Artist", "Song", "Frozen (Original Motion Picture Soundtrack)"))
        self.assertFalse(taste.excluded_by_rules(rules, "x", "Ordinary Artist", "Rain", "Ordinary Album"))
        self.assertFalse(taste.excluded_by_rules(rules, "x", "Keep Artist", "Rain Sounds", "Rain Sounds"))

    def test_manual_group_membership_survives_rebuild_and_undo(self):
        self.build()
        taste.act(self.c, "set_groups", "apple:positive", json.dumps(["folk", "indie"]))
        self.build()
        memberships = self.c.execute("SELECT group_id,basis FROM memberships WHERE track_id='apple:positive'").fetchall()
        self.assertEqual({r[0] for r in memberships}, {"folk", "indie"})
        self.assertTrue(all(r[1] == "Your group assignment" for r in memberships))
        action = taste.profile(self.c)["last_action"]["id"]
        taste.act(self.c, "undo", str(action))
        self.assertEqual({r[0] for r in self.c.execute("SELECT group_id FROM memberships WHERE track_id='apple:positive'")}, {"electronic", "jazz"})

    def test_missing_source_does_not_delete_previous_profile(self):
        self.build()
        empty = self.directory / "empty"
        empty.mkdir()
        with self.assertRaises(taste.ProfileError):
            taste.rebuild(self.c, empty)
        self.assertEqual(taste.profile(self.c)["source_tracks"], 5)

    def test_spotify_private_zero_and_short_streams_not_positive_labels(self):
        rows = [
            {"ts": "2024-01-01T00:00:00Z", "ms_played": 0},
            {"ts": "2024-01-02T00:00:00Z", "ms_played": 60000, "incognito_mode": True},
            {"ts": "2024-01-03T00:00:00Z", "ms_played": 2000, "skipped": True}]
        for index, r in enumerate(rows):
            r.update(master_metadata_track_name="Example %d" % index, master_metadata_album_artist_name="Synthetic Artist",
                     spotify_track_uri="spotify:track:" + str(index) * 22)
        source = self.directory / "export"
        source.mkdir()
        source.joinpath("Streaming_History_Audio_2024.json").write_text(json.dumps(rows))
        c = spotify_import.open_database(self.directory / "spotify-history.sqlite3")
        spotify_import.import_directory(c, source)
        c.close()
        self.build()
        result = self.c.execute("SELECT excluded,score FROM tracks WHERE source='spotify' ORDER BY source_id").fetchall()
        self.assertEqual([tuple(x) for x in result], [(1, 0), (1, 0), (0, 0)])
        taste.act(self.c, "keep_artist", "Synthetic Artist")
        result = self.c.execute("SELECT excluded FROM tracks WHERE source='spotify' ORDER BY source_id").fetchall()
        self.assertEqual([x[0] for x in result], [1, 1, 0])
        self.assertEqual(self.c.execute("SELECT excluded FROM tracks WHERE id='apple:negative'").fetchone()[0], 1)

    def test_historical_dates_and_loved_evidence_stay_separate(self):
        c = lastfm_import.open_history(self.directory / "listening-imports.sqlite3")
        for kind, timestamp in [("scrobbles", 42), ("loved", None)]:
            run = lastfm_import.start_run(c, "example-user", kind)
            lastfm_import.save_page(c, run, [{"event_time": timestamp, "artist": "Synthetic Artist",
                "title": "Historical song", "album": "Old album", "track_mbid": ""}], 1, 1)
        c.close()
        self.build()
        s = json.loads(self.c.execute("SELECT signals FROM tracks WHERE source='lastfm'").fetchone()[0])
        self.assertEqual(s["undated_scrobbles"], 1)
        self.assertEqual(s["dated_scrobbles"], 0)
        self.assertTrue(s["loved"])

    def test_enrichment_cache_and_artist_basis_are_explicit(self):
        self.build()
        client = type("FakeClient", (), {"request": lambda _, *a, **k: {"toptags": {"tag": [
            {"name": "Indie Rock", "count": "100", "url": "do-not-store"}]}}})()
        with patch("builtins.print"):
            taste.enrich(self.c, 1, client)
        tags = self.c.execute("SELECT tags FROM artist_features").fetchone()[0]
        self.assertNotIn("do-not-store", tags)
        self.assertIn("indie", {g["id"] for g in taste.profile(self.c)["groups"]})
        self.build()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM artist_features").fetchone()[0], 1)

    def test_source_locks_and_active_wal_are_respected(self):
        path = self.directory / "apple-music-snapshots.sqlite3"
        from collector import collector_lock
        with collector_lock(path):
            with self.assertRaises(BlockingIOError):
                taste.source_db(path, apple_snapshot.APPLICATION_ID)
        source = apple_snapshot.open_store(path)
        with source:
            source.execute("UPDATE runs SET state='partial'")
        reader = taste.source_db(path, apple_snapshot.APPLICATION_ID)
        self.assertEqual(reader.execute("SELECT state FROM runs").fetchone()[0], "partial")
        reader.close()
        source.close()


class ServerTests(unittest.TestCase):
    setUp = ProfileTests.setUp
    tearDown = ProfileTests.tearDown
    build = ProfileTests.build
    def test_loopback_allowlist_and_csrf(self):
        self.build()
        server = profile_server.make_server(self.path, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:%d" % server.server_port
        def request(path, body=None, headers=None):
            payload = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=payload, headers=headers or {})
            return urllib.request.urlopen(req, timeout=5)
        try:
            response = request("/api/profile")
            self.assertIn("no-store", response.headers["Cache-Control"])
            token = json.loads(response.read())["csrf"]
            for path in ("/Data/taste.sqlite3", "/../Data/secret", "/api/tracks?offset=-1"):
                with self.assertRaises(urllib.error.HTTPError):
                    request(path)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/profile", headers={"Host": "untrusted.example"})
            self.assertEqual(error.exception.code, 403)
            action = {"kind": "rename_group", "target": "electronic", "value": "Synthetic renamed group"}
            for origin in (None, "https://untrusted.example"):
                headers = {"Content-Type": "application/json", "X-Review-CSRF": token}
                if origin:
                    headers["Origin"] = origin
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/actions", action, headers)
                self.assertEqual(error.exception.code, 403)
            headers = {"Content-Type": "application/json", "X-Review-CSRF": token, "Origin": base}
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/actions", action, {**headers, "X-Review-CSRF": "incorrect-token"})
            self.assertEqual(error.exception.code, 403)
            with request("/api/actions", action, headers) as response:
                data = json.loads(response.read())
                self.assertIn("Synthetic renamed group", [g["name"] for g in data["profile"]["groups"]])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
