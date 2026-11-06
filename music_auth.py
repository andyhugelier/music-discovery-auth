"""NAS authorization tools. Never print credentials or upstream error bodies."""

import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from music_config import ConfigError, DEFAULT_CONFIG, load_settings, save_settings


class AuthError(Exception):
    """A safe, user-facing error."""

    def __init__(self, message, *, kind="authorization", retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def validate_token(token):
    if (not isinstance(token, str) or not token or len(token) > 65536
            or not token.isascii() or any(c.isspace() for c in token)
            or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise AuthError("Invalid token format; nothing was saved.")
    return token


def read_private(path):
    """Refuse symlinks, non-files and credentials accessible to other users."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise AuthError("Credential files must be owned by the runtime user and have mode 600 or 400.")
            value = stream.read(65537)
            if len(value) > 65536:
                raise AuthError("Credential file is too large.")
            return value
    except (OSError, UnicodeError):
        raise AuthError("Cannot read credential file; check its path, owner and permissions.") from None


def private_directory(directory):
    directory = Path(directory)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise AuthError("Credential directory must be owned by the runtime user and have mode 700.")
        return directory
    except OSError:
        raise AuthError("Cannot access the private credential directory.") from None


def write_private(directory, name, value):
    directory = private_directory(directory)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=directory)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def developer_token(lifetime=3600, settings=None):
    import jwt

    settings = settings or load_settings()
    team = settings.team_id
    key_id = settings.key_id
    key_path = settings.private_key_path
    if not all(len(value) == 10 and value.isascii() and value.isalnum()
               for value in (team, key_id)) or not key_path:
        raise AuthError("Set APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_PRIVATE_KEY_PATH.")
    issued = int(time.time())
    try:
        return jwt.encode(
            {"iss": team, "iat": issued, "exp": issued + lifetime},
            read_private(key_path), algorithm="ES256", headers={"kid": key_id},
        )
    except AuthError:
        raise
    except Exception:
        raise AuthError("Could not sign developer token; check the MusicKit key configuration.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_after_seconds(value):
    """Interpret Retry-After without retaining or logging upstream headers."""
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(1, min(seconds, 86400))


def request_api(path, developer, user=None, *, expect_json=True):
    headers = {"Authorization": "Bearer " + validate_token(developer)}
    if user is not None:
        headers["Music-User-Token"] = validate_token(user)
    request = urllib.request.Request("https://api.music.apple.com" + path, headers=headers)
    # Never forward bearer credentials to a redirect target.
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if not expect_json:
                # Apple's /v1/test documents HTTP 200 with EmptyBodyResponse.
                # Its success status validates developer access; there is no JSON to parse.
                if response.status != 200:
                    raise AuthError("Developer-token check returned an unexpected HTTP status.")
                return None
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise AuthError("Apple returned an unexpectedly large response.")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            if user is None:
                raise AuthError("Developer authorization failed; check the key, team, permissions and NAS clock.",
                                kind="developer_authorization") from None
            raise AuthError("User access denied; reconnect and import a new user token, and check your subscription.",
                            kind="user_authorization") from None
        if error.code == 429:
            raise AuthError("Apple rate limit reached; wait before retrying.", kind="rate_limit",
                            retry_after=retry_after_seconds(error.headers.get("Retry-After"))) from None
        raise AuthError("Apple request failed (HTTP %d); no credentials were changed." % error.code,
                        kind="api") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AuthError("Could not reach Apple securely; check connectivity and try again.", kind="network") from None
    except (ValueError, UnicodeError):
        raise AuthError("Apple returned an unreadable JSON response for the data request.", kind="response") from None


def check_access(user, settings=None):
    developer = developer_token(settings=settings) if settings is not None else developer_token()
    request_api("/v1/test", developer, expect_json=False)
    response = request_api("/v1/me/recent/played/tracks?limit=10", developer, user)
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise AuthError("Apple returned an unexpected recent-tracks response.")
    return len(response["data"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("MUSIC_CONFIG_PATH", DEFAULT_CONFIG))
    parser.add_argument("--credential-dir", default=None)
    parser.add_argument("command", choices=["configure", "import-user-token", "check", "browser-token", "forget"])
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "configure":
            if not sys.stdin.isatty():
                raise AuthError("Configuration requires an interactive terminal.")
            # Only nonsecret IDs/paths belong in this file; no token input here.
            def prompt(label, current):
                return input(label + (" [Enter keeps existing]" if current else "") + ": ").strip() or current
            settings = replace(settings,
                               team_id=prompt("Apple Team ID", settings.team_id),
                               key_id=prompt("Apple Key ID", settings.key_id),
                               private_key_path=prompt("Private-key path inside the container", settings.private_key_path))
            save_settings(settings, args.config)
            print("Saved nonsecret runtime configuration. No tokens were stored in this file.")
            return 0
        directory = private_directory(args.credential_dir or settings.credential_dir)
        if args.command == "import-user-token":
            if not sys.stdin.isatty():
                raise AuthError("Import requires an interactive terminal; use SSH and docker exec -it.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = validate_token(getpass.getpass("Paste Music User Token (hidden): "))
            count = check_access(token, settings)
            write_private(directory, "music-user-token", token)
            print("Authorization verified and saved; %d recent tracks returned." % count)
        elif args.command == "check":
            count = check_access(validate_token(read_private(directory / "music-user-token")), settings)
            print("NAS authorization works; %d recent tracks returned." % count)
        elif args.command == "browser-token":
            write_private(directory, "browser-developer-token", developer_token(lifetime=900, settings=settings))
            print("Saved a 15-minute browser developer token in the private credential directory.")
        else:
            (directory / "music-user-token").unlink(missing_ok=True)
            print("Removed the NAS user token. This does not revoke Apple's authorization.")
        return 0
    except AuthError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Canceled; authorization was not updated.", file=sys.stderr)
        return 1
    except Exception:
        print("Authorization operation failed; check dependencies, storage and permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
