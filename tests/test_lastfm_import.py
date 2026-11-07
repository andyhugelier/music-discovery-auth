from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import lastfm_import as importer


FAKE_KEY = "a" * 32  # Synthetic, never a real service credential.
USER = "example-user"


def resource(title="Track", timestamp=100, artist="Artist"):
    value = {"name": title, "artist": {"#text": artist}, "album": {"#text": "Album"}, "mbid": ""}
    if timestamp is not None:
        value["date"] = {"uts": str(timestamp)}
    return value


def page(records, index=1, total=None, pages=1, kind="scrobbles"):
    return {importer.METHODS[kind][1]: {"track": records,
            "@attr": {"user": USER, "page": str(index), "total": str(len(records) if total is None else total),
                      "totalPages": str(pages)}}}


class LastfmClientTests(unittest.TestCase):
    def test_requests_are_https_read_only_with_key_and_user(self):
        client = importer.Client(FAKE_KEY, USER)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"user":{"name":"example-user","playcount":"42"}}'
        with patch.object(importer.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(client.profile(), {"username": USER, "reported_scrobbles": 42})
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://ws.audioscrobbler.com/2.0/?"))
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)
        with self.assertRaises(importer.ImportFailure):
            client.request("track.scrobble")

    def test_api_error_never_echoes_message_or_key(self):
        client = importer.Client(FAKE_KEY, USER)
        for code, kind in ((10, "authorization"), (26, "authorization"), (6, "account"), (99, "service")):
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                {"error": code, "message": "synthetic-sensitive " + FAKE_KEY}).encode()
            with patch.object(importer.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(importer.ImportFailure) as error:
                    client._request("user.getInfo", {})
            self.assertEqual(error.exception.kind, kind)
            self.assertNotIn(FAKE_KEY, str(error.exception))
            self.assertNotIn("synthetic-sensitive", str(error.exception))

    def test_network_failures_retry_at_most_three_times(self):
        client = importer.Client(FAKE_KEY, USER)
        error = importer.ImportFailure("safe", "network", True)
        with patch.object(client, "_request", side_effect=error) as request, \
                patch.object(importer.time, "sleep"):
            with self.assertRaises(importer.ImportFailure):
                client.request("user.getInfo")
        self.assertEqual(request.call_count, 3)

    def test_long_rate_limit_stops_instead_of_retrying_early(self):
        client = importer.Client(FAKE_KEY, USER)
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid/" + FAKE_KEY,
                                                         429, "synthetic-secret", {"Retry-After": "600"}, None)
        with patch.object(importer.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(importer.ImportFailure) as error:
                client.request("user.getInfo")
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn(FAKE_KEY, str(error.exception))

    def test_account_identity_must_match(self):
        client = importer.Client(FAKE_KEY, USER)
        with patch.object(client, "request", return_value={"user": {"name": "somebody-else", "playcount": "1"}}):
            with self.assertRaises(importer.ImportFailure):
                client.profile()

    def test_invalid_inputs_and_malformed_json(self):
        for key in ("", "short", "x" * 32):
            with self.assertRaises(importer.ImportFailure):
                importer.Client(key, USER)
        with self.assertRaises(importer.ImportFailure):
            importer.Client(FAKE_KEY, "user&api_key=bad")
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"<html>synthetic-secret</html>"
        with patch.object(importer.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(importer.ImportFailure) as error:
                importer.Client(FAKE_KEY, USER).profile()
        self.assertNotIn("synthetic-secret", str(error.exception))


class LastfmImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "history.sqlite3"
        self.private = Path(self.temp.name) / "private"
        self.connection = importer.open_history(self.database)
        self.addCleanup(self.connection.close)
        self.client = MagicMock(user=USER)

    def scalar(self, query):
        return self.connection.execute(query).fetchone()[0]

    def download(self, responses, kind="scrobbles", pages=10, fresh=False):
        self.client.request.side_effect = responses
        with redirect_stdout(io.StringIO()):
            importer.import_history(self.connection, self.client, kind, pages, fresh)

    def test_full_import_preserves_same_second_duplicate_occurrences(self):
        responses = [page([resource(), resource()], total=4, pages=2),
                     page([resource(), resource("Other")], index=2, total=4, pages=2)]
        self.download(responses)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 4)
        self.assertEqual(self.scalar("SELECT MAX(occurrence) FROM evidence"), 3)
        self.assertEqual(self.scalar("SELECT state FROM runs"), "complete")
        self.download(responses)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 4)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runs"), 2)

    def test_resume_uses_saved_page_and_fixed_cutoff(self):
        self.download([page([resource()], total=2, pages=2)], pages=1)
        cutoff = self.scalar("SELECT cutoff FROM runs")
        self.download([page([resource("Other", 200)], index=2, total=2, pages=2)])
        self.assertEqual(self.client.request.call_args.kwargs["page"], 2)
        self.assertEqual(self.client.request.call_args.kwargs["to"], cutoff)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runs"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 2)

    def test_service_failure_keeps_pages_for_resume(self):
        with self.assertRaises(importer.ImportFailure):
            self.download([page([resource()], total=2, pages=2),
                           importer.ImportFailure("safe", "network")])
        self.assertEqual(self.scalar("SELECT next_page FROM runs"), 2)
        self.assertEqual(self.scalar("SELECT error_kind FROM runs"), "network")
        self.download([page([resource("Other")], index=2, total=2, pages=2)])
        self.assertIsNone(self.scalar("SELECT error_kind FROM runs"))
        self.assertEqual(self.scalar("SELECT state FROM runs"), "complete")

    def test_changed_counts_and_fresh_run(self):
        self.download([page([resource()], total=2, pages=2)], pages=1)
        with self.assertRaises(importer.ImportFailure):
            self.download([page([resource("Other")], index=2, total=3, pages=2)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 1)
        self.download([page([resource(), resource("Other")])], fresh=True)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 2)

    def test_repeated_page_and_incomplete_final_counts_fail(self):
        first = page([resource()], total=3, pages=3)
        self.download([first], pages=1)
        with self.assertRaises(importer.ImportFailure):
            self.download([page([resource()], index=2, total=3, pages=3)])
        self.assertEqual(self.scalar("SELECT next_page FROM runs"), 2)
        with self.assertRaises(importer.ImportFailure):
            self.download([page([resource("Other")], index=2, total=3, pages=2)])

    def test_final_page_does_not_claim_complete_when_records_are_missing(self):
        self.download([page([resource()], total=3, pages=2)], pages=1)
        with self.assertRaises(importer.ImportFailure):
            self.download([page([resource("Other")], index=2, total=3, pages=2)])
        self.assertEqual(self.scalar("SELECT state FROM runs"), "partial")
        self.assertEqual(self.scalar("SELECT received FROM runs"), 1)

    def test_page_transaction_rolls_back_all_progress(self):
        self.connection.execute("CREATE TRIGGER fail_page BEFORE INSERT ON pages BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.download([page([resource()])])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM run_events"), 0)
        self.assertEqual(self.scalar("SELECT next_page FROM runs"), 1)

    def test_now_playing_is_excluded_and_empty_history_completes(self):
        now_playing = resource("Currently playing", None)
        now_playing["@attr"] = {"nowplaying": "true"}
        self.download([page([now_playing], total=0, pages=0)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 0)
        self.assertEqual(self.scalar("SELECT state FROM runs"), "complete")

    def test_loved_tracks_are_separate_and_missing_time_is_unknown(self):
        love = resource(timestamp=None)
        love["artist"] = {"name": "Artist"}
        self.download([page([love], kind="loved")], kind="loved")
        self.download([page([resource()])])
        coverage = importer.coverage(self.connection, USER)
        self.assertEqual(coverage["evidence"]["loved"]["records"], 1)
        self.assertIsNone(coverage["evidence"]["loved"]["earliest_utc"])
        self.assertEqual(coverage["evidence"]["scrobbles"]["records"], 1)
        loved_request = self.client.request.call_args_list[0]
        self.assertNotIn("to", loved_request.kwargs)

    def test_different_accounts_remain_separate(self):
        self.download([page([resource()])])
        other = MagicMock(user="other-user")
        response = page([resource()])
        response["recenttracks"]["@attr"]["user"] = "other-user"
        other.request.return_value = response
        with redirect_stdout(io.StringIO()):
            importer.import_history(self.connection, other, "scrobbles")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM evidence"), 2)

    def test_bad_metadata_and_timestamps_are_rejected(self):
        for value in (page([resource(timestamp=None)]), page([resource(timestamp=10 ** 12)]),
                      page([resource(artist="")]), page([resource(title="")])):
            with self.assertRaises(importer.ImportFailure):
                importer.parse_page(value, "scrobbles", USER, 1, 1000)
        wrong_user = page([resource()])
        wrong_user["recenttracks"]["@attr"]["user"] = "somebody-else"
        with self.assertRaises(importer.ImportFailure):
            importer.parse_page(wrong_user, "scrobbles", USER, 1, 1000)

    def test_collector_or_unrelated_database_is_not_modified(self):
        other = Path(self.temp.name) / "other.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE preserve_me (value INTEGER)")
        connection.close()
        with self.assertRaises(importer.ImportFailure):
            importer.open_history(other)
        connection = sqlite3.connect(other)
        self.assertEqual([row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["preserve_me"])
        connection.close()

    def test_setup_saves_only_after_verification_and_hides_key(self):
        args = ["--user", USER, "--private-dir", str(self.private), "setup"]
        with patch.object(importer.sys.stdin, "isatty", return_value=True), \
                patch.object(importer.getpass, "getpass", return_value=FAKE_KEY), \
                patch.object(importer.Client, "profile", return_value={}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(importer.main(args), 0)
        self.assertEqual(importer.read_private(self.private / "lastfm-api-key"), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertEqual((self.private / "lastfm-api-key").stat().st_mode & 0o777, 0o600)
        with patch.object(importer.sys.stdin, "isatty", return_value=True), \
                patch.object(importer.getpass, "getpass", return_value="b" * 32), \
                patch.object(importer.Client, "profile", side_effect=importer.ImportFailure("safe", "authorization")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(importer.main(args), 1)
        self.assertEqual(importer.read_private(self.private / "lastfm-api-key"), FAKE_KEY)

    def test_status_needs_no_key_and_data_survives_reopen(self):
        self.download([page([resource()])])
        self.connection.close()
        with patch.object(importer, "read_private") as read, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(importer.main(["--user", USER, "--database", str(self.database), "status"]), 0)
        read.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["evidence"]["scrobbles"]["records"], 1)

    def test_epoch_dates_are_flagged_without_altering_source_records(self):
        self.download([page([resource("Unknown date A", 1), resource("Unknown date B", 2786),
                             resource("Dated record", 1259128520)])])
        evidence = importer.coverage(self.connection, USER)["evidence"]["scrobbles"]
        self.assertEqual(evidence["records"], 3)
        self.assertEqual(evidence["suspicious_epoch_timestamp_records"], 2)
        self.assertEqual(evidence["missing_timestamp_records"], 0)
        self.assertEqual(evidence["earliest_utc"], importer.utc(1259128520))
        self.assertEqual(self.scalar("SELECT MIN(event_time) FROM evidence"), 1)


if __name__ == "__main__":
    unittest.main()
