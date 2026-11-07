"""Renewal outages must keep both disk and memory credentials usable for a retry."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ibfetch


class SessionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"
        self.saved = {"access_token":"synthetic-access", "refresh_token":"synthetic-refresh",
                      "client_id":"synthetic-client", "expires_at":0}
        self.path.write_text(json.dumps(self.saved))
        self.session = ibfetch.Session(self.path.parent)

    def test_temporary_errors_and_incomplete_responses_allow_later_renewal(self):
        for reply in ((429, {}), (503, {"error":"server_error"}), (200, {}), (200, {"access_token": []})):
            with self.subTest(reply=reply), patch.object(ibfetch, "oauth_post", return_value=reply):
                with self.assertRaisesRegex(ibfetch.Fail, "temporarily unavailable.*sign-in is retained"):
                    self.session.token()
            self.assertEqual(self.session.tokens, self.saved)
            self.assertEqual(json.loads(self.path.read_text()), self.saved)
        with patch.object(ibfetch, "oauth_post", return_value=(200, {"access_token":"synthetic-new", "expires_in":3600})):
            self.assertEqual(self.session.token(), "synthetic-new")
        self.assertEqual(self.session.tokens["refresh_token"], "synthetic-refresh")
        self.assertEqual(json.loads(self.path.read_text())["access_token"], "synthetic-new")

    def test_transport_and_tls_failures_preserve_credentials_without_bypassing_validation(self):
        for error in (ibfetch.Fail("Could not reach iBroadcast"), ibfetch.Fail("Could not verify HTTPS certificate")):
            with self.subTest(error=str(error)), patch.object(ibfetch, "oauth_post", side_effect=error):
                with self.assertRaises(ibfetch.Fail):
                    self.session.token()
            self.assertEqual(json.loads(self.path.read_text()), self.saved)
            self.assertEqual(self.session.tokens, self.saved)

    def test_permanent_rejection_requires_sign_in_without_deleting_another_sessions_data(self):
        with patch.object(ibfetch, "oauth_post", return_value=(400, {"error":"invalid_grant"})):
            with self.assertRaisesRegex(ibfetch.Fail, "session expired"):
                self.session.token()
        self.assertEqual(json.loads(self.path.read_text()), self.saved)
