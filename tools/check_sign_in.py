"""Exercise the desktop sign-in window with disposable homes and a loopback provider."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Packaged executable; defaults to source Electron")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    outcome = {"value": "pending", "starts": 0}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if urlparse(self.path).path == "/oauth/device/code":
                outcome["value"] = "pending"
                outcome["starts"] += 1
                return self.send({"device_code": "synthetic-device", "user_code": "TEST-CODE", "interval": 1,
                                  "expires_in": 600, "verification_uri": "https://oauth.ibroadcast.com/device",
                                  "verification_uri_complete": "https://oauth.ibroadcast.com/device?user_code=TEST-CODE"})
            self.send({}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/_signin/outcome":
                outcome["value"] = json.loads(raw)
                return self.send({"ok": True})
            if self.path == "/oauth/token":
                body = parse_qs(raw.decode())
                if body.get("grant_type") == ["device_code"] and outcome["value"] != "approve":
                    return self.send({"error": "access_denied" if outcome["value"] == "deny"
                                      else "authorization_pending"}, 400)
                return self.send({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600})
            if self.path == "/api/status":
                return self.send({"user": {"id": "synthetic-account", "username": "Demo account", "premium": True},
                                  "status": {"lastmodified": "synthetic-revision"}})
            if self.path == "/library":
                return self.send({"artists": {}, "albums": {}, "tracks": {}})
            self.send({}, 404)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="ibfetch-sign-in-check-") as tmp:
            home = Path(tmp)
            (home / "app-settings.json").write_text(json.dumps({"auto_updates": False}))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CSC_", "WIN_CSC_", "APPLE_", "IBFETCH_"))
                   and key not in ("ELECTRON_RUN_AS_NODE", "IBROADCAST_CLIENT_ID")}
            env.update(IBFETCH_HOME=str(home), IBFETCH_ELECTRON_PROFILE=str(home / "profile"),
                       IBFETCH_PYTHON=sys.executable, IBFETCH_BASE="http://127.0.0.1:" + str(provider.server_port),
                       IBFETCH_SIGN_IN_SMOKE_REPORT=str(args.report), IBROADCAST_CLIENT_ID="synthetic-client")
            command = ([str(args.app.resolve())] if args.app else
                       [str(ROOT / "electron/node_modules/.bin/electron"), str(ROOT / "electron")])
            args.report.unlink(missing_ok=True)
            process = subprocess.Popen(command, env=env, cwd=home,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = process.wait(timeout=110)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
                raise RuntimeError("The isolated sign-in check timed out") from None
            result = json.loads(args.report.read_text()) if args.report.exists() else {"ok": False}
            if code or not result.get("ok"):
                raise RuntimeError("Sign-in check failed: " + json.dumps(result))
            if not (home / "tokens.json").exists():
                raise RuntimeError("Synthetic sign-in was not persisted")
            print("Verified isolated sign-in, cancellation, denial, browser fallback and approval.")
    finally:
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    main()
