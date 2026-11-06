import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

import music_auth as auth


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "private"

    def test_persistent_credentials_and_permissions(self):
        auth.write_private(self.directory, "music-user-token", "synthetic-user-token")
        self.assertEqual(auth.read_private(self.directory / "music-user-token"), "synthetic-user-token")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / "music-user-token").stat().st_mode), 0o600)
        # A separate CLI invocation reloads the stored credential.
        with patch.object(auth, "check_access", return_value=0) as check:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(auth.main(["--credential-dir", str(self.directory), "check"]), 0)
            self.assertEqual(check.call_args.args[0], "synthetic-user-token")

    def test_unsafe_permissions_and_symlinks_rejected(self):
        auth.write_private(self.directory, "token", "synthetic")
        path = self.directory / "token"
        path.chmod(0o644)
        with self.assertRaises(auth.AuthError):
            auth.read_private(path)
        path.chmod(0o600)
        link = self.directory / "link"
        link.symlink_to(path)
        with self.assertRaises(auth.AuthError):
            auth.read_private(link)
        self.directory.chmod(0o755)
        with self.assertRaises(auth.AuthError):
            auth.private_directory(self.directory)

    def test_atomic_failure_preserves_existing_token(self):
        auth.write_private(self.directory, "token", "old")
        with patch.object(auth.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                auth.write_private(self.directory, "token", "new")
        self.assertEqual(auth.read_private(self.directory / "token"), "old")
        self.assertEqual(list(self.directory.iterdir()), [self.directory / "token"])

    def test_failed_import_preserves_existing_token(self):
        auth.write_private(self.directory, "music-user-token", "old")
        with patch.object(auth.sys.stdin, "isatty", return_value=True), \
                patch.object(auth.getpass, "getpass", return_value="new"), \
                patch.object(auth, "check_access", side_effect=auth.AuthError("Access denied")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(auth.main(["--credential-dir", str(self.directory), "import-user-token"]), 1)
        self.assertEqual(auth.read_private(self.directory / "music-user-token"), "old")

    def test_successful_import_validates_before_saving(self):
        with patch.object(auth.sys.stdin, "isatty", return_value=True), \
                patch.object(auth.getpass, "getpass", return_value="synthetic-user"), \
                patch.object(auth, "check_access", return_value=2), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(auth.main(["--credential-dir", str(self.directory), "import-user-token"]), 0)
        self.assertNotIn("synthetic-user", output.getvalue())
        self.assertEqual(auth.read_private(self.directory / "music-user-token"), "synthetic-user")

    def test_invalid_header_values(self):
        for value in ("", "abc\r\ninjected", "a b", "é", "a" * 65537, None):
            with self.subTest(value_type=type(value)), self.assertRaises(auth.AuthError):
                auth.validate_token(value)

    def test_signing_and_expiration_with_synthetic_key(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        auth.write_private(self.directory, "test-key", pem)
        with patch.dict(os.environ, {"APPLE_TEAM_ID": "ABCDEFGHIJ", "APPLE_KEY_ID": "0123456789",
                                     "APPLE_PRIVATE_KEY_PATH": str(self.directory / "test-key")}):
            token = auth.developer_token(lifetime=900)
        decoded = jwt.decode(token, key.public_key(), algorithms=["ES256"])
        self.assertEqual(decoded["exp"] - decoded["iat"], 900)
        self.assertEqual(decoded["iss"], "ABCDEFGHIJ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "0123456789")

    def test_request_uses_headers_timeout_and_redacts_errors(self):
        for code, user, expected in [(401, None, "Developer authorization"),
                                     (403, "synthetic-user", "User access denied"),
                                     (429, "synthetic-user", "rate limit"),
                                     (503, "synthetic-user", "HTTP 503")]:
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://api.music.apple.com/test", code, "synthetic-secret-error", {}, None)
            with patch.object(auth.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(auth.AuthError) as error:
                    auth.request_api("/test", "synthetic-developer", user)
            self.assertIn(expected, str(error.exception))
            self.assertNotIn("synthetic", str(error.exception))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-developer")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(auth.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_empty_developer_probe_then_json_history(self):
        probe, history = MagicMock(), MagicMock()
        probe.status = 200
        probe.read.return_value = b""
        history.read.return_value = b'{"data": [{"id": "synthetic-track"}]}'
        opener = MagicMock()
        opener.open.side_effect = [probe, history]
        probe.__enter__.return_value = probe
        history.__enter__.return_value = history
        with patch.object(auth, "developer_token", return_value="synthetic-developer"), \
                patch.object(auth.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(auth.check_access("synthetic-user"), 1)
        probe.read.assert_not_called()
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(opener.open.call_args_list[0].args[0].full_url,
                         "https://api.music.apple.com/v1/test")
        self.assertEqual(opener.open.call_args_list[1].args[0].get_header("Music-user-token"),
                         "synthetic-user")

    def test_empty_or_invalid_data_response_is_still_an_error(self):
        for raw in (b"", b"<html>synthetic-sensitive-upstream-error</html>"):
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = raw
            with patch.object(auth.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(auth.AuthError) as error:
                    auth.request_api("/v1/me/recent/played/tracks", "synthetic", "synthetic-user")
            self.assertIn("data request", str(error.exception))
            self.assertNotIn("synthetic", str(error.exception))

    def test_empty_developer_probe_requires_success_status(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 204
        with patch.object(auth.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(auth.AuthError):
                auth.request_api("/v1/test", "synthetic", expect_json=False)

    def test_developer_failure_does_not_trigger_user_request(self):
        with patch.object(auth, "developer_token", return_value="synthetic"), \
                patch.object(auth, "request_api", side_effect=auth.AuthError("Developer failed")) as request:
            with self.assertRaises(auth.AuthError):
                auth.check_access("synthetic-user")
        self.assertEqual(request.call_count, 1)

    def test_empty_history_is_valid_authorization(self):
        with patch.object(auth, "developer_token", return_value="synthetic"), \
                patch.object(auth, "request_api", side_effect=[{}, {"data": []}]):
            self.assertEqual(auth.check_access("synthetic-user"), 0)

    def test_malformed_history_is_not_success(self):
        with patch.object(auth, "developer_token", return_value="synthetic"), \
                patch.object(auth, "request_api", side_effect=[{}, {"unexpected": []}]):
            with self.assertRaises(auth.AuthError):
                auth.check_access("synthetic-user")


if __name__ == "__main__":
    unittest.main()
