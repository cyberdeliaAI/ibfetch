"""Isolated regressions: no credentials, real library or external service requests."""

import ast
import concurrent.futures
import contextlib
import gzip
import io
import json
import os
import stat
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ibfetch as app


def library():
    return app.Library({
        "artists": {"1": {"name": "Björk"}, "2": {"name": "The Beatles"}},
        "albums": {
            "10": {"artist_id": 1, "name": "Album", "tracks": [1, 2, 3], "disc": 1},
            "20": {"artist_id": 2, "name": "Other", "tracks": [4], "year": 1998}},
        "tracks": {
            "1": {"album_id": 10, "artist_id": 1, "title": "Song", "track": 1,
                  "file": "/orig/one", "type": "audio/flac", "genre": "Pop",
                  "genres_additional": ["Electronic"], "length": 60},
            "2": {"album_id": 10, "artist_id": 1, "title": "Song", "track": 1,
                  "file": "/orig/two", "type": "audio/flac", "genre": "Pop", "length": 90},
            "3": {"album_id": 10, "artist_id": 1, "title": "Song [1]", "track": 2,
                  "file": "/orig/three", "type": "audio/flac", "trashed": True},
            "4": {"album_id": 20, "artist_id": 2, "title": "Other", "track": 1,
                  "file": "/128/four", "type": "audio/mpeg", "genre": "Rock", "length": 120}},
        "tags": {"1": {"name": "Tag", "tracks": [1, 4]}},
        "playlists": {"1": {"name": "List", "tracks": [1, 2]}}}, "test-account", True)


def filters(**values):
    base = {k: None for k in ("artist", "track_artist", "album", "genre", "year", "format",
                              "tag", "playlist", "album_id", "track_id")}
    base.update(skip_the=False, all=False)
    base.update(values)
    return SimpleNamespace(**base)


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = headers


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.lib = library()

    def test_accents_exact_names_and_literal_brackets(self):
        self.assertEqual(app.select(self.lib, filters(artist=["bjork"])), [(10, [1, 2])])
        self.assertTrue(app.Pattern("Album [Live]").match("album [live]"))
        self.assertFalse(app.Pattern("Album [Live]").match("album L"))
        self.assertFalse(app.Pattern("bjork").match("Björk Extra"))

    def test_filters_intersect_and_repeated_values_union(self):
        self.assertEqual(app.select(self.lib, filters(artist=["bjork", "The Beatles"],
                                                     year=["1990-1999"])), [(20, [4])])
        self.assertEqual(app.select(self.lib, filters(tag=["Tag"], playlist=["List"])), [(10, [1])])

    def test_skip_the_additional_genre_and_trash(self):
        self.assertEqual(app.select(self.lib, filters(artist=["B*"], skip_the=True)),
                         [(10, [1, 2]), (20, [4])])
        self.assertEqual(app.select(self.lib, filters(genre=["Electronic"])), [(10, [1])])
        self.assertEqual(app.select(self.lib, filters(all=True)), [(10, [1, 2]), (20, [4])])

    def test_empty_selection_requires_explicit_all(self):
        with self.assertRaises(app.Fail):
            app.select(self.lib, filters())
        with self.assertRaises(app.Fail):
            app.select(self.lib, filters(tag=["Missing"]))

    def test_unknown_year_has_no_placeholder_brackets(self):
        rel = app.Layout(app.DEFAULT_LAYOUT).relpath(self.lib, 10, 1, "flac")
        self.assertEqual(str(rel), str(Path("Björk/Album/01 Song.flac")))

    def test_collision_names_stay_the_same_in_separate_selections(self):
        layout = app.Layout("{title}")
        together = app.plan_jobs(self.lib, [(10, [1, 2])], "orig", layout)
        separately = app.plan_jobs(self.lib, [(10, [2])], "orig", layout)
        self.assertEqual(together[1], separately[0])
        self.assertNotEqual(together[0][0], together[1][0])

    def test_collision_suffix_cannot_collide_with_a_literal_title(self):
        self.lib.tracks.rows[3]["trashed"] = False
        jobs = app.plan_jobs(self.lib, [(10, [1, 2, 3])], "orig", app.Layout("{title}"))
        paths = [app.fold(str(job[0])) for job in jobs]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(str(jobs[2][0]), "Song [1].flac")

    def test_quality_paths_are_distinct_for_mp3_originals(self):
        jobs = [app.plan_jobs(self.lib, [(20, [4])], quality, app.Layout(app.DEFAULT_LAYOUT))[0]
                for quality in ("orig", "128", "320")]
        self.assertEqual(len({job[0] for job in jobs}), 3)
        self.assertEqual([job[1] for job in jobs], ["/orig/four", "/128/four", "/320/four"])

    def test_names_are_relative_and_utf8_bounded(self):
        rel = app.Layout("../../{title}").relpath(self.lib, 10, 1, "flac")
        self.assertFalse(rel.is_absolute())
        self.assertNotIn("..", rel.parts)
        self.assertLessEqual(len(app.safe_name("🎵" * 150).encode("utf-8")), 150)
        self.assertEqual(app.safe_name("CON"), "CON_")

    def test_compact_field_map_and_deduplicated_tracks(self):
        table = app.Table({"map": {"title": 1, "invalid": -1}, "5": [0, "Title"]})
        self.assertEqual(table.get(5, "title"), "Title")
        self.assertEqual(table.get(5, "invalid", "default"), "default")
        self.assertEqual(app.sort_groups(self.lib, {10: [2, 1, 1]}), [(10, [1, 2])])

    def test_default_plan_only_names_tracks_in_target_directories(self):
        for i in range(100, 1100):
            album = i // 10 + 100
            self.lib.albums.rows[album] = {"name": "Album " + str(album), "artist_id": 2, "year": 2000}
            self.lib.tracks.rows[i] = {"album_id": album, "artist_id": 2, "title": "Track",
                                       "track": i, "type": "audio/flac", "file": "/orig/test"}
        layout = app.Layout(app.DEFAULT_LAYOUT)
        with patch.object(layout, "relpath", wraps=layout.relpath) as name:
            first = app.plan_jobs(self.lib, [(10, [1])], "orig", layout)
            # One representative per album builds the directory index, not one per track.
            self.assertLess(name.call_count, 120)
        with patch.object(layout, "relpath", wraps=layout.relpath) as name:
            self.assertEqual(app.plan_jobs(self.lib, [(10, [1])], "orig", layout), first)
            self.assertEqual(name.call_count, 1)

    def test_default_directory_aliases_still_resolve_collisions(self):
        self.lib.albums.rows[11] = {"artist_id": 1, "name": "Album", "year": 0}
        self.lib.tracks.rows[5] = dict(self.lib.tracks.rows[1], album_id=11)
        together = app.plan_jobs(self.lib, [(10, [1]), (11, [5])], "orig", app.Layout(app.DEFAULT_LAYOUT))
        self.assertNotEqual(together[0][0], together[1][0])
        self.assertEqual(app.plan_jobs(self.lib, [(11, [5])], "orig", app.Layout(app.DEFAULT_LAYOUT))[0], together[1])

    def test_arbitrary_layout_is_cached_and_cancel_is_observed(self):
        layout = app.Layout("{title}")
        first = app.plan_jobs(self.lib, [(10, [1])], "orig", layout)
        with patch.object(layout, "relpath", side_effect=AssertionError("not cached")):
            self.assertEqual(app.plan_jobs(self.lib, [(10, [1])], "orig", layout), first)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(app.Fail):
            app.plan_jobs(self.lib, [(10, [1])], "orig", layout, stop)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        # Lock slots live in ibfetch's local settings folder, never beside the audio.
        self.lock_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.lock_tmp.cleanup)
        self.locks = Path(self.lock_tmp.name) / "locks"
        self.lib = library()
        self.session = Mock()
        self.stop = threading.Event()
        self.final = self.out / "song.flac"
        self.record = app.DownloadRecord(self.final, self.locks)
        self.part = self.out / "song.flac.part"
        self.part_record = app.DownloadRecord(self.final, self.locks, partial=True)

    def fetch(self, response=None, progress=None, quality="orig", **options):
        if response is not None:
            self.session.stream.return_value = response
        options.setdefault("locks", self.locks)
        return app.fetch_track(self.session, self.lib, self.out, Path("song.flac"),
                               "/orig/one", 1, self.stop, progress, quality, **options)

    def response(self, data=b"test-audio", status=200, **headers):
        return Response(data, status, **{"Content-Length": str(len(data)),
                                        "Content-Type": "audio/flac", **headers})

    def interrupt(self, data=b"test-audio"):
        def progress(*unused):
            self.stop.set()
        with patch.object(app, "CHUNK", 4):
            self.assertEqual(self.fetch(self.response(data), progress)[0], "stopped")
        self.stop.clear()
        return self.part.stat().st_size

    def test_download_then_verified_skip(self):
        self.assertEqual(self.fetch(self.response()), ("ok", 10, ""))
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.session.stream.reset_mock()
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.session.stream.assert_not_called()
        record = json.loads(self.record.read_text())
        self.assertEqual(record["identity"]["quality"], "orig")
        self.assertEqual(record["sha256"], app.file_digest(self.final))
        self.assertNotIn("token", self.record.read_text())
        self.assertFalse(self.part.exists())
        self.assertFalse(self.part_record.exists())

    def test_unrelated_or_legacy_file_is_never_overwritten(self):
        self.final.write_bytes(b"my-existing-file")
        result = self.fetch()
        self.assertEqual(result[0], "skip")
        self.assertIn("Unverified:", result[2])
        self.assertEqual(self.final.read_bytes(), b"my-existing-file")
        self.session.stream.assert_not_called()

    def test_changed_file_is_preserved_and_reported_as_modified(self):
        self.fetch(self.response())
        before = self.final.stat()
        self.final.write_bytes(b"X" * 10)
        # Fast filesystems can reuse a timestamp tick for consecutive same-size writes.
        # This case exercises a changed mtime; the separate --verify test keeps it unchanged.
        os.utime(self.final, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        result = self.fetch()
        self.assertEqual(result[0], "skip")
        self.assertIn("Modified:", result[2])
        self.assertEqual(self.final.read_bytes(), b"X" * 10)

    def test_different_account_or_quality_is_not_adopted(self):
        self.fetch(self.response())
        self.assertIn("Unverified:", self.fetch(quality="320", adopt_existing=True)[2])
        self.lib.user_id = "different-account"
        self.assertIn("Unverified:", self.fetch(adopt_existing=True)[2])

    def test_interrupted_file_resumes_with_matching_range(self):
        offset = self.interrupt()
        self.assertEqual(offset, 4)
        remaining = b"test-audio"[offset:]
        reply = self.response(remaining, 206, **{"Content-Range": "bytes 4-9/10"})
        self.assertEqual(self.fetch(reply), ("ok", 10, ""))
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.assertEqual(self.session.stream.call_args.args[-1], 4)

    def test_wrong_range_does_not_append(self):
        self.interrupt()
        before = self.part.read_bytes()
        reply = self.response(b"bad", 206, **{"Content-Range": "bytes 0-2/10"})
        self.assertEqual(self.fetch(reply)[0], "fail")
        self.assertEqual(self.part.read_bytes(), before)
        self.assertFalse(self.final.exists())

    def test_missing_range_does_not_append(self):
        self.interrupt()
        self.assertEqual(self.fetch(self.response(b"bad", 206))[0], "fail")
        self.assertEqual(self.part.read_bytes(), b"test")

    def test_416_rejects_unknown_or_different_total(self):
        self.interrupt()
        for headers in ({}, {"Content-Range": "bytes */3"}, {"Content-Range": "bytes */10"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.fetch(Response(status=416, **headers))[0], "fail")
                self.assertFalse(self.final.exists())
                self.assertEqual(self.part.stat().st_size, 4)

    def test_416_accepts_exact_complete_partial(self):
        self.interrupt(b"test")
        reply = Response(status=416, **{"Content-Range": "bytes */4"})
        self.assertEqual(self.fetch(reply), ("ok", 4, ""))
        self.assertEqual(self.final.read_bytes(), b"test")

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        self.interrupt()
        self.assertEqual(self.fetch(self.response()), ("ok", 10, ""))
        self.assertEqual(self.final.read_bytes(), b"test-audio")

    def test_unverified_partial_is_preserved(self):
        self.part.write_bytes(b"legacy")
        self.assertEqual(self.fetch()[0], "fail")
        self.assertEqual(self.part.read_bytes(), b"legacy")
        self.session.stream.assert_not_called()

    def test_waiting_download_can_be_cancelled_without_touching_audio(self):
        with app.target_lock(self.out, Path("song.flac"), locks=self.locks):
            timer = threading.Timer(0.15, self.stop.set)
            timer.start()
            try:
                self.assertEqual(self.fetch()[0], "stopped")
            finally:
                timer.join()
        self.session.stream.assert_not_called()

    def test_colliding_lock_slots_serialize_both_downloads(self):
        # Real target names that share a slot; neither file may become a failure.
        slots = {}
        for i in range(10000):
            rel = Path("track-{}.flac".format(i))
            key = app.hashlib.sha256(app.fold(str((self.out / rel).resolve())).encode()).hexdigest()[:3]
            if key in slots:
                first, second = slots[key], rel
                break
            slots[key] = rel
        else:
            self.fail("expected a collision within 4096 lock slots")
        self.session.stream.side_effect = lambda *args: self.response()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            with app.target_lock(self.out, first, locks=self.locks):
                futures = [pool.submit(app.fetch_track, self.session, self.lib, self.out, rel,
                                       "/orig/one", 1, self.stop, locks=self.locks) for rel in (first, second)]
                self.assertFalse(any(f.done() for f in futures))
            self.assertEqual([f.result(timeout=5)[0] for f in futures], ["ok", "ok"])
        self.assertEqual(len(list(self.locks.glob("slot-*.lock"))), 1)
        self.assertFalse((self.out / ".ibfetch-locks").exists())
        self.assertEqual((self.out / first).read_bytes(), b"test-audio")
        self.assertEqual((self.out / second).read_bytes(), b"test-audio")

    def test_lock_file_count_is_bounded_across_more_than_4096_targets(self):
        for i in range(4200):
            with app.target_lock(self.out, Path("{}.flac".format(i)), locks=self.locks):
                pass
        self.assertLessEqual(len(list(self.locks.glob("slot-*.lock"))), 4096)

    def test_two_workers_for_one_target_download_once_then_skip(self):
        self.session.stream.side_effect = lambda *args: self.response()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            with app.target_lock(self.out, Path("song.flac"), locks=self.locks):
                futures = [pool.submit(self.fetch) for _ in range(2)]
            states = sorted(f.result(timeout=5)[0] for f in futures)
        self.assertEqual(states, ["ok", "skip"])
        self.assertEqual(self.session.stream.call_count, 1)

    @unittest.skipIf(os.name == "nt", "Unix flock error injection")
    def test_unsupported_filesystem_lock_fails_closed(self):
        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError(app.errno.ENOSYS, "unsupported")):
            with self.assertRaises(OSError):
                self.fetch()
        self.session.stream.assert_not_called()
        self.assertFalse(self.part.exists())

    def test_non_audio_and_empty_responses_are_not_completed(self):
        self.assertEqual(self.fetch(Response(b"{}", 200, **{
            "Content-Type": "application/json", "Content-Length": "2"}))[0], "fail")
        self.assertFalse(self.final.exists())
        self.assertEqual(self.fetch(self.response(b""))[0], "fail")
        self.assertFalse(self.final.exists())

    def test_lock_is_released_even_on_exception(self):
        self.session.stream.side_effect = app.Fail("simulated auth failure")
        with self.assertRaises(app.Fail):
            self.fetch()
        with app.target_lock(self.out, Path("song.flac"), locks=self.locks):
            pass

    def test_normal_skip_does_not_read_audio_or_hash_it(self):
        self.fetch(self.response())
        with patch.object(app, "file_digest", side_effect=AssertionError("unexpected audio scan")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_verify_detects_corruption_even_with_original_size_and_mtime(self):
        self.fetch(self.response())
        before = self.final.stat()
        self.final.write_bytes(b"X" * before.st_size)
        os.utime(self.final, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.assertIn("Modified:", self.fetch(verify=True)[2])

    def test_explicit_adoption_accepts_a_legacy_local_baseline(self):
        self.final.write_bytes(b"trusted-local-file")
        self.assertIn("Adopted:", self.fetch(adopt_existing=True)[2])
        self.assertTrue(json.loads(self.record.read_text())["adopted"])
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.session.stream.assert_not_called()

    def test_adoption_leaves_an_unchanged_verified_baseline_intact(self):
        self.fetch(self.response())
        before = self.record.read_bytes()
        with patch.object(app, "file_digest", side_effect=AssertionError("unexpected audio scan")):
            self.assertEqual(self.fetch(adopt_existing=True), ("skip", 0, ""))
        self.assertEqual(self.record.read_bytes(), before)
        self.assertFalse(json.loads(before)["adopted"])
        self.assertEqual(self.fetch(adopt_existing=True, verify=True), ("skip", 0, ""))
        self.assertFalse(json.loads(self.record.read_text())["adopted"])

    def test_completion_is_recovered_after_real_process_death_after_rename(self):
        code = """
import io, json, os, sys, threading
from pathlib import Path
import ibfetch
from types import SimpleNamespace
lib = ibfetch.Library({'tracks': {'1': {'file': '/orig/one'}}, 'artists': {}, 'albums': {}}, 'test-account', True)
response = io.BytesIO(b'test-audio')
response.status = 200
response.headers = {'Content-Length': '10', 'Content-Type': 'audio/flac'}
session = SimpleNamespace(stream=lambda *args: response)
replace = os.replace
def interrupted(source, destination):
    replace(source, destination)
    if Path(destination).name == 'song.flac':
        print('renamed', flush=True)
        sys.stdin.read()
os.replace = interrupted
ibfetch.fetch_track(session, lib, Path(sys.argv[1]), Path('song.flac'),
                    '/orig/one', 1, threading.Event(), locks=Path(sys.argv[2]))
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.out), str(self.locks)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "renamed")
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.assertFalse(self.record.exists())
        self.assertTrue(json.loads(self.part_record.read_text())["completed"])
        self.assertIn("Recovered:", self.fetch()[2])
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.assertFalse(self.part_record.exists())
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.session.stream.assert_not_called()

    def test_recovery_rejects_identity_only_and_changed_or_unrelated_files(self):
        self.fetch(self.response())
        record = json.loads(self.record.read_text())
        self.record.unlink()
        for pending, audio in (({"identity": record["identity"]}, b"test-audio"),
                               ({**record, "completed": True}, b"other-data"),
                               ({**record, "completed": True, "identity": {**record["identity"], "track_id": 9}}, b"test-audio")):
            with self.subTest(pending=pending, audio=audio):
                self.final.write_bytes(audio)
                self.part_record.write_text(json.dumps(pending))
                self.assertIn("Unverified:", self.fetch()[2])
                self.assertFalse(self.record.exists())
                self.assertTrue(self.part_record.exists())
                self.assertEqual(self.final.read_bytes(), audio)

    def test_crash_after_completed_record_only_leaves_safe_receipt_cleanup(self):
        unlink = app.DownloadRecord.unlink
        def interrupted(path, *args, **kwargs):
            if path == self.part_record:
                raise OSError("interrupted after record write")
            return unlink(path, *args, **kwargs)
        with patch.object(app.DownloadRecord, "unlink", interrupted):
            with self.assertRaises(OSError):
                self.fetch(self.response())
        self.assertTrue(self.part_record.exists())
        with patch.object(app, "file_digest", side_effect=AssertionError("unexpected audio scan")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.assertFalse(self.part_record.exists())

    def test_rename_timestamp_change_uses_the_final_baseline(self):
        replace = os.replace
        def rename_with_timestamp_change(source, target):
            replace(source, target)
            if target == self.final:
                info = self.final.stat()
                os.utime(self.final, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        with patch.object(os, "replace", rename_with_timestamp_change):
            self.assertEqual(self.fetch(self.response())[0], "ok")
        self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_cli_summary_distinguishes_unverified_modified_and_adopted_files(self):
        args = SimpleNamespace(out=str(self.out), jobs=1, quality="orig", layout="{title}",
                               verify=False, adopt_existing=False)
        jobs = [(Path("one.flac"), "/orig/one", 1), (Path("two.flac"), "/orig/two", 2)]
        for notes, summary in ((["Unverified: preserved", "Modified: preserved"], "1 unverified, 1 modified"),
                               (["Adopted: trusted", ""], "1 adopted")):
            output = io.StringIO()
            with patch.object(app, "plan_jobs", return_value=jobs), patch.object(app, "fetch_track", side_effect=[("skip", 0, note) for note in notes]), contextlib.redirect_stdout(output):
                self.assertEqual(app.run_downloads(self.session, self.lib, [(10, [1, 2])], args), 0)
            self.assertIn(summary, output.getvalue())
            self.assertNotIn("Adopt existing", output.getvalue())
            if "Unverified" in notes[0]:
                self.assertIn("--adopt-existing", output.getvalue())
                self.assertIn("--verify", output.getvalue())

    def test_older_record_can_be_verified_once_then_fast_skipped(self):
        self.fetch(self.response())
        record = json.loads(self.record.read_text())
        record.pop("mtime_ns")
        self.record.write_text(json.dumps(record))
        self.assertIn("Unverified:", self.fetch()[2])
        self.assertEqual(self.fetch(verify=True), ("skip", 0, ""))
        with patch.object(app, "file_digest", side_effect=AssertionError("unexpected scan")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_complete_identity_survives_a_changed_stream_path(self):
        self.fetch(self.response())
        self.lib.tracks.rows[1]["file"] = "/orig/new-server-path"
        self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_alternative_audio_content_types_are_accepted(self):
        for kind in ("application/ogg", "video/mp4", "application/x-flac", ""):
            with self.subTest(kind=kind):
                if self.final.exists():
                    self.final.unlink()
                self.assertEqual(self.fetch(Response(b"audio", 200, **{
                    "Content-Type": kind, "Content-Length": "5"}))[0], "ok")

    def test_os_lock_is_released_after_forced_process_death(self):
        signals = [None] if os.name == "nt" else [signal.SIGKILL, signal.SIGHUP]
        code = ("import sys; from pathlib import Path; import ibfetch; "
                "lock=ibfetch.target_lock(Path(sys.argv[1]),Path('song.flac'),locks=Path(sys.argv[2])); "
                "lock.__enter__(); print('locked',flush=True); sys.stdin.read()")
        for sig in signals:
            with self.subTest(signal=sig):
                child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.out), str(self.locks)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "locked")
                    with self.assertRaises(app.TargetBusy):
                        with app.target_lock(self.out, Path("song.flac"), locks=self.locks):
                            pass
                    child.kill() if sig is None else child.send_signal(sig)
                    child.wait(timeout=5)
                    with app.target_lock(self.out, Path("song.flac"), locks=self.locks):
                        pass
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

    def test_lock_slots_stay_in_the_local_lock_folder(self):
        self.assertEqual(self.fetch(self.response()), ("ok", 10, ""))
        self.assertFalse((self.out / ".ibfetch-locks").exists())
        self.assertEqual(len(list(self.locks.glob("slot-*.lock"))), 1)
        with self.assertRaises(ValueError):
            with app.target_lock(self.out, Path("song.flac")):
                pass

    @unittest.skipIf(os.name == "nt", "Unix flock semantics")
    def test_silently_granted_locks_fail_closed(self):
        # macOS SMB mounts grant a second flock on the same file; nothing would be protected.
        import fcntl
        with patch.object(fcntl, "flock", return_value=None):
            with self.assertRaises(app.Fail) as raised:
                self.fetch(self.response(), locks=Path(self.lock_tmp.name) / "share")
        self.assertIn("not enforced", str(raised.exception))
        self.session.stream.assert_not_called()
        self.assertFalse(self.part.exists())

    def test_fresh_download_is_hashed_while_written_not_read_again(self):
        with patch.object(app, "file_hasher", side_effect=AssertionError("unexpected audio read")):
            self.assertEqual(self.fetch(self.response()), ("ok", 10, ""))
        record = json.loads(self.record.read_text())
        self.assertEqual(record["sha256"], app.hashlib.sha256(b"test-audio").hexdigest())

    def test_resume_reads_the_partial_once_to_continue_its_checksum(self):
        self.interrupt()
        reply = self.response(b"-audio", 206, **{"Content-Range": "bytes 4-9/10"})
        with patch.object(app, "file_hasher", wraps=app.file_hasher) as reads:
            self.assertEqual(self.fetch(reply), ("ok", 10, ""))
        self.assertEqual(reads.call_count, 1)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["sha256"], app.hashlib.sha256(b"test-audio").hexdigest())

    def test_dropped_connection_continues_the_checksum_without_reading_back(self):
        class Dropping(Response):
            def read(self, amount=-1):
                if self.tell() >= 4:
                    raise ConnectionResetError("dropped")
                return super().read(amount)
        first = Dropping(b"test-audio", 200, **{"Content-Length": "10", "Content-Type": "audio/flac"})
        second = self.response(b"-audio", 206, **{"Content-Range": "bytes 4-9/10"})
        self.session.stream.side_effect = [first, second]
        with patch.object(app, "CHUNK", 4), patch.object(self.stop, "wait", return_value=False), \
                patch.object(app, "file_hasher", side_effect=AssertionError("unexpected audio read")):
            self.assertEqual(self.fetch(), ("ok", 10, ""))
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.assertEqual(json.loads(self.record.read_text())["sha256"],
                         app.hashlib.sha256(b"test-audio").hexdigest())

    def test_share_timestamp_on_rename_needs_no_second_read(self):
        replace = os.replace
        def share_rename(source, target):
            replace(source, target)
            if target == self.final:  # the NAS gives the renamed file its own time
                info = self.final.stat()
                os.utime(self.final, ns=(info.st_atime_ns, info.st_mtime_ns + 220_000_000))
        with patch.object(os, "replace", share_rename), \
                patch.object(app, "file_hasher", side_effect=AssertionError("unexpected audio read")):
            self.assertEqual(self.fetch(self.response()), ("ok", 10, ""))
        self.assertEqual(json.loads(self.record.read_text())["mtime_ns"], self.final.stat().st_mtime_ns)
        with patch.object(app, "file_hasher", side_effect=AssertionError("unexpected audio read")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_one_failed_track_does_not_stop_other_tracks(self):
        transfer = app.Transfer(self.session, self.lib,
                                [(Path("a.flac"), "/a", 1), (Path("b.flac"), "/b", 2)],
                                self.out, 1)
        with patch.object(app, "fetch_track", side_effect=[app.Fail("failed"), ("ok", 10, "")]):
            transfer.run()
        self.assertEqual(transfer.counts, {"ok": 1, "skip": 0, "fail": 1})
        self.assertEqual(transfer.snapshot()["done"], 2)


class WorkFolderTests(unittest.TestCase):
    """Download to a local work folder, then move each finished track to the output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.out, self.work, self.locks = root / "nas", root / "work", root / "home" / "locks"
        self.out.mkdir()
        self.lib = library()
        self.session = Mock()
        self.session.stream.side_effect = lambda *args: Response(
            b"test-audio", 200, **{"Content-Length": "10", "Content-Type": "audio/flac"})
        self.stop = threading.Event()
        self.target = self.out / "Artist" / "song.flac"
        self.local = app.work_root(self.work, self.out) / "Artist" / "song.flac"

    def fetch(self, **options):
        return app.fetch_track(self.session, self.lib, self.out, Path("Artist/song.flac"), "/orig/one",
                               1, self.stop, locks=self.locks, work=self.work, **options)

    def work_files(self):
        return [p for p in self.work.rglob("*") if p.is_file()]

    def test_finished_track_moves_and_the_work_folder_empties(self):
        with patch.object(app, "file_hasher", side_effect=AssertionError("unexpected fresh audio read")):
            self.assertEqual(self.fetch(), ("ok", 10, ""))
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        record = json.loads(app.DownloadRecord(self.target, self.locks).read_text())
        self.assertEqual(record["sha256"], app.hashlib.sha256(b"test-audio").hexdigest())
        self.assertEqual(record["mtime_ns"], self.target.stat().st_mtime_ns)
        self.assertEqual(self.work_files(), [])
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()),
                         [".ibfetch.json", "song.flac"])
        self.session.stream.reset_mock()
        with patch.object(app, "file_hasher", side_effect=AssertionError("unexpected audio read")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.session.stream.assert_not_called()

    def test_failed_move_keeps_the_download_and_moves_it_next_time(self):
        with patch.object(app.shutil, "copyfile", side_effect=OSError(app.errno.EIO, "share went away")):
            state, _, note = self.fetch()
        self.assertEqual(state, "fail")
        self.assertIn("moves on the next run", note)
        self.assertTrue(self.local.exists())
        self.assertFalse(self.target.exists())
        self.session.stream.reset_mock()
        self.assertEqual(self.fetch(), ("ok", 10, ""))
        self.session.stream.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        self.assertEqual(self.work_files(), [])

    def test_verify_rejects_corrupt_cached_audio_even_with_unchanged_size_and_time(self):
        with patch.object(app.shutil, "copyfile", side_effect=OSError(app.errno.EIO, "share went away")):
            self.fetch()
        before = self.local.stat()
        self.local.write_bytes(b"other-data")
        os.utime(self.local, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.session.stream.reset_mock()
        with patch.object(app.shutil, "copyfile") as copy:
            state, _, note = self.fetch(verify=True)
        self.assertEqual(state, "fail")
        self.assertIn("Modified:", note)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.local.read_bytes(), b"other-data")
        copy.assert_not_called()
        self.session.stream.assert_not_called()

    def test_verify_checks_a_valid_cached_copy_then_moves_without_downloading_again(self):
        with patch.object(app.shutil, "copyfile", side_effect=OSError(app.errno.EIO, "share went away")):
            self.fetch()
        self.session.stream.reset_mock()
        with patch.object(app, "file_hasher", wraps=app.file_hasher) as reads:
            self.assertEqual(self.fetch(verify=True), ("ok", 10, ""))
        self.assertIn(self.local, [call.args[0] for call in reads.call_args_list])
        self.session.stream.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        self.assertEqual(self.work_files(), [])

    def test_legacy_work_folder_record_migrates_and_moves_without_redownloading(self):
        with patch.object(app.shutil, "copyfile", side_effect=OSError(app.errno.EIO, "share went away")):
            self.fetch()
        record = app.DownloadRecord(self.local, self.locks)
        legacy = self.local.with_name(self.local.name + ".ibfetch.json")
        legacy.write_bytes(record.read_bytes())
        record.manifest.unlink()
        self.session.stream.reset_mock()
        self.assertEqual(self.fetch(verify=True), ("ok", 10, ""))
        self.session.stream.assert_not_called()
        self.assertEqual(self.work_files(), [])
        self.assertEqual(self.target.read_bytes(), b"test-audio")

    def test_move_interrupted_after_rename_is_recovered(self):
        write = app.write_private
        def interrupted(path, data):
            if path == app.DownloadRecord(self.target, self.locks):
                raise OSError(app.errno.EIO, "interrupted")
            return write(path, data)
        with patch.object(app, "write_private", interrupted):
            self.assertEqual(self.fetch()[0], "fail")
        self.assertTrue(self.target.exists())
        self.assertTrue(self.local.exists())
        self.assertIn("Recovered:", self.fetch()[2])
        self.assertEqual(self.work_files(), [])
        self.assertEqual(self.fetch(), ("skip", 0, ""))

    def test_leftover_copy_after_a_verified_move_is_discarded(self):
        with patch.object(app, "_remove_local_copy", side_effect=OSError(app.errno.EIO, "interrupted")):
            self.assertEqual(self.fetch()[0], "fail")
        self.assertTrue(app.DownloadRecord(self.target, self.locks).exists())
        self.assertTrue(self.local.exists())
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.assertEqual(self.work_files(), [])

    def test_leftover_modified_audio_is_preserved_even_if_its_timestamp_is_restored(self):
        with patch.object(app, "_remove_local_copy", side_effect=OSError(app.errno.EIO, "interrupted")):
            self.fetch()
        before = self.local.stat()
        record = app.DownloadRecord(self.local, self.locks).read_bytes()
        for restored in (False, True):
            with self.subTest(restored_timestamp=restored):
                self.local.write_bytes(b"other-data")
                os.utime(self.local, ns=(before.st_atime_ns, before.st_mtime_ns + (0 if restored else 2_000_000_000)))
                state, _, note = self.fetch()
                self.assertEqual(state, "skip")
                self.assertIn("Modified:", note)
                self.assertEqual(self.local.read_bytes(), b"other-data")
                self.assertEqual(app.DownloadRecord(self.local, self.locks).read_bytes(), record)
                self.assertEqual(self.target.read_bytes(), b"test-audio")

    def test_recovering_an_interrupted_move_preserves_a_modified_local_copy(self):
        write = app.write_private
        def interrupted(path, data):
            if path == app.DownloadRecord(self.target, self.locks):
                raise OSError(app.errno.EIO, "interrupted")
            return write(path, data)
        with patch.object(app, "write_private", interrupted):
            self.fetch()
        before = self.local.stat()
        self.local.write_bytes(b"other-data")
        os.utime(self.local, ns=(before.st_atime_ns, before.st_mtime_ns))
        state, _, note = self.fetch()
        self.assertEqual(state, "skip")
        self.assertIn("Recovered:", note)
        self.assertIn("Modified:", note)
        self.assertEqual(self.local.read_bytes(), b"other-data")
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        target_record = json.loads(app.DownloadRecord(self.target, self.locks).read_text())
        self.assertEqual(target_record["sha256"], app.file_digest(self.target))

    def test_leftover_for_another_identity_is_never_deleted(self):
        with patch.object(app, "_remove_local_copy", side_effect=OSError(app.errno.EIO, "interrupted")):
            self.fetch()
        local_record = app.DownloadRecord(self.local, self.locks)
        record = json.loads(local_record.read_text())
        record["identity"]["track_id"] = 999
        local_record.write_text(json.dumps(record))
        state, _, note = self.fetch()
        self.assertEqual(state, "skip")
        self.assertIn("Unverified:", note)
        self.assertTrue(self.local.exists())
        self.assertTrue(local_record.exists())

    def test_cleanup_preserves_audio_changed_during_its_checksum_check(self):
        with patch.object(app, "_remove_local_copy", side_effect=OSError(app.errno.EIO, "interrupted")):
            self.fetch()
        digest = app.file_digest
        def changed_after_hash(path):
            result = digest(path)
            if path == self.local:
                before = self.local.stat()
                self.local.write_bytes(b"other-data")
                os.utime(self.local, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return result
        with patch.object(app, "file_digest", changed_after_hash):
            state, _, note = self.fetch()
        self.assertEqual(state, "skip")
        self.assertIn("Modified:", note)
        self.assertEqual(self.local.read_bytes(), b"other-data")

    def test_fresh_move_preserves_local_audio_changed_while_copying(self):
        copy = app.shutil.copyfile
        def changed_after_copy(source, target):
            result = copy(source, target)
            before = self.local.stat()
            self.local.write_bytes(b"other-data")
            os.utime(self.local, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return result
        with patch.object(app.shutil, "copyfile", changed_after_copy):
            state, _, note = self.fetch()
        self.assertEqual(state, "fail")
        self.assertIn("Modified:", note)
        self.assertEqual(self.local.read_bytes(), b"other-data")
        self.assertTrue(app.DownloadRecord(self.local, self.locks).exists())
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        self.assertIn("Modified:", self.fetch()[2])

    def test_changed_copy_in_the_work_folder_is_not_moved(self):
        with patch.object(app.shutil, "copyfile", side_effect=OSError(app.errno.EIO, "share went away")):
            self.fetch()
        info = self.local.stat()
        self.local.write_bytes(b"other-data")
        os.utime(self.local, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        state, _, note = self.fetch()
        self.assertEqual(state, "fail")
        self.assertIn("Modified:", note)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.local.read_bytes(), b"other-data")

    def test_existing_unrelated_target_is_preserved(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"my-own-file")
        self.assertIn("Unverified:", self.fetch()[2])
        self.assertEqual(self.target.read_bytes(), b"my-own-file")
        self.session.stream.assert_not_called()

    def test_work_folder_must_be_outside_the_download_folder(self):
        for work in (self.out, self.out / "work", self.out.parent):
            with self.subTest(work=work), self.assertRaises(app.Fail):
                app.check_work_folder(work, self.out)
        app.check_work_folder(self.work, self.out)
        self.assertTrue(self.work.is_dir())

    def test_cli_rejects_a_relative_work_folder(self):
        args = SimpleNamespace(out=str(self.out), jobs=1, quality="orig", layout="{title}",
                               verify=False, adopt_existing=False, work_folder="relative/work")
        with self.assertRaises(app.Fail):
            app.run_downloads(self.session, self.lib, [(10, [1])], args, self.tmp.name)


class ManifestTests(unittest.TestCase):
    """Exercise the on-disk format and migration without trusting logical record mocks."""

    setUp = DownloadTests.setUp
    fetch = DownloadTests.fetch
    response = DownloadTests.response

    def baseline(self, track_id=1):
        return {"identity": {"version": 1, "user_id": "test-account", "track_id": track_id,
                             "quality": "orig", "source": "/orig/one"},
                "size": 10, "mtime_ns": self.final.stat().st_mtime_ns,
                "sha256": app.hashlib.sha256(b"test-audio").hexdigest(), "adopted": False}

    def test_one_manifest_contains_multiple_completed_files_without_adjacent_json(self):
        self.fetch(self.response())
        self.session.stream.side_effect = lambda *args: self.response()
        app.fetch_track(self.session, self.lib, self.out, Path("two.flac"), "/orig/two", 2,
                        self.stop, locks=self.locks)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [".ibfetch.json", "song.flac", "two.flac"])
        data = json.loads((self.out / app.MANIFEST_NAME).read_text())
        self.assertEqual(set(data["files"]), {"song.flac", "two.flac"})
        self.assertEqual(data["partials"], {})
        if os.name == "nt":
            import ctypes
            self.assertTrue(ctypes.windll.kernel32.GetFileAttributesW(str(self.out / app.MANIFEST_NAME)) & 2)

    def test_all_recognized_legacy_records_in_a_folder_migrate_without_reading_audio(self):
        self.final.write_bytes(b"test-audio")
        baseline = self.baseline()
        for name in ("song.flac", "other.flac"):
            if name != "song.flac":
                (self.out / name).write_bytes(b"test-audio")
            (self.out / (name + ".ibfetch.json")).write_text(json.dumps(baseline))
        with patch.object(app, "file_digest", side_effect=AssertionError("unexpected audio scan")):
            self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [".ibfetch.json", "other.flac", "song.flac"])
        data = json.loads((self.out / app.MANIFEST_NAME).read_text())
        self.assertEqual(data["files"]["song.flac"], baseline)
        self.assertEqual(data["files"]["other.flac"], baseline)

    def test_legacy_partial_resumes_into_the_same_manifest(self):
        self.part.write_bytes(b"test")
        identity = {"version": 1, "user_id": "test-account", "track_id": 1,
                    "quality": "orig", "source": "/orig/one"}
        legacy = self.out / "song.flac.part.ibfetch.json"
        legacy.write_text(json.dumps({"identity": identity}))
        reply = self.response(b"-audio", 206, **{"Content-Range": "bytes 4-9/10"})
        self.assertEqual(self.fetch(reply), ("ok", 10, ""))
        self.assertFalse(legacy.exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [".ibfetch.json", "song.flac"])

    def test_legacy_completion_receipt_still_recovers_a_renamed_file(self):
        self.final.write_bytes(b"test-audio")
        pending = self.baseline()
        pending.pop("mtime_ns")
        pending["completed"] = True
        legacy = self.out / "song.flac.part.ibfetch.json"
        legacy.write_text(json.dumps(pending))
        self.assertIn("Recovered:", self.fetch()[2])
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.assertFalse(legacy.exists())
        self.assertFalse(self.part_record.exists())
        self.session.stream.assert_not_called()

    def test_failed_migration_write_keeps_every_legacy_record_and_audio(self):
        self.final.write_bytes(b"test-audio")
        legacy = self.out / "song.flac.ibfetch.json"
        legacy.write_text(json.dumps(self.baseline()))
        original = legacy.read_bytes()
        with patch.object(app, "write_private", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                self.record.exists()
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.assertFalse((self.out / app.MANIFEST_NAME).exists())

    def test_interrupted_migration_retries_cleanup_after_the_manifest_was_saved(self):
        self.final.write_bytes(b"test-audio")
        legacy = self.out / "song.flac.ibfetch.json"
        baseline = self.baseline()
        legacy.write_text(json.dumps(baseline))
        unlink = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path == legacy:
                raise OSError("interrupted before legacy cleanup")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", interrupted):
            with self.assertRaises(OSError):
                self.record.exists()
        self.assertTrue(legacy.exists())
        self.assertEqual(json.loads((self.out / app.MANIFEST_NAME).read_text())["files"]["song.flac"], baseline)
        self.assertEqual(self.fetch(), ("skip", 0, ""))
        self.assertFalse(legacy.exists())

    def test_damaged_or_future_manifests_are_preserved_without_touching_audio(self):
        self.final.write_bytes(b"test-audio")
        manifest = self.out / app.MANIFEST_NAME
        for raw in ("{", "[]", '{"version":2,"files":{},"partials":{}}'):
            with self.subTest(raw=raw):
                manifest.write_text(raw)
                with self.assertRaises(app.Fail):
                    self.fetch()
                self.assertEqual(manifest.read_text(), raw)
                self.assertEqual(self.final.read_bytes(), b"test-audio")
        self.session.stream.assert_not_called()

    def test_unrecognized_and_conflicting_sidecars_are_not_deleted_or_silently_used(self):
        self.fetch(self.response())
        legacy = self.out / "song.flac.ibfetch.json"
        unrelated = self.out / "notes.ibfetch.json"
        unrelated.write_text('{"notes":"not an ibfetch record"}')
        record = json.loads(self.record.read_text())
        record["identity"]["track_id"] = 99
        legacy.write_text(json.dumps(record))
        with self.assertRaises(app.Fail):
            self.fetch()
        self.assertTrue(legacy.exists())
        self.assertEqual(json.loads(unrelated.read_text()), {"notes": "not an ibfetch record"})
        self.assertEqual(self.final.read_bytes(), b"test-audio")

    def test_separate_process_downloads_do_not_lose_other_tracks_in_the_manifest(self):
        code = """
import io, sys, threading
from pathlib import Path
from types import SimpleNamespace
import ibfetch
lib=ibfetch.Library({'artists':{},'albums':{},'tracks':{'1':{'file':'/orig/one'}}},'test-account',True)
def stream(*args):
    response=io.BytesIO(b'test-audio')
    response.status=200
    response.headers={'Content-Length':'10','Content-Type':'audio/flac'}
    return response
for i in range(15):
    result=ibfetch.fetch_track(SimpleNamespace(stream=stream),lib,Path(sys.argv[1]),
        Path(sys.argv[3]+'-'+str(i)+'.flac'),'/orig/one',1,threading.Event(),locks=Path(sys.argv[2]))
    assert result == ('ok',10,''), result
"""
        children = [subprocess.Popen([sys.executable, "-B", "-c", code, str(self.out), str(self.locks), prefix],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for prefix in ("one", "two", "three")]
        try:
            for child in children:
                _, errors = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, errors)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
        manifest = json.loads((self.out / app.MANIFEST_NAME).read_text())
        self.assertEqual(len(manifest["files"]), 45)
        self.assertEqual(manifest["partials"], {})
        self.assertEqual(len(list(self.out.iterdir())), 46)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.instance = app.App(self.home)
        self.addCleanup(self.instance.shutdown)

    def connected(self):
        self.instance.session = Mock()
        self.instance.lib = library()
        self.instance.lib_state = "ready"
        return self.instance

    def test_wrong_json_types_do_not_crash_startup(self):
        for data in ([], None, "text", 123):
            with self.subTest(data=data):
                (self.home / "tokens.json").write_text(json.dumps(data))
                (self.home / "app-settings.json").write_text(json.dumps(data))
                self.assertIsNone(app.App(self.home).session)
                with self.assertRaises(app.Fail):
                    app.Session(self.home)

    def test_theme_persists_independently_of_sign_in_and_download_settings(self):
        self.instance.set_appearance({"appearance":"dark", "palette":"amber"})
        restored = app.App(self.home)
        self.assertEqual(restored.settings["appearance"], "dark")
        self.assertEqual(restored.settings["palette"], "amber")
        self.assertEqual(restored.settings["quality"], "orig")
        self.assertIsNone(restored.session)
        with self.assertRaises(app.Fail):
            self.instance.set_appearance({"appearance":"invalid", "palette":"forest"})
        self.assertEqual(app.App(self.home).settings["palette"], "amber")

    def test_invalid_saved_settings_use_defaults(self):
        (self.home / "app-settings.json").write_text(json.dumps(
            {"quality": "bad", "jobs": -3, "layout": [], "out": {}}))
        instance = app.App(self.home)
        self.assertEqual(instance.settings["quality"], "orig")
        self.assertEqual(instance.settings["jobs"], 3)
        self.assertEqual(instance.settings["layout"], app.DEFAULT_LAYOUT)

    def test_cancelled_login_cannot_persist_late_tokens(self):
        instance = self.instance
        cancel = instance.login_cancel = threading.Event()
        generation = instance.login_generation
        def late(*unused):
            instance.cancel_login()
            return {"access_token": "fake"}
        with patch.object(app, "device_poll", side_effect=late):
            instance._poll_login("test", {}, cancel, generation)
        self.assertFalse((self.home / "tokens.json").exists())
        self.assertIsNone(instance.session)

    def test_cancel_during_device_request_does_not_start_polling(self):
        instance = self.instance
        def late(*unused):
            instance.cancel_login()
            return {"device_code": "test", "verification_uri": "https://example.invalid"}
        with patch.object(app, "device_start", side_effect=late), patch.object(app.threading, "Thread") as thread:
            instance.start_login()
        thread.assert_not_called()
        self.assertIsNone(instance.login)

    def test_late_library_success_and_failure_are_discarded(self):
        for result in (library(), app.Fail("old failure")):
            with self.subTest(result=type(result).__name__):
                instance = self.connected()
                session = instance.session
                generation = instance.library_generation
                def late(*unused, **kwargs):
                    instance.sign_out()
                    if isinstance(result, Exception):
                        raise result
                    return result
                with patch.object(app, "load_library", side_effect=late):
                    instance._load(False, session, generation)
                self.assertIsNone(instance.lib)
                self.assertEqual(instance.lib_state, "none")
                self.assertEqual(instance.lib_error, "")

    def test_work_folder_setting_round_trips_and_is_validated(self):
        (self.home / "app-settings.json").write_text(json.dumps({"work": " /tmp/ibfetch-work "}))
        self.assertEqual(app.App(self.home).settings["work"], "/tmp/ibfetch-work")
        (self.home / "app-settings.json").write_text(json.dumps({"work": 5}))
        self.assertEqual(app.App(self.home).settings["work"], "")
        instance = self.connected()
        body = {"out": str(self.home / "music"), "work": "relative", "items": [{"album_id": 10, "tracks": [1]}]}
        with self.assertRaises(app.Fail):
            instance.download(body)
        self.assertIsNone(instance.transfer)
        body["work"] = str(self.home / "work")
        with patch.object(app.threading, "Thread"):
            instance.download(body)
        self.assertEqual(instance.transfer.work, self.home / "work")
        self.assertEqual(instance.transfer.locks, app.lock_folder(self.home))
        self.assertEqual(instance.settings["work"], str(self.home / "work"))

    def test_ready_transfer_reserves_the_slot_before_thread_start(self):
        instance = self.connected()
        body = {"out": str(self.home / "music"), "quality": "orig", "jobs": 1,
                "items": [{"album_id": 10, "tracks": [1]}]}
        with patch.object(app.threading, "Thread"):
            instance.download(body)
            transfer = instance.transfer
            with self.assertRaises(app.Fail):
                instance.download(body)
            with self.assertRaises(app.Fail):
                instance.sign_out()
        self.assertIs(instance.transfer, transfer)

    def test_simultaneous_starts_admit_only_one_transfer(self):
        instance = self.connected()
        body = {"out": str(self.home / "music"), "items": [{"album_id": 10, "tracks": [1]}]}
        barrier = threading.Barrier(3)
        outcomes = []
        real_thread = threading.Thread
        def request():
            barrier.wait()
            try:
                instance.download(body)
                outcomes.append("ok")
            except app.Fail:
                outcomes.append("busy")
        with patch.object(app.threading, "Thread"):
            workers = [real_thread(target=request) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(5)
        self.assertEqual(sorted(outcomes), ["busy", "ok"])

    def test_planning_does_not_hold_app_lock_and_cancel_remains_responsive(self):
        instance = self.connected()
        entered, release = threading.Event(), threading.Event()
        def slow(*unused):
            entered.set()
            self.assertTrue(release.wait(5))
            return [(Path("song.flac"), "/orig/test", 1)]
        with patch.object(app, "plan_jobs", side_effect=slow), patch.object(app, "fetch_track") as fetch:
            instance.download({"out": str(self.home / "music"),
                               "items": [{"album_id": 10, "tracks": [1]}]})
            self.assertTrue(entered.wait(5))
            self.assertTrue(instance.lock.acquire(timeout=1))
            instance.lock.release()
            self.assertEqual(instance.state()["transfer"]["state"], "planning")
            instance.cancel_download()
            release.set()
            instance.transfer_thread.join(5)
            fetch.assert_not_called()
            self.assertEqual(instance.transfer.state, "stopped")

    def test_library_replacement_discards_prepared_jobs(self):
        instance = self.connected()
        entered, release = threading.Event(), threading.Event()
        def slow(*unused):
            entered.set()
            self.assertTrue(release.wait(5))
            return [(Path("song.flac"), "/orig/test", 1)]
        with patch.object(app, "plan_jobs", side_effect=slow), patch.object(app, "fetch_track") as fetch:
            instance.download({"out": str(self.home / "music"),
                               "items": [{"album_id": 10, "tracks": [1]}]})
            self.assertTrue(entered.wait(5))
            instance.lib = library()
            instance.library_generation += 1
            release.set()
            instance.transfer_thread.join(5)
            fetch.assert_not_called()
            self.assertEqual(instance.transfer.state, "stopped")

    def test_shutdown_invalidates_pending_login(self):
        generation = self.instance.login_generation
        cancel = self.instance.login_cancel = threading.Event()
        self.instance.shutdown()
        with patch.object(app, "device_poll", return_value={"access_token": "fake"}):
            self.instance._poll_login("test", {}, cancel, generation)
        self.assertFalse((self.home / "tokens.json").exists())

    def test_deactivated_session_cannot_refresh_or_restore_tokens(self):
        app.write_private(self.home / "tokens.json", json.dumps({"access_token": "test",
                          "refresh_token": "test-refresh", "client_id": "test"}).encode())
        session = app.Session(self.home)
        session.deactivate()
        with patch.object(app, "oauth_post") as request:
            with self.assertRaises(app.Fail):
                session.token()
            with self.assertRaises(app.Fail):
                session.refresh("test")
        request.assert_not_called()

    def test_private_atomic_write_has_no_shared_temporary_name(self):
        path = self.home / "private.json"
        app.write_private(path, b"first")
        app.write_private(path, b"second")
        self.assertEqual(path.read_bytes(), b"second")
        self.assertEqual(list(self.home.glob("*.tmp")), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_source_still_parses_with_python_38_grammar(self):
        ast.parse(Path(app.__file__).read_text("utf-8"), feature_version=(3, 8))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.instance = app.App(Path(cls.tmp.name))
        handler = app.make_handler(cls.instance, "test-private-token")
        cls.server = app.LocalHTTPServer(("127.0.0.1", 0), handler)
        cls.server.daemon_threads = True
        handler.port = cls.server.server_address[1]
        cls.url = "http://127.0.0.1:" + str(handler.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.instance.shutdown()
        cls.tmp.cleanup()

    def test_private_entry_and_security_headers(self):
        with app.http("GET", self.url + "/?t=test-private-token") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"<title>ibfetch</title>", response.read())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    def test_reload_restores_saved_theme_with_both_inline_scripts_hashed(self):
        body = json.dumps({"appearance":"light", "palette":"amber"}).encode()
        with app.http("POST", self.url + "/api/appearance", body,
                      {"X-Ibfetch-Token":"test-private-token"}) as response:
            self.assertEqual(response.status, 200)
        try:
            with app.http("GET", self.url + "/?t=test-private-token") as response:
                page = response.read()
                self.assertIn(b'name="ibfetch-palette" content="amber"', page)
                self.assertIn(b'name="ibfetch-appearance" content="light"', page)
                self.assertEqual(response.headers["Content-Security-Policy"].count("sha256-"), 2)
        finally:
            self.instance.set_appearance({"appearance":"auto", "palette":"forest"})

    def test_loopback_startup_never_requires_a_reverse_dns_lookup(self):
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected reverse DNS")):
            server = app.LocalHTTPServer(("127.0.0.1", 0), app.make_handler(self.instance, "test"))
            server.server_close()
        with self.assertRaises(app.Fail):
            app.LocalHTTPServer(("0.0.0.0", 0), app.make_handler(self.instance, "test"))

    def test_host_token_and_cookie_guards(self):
        cases = [("/", {}, 403),
                 ("/?t=test-private-token", {"Host": "evil.invalid"}, 403),
                 ("/api/state", {"Cookie": "ibfetch=test-private-token"}, 403),
                 ("/api/state", {"X-Ibfetch-Token": "test-private-token"}, 200)]
        for path, headers, status in cases:
            with self.subTest(path=path, headers=headers), app.http("GET", self.url + path,
                                                                  headers=headers) as response:
                self.assertEqual(response.status, status)

    def test_post_authentication_and_bad_json(self):
        with app.http("POST", self.url + "/api/download", b"{}") as response:
            self.assertEqual(response.status, 403)
        with app.http("POST", self.url + "/api/download", b"x",
                      {"X-Ibfetch-Token": "test-private-token"}) as response:
            self.assertEqual(response.status, 400)

    def test_negative_content_length_is_rejected_without_reading(self):
        with app.http("POST", self.url + "/api/download", b"",
                      {"X-Ibfetch-Token": "test-private-token", "Content-Length": "-1"}) as response:
            self.assertEqual(response.status, 400)


class MockBroadcastTests(unittest.TestCase):
    """Exercise the actual HTTP/cache/refresh/download chain against loopback only."""

    def test_oauth_cache_download_and_token_refresh(self):
        audio = b"synthetic-audio-data"
        calls = []
        lib = library()
        payload = {"library": {"artists": {str(k): v for k, v in lib.artists.rows.items()},
                               "albums": {str(k): v for k, v in lib.albums.rows.items()},
                               "tracks": {str(k): v for k, v in lib.tracks.rows.items()},
                               "expires": "1999999999"}, "status": {"lastmodified": "test-revision"}}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def send(self, data, headers=None):
                if not isinstance(data, bytes):
                    data = json.dumps(data).encode()
                self.send_response(200)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                calls.append(self.path.split("?", 1)[0])
                if self.path.startswith("/oauth/device/code"):
                    self.send({"device_code": "fake-device", "user_code": "TEST", "interval": 1,
                               "verification_uri": "https://example.invalid/device"})
                else:
                    self.send(audio, {"Content-Type": "audio/flac"})

            def do_POST(self):
                calls.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/oauth/token":
                    self.send({"access_token": "fake-access", "refresh_token": "fake-refresh",
                               "expires_in": 3600})
                elif self.path == "/api/status":
                    self.send({"user": {"id": "test-account", "premium": True, "username": "Test"},
                               "status": {"lastmodified": "test-revision"}})
                else:
                    self.send(gzip.compress(json.dumps(payload).encode()), {"Content-Encoding": "gzip"})

        server = app.LocalHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(app, "OAUTH_URL", base + "/oauth"), \
                    patch.object(app, "API_URL", base + "/api"), \
                    patch.object(app, "LIBRARY_URL", base + "/library"), \
                    patch.object(app, "STREAM_URL", base + "/stream"), \
                    patch.object(app.time, "sleep"):
                home = Path(tmp)
                code = app.device_start("test-client")
                tokens = app.device_poll("test-client", code)
                tokens["expires_at"] = 0
                app.write_private(home / "tokens.json", json.dumps(tokens).encode())
                session = app.Session(home)
                loaded = app.load_library(session, home, quiet=True)
                self.assertEqual(loaded.username, "Test")
                self.assertEqual(app.load_library(session, home, quiet=True).tracks.get(1, "title"), "Song")
                self.assertEqual(calls.count("/library"), 1)
                self.assertEqual(calls.count("/oauth/token"), 2)
                result = app.fetch_track(session, loaded, home / "music", Path("song.flac"),
                                         "/orig/one", 1, threading.Event(), locks=app.lock_folder(home))
                self.assertEqual(result, ("ok", len(audio), ""))
                self.assertEqual((home / "music/song.flac").read_bytes(), audio)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
