"""Account-specific cache and logout regressions using only synthetic libraries."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import ibfetch as app


def data(name):
    return {'artists': {'1': {'name': name}},
            'albums': {'1': {'name': name + ' album', 'artist_id': 1}},
            'tracks': {'1': {'title': name + ' track', 'album_id': 1, 'artist_id': 1}}}


class FakeSession:
    def __init__(self, account):
        self.account, self.lock, self.active, self.downloads = account, threading.Lock(), True, 0

    def deactivate(self):
        self.active = False

    def api(self, mode, **kwargs):
        if mode == 'status':
            return {'user': {'id': self.account, 'username': self.account},
                    'status': {'lastmodified': 'same-revision'}}, b''
        self.downloads += 1
        library = data(self.account)
        return library, json.dumps(library).encode()


class AccountSwitchTests(unittest.TestCase):
    def test_same_revision_and_track_ids_in_another_account_never_reuse_first_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first, second = FakeSession('first-account'), FakeSession('second-account')
            self.assertEqual(app.load_library(first, home, quiet=True).album_artist(1), 'first-account')
            library = app.load_library(second, home, quiet=True)
            self.assertEqual(library.album_artist(1), 'second-account')
            self.assertEqual(library.user_id, 'second-account')
            self.assertEqual(second.downloads, 1)
            self.assertEqual(app.load_library(second, home, quiet=True).user_id, 'second-account')
            self.assertEqual(second.downloads, 1)

    def test_missing_account_identity_cannot_reuse_an_unowned_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = FakeSession('')
            with self.assertRaisesRegex(app.Fail, 'account ID'):
                app.load_library(session, Path(tmp), quiet=True)
            self.assertEqual(session.downloads, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_logout_forgets_the_first_library_and_new_login_publishes_only_the_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = app.App(home)
            self.addCleanup(state.shutdown)
            first = FakeSession('first-account')
            state.session = first
            state._load(False, first, state.library_generation)
            (home / 'tokens.json').write_text('{"access_token":"synthetic-access"}')
            with patch.object(app, 'oauth_post'):
                state.sign_out()
            self.assertFalse(first.active)
            self.assertIsNone(state.lib)
            for name in ('tokens.json', 'library-cache.json.gz', 'library-cache.meta.json'):
                self.assertFalse((home / name).exists())
            second = state.session = FakeSession('second-account')
            state._load(False, second, state.library_generation)
            self.assertEqual(state.state()['user']['name'], 'second-account')
            self.assertEqual(state.lib.album_artist(1), 'second-account')
            with patch.object(app, 'load_library', return_value=app.Library(data('late-first'), 'first-account', False)):
                state._load(False, first, state.library_generation - 1)
            self.assertEqual(state.lib.user_id, 'second-account')
