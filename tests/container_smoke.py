"""Run via stdin inside the built image with networking disabled and synthetic data."""

import errno
import json
import os
from pathlib import Path
import platform
import resource
import ssl
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import collector
import music_auth
from music_config import Settings, save_settings


assert os.geteuid() == 1027
assert platform.machine() == "x86_64"
assert Path(ssl.get_default_verify_paths().cafile).is_file()
try:
    Path("/app/should-not-be-writable").touch()
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError("Image root filesystem should be read-only")

private = Path("/app/data/private")
key = ec.generate_private_key(ec.SECP256R1())
music_auth.write_private(private, "synthetic-key.p8", key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode())
music_auth.write_private(private, "music-user-token", "synthetic-user-token")
settings = Settings(team_id="ABCDEFGHIJ", key_id="0123456789",
                    private_key_path=str(private / "synthetic-key.p8"),
                    credential_dir=str(private))
configuration = private / "apple-music.json"
save_settings(settings, configuration)
response = {"data": [{"id": "synthetic-track", "type": "songs",
                       "attributes": {"name": "Synthetic track"}}]}
with patch.object(music_auth, "request_api", side_effect=[None, response, None, response]):
    for attempt in range(2):
        assert collector.main(["--config", str(configuration), "run", "--once"]) == 0
assert collector.main(["--config", str(configuration), "healthcheck"]) == 0
result = collector.status(settings)
assert result["successful_polls"] == 2 and result["unique_snapshots"] == 1
assert collector.main(["--config", str(configuration), "backup", "--output", "/app/data/backup.sqlite3"]) == 0
print(json.dumps({"container_smoke": "passed", "architecture": platform.machine(),
                  "python": platform.python_version(), "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
