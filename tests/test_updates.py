"""Anonymous, bounded, asynchronous release notifications; no external service requests."""

import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

import ibfetch as app


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def wait(self, checker):
        deadline = time.monotonic() + 5
        while checker.snapshot()["checking"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(checker.snapshot()["checking"])

    def test_stable_numeric_comparison_and_rejection_of_untrusted_metadata(self):
        self.assertGreater(app.stable_version("1.10.0"), app.stable_version("1.9.99"))
        for tag in ("v1.2.3-beta", "v1.2.3/../../other", "v9999999.1.0", "javascript:alert(1)", None):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                app.release_info({"tag_name": tag})
        for flag in ("draft", "prerelease"):
            with self.assertRaises(ValueError):
                app.release_info({"tag_name": "v9.0.0", flag: True})
        self.assertEqual(app.release_info({"tag_name": "v9.0.0", "html_url": "https://evil.invalid/"}),
                         {"version": "9.0.0"})

    def test_daily_attempts_survive_restart_and_failure_but_manual_checks_work(self):
        checker = app.UpdateChecker(self.home)
        with patch.object(app, "latest_release", side_effect=OSError("private URL/credential")) as request:
            checker.check(); self.wait(checker)
            checker.check(); self.wait(checker)
            restored = app.UpdateChecker(self.home)
            restored.check(); self.wait(restored)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(restored.snapshot()["error"])
            self.assertNotIn("private URL", (self.home / "updates.json").read_text())
        with patch.object(app, "latest_release", return_value={"version": "9.0.0"}) as request:
            restored.check(manual=True); self.wait(restored)
            self.assertTrue(restored.snapshot()["available"])
            self.assertEqual(restored.snapshot()["url"], app.RELEASES_URL + "/tag/v9.0.0")
            self.assertFalse(restored.snapshot()["error"])
            self.assertEqual(request.call_count, 1)

    def test_check_never_blocks_state_or_duplicates_an_inflight_request(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(5)
            return {"version": "9.0.0"}
        checker = app.UpdateChecker(self.home)
        with patch.object(app, "latest_release", side_effect=slow) as request:
            try:
                self.assertTrue(checker.check()["checking"])
                self.assertTrue(entered.wait(2))
                self.assertTrue(checker.check(manual=True)["checking"])
                self.assertIsNone(checker.snapshot()["latest"])
                self.assertEqual(request.call_count, 1)
            finally:
                release.set(); self.wait(checker)

    def test_an_open_app_checks_again_only_after_the_daily_interval(self):
        instance = app.App(self.home)
        self.addCleanup(instance.shutdown)
        with patch.object(app, "latest_release", return_value={"version": "9.0.0"}) as request:
            instance.start_updates(); self.wait(instance.updates)
            instance.state(); self.wait(instance.updates)
            self.assertEqual(request.call_count, 1)
            later = instance.updates.cache["attempted_at"] + app.UPDATE_INTERVAL + 1
            with patch.object(app.time, "time", return_value=later):
                instance.state(); self.wait(instance.updates)
            self.assertEqual(request.call_count, 2)

    def test_saved_opt_out_does_not_check_but_allows_manual_request(self):
        (self.home / "app-settings.json").write_text(json.dumps({"auto_updates": False, "palette": "amber"}))
        instance = app.App(self.home)
        self.addCleanup(instance.shutdown)
        with patch.object(app, "latest_release", return_value={"version": "9.0.0"}) as request:
            instance.start_updates(); instance.state()
            request.assert_not_called()
            instance.updates.check(manual=True); self.wait(instance.updates)
        self.assertFalse(instance.state()["updates"]["automatic"])
        instance.set_updates({"automatic": False})
        self.assertEqual(app.read_object(self.home / "app-settings.json")["palette"], "amber")
        for bad in ("false", 0, None):
            with self.assertRaises(app.Fail): instance.set_updates({"automatic": bad})

    def test_shutdown_ignores_a_late_success(self):
        entered, release = threading.Event(), threading.Event()
        instance = app.App(self.home)
        def slow():
            entered.set(); release.wait(5)
            return {"version": "9.0.0"}
        with patch.object(app, "latest_release", side_effect=slow):
            try:
                instance.start_updates(); self.assertTrue(entered.wait(2))
                instance.shutdown()
            finally:
                release.set(); self.wait(instance.updates)
        self.assertIsNone(instance.state()["updates"]["latest"])
        self.assertNotIn("latest", app.read_object(self.home / "updates.json"))

    def test_invalid_cache_and_read_only_cache_do_not_break_the_app(self):
        for value in (True, -1, time.time() + 10000, float("nan"), float("inf"), 10**400, "yesterday"):
            (self.home / "updates.json").write_text(json.dumps({"attempted_at": value,
                "checked_at": value, "latest": {"version": "javascript:bad"}}))
            checker = app.UpdateChecker(self.home)
            self.assertIsNone(checker.snapshot()["checked_at"])
            self.assertIsNone(checker.snapshot()["latest"])
        with patch.object(app, "write_private", side_effect=OSError("read only")), \
                patch.object(app, "latest_release", return_value={"version": "9.0.0"}):
            checker.check(); self.wait(checker)
            self.assertTrue(checker.snapshot()["available"])

    def test_anonymous_client_bounds_response_and_disallows_redirects(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b'{"tag_name":"v9.0.0","html_url":"https://evil.invalid/"}'
        opener = Mock(); opener.open.return_value = response
        with patch.object(app.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(app.latest_release(), {"version": "9.0.0"})
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, None, None, None, None))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, app.UPDATE_API)
        self.assertEqual(set(k.lower() for k in request.headers), {"user-agent", "accept"})
        self.assertIsNone(request.data)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 8})
        response.read.assert_called_once_with(1024 * 1024 + 1)
        response.read.return_value = b"x" * (1024 * 1024 + 1)
        with patch.object(app.urllib.request, "build_opener", return_value=opener), self.assertRaises(ValueError):
            app.latest_release()

    def test_previous_success_remains_available_when_a_later_check_fails(self):
        checker = app.UpdateChecker(self.home)
        with patch.object(app, "latest_release", return_value={"version": "9.0.0"}):
            checker.check(); self.wait(checker)
        with patch.object(app, "latest_release", side_effect=ValueError("bad data")):
            checker.check(manual=True); self.wait(checker)
        self.assertTrue(checker.snapshot()["available"])
        self.assertTrue(checker.snapshot()["error"])

    def test_embedded_official_logos_match_the_original_assets_byte_for_byte(self):
        root = Path(__file__).resolve().parent.parent
        for kind, encoded in app.POWERED_LOGOS.items():
            self.assertEqual(base64.b64decode(encoded),
                             (root / "assets" / ("ibroadcast-" + kind + "-compact-powered.svg")).read_bytes())
        self.assertEqual(base64.b64decode(app.IBFETCH_ICON), (root / "assets/ibfetch-icon.svg").read_bytes())
