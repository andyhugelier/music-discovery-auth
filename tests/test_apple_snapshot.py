from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import apple_snapshot as snapshot
from music_config import Settings


def resource(identifier="song", kind="library-songs", **attributes):
    return {"id": identifier, "type": kind, "attributes": attributes}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "apple.sqlite3"
        self.connection = snapshot.open_store(self.path)
        self.addCleanup(self.connection.close)

    def test_complete_export_plans_playlist_tracks_and_ratings_and_recommendations(self):
        def get(path):
            endpoint = snapshot.parse.urlsplit(path).path
            values = {
                "/v1/me/library/songs": [resource("i.a", name="Song", inFavorites=True)],
                "/v1/me/library/playlists": [resource("p.one", "library-playlists", canEdit=True)],
                "/v1/me/library/playlists/p.one/tracks": [resource("i.a", name="Song")],
                "/v1/me/recommendations": [resource("rec", "personal-recommendation")],
                "/v1/me/recommendations/rec/contents": [resource("album", "albums")],
                "/v1/me/ratings/library-songs": [resource("i.a", "ratings", value=-1)],
            }
            return {"data": values.get(endpoint, [])}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect(self.connection, MagicMock(get=get)), "complete")
        report = snapshot.summary(self.connection)
        names = {row["name"] for row in report["coverage"]}
        self.assertIn("playlist_tracks", names)
        self.assertIn("recommendation_contents", names)
        self.assertEqual(report["preference_signals"]["favorited_resources"], 1)
        self.assertEqual(report["preference_signals"]["known_song_dislikes"], 1)
        self.assertEqual(report["preference_signals"]["known_song_likes"], 0)

    def test_root_pagination_resumes_without_repeating_saved_page(self):
        run = snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='library_songs'").fetchone()
        payload = {"data": [resource("i.a")], "next": "/v1/me/library/songs?offset=100"}
        snapshot.save_page(self.connection, section, section["next_path"], payload)
        paths = []
        def get(path):
            paths.append(path)
            return {"data": []}
        with redirect_stdout(io.StringIO()):
            snapshot.collect(self.connection, MagicMock(get=get))
        self.assertIn("/v1/me/library/songs?limit=100&offset=100", paths)
        self.assertNotIn(snapshot.safe_path(snapshot.BASE_SECTIONS["library_songs"][1]), paths)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_unsafe_and_cross_endpoint_pagination_rejected(self):
        for path in ("https://evil.invalid/v1/me/library/songs", "/v1/me/library/songs?token=secret",
                     "/v1/me/../favorites", "/v1/me/library/songs#fragment",
                     "/v1/me/favorites", "/v1/me/library/songs\nInjected"):
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.safe_path(path)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.safe_path("/v1/me/library/albums?offset=100", "/v1/me/library/songs")
        self.assertEqual(snapshot.safe_path("https://api.music.apple.com/v1/me/library/songs?offset=100"),
                         "/v1/me/library/songs?offset=100")

    def test_sanitization_removes_secrets_media_and_urls_but_preserves_metadata(self):
        payload = {"data": [resource("song", name="Song", inFavorites=True, genreNames=["Jazz"],
            playParams={"catalogId": "123"}, artwork={"url": "https://example.invalid/signed"})],
            "developerToken": "synthetic-secret", "nested": {"text": "synthetic-secret", "url": "https://example.invalid/"}}
        cleaned = snapshot.sanitize(payload, ("synthetic-secret",))
        self.assertNotIn("synthetic-secret", json.dumps(cleaned))
        self.assertNotIn("https://", json.dumps(cleaned))
        self.assertNotIn("artwork", cleaned["data"][0]["attributes"])
        self.assertEqual(cleaned["data"][0]["attributes"]["playParams"]["catalogId"], "123")
        self.assertTrue(cleaned["data"][0]["attributes"]["inFavorites"])

    def test_optional_denial_remains_explicitly_unavailable(self):
        def get(path):
            if "/music-summaries" in path:
                raise snapshot.SnapshotError("http", 403)
            return {"data": []}
        with redirect_stdout(io.StringIO()):
            state = snapshot.collect(self.connection, MagicMock(get=get))
        self.assertEqual(state, "complete_with_unavailable")
        self.assertEqual(snapshot.summary(self.connection)["incomplete_sections"][0]["http_status"], 403)

    def test_auth_failure_stops_with_pending_sections(self):
        client = MagicMock()
        client.get.side_effect = snapshot.SnapshotError("http", 401)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect(self.connection, client), "partial")
        self.assertEqual(client.get.call_count, 1)

    def test_embedded_pagination_is_queued_with_source_category(self):
        snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='replay'").fetchone()
        payload = {"data": [resource("2025", "music-summaries", year=2025)]}
        payload["data"][0]["views"] = {"top-songs": {"data": [],
            "next": "/v1/me/music-summaries/2025/views/top-songs?offset=10"}}
        snapshot.save_page(self.connection, section, section["next_path"], payload)
        queued = self.connection.execute("SELECT * FROM sections WHERE name='embedded_pagination'").fetchone()
        self.assertEqual(queued["category"], "aggregate")
        self.assertEqual(queued["state"], "pending")

    def test_transaction_rolls_back_bad_resources(self):
        snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='library_songs'").fetchone()
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.save_page(self.connection, section, section["next_path"], {"data": [{}]})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT pages FROM sections WHERE id=?", (section["id"],)).fetchone()[0], 0)

    def test_pagination_loop_is_not_claimed_complete(self):
        snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='library_songs'").fetchone()
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.save_page(self.connection, section, section["next_path"], {"data": [], "next": section["next_path"]})

    def test_limit_fallback_uses_default_without_dropping_other_filters(self):
        seen = []
        def get(path):
            seen.append(path)
            if "/library/songs?limit=" in path:
                raise snapshot.SnapshotError("http", 400)
            return {"data": []}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect(self.connection, MagicMock(get=get)), "complete")
        self.assertIn("/v1/me/library/songs", seen)

    def test_existing_unrelated_database_is_preserved(self):
        other = Path(self.temp.name) / "other.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE preserve_me(value TEXT)")
        connection.close()
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.open_store(other)
        connection = sqlite3.connect(other)
        self.assertEqual([row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["preserve_me"])
        connection.close()

    def test_status_does_not_load_credentials(self):
        snapshot.begin(self.connection)
        with patch.object(snapshot, "load_settings") as settings, redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.main(["--database", str(self.path), "status"]), 0)
        settings.assert_not_called()

    def test_client_is_get_only_and_does_not_echo_upstream_errors(self):
        settings = Settings(team_id="ABCDEFGHIJ", key_id="0123456789", private_key_path="/run/secrets/test.p8")
        client = snapshot.Client(settings)
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid/synthetic-secret", 401,
                                                         "synthetic-secret", {}, None)
        with patch.object(snapshot, "developer_token", return_value="synthetic-developer"), \
                patch.object(snapshot, "read_private", return_value="synthetic-user"), \
                patch.object(snapshot.request, "build_opener", return_value=opener):
            with self.assertRaises(snapshot.SnapshotError) as error:
                client.get("/v1/me/storefront")
        self.assertNotIn("synthetic", str(error.exception))
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Music-user-token"), "synthetic-user")

    def test_continuation_preserves_options_and_uses_server_offset(self):
        path = snapshot.continuation_path("/v1/me/library/songs?offset=137",
            "/v1/me/library/songs?limit=100&extend=inFavorites&offset=12")
        options = dict(snapshot.parse.parse_qsl(snapshot.parse.urlsplit(path).query))
        self.assertEqual(options, {"limit": "100", "extend": "inFavorites", "offset": "137"})
        explicit = snapshot.continuation_path("/v1/me/library/songs?offset=137&limit=50",
                                              "/v1/me/library/songs?limit=100")
        self.assertEqual(dict(snapshot.parse.parse_qsl(snapshot.parse.urlsplit(explicit).query))["limit"], "50")

    def test_old_checkpoint_recovers_accepted_page_size_without_restarting(self):
        run = snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='library_songs'").fetchone()
        snapshot.save_page(self.connection, section, section["next_path"], {"data": [resource("i.first")],
            "next": "/v1/me/library/songs?offset=100"})
        with self.connection:
            self.connection.execute("UPDATE sections SET pages=999,items=25050,next_path=? WHERE id=?",
                                    ("/v1/me/library/songs?offset=25050", section["id"]))
        resumed = self.connection.execute("SELECT * FROM sections WHERE id=?", (section["id"],)).fetchone()
        self.assertEqual(snapshot.resume_path(self.connection, resumed),
                         "/v1/me/library/songs?limit=100&offset=25050")
        self.assertEqual(snapshot.begin(self.connection), run)
        self.assertEqual(self.connection.execute("SELECT items FROM sections WHERE id=?", (section["id"],)).fetchone()[0], 25050)

    def test_resume_does_not_restore_a_rejected_initial_limit(self):
        snapshot.begin(self.connection)
        section = self.connection.execute("SELECT * FROM sections WHERE name='favorite_song_flags'").fetchone()
        snapshot.save_page(self.connection, section, "/v1/me/library/songs?extend=inFavorites",
                           {"data": [resource()], "next": "/v1/me/library/songs?offset=25"})
        resumed = self.connection.execute("SELECT * FROM sections WHERE id=?", (section["id"],)).fetchone()
        options = dict(snapshot.parse.parse_qsl(snapshot.parse.urlsplit(snapshot.resume_path(self.connection, resumed)).query))
        self.assertEqual(options, {"extend": "inFavorites", "offset": "25"})

    def test_large_library_does_not_starve_other_sections(self):
        requested = []
        def get(path):
            endpoint = snapshot.parse.urlsplit(path).path
            requested.append(endpoint)
            if endpoint == "/v1/me/library/songs":
                offset = int(dict(snapshot.parse.parse_qsl(snapshot.parse.urlsplit(path).query)).get("offset", 0))
                return {"data": [resource("i.%d" % offset)],
                        "next": "/v1/me/library/songs?offset=%d" % (offset + 1)}
            return {"data": []}
        with redirect_stdout(io.StringIO()):
            snapshot.collect(self.connection, MagicMock(get=get), max_pages=20)
        for endpoint in ("/v1/me/music-summaries", "/v1/me/recommendations", "/v1/me/history/heavy-rotation",
                         "/v1/me/library/playlists"):
            self.assertIn(endpoint, requested[:len(snapshot.BASE_SECTIONS)])

    def test_page_progress_reports_ordinal_and_cumulative_count(self):
        with redirect_stdout(io.StringIO()) as output:
            snapshot.collect(self.connection, MagicMock(get=lambda path: {"data": []}), max_pages=1)
        self.assertIn("page=1; items=0; section_items=0", output.getvalue())
