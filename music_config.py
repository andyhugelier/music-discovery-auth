"""Nonsecret, persistent runtime settings shared by authorization and collection."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile


DEFAULT_CONFIG = "/app/config/private/apple-music.json"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    team_id: str = ""
    key_id: str = ""
    private_key_path: str = ""
    credential_dir: str = "/app/config/private"
    database_path: str = "/app/data/listening-history.sqlite3"
    poll_seconds: int = 300
    max_backoff_seconds: int = 3600

    def validate(self, require_apple=False):
        for value in (self.team_id, self.key_id):
            if not isinstance(value, str) or (value and not (
                    len(value) == 10 and value.isascii() and value.isalnum())):
                raise ConfigError("Apple Team ID and Key ID must each have 10 alphanumeric characters.")
        for value in (self.private_key_path, self.credential_dir, self.database_path):
            if not isinstance(value, str) or (value and not Path(value).is_absolute()):
                raise ConfigError("Configuration paths must be absolute paths.")
        if not self.credential_dir or not self.database_path:
            raise ConfigError("Credential and database paths are required.")
        if type(self.poll_seconds) is not int or not 30 <= self.poll_seconds <= 86400:
            raise ConfigError("Polling interval must be an integer between 30 and 86400 seconds.")
        if (type(self.max_backoff_seconds) is not int
                or not self.poll_seconds <= self.max_backoff_seconds <= 86400):
            raise ConfigError("Maximum retry delay must be between the polling interval and 86400 seconds.")
        if require_apple and not all((self.team_id, self.key_id, self.private_key_path)):
            raise ConfigError("Configure the Apple Team ID, Key ID and private-key path first.")
        return self


def load_settings(path=None):
    path = Path(path or os.environ.get("MUSIC_CONFIG_PATH", DEFAULT_CONFIG))
    values = {}
    try:
        with path.open() as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ConfigError("Runtime configuration is too large.")
        values = json.loads(raw)
    except FileNotFoundError:
        pass  # Preserve compatibility with the existing environment-only CLI.
    except (OSError, ValueError):
        raise ConfigError("Cannot read runtime configuration; check its permissions and JSON format.") from None
    if not isinstance(values, dict) or set(values) - set(Settings.__dataclass_fields__):
        raise ConfigError("Runtime configuration contains unsupported fields; never store tokens here.")
    for environment, field in (("APPLE_TEAM_ID", "team_id"), ("APPLE_KEY_ID", "key_id"),
                               ("APPLE_PRIVATE_KEY_PATH", "private_key_path")):
        if environment in os.environ:
            values[field] = os.environ[environment]
    return Settings(**values).validate()


def save_settings(settings, path):
    """Atomic nonsecret config write. Preserve the old file if writing fails."""
    settings.validate(require_apple=True)
    path = Path(path)
    if not path.is_absolute():
        raise ConfigError("Configuration file path must be absolute.")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(settings), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ConfigError("Cannot save runtime configuration; check directory ownership and permissions.") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
