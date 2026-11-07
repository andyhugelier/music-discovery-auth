from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import spotify_import as importer


def event(**changes):
    result = {"ts": "2024-01-01T00:00:00Z", "ms_played": 120000,
              "master_metadata_track_name": "Synthetic Track",
              "master_metadata_album_artist_name": "Synthetic Artist",
              "master_metadata_album_album_name": "Synthetic Album",
              "spotify_track_uri": "spotify:track:" + "A" * 22,
              "skipped": False, "incognito_mode": False, "shuffle": None,
              "ip_addr": "synthetic-private-ip", "platform": "synthetic-private-device"}
    result.update(changes)
    return result


class SpotifyImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "history.sqlite3"
        self.connection = importer.open_database(self.database)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def write(self, rows, name="Streaming_History_Audio_2024.json"):
        path = self.source / name
        path.write_text(json.dumps(rows))
        return path

    def count(self, table):
        return self.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_import_reimport_provenance_and_no_raw_private_fields(self):
        self.write([event(), event(ts="2024-01-02T00:00:00Z")])
        first = importer.import_directory(self.connection, self.source)
        again = importer.import_directory(self.connection, self.source)
        self.assertEqual((first["new_events"], again["new_events"]), (2, 0))
        self.assertEqual((self.count("events"), self.count("origins")), (2, 2))
        self.assertEqual(self.count("batches"), 2)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        dump = "\n".join(self.connection.iterdump())
        for forbidden in ("synthetic-private-ip", "synthetic-private-device", "ip_addr", "platform"):
            self.assertNotIn(forbidden, dump)
        self.assertEqual(os.stat(self.database).st_mode & 0o777, 0o600)

    def test_same_timestamp_duplicates_and_partitioned_delivery_preserved(self):
        self.write([event(), event()])
        self.write([event()], "Streaming_History_Audio_2024_1.json")
        self.assertEqual(importer.import_directory(self.connection, self.source)["new_events"], 3)
        self.assertEqual(importer.import_directory(self.connection, self.source)["new_events"], 0)
        self.assertEqual(self.count("events"), 3)

    def test_identical_payload_copy_not_double_counted(self):
        self.write([event()])
        self.write([event()], "Streaming_History_Audio_2025.json")
        report = importer.inspect(self.source)
        self.assertEqual(report["distinct_payload_files"], 1)
        self.assertEqual(report["rows"], 1)
        self.assertEqual(importer.import_directory(self.connection, self.source)["new_events"], 1)

    def test_overlap_new_delivery_and_different_account(self):
        path = self.write([event()])
        importer.import_directory(self.connection, self.source)
        path.unlink()
        self.write([event(), event(ts="2025-02-01T00:00:00Z")], "Streaming_History_Audio_2025.json")
        report = importer.import_directory(self.connection, self.source)
        self.assertEqual(report["new_events"], 1)
        self.assertEqual(self.count("events"), 2)
        self.assertEqual(self.count("origins"), 3)
        self.assertEqual(importer.import_directory(self.connection, self.source, account="second")["new_events"], 2)

    def test_changed_location_device_fields_do_not_create_new_event(self):
        path = self.write([event()])
        importer.import_directory(self.connection, self.source)
        path.write_text(json.dumps([event(ip_addr="another-private-ip", platform="another-device")]))
        self.assertEqual(importer.import_directory(self.connection, self.source)["new_events"], 0)
        self.assertEqual(self.count("origins"), 2)

    def test_media_nonmusic_and_nullable_flags_are_separate(self):
        self.write([event(ms_played=0, skipped=None), event(spotify_track_uri=None,
                    master_metadata_track_name=None, master_metadata_album_artist_name=None,
                    spotify_episode_uri="spotify:episode:" + "B" * 22, episode_name="Example Episode")])
        self.write([event(incognito_mode=True)], "Streaming_History_Video_2024.json")
        importer.import_directory(self.connection, self.source)
        report = importer.summary(self.connection)
        self.assertEqual(report["audio_music"]["records"], 1)
        self.assertEqual(report["audio_music"]["skipped_unknown"], 1)
        self.assertEqual(report["audio_music"]["zero_ms_records"], 1)
        self.assertEqual(len(report["coverage"]), 3)
        video = self.connection.execute("SELECT incognito FROM events WHERE media='video'").fetchone()[0]
        self.assertEqual(video, 1)

    def test_late_invalid_file_rolls_back_whole_delivery(self):
        self.write([event()])
        self.write([event(ms_played=-1)], "Streaming_History_Audio_2025.json")
        with self.assertRaises(importer.ImportFailure):
            importer.import_directory(self.connection, self.source)
        for table in ("events", "origins", "files", "batches", "batch_files"):
            self.assertEqual(self.count(table), 0)

    def test_timezones_boolean_duration_and_bad_input_validation(self):
        one, fingerprint = importer.normalize_row(event(), "audio")
        two, other = importer.normalize_row(event(ts="2023-12-31T19:00:00-05:00"), "audio")
        self.assertEqual(fingerprint, other)
        for changes in ({"ms_played": True}, {"ts": "2024-01-01"}, {"skipped": "false"},
                        {"spotify_track_uri": "file:///private/something"}):
            with self.assertRaises(importer.ImportFailure):
                importer.normalize_row(event(**changes), "audio")
        self.assertEqual(one["ended_utc"], two["ended_utc"])

    def test_rejects_foreign_database_and_source_symlinks(self):
        other = self.root / "other.sqlite3"
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE unrelated(value)")
        c.close()
        other.chmod(0o600)
        with self.assertRaises(importer.ImportFailure):
            importer.open_database(other)
        original = self.root / "original.json"
        original.write_text(json.dumps([event()]))
        self.source.joinpath("Streaming_History_Audio_2024.json").symlink_to(original)
        with self.assertRaises(OSError):
            importer.inspect(self.source)

    def test_writer_lock_and_reopen(self):
        with importer.database_lock(self.database):
            with self.assertRaises(importer.ImportFailure):
                with importer.database_lock(self.database):
                    self.fail("Second writer acquired lock")
        self.write([event()])
        importer.import_directory(self.connection, self.source)
        c = importer.open_database(self.database, readonly=True)
        try:
            self.assertEqual(importer.summary(c)["audio_music"]["records"], 1)
        finally:
            c.close()

    def test_cli_does_not_echo_raw_json_errors(self):
        self.source.joinpath("Streaming_History_Audio_2024.json").write_text('{"secret-input": broken')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = importer.main(["inspect", str(self.source)])
        self.assertEqual(status, 1)
        self.assertNotIn("secret-input", out.getvalue() + err.getvalue())
        self.assertNotIn(str(self.source), err.getvalue())


if __name__ == "__main__":
    unittest.main()
