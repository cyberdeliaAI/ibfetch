"""Atomic writes tolerate Windows sharing delays without destroying the baseline."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ibfetch


class PrivateWriteTests(unittest.TestCase):
    def test_windows_sharing_delay_retries_same_complete_temporary_file(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'settings.json'
                target.write_bytes(b'old')
                replace = os.replace
                sources = []

                def blocked(source, destination):
                    sources.append(source)
                    self.assertEqual(Path(source).read_bytes(), b'new')
                    self.assertEqual(target.read_bytes(), b'old')
                    if len(sources) < 3:
                        error = PermissionError(errno.EACCES, 'temporarily unavailable')
                        error.winerror = code
                        raise error
                    replace(source, destination)

                with patch.object(os, 'replace', blocked), patch.object(ibfetch.time, 'sleep') as sleep:
                    ibfetch.write_private(target, b'new')
                self.assertEqual(len(set(sources)), 1)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(target.read_bytes(), b'new')
                self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_persistent_windows_denial_is_bounded_and_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            target.write_bytes(b'old')
            error = PermissionError(errno.EACCES, 'access denied')
            error.winerror = 5
            with patch.object(os, 'replace', side_effect=error) as replace, \
                    patch.object(ibfetch.time, 'sleep') as sleep:
                with self.assertRaises(PermissionError):
                    ibfetch.write_private(target, b'new')
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(sleep.call_count, 5)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_disk_failure_and_non_windows_denial_are_not_retried(self):
        for error in (OSError(errno.ENOSPC, 'full'), PermissionError(errno.EACCES, 'denied')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'settings.json'
                target.write_bytes(b'old')
                with patch.object(os, 'replace', side_effect=error) as replace, \
                        patch.object(ibfetch.time, 'sleep') as sleep:
                    with self.assertRaises(OSError):
                        ibfetch.write_private(target, b'new')
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(target.read_bytes(), b'old')
                self.assertEqual(list(Path(directory).iterdir()), [target])
