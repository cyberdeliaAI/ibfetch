"""Sign-in window lifecycle regressions; all credentials and homes are synthetic."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ibfetch as app


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = app.App(self.home)
        self.addCleanup(self.state.shutdown)
        self.device = {"device_code": "synthetic-private-device", "user_code": "TEST-CODE",
                       "verification_uri": "https://oauth.ibroadcast.com/device", "expires_in": 600}

    def begin(self):
        with patch.object(app, "device_start", return_value=self.device), patch.object(app.threading, "Thread"):
            return self.state.start_login()

    def test_public_login_has_only_user_code_url_expiry_and_opaque_request_id(self):
        login = self.begin()
        self.assertEqual(login["state"], "waiting")
        self.assertEqual(login["code"], "TEST-CODE")
        self.assertGreater(login["expires_at"], time.time())
        self.assertRegex(login["request_id"], r"^[A-Za-z0-9_-]{24}$")
        self.assertNotIn("synthetic-private-device", json.dumps(self.state.state()))
        self.assertFalse((self.home / "tokens.json").exists())

    def test_old_window_close_cannot_cancel_a_new_attempt(self):
        old = self.begin()
        newer = self.begin()
        self.assertNotEqual(old["request_id"], newer["request_id"])
        self.assertFalse(self.state.cancel_login(old["request_id"]))
        self.assertEqual(self.state.login, newer)
        self.assertFalse(self.state.login_cancel.is_set())
        self.assertTrue(self.state.cancel_login(newer["request_id"]))
        self.assertTrue(self.state.login_cancel.is_set())
        self.assertIsNone(self.state.login)

    def test_cancel_during_start_does_not_publish_a_late_window(self):
        entered, release = threading.Event(), threading.Event()
        def start(_client):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test start did not finish")
            return self.device
        with patch.object(app, "device_start", side_effect=start), patch.object(self.state, "_poll_login") as poll:
            worker = threading.Thread(target=self.state.start_login)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                pending = self.state.state()["login"]
                self.assertEqual(pending["state"], "starting")
                self.state.cancel_login(pending["request_id"])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            poll.assert_not_called()
        self.assertIsNone(self.state.login)

    def test_closing_a_late_window_never_revokes_an_approved_session(self):
        login = self.begin()
        with patch.object(app, "device_poll", return_value={"access_token": "synthetic-access"}), \
                patch.object(self.state, "load_library"):
            self.state._poll_login("synthetic-client", self.device, self.state.login_cancel,
                                   self.state.login_generation)
        self.assertIsNotNone(self.state.session)
        self.assertIsNone(self.state.login)
        self.assertFalse(self.state.cancel_login(login["request_id"]))
        self.assertTrue((self.home / "tokens.json").exists())
        self.assertTrue(self.state.session.active)

    def test_failed_start_can_be_retried_and_invalid_cancellation_is_rejected(self):
        with patch.object(app, "device_start", side_effect=app.Fail("Could not reach iBroadcast.")):
            with self.assertRaises(app.Fail):
                self.state.start_login()
        self.assertEqual(self.state.login["state"], "error")
        login = self.begin()
        for value in ({}, 12, "short", "x" * 97):
            with self.subTest(value=value), self.assertRaises(app.Fail):
                self.state.cancel_login(value)
        self.assertEqual(self.state.login, login)
