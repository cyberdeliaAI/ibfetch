"""Signing boundaries and verification without touching a keychain or Apple service."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ibfetch_build", ROOT / "tools/build_desktop.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
SECRETS = {key: "synthetic-secret" for key in build.MAC_SIGNING_SECRETS}
SECRETS["APPLE_TEAM_ID"] = "TESTTEAM12"


class SigningTests(unittest.TestCase):
    def test_ordinary_builds_strip_every_signing_secret_including_overrides(self):
        with patch.dict(os.environ, {**SECRETS, "WIN_CSC_LINK": "private", "CSC_FOR_PULL_REQUEST": "true"}):
            plain, pack = build.build_environments(False)
        for env in (plain, pack):
            for key in SECRETS:
                self.assertNotIn(key, env)
            self.assertNotIn("WIN_CSC_LINK", env)
            self.assertEqual(env["CSC_IDENTITY_AUTO_DISCOVERY"], "false")
            self.assertEqual(env["CSC_FOR_PULL_REQUEST"], "false")

    def test_signed_builds_require_credentials_mac_and_an_explicit_event(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(build.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "Missing macOS signing secrets"):
                build.build_environments(True)
        for platform, event in (("win32", "workflow_dispatch"), ("darwin", "pull_request"), ("darwin", "push")):
            with self.subTest(platform=platform, event=event), \
                    patch.dict(os.environ, {**SECRETS, "GITHUB_EVENT_NAME": event}, clear=True), \
                    patch.object(build.sys, "platform", platform), self.assertRaises(ValueError):
                build.build_environments(True)
        with patch.dict(os.environ, {**SECRETS, "APPLE_TEAM_ID": "wrong"}, clear=True), \
                patch.object(build.sys, "platform", "darwin"), self.assertRaisesRegex(ValueError, "team identifier"):
            build.build_environments(True)

    def test_only_the_pack_sign_notarize_child_receives_credentials(self):
        with patch.dict(os.environ, {**SECRETS, "GITHUB_EVENT_NAME": "workflow_dispatch"}, clear=True), \
                patch.object(build.sys, "platform", "darwin"):
            plain, pack = build.build_environments(True)
        for key, value in SECRETS.items():
            self.assertNotIn(key, plain)
            self.assertEqual(pack[key], value)
        self.assertEqual(pack["CSC_FOR_PULL_REQUEST"], "false")

    def test_verification_rejects_wrong_team_adhoc_and_missing_hardened_runtime(self):
        good = "Authority=Developer ID Application: Synthetic\nTeamIdentifier=TESTTEAM12\nCodeDirectory v=20500 flags=0x10000(runtime)\n"
        bad = (good.replace("TESTTEAM12", "OTHERTEAM1"), good.replace("Authority=Developer ID Application:", "Signature=adhoc"),
               good.replace("0x10000(runtime)", "0x0(none)"))
        for output in bad:
            with self.subTest(signature=output), patch.object(build.subprocess, "run", return_value=Mock(stdout="", stderr=output)) as run:
                with self.assertRaisesRegex(RuntimeError, "hardened Developer ID"):
                    build.verify_macos_app(Path("synthetic.app"), "TESTTEAM12")
                self.assertFalse(any("stapler" in c.args[0] for c in run.call_args_list))

    def test_verified_app_requires_app_backend_ticket_and_gatekeeper(self):
        good = "Authority=Developer ID Application: Synthetic\nTeamIdentifier=TESTTEAM12\nCodeDirectory v=20500 flags=0x10000(runtime)\n"
        with tempfile.TemporaryDirectory() as tmp, patch.object(build, "ROOT", Path(tmp)), \
                patch.object(build.subprocess, "run", return_value=Mock(stdout="", stderr=good)) as run:
            build.verify_macos_app(Path("synthetic.app"), "TESTTEAM12")
            commands = [c.args[0] for c in run.call_args_list]
            self.assertTrue(any(Path(cmd[-1]).parts[-2:] == ("backend", "ibfetch-server") for cmd in commands))
            self.assertTrue(any("stapler" in cmd for cmd in commands))
            self.assertTrue(any(cmd[0] == "spctl" for cmd in commands))
            self.assertTrue((Path(tmp) / "build/reports/macos-signing.json").exists())
