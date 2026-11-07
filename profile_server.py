"""Loopback-only private taste review. Serves allowlisted assets, never Data files."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

from collector import collector_lock, CollectorError
import taste_profile as taste


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def make_server(database, port=8765):
    database = Path(database)
    if not database.is_file():
        raise taste.ProfileError("Build the taste profile before starting the review app.")
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Search strings, artist names and private request paths are not logged.

        def valid_host(self):
            return self.headers.get("Host") in {"127.0.0.1:%d" % self.server.server_port,
                                                "localhost:%d" % self.server.server_port}

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = json.dumps(value).encode() if content_type.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "Unsupported host."})
            target = urlsplit(self.path)
            if target.path in ASSETS:
                filename, content_type = ASSETS[target.path]
                return self.respond(200, (taste.ROOT / "review" / filename).read_bytes(), content_type)
            if target.path not in {"/api/profile", "/api/tracks"}:
                return self.respond(404, {"error": "Not found."})
            c = None
            try:
                c = taste.connect(database)
                if target.path == "/api/profile":
                    return self.respond(200, {"profile": taste.profile(c), "csrf": csrf})
                query = parse_qs(target.query, max_num_fields=10)
                value = lambda k, default="": query.get(k, [default])[0]
                offset = int(value("offset", "0"))
                if not 0 <= offset <= 1000000:
                    raise ValueError()
                return self.respond(200, taste.tracks(c, group=value("group") or None,
                                     query=value("q"), excluded=value("excluded") == "1", offset=offset))
            except (taste.ProfileError, ValueError):
                return self.respond(400, {"error": "Invalid profile request."})
            except (OSError, sqlite3.Error):
                return self.respond(503, {"error": "Profile unavailable. Finish any active build, then retry."})
            finally:
                if c is not None:
                    c.close()

        def do_POST(self):
            expected_origin = "http://" + self.headers.get("Host", "")
            if (not self.valid_host() or self.headers.get("Origin") != expected_origin or
                    not hmac.compare_digest(self.headers.get("X-Review-CSRF", ""), csrf)):
                return self.respond(403, {"error": "Open the local review app to make changes."})
            if self.path != "/api/actions":
                return self.respond(404, {"error": "Not found."})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "Expected JSON."})
            c = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
                with collector_lock(database):
                    c = taste.connect(database)
                    value = taste.act(c, body.get("kind"), body.get("target"), body.get("value", ""))
                    return self.respond(200, {"profile": value})
            except taste.ProfileError as error:
                return self.respond(400, {"error": str(error)})
            except (ValueError, TypeError):
                return self.respond(400, {"error": "Invalid review action."})
            except (CollectorError, OSError, sqlite3.Error):
                return self.respond(503, {"error": "Profile busy or unavailable. Retry after the active operation."})
            finally:
                if c is not None:
                    c.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(taste.ROOT / "Data/taste-profile.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    os.umask(0o077)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port from 1024 to 65535.")
    server = make_server(args.database, args.port)
    print("Private taste review: http://127.0.0.1:%d (Ctrl+C to stop)" % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
