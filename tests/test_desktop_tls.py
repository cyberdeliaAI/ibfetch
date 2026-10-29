"""Desktop trust-bundle regressions against an isolated HTTPS server, never an account."""

import os
from pathlib import Path
import runpy
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

import ibfetch

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests/fixtures/tls"
HOOK = ROOT / "tools/desktop_certificates.py"


class DesktopTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        cls.server = ibfetch.LocalHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(FIXTURES / "localhost.pem"), str(FIXTURES / "localhost-key.pem"))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name) / "certificates/cacert.pem"
        self.bundle.parent.mkdir()
        shutil.copy2(FIXTURES / "ca.pem", self.bundle)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            os.environ.pop(key, None)
        # Keep all requests loopback-only even on a runner with a configured proxy.
        os.environ["no_proxy"] = "localhost,127.0.0.1"

    def bootstrap(self):
        with patch.object(sys, "_MEIPASS", self.tmp.name, create=True):
            runpy.run_path(str(HOOK))

    def request(self, hostname="localhost"):
        # A real frozen process configures trust before urllib's first cached opener.
        with patch.object(ibfetch.urllib.request, "_opener", None):
            with ibfetch.http("GET", "https://" + hostname + ":" + str(self.port) + "/", timeout=5) as response:
                return response.read()

    def test_bundled_roots_repair_https_without_trusting_the_builder_installation(self):
        with self.assertRaises(urllib.error.URLError):
            self.request()
        self.bootstrap()
        self.assertEqual(os.environ["SSL_CERT_FILE"], str(self.bundle))
        self.assertEqual(self.request(), b"ok")

    def test_hostname_verification_remains_enabled(self):
        self.bootstrap()
        with self.assertRaises(urllib.error.URLError) as caught:
            self.request("127.0.0.1")
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)

    def test_explicit_trusted_file_is_preserved_even_without_a_bundled_file(self):
        explicit = str(FIXTURES / "ca.pem")
        os.environ["SSL_CERT_FILE"] = explicit
        self.bundle.unlink()
        self.bootstrap()
        self.assertEqual(os.environ["SSL_CERT_FILE"], explicit)
        self.assertEqual(self.request(), b"ok")

    def test_explicit_trusted_directory_is_preserved(self):
        os.environ["SSL_CERT_DIR"] = self.tmp.name
        self.bundle.unlink()
        self.bootstrap()
        self.assertNotIn("SSL_CERT_FILE", os.environ)
        self.assertEqual(os.environ["SSL_CERT_DIR"], self.tmp.name)

    def test_missing_or_invalid_bundles_fail_closed(self):
        self.bundle.unlink()
        with self.assertRaisesRegex(RuntimeError, "certificate bundle is missing"):
            self.bootstrap()
        self.bundle.write_text("not a certificate")
        with self.assertRaises(ssl.SSLError):
            self.bootstrap()
        self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_certificate_failures_are_distinguished_without_exposing_request_data(self):
        reason = ssl.SSLCertVerificationError(1, "private URL or credential must not be shown")
        error = urllib.error.URLError(reason)
        for invoke in (lambda: ibfetch.device_start("synthetic-client"),
                       lambda: ibfetch.oauth_post("token", {"refresh_token": "synthetic-secret"})):
            with patch.object(ibfetch, "http", side_effect=error):
                with self.assertRaisesRegex(ibfetch.Fail, "HTTPS certificate") as caught:
                    invoke()
            self.assertNotIn("private URL", str(caught.exception))
            self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(ibfetch.connection_error(urllib.error.URLError("private details")),
                         "Could not reach iBroadcast (URLError).")

