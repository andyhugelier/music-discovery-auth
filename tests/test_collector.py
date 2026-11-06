from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import collector
import music_auth
from music_config import ConfigError, Settings, load_settings, save_settings


def track(identifier="a", resource_type="songs"):
    return {"id": identifier, "type": resource_type,
            "attributes": {"name": "A song", "artistName": "An artist", "albumName": "Album",
                           "durationInMillis": 120000, "isrc": "SYNTHETIC",
                           "url": "https://example.invalid/synthetic-sensitive-url"}}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_persistent_configuration_and_environment_override(self):
        settings = Settings(team_id="ABCDEFGHIJ", key_id="0123456789", private_key_path="/run/secrets/test.p8")
        save_settings(settings, self.path)
        self.assertEqual(load_settings(self.path), settings)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {"APPLE_TEAM_ID": "KLMNOPQRST"}):
            self.assertEqual(load_settings(self.path).team_id, "KLMNOPQRST")

    def test_bad_configuration_is_rejected_without_echoing_values(self):
        for content in [{"music_user_token": "synthetic-secret"}, {"poll_seconds": True},
                        {"poll_seconds": 0}, {"database_path": "relative.db"},
                        {"team_id": "synthetic-secret"}, [], {"max_backoff_seconds": 10}]:
            self.path.write_text(json.dumps(content))
            with self.assertRaises(ConfigError) as error:
                load_settings(self.path)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_invalid_configuration_does_not_overwrite_existing(self):
        self.path.write_text("existing")
        with self.assertRaises(ConfigError):
            save_settings(Settings(), self.path)
        self.assertEqual(self.path.read_text(), "existing")

    def test_configure_command_persists_without_credentials(self):
        with patch.object(music_auth.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["ABCDEFGHIJ", "0123456789", "/run/secrets/test.p8"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(music_auth.main(["--config", str(self.path), "configure"]), 0)
        self.assertEqual(load_settings(self.path).team_id, "ABCDEFGHIJ")
        self.assertNotIn("music_user_token", self.path.read_text())


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=str(Path(self.temp.name) / "history.sqlite3"))
        self.connection = collector.open_database(self.settings.database_path)
        self.addCleanup(self.connection.close)
        self.items = collector.normalize_tracks({"data": [track("a"), track("b")]})

    def scalar(self, sql):
        return self.connection.execute(sql).fetchone()[0]

    def test_unchanged_snapshots_deduplicate_storage_not_polls(self):
        now = time.time()
        self.assertTrue(collector.save_success(self.connection, self.items, now, now, now + 300))
        self.assertFalse(collector.save_success(self.connection, self.items, now + 1, now + 1, now + 301))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM poll_runs"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshot_items"), 2)
        self.assertNotIn("synthetic-sensitive-url", self.scalar("SELECT metadata_json FROM snapshot_items LIMIT 1"))

    def test_order_repeats_and_resource_types_are_preserved(self):
        items = collector.normalize_tracks({"data": [track("a"), track("a"), track("a", "library-songs")]})
        collector.save_success(self.connection, items, 1, 2, 302)
        collector.save_success(self.connection, list(reversed(items)), 3, 4, 304)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshot_items"), 6)
        self.assertEqual(self.scalar("SELECT apple_type FROM snapshot_items WHERE snapshot_id=1 AND position=2"), "library-songs")

    def test_a_b_a_snapshot_sequence_is_not_lost(self):
        for index, items in enumerate([self.items, list(reversed(self.items)), self.items]):
            collector.save_success(self.connection, items, index, index, index + 300)
        rows = self.connection.execute("SELECT snapshot_id FROM poll_runs ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2, 1])

    def test_success_writes_are_transactional(self):
        self.connection.execute("CREATE TRIGGER fail_poll BEFORE INSERT ON poll_runs BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            collector.save_success(self.connection, self.items, 1, 2, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshots"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM snapshot_items"), 0)

    def test_empty_history_is_a_successful_observation(self):
        with patch.object(collector, "fetch_tracks", return_value=[]):
            self.assertEqual(collector.poll_once(self.connection, self.settings), (0, 300))
        self.assertTrue(collector.status(self.settings)["healthy"])
        self.assertEqual(self.scalar("SELECT item_count FROM poll_runs"), 0)

    def test_failure_is_recorded_and_log_is_redacted(self):
        error = music_auth.AuthError("synthetic-secret", kind="network")
        with patch.object(collector, "fetch_tracks", side_effect=error), self.assertLogs(collector.LOG, "WARNING") as logs:
            failures, delay = collector.poll_once(self.connection, self.settings, failures=1)
        self.assertEqual((failures, delay), (2, 600))
        self.assertNotIn("synthetic-secret", str(logs.output))
        self.assertEqual(self.scalar("SELECT error_kind FROM poll_runs"), "network")
        self.assertFalse(collector.status(self.settings)["healthy"])

    def test_backoff_cap_authorization_delay_and_retry_after(self):
        self.assertEqual(collector.retry_delay(self.settings, 100, music_auth.AuthError("", kind="network")), 3600)
        self.assertEqual(collector.retry_delay(self.settings, 1, music_auth.AuthError("", kind="user_authorization")), 3600)
        self.assertEqual(collector.retry_delay(self.settings, 1, music_auth.AuthError("", kind="rate_limit", retry_after=7200)), 7200)
        self.assertEqual(music_auth.retry_after_seconds("600"), 600)
        self.assertIsNone(music_auth.retry_after_seconds("synthetic-not-a-date"))

    def test_new_success_resets_backoff(self):
        with patch.object(collector, "fetch_tracks", return_value=self.items):
            self.assertEqual(collector.poll_once(self.connection, self.settings, failures=6), (0, 300))

    def test_health_detects_stale_collection_and_failure_after_success(self):
        now = time.time()
        collector.save_success(self.connection, self.items, now - 1000, now - 1000, now - 700)
        self.assertFalse(collector.status(self.settings)["healthy"])
        collector.save_success(self.connection, self.items, now, now, now + 300)
        self.assertTrue(collector.status(self.settings)["healthy"])
        collector.save_failure(self.connection, "network", now, now, now + 300)
        self.assertFalse(collector.status(self.settings)["healthy"])

    def test_second_collector_is_rejected_and_lock_releases(self):
        with collector.collector_lock(self.settings.database_path):
            with self.assertRaises(collector.CollectorError):
                with collector.collector_lock(self.settings.database_path):
                    self.fail("Acquired duplicate lock")
        with collector.collector_lock(self.settings.database_path):
            pass

    def test_restart_honors_persisted_next_attempt(self):
        now = time.time()
        collector.save_failure(self.connection, "rate_limit", now, now, now + 7200)
        stop = unittest.mock.Mock()
        stop.wait.return_value = True
        with patch.object(collector, "fetch_tracks") as fetch:
            self.assertEqual(collector.run(self.settings, stop), 0)
        self.assertGreater(stop.wait.call_args.args[0], 7100)
        fetch.assert_not_called()

    def test_one_off_run_and_database_reopen(self):
        with patch.object(collector, "fetch_tracks", return_value=self.items):
            self.assertEqual(collector.run(self.settings, threading.Event(), once=True), 0)
        self.assertEqual(collector.status(self.settings)["successful_polls"], 1)

    def test_graceful_stop_releases_database_lock(self):
        stop = unittest.mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(collector, "fetch_tracks", return_value=self.items):
            self.assertEqual(collector.run(self.settings, stop), 0)
        with collector.collector_lock(self.settings.database_path):
            pass

    def test_online_backup_can_be_restored_and_never_overwrites(self):
        now = time.time()
        collector.save_success(self.connection, self.items, now, now, now + 300)
        destination = str(Path(self.temp.name) / "backup.sqlite3")
        collector.backup(self.settings, destination)
        restored = collector.status(replace(self.settings, database_path=destination))
        self.assertEqual(restored["successful_polls"], 1)
        self.assertEqual(restored["unique_snapshots"], 1)
        with self.assertRaises(FileExistsError):
            collector.backup(self.settings, destination)

    def test_malformed_resources_are_rejected(self):
        for response in ({}, {"data": [None]}, {"data": [{"id": "a"}]},
                         {"data": [{"id": "a", "type": "songs", "attributes": []}]}):
            with self.assertRaises(music_auth.AuthError):
                collector.normalize_tracks(response)

    def test_credentials_are_reloaded_each_poll(self):
        private = Path(self.temp.name) / "private"
        music_auth.write_private(private, "music-user-token", "synthetic-old")
        settings = replace(self.settings, credential_dir=str(private), team_id="ABCDEFGHIJ",
                           key_id="0123456789", private_key_path="/run/secrets/test.p8")
        with patch.object(music_auth, "developer_token", return_value="synthetic-developer"), \
                patch.object(music_auth, "request_api", side_effect=[None, {"data": []}, None, {"data": []}]) as request:
            collector.fetch_tracks(settings)
            music_auth.write_private(private, "music-user-token", "synthetic-new")
            collector.fetch_tracks(settings)
        self.assertEqual(request.call_args_list[1].args[2], "synthetic-old")
        self.assertEqual(request.call_args_list[3].args[2], "synthetic-new")

    def test_real_process_shutdown_restart_and_health_command(self):
        # Exercise actual signals and CLI configuration without contacting Apple.
        config = Path(self.temp.name) / "runtime.json"
        save_settings(replace(self.settings, team_id="ABCDEFGHIJ", key_id="0123456789",
                              private_key_path="/run/secrets/test.p8"), config)
        worker = [sys.executable, "-c",
                  "import collector; collector.fetch_tracks=lambda settings: []; raise SystemExit(collector.main())",
                  "--config", str(config), "run"]
        for iteration in range(2):
            process = subprocess.Popen(worker, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Collector process exited unexpectedly")
                    if collector.status(self.settings)["successful_polls"] == 1:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Collector did not persist its first poll")
                if iteration:
                    time.sleep(0.1)  # New process must honor the existing next-attempt time.
                health = subprocess.run([sys.executable, "collector.py", "--config", str(config), "healthcheck"],
                                        capture_output=True, timeout=5)
                self.assertEqual(health.returncode, 0)
                self.assertEqual(collector.status(self.settings)["successful_polls"], 1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, "SIGTERM must close cleanly")


if __name__ == "__main__":
    unittest.main()
