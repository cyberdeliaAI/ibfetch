#!/usr/bin/env python3
"""ibfetch: download a chosen part of your iBroadcast library.

Pick tracks by album artist, track artist, album, genre, year, format, tag or
playlist and download only those, in their original format or a set bitrate.
Standard library only, Python 3.8 or newer. Works on Linux, macOS and Windows.

    ibfetch                      # opens the app in your browser
    ibfetch login                # or use it from the command line
    ibfetch list --artist "4hero"
    ibfetch get --artist "A*" -o ~/Music/iBroadcast
"""

import argparse
import base64
import errno
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from types import SimpleNamespace

VERSION = "1.5.0"
CLIENT = "ibfetch"
USER_AGENT = f"{CLIENT}/{VERSION}"
# Public OAuth client ID (the Tagcast app). Device-code sign-in uses no client secret.
# Register your own app with iBroadcast and pass --client-id or IBROADCAST_CLIENT_ID to use that instead.
DEFAULT_CLIENT_ID = "5d8e2ee2bfee11f1b50eb49691aa2236"
SCOPES = ["user.library:read", "user.account:read"]
QUALITIES = ("orig", "320", "256", "192", "128", "96")
DEFAULT_LAYOUT = "{albumartist}/{album} ({year})/{disctrack} {title}"
CONFIRM_ABOVE = 1000  # tracks; bigger downloads ask first
CHUNK = 256 * 1024

# Point everything at a test server: IBFETCH_BASE=http://127.0.0.1:9555
_base = os.environ.get("IBFETCH_BASE", "").rstrip("/")
OAUTH_URL = f"{_base}/oauth" if _base else "https://oauth.ibroadcast.com"
API_URL = f"{_base}/api" if _base else "https://api.ibroadcast.com"
LIBRARY_URL = f"{_base}/library" if _base else "https://library.ibroadcast.com"
STREAM_URL = f"{_base}/stream" if _base else "https://streaming.ibroadcast.com"
DOWNLOAD_URL = f"{_base}/download" if _base else "https://download.ibroadcast.com/"
# Where original files come from. The download service applies iBroadcast's current tags;
# the streaming server returns the file exactly as uploaded and can resume it.
SOURCES = ("download", "stream")

MIME_EXT = {
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a", "audio/aac": "aac",
    "audio/ogg": "ogg", "audio/vorbis": "ogg", "audio/opus": "opus", "audio/x-ms-wma": "wma",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/aiff": "aiff",
    "audio/x-aiff": "aiff", "audio/ape": "ape", "audio/x-ape": "ape", "audio/x-wavpack": "wv",
    "audio/dsf": "dsf", "audio/x-dsf": "dsf",
}
FORMAT_NAMES = {"mp3": "MP3", "flac": "FLAC", "m4a": "M4A", "aac": "AAC", "ogg": "OGG",
                "opus": "OPUS", "wma": "WMA", "wav": "WAV", "aiff": "AIFF", "ape": "APE",
                "wv": "WV", "dsf": "DSF"}


class Fail(Exception):
    """A problem to report to the user without a traceback."""


# -- small helpers ------------------------------------------------------------

def text(value):
    return "" if value is None else str(value)


def number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def fold(value):
    """Compare without case, accents or extra spacing ("Björk" matches "bjork")."""
    value = unicodedata.normalize("NFKD", text(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.casefold().split())


class Pattern:
    """A name pattern. Only * and ? are wildcards, so brackets in titles stay literal."""

    def __init__(self, raw):
        self.raw = raw
        folded = fold(raw)
        self.regex = re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c)
                                        for c in folded), re.S)

    def match(self, value, skip_the=False):
        value = fold(value)
        if self.regex.fullmatch(value):
            return True
        return skip_the and value.startswith("the ") and bool(self.regex.fullmatch(value[4:]))


def patterns(values):
    return [Pattern(v) for v in values or []]


def any_match(pats, value, skip_the=False):
    return any(p.match(value, skip_the) for p in pats)


def year_range(raw):
    m = re.fullmatch(r"\s*(\d{4})\s*(?:(-)\s*(\d{4})?)?\s*", raw or "")
    if not m:
        raise Fail(f"Use a year like 1997, a range like 1990-1999 or an open range like 2010-: {raw!r}")
    low = int(m.group(1))
    high = low if not m.group(2) else int(m.group(3) or 9999)
    return low, high


def duration(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def size(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


RESERVED = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
WINDOWS_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def safe_name(value, limit=150):
    """One path component that works on Linux, macOS and Windows."""
    value = RESERVED.sub("_", text(value))
    value = " ".join(value.split())
    if len(value.encode("utf-8")) > limit:
        value = value.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip()
    value = value.rstrip(". ")
    if value.startswith("."):
        value = "_" + value[1:]
    if value.split(".")[0].casefold() in WINDOWS_NAMES:
        value += "_"
    return value or "_"


def home_dir(override=None):
    if override:
        return Path(override).expanduser()
    if os.environ.get("IBFETCH_HOME"):
        return Path(os.environ["IBFETCH_HOME"]).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / CLIENT


def write_private(path, data):
    if isinstance(path, DownloadRecord):
        path.write_bytes(data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def read_object(path):
    """Local JSON must be an object; damaged or unrelated files are not trusted."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# -- HTTP -----------------------------------------------------------------------

def http(method, url, data=None, headers=None, timeout=60):
    """Open a request. HTTP errors come back as responses so callers can read the status."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        return error


def status_of(response):
    return getattr(response, "status", None) or response.getcode()


def read_body(response):
    with response:
        raw = response.read()
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw


def as_json(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError, HTTPException)


def connection_error(error, service="iBroadcast"):
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (f"Could not verify {service}'s HTTPS certificate. Update the desktop app, "
                "or check your Python installation's trusted certificates.")
    return f"Could not reach {service} ({error.__class__.__name__})."


RELEASES_URL = "https://github.com/cyberdeliaAI/ibfetch/releases"
UPDATE_API = "https://api.github.com/repos/cyberdeliaAI/ibfetch/releases/latest"
UPDATE_INTERVAL = 24 * 60 * 60


def stable_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", value):
        raise ValueError("Not a stable version")
    return tuple(int(part) for part in value.split("."))


def release_info(data):
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ValueError("Not a stable release")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("Unsupported release tag")
    stable_version(tag[1:])
    return {"version": tag[1:]}


class NoUpdateRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def latest_release():
    # A separate, anonymous client: no account session, proxies, cookies or redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoUpdateRedirect())
    request = urllib.request.Request(UPDATE_API, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/vnd.github+json"})
    with opener.open(request, timeout=8) as response:
        if status_of(response) != 200:
            raise ValueError("Release check failed")
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Release response too large")
    return release_info(json.loads(raw))


class UpdateChecker:
    """Background notifications only; attempts, including failures, are limited daily."""

    def __init__(self, home, automatic=True):
        self.path = home / "updates.json"
        self.lock = threading.RLock()
        self.automatic, self.checking, self.closed = automatic, False, False
        self.cache = {}
        saved = read_object(self.path)
        for key in ("attempted_at", "checked_at"):
            value = saved.get(key)
            if type(value) in (int, float) and 0 <= value <= time.time() and math.isfinite(value):
                self.cache[key] = value
        try:
            self.cache["latest"] = release_info({"tag_name": "v" + saved["latest"]["version"]})
        except (KeyError, TypeError, ValueError):
            pass
        if saved.get("error"):
            self.cache["error"] = True

    def snapshot(self):
        with self.lock:
            latest = self.cache.get("latest", {}).get("version")
            available = bool(latest and stable_version(latest) > stable_version(VERSION))
            return {"current": VERSION, "latest": latest, "available": available,
                    "url": RELEASES_URL + "/tag/v" + latest if available else RELEASES_URL + "/latest",
                    "checking": self.checking, "automatic": self.automatic,
                    "checked_at": self.cache.get("checked_at"), "error": bool(self.cache.get("error"))}

    def _persist(self):
        try:
            write_private(self.path, json.dumps(self.cache).encode())
        except OSError:
            pass  # A read-only home must not prevent downloading or checking in memory.

    def check(self, manual=False):
        with self.lock:
            attempted = self.cache.get("attempted_at")
            if (self.closed or self.checking or not manual and (not self.automatic or
                    attempted is not None and 0 <= time.time() - attempted < UPDATE_INTERVAL)):
                return self.snapshot()
            self.checking = True
            self.cache["attempted_at"] = time.time()
            threading.Thread(target=self._run, daemon=True).start()
            return self.snapshot()

    def _run(self):
        with self.lock:
            self._persist()
        try:
            latest = latest_release()
            error = False
        except (OSError, ValueError, HTTPException):
            latest, error = None, True
        with self.lock:
            self.checking = False
            if self.closed:
                return
            if latest:
                self.cache.update(latest=latest, checked_at=time.time())
            self.cache["error"] = error
            self._persist()


def textual(response):
    """True for an error page or API answer rather than file data."""
    kind = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return (kind.startswith("text/") or kind in ("application/json", "application/xml", "application/xhtml+xml")
            or kind.endswith("+json"))


class ServiceError(Exception):
    """The download service could not deliver a track. retry: a new request may succeed."""

    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


def post_read(url, body, headers, timeout):
    """POST a read request (status, library). Retries temporary failures. Returns (json, raw)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept-Encoding": "gzip", **headers}
    delays = (0, 2, 6)
    for attempt, delay in enumerate(delays):
        time.sleep(delay)
        last = attempt == len(delays) - 1
        try:
            response = http("POST", url, data, headers, timeout)
            code = status_of(response)
            raw = read_body(response)
        except NETWORK_ERRORS as error:
            if last:
                raise Fail(connection_error(error)) from None
            continue
        if (code == 429 or code >= 500) and not last:
            continue
        parsed = as_json(raw)
        if code == 401:
            parsed.setdefault("authenticated", False)
        elif code >= 400:
            raise Fail(parsed.get("message") or f"iBroadcast returned HTTP {code}.")
        elif not parsed:
            raise Fail("iBroadcast returned no usable answer.")
        return parsed, raw
    raise Fail("iBroadcast did not answer.")


# -- OAuth ----------------------------------------------------------------------

def oauth_post(path, form):
    try:
        response = http("POST", f"{OAUTH_URL}/{path}", urllib.parse.urlencode(form).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded"}, 45)
        return status_of(response), as_json(read_body(response))
    except NETWORK_ERRORS as error:
        raise Fail(connection_error(error, "iBroadcast sign-in")) from None


def token_record(data, client_id, refresh_token=""):
    if not data.get("access_token"):
        raise Fail("iBroadcast sign-in returned no access token.")
    return {"client_id": client_id, "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_at": time.time() + number(data.get("expires_in") or 3600),
            "scope": text(data.get("scope"))}


def device_start(client_id):
    """Ask iBroadcast for a sign-in code. Returns the device code response."""
    query = urllib.parse.urlencode({"client_id": client_id, "scope": " ".join(SCOPES)})
    try:
        response = http("GET", f"{OAUTH_URL}/device/code?{query}", timeout=45)
        code, data = status_of(response), as_json(read_body(response))
    except NETWORK_ERRORS as error:
        raise Fail(connection_error(error, "iBroadcast sign-in")) from None
    if code >= 400 or not data.get("device_code"):
        raise Fail(f"iBroadcast refused the sign-in request (HTTP {code}). Check the client ID.")
    data.setdefault("verification_uri", "https://oauth.ibroadcast.com/device")
    return data


def device_poll(client_id, data, cancel=None):
    """Wait until the code is approved. Returns the token record."""
    interval = max(number(data.get("interval")) or 5, 1)
    deadline = time.time() + (number(data.get("expires_in")) or 900)
    while time.time() < deadline:
        if cancel is not None:
            if cancel.wait(interval):
                raise Fail("Sign-in cancelled.")
        else:
            time.sleep(interval)
        code, result = oauth_post("token", {"grant_type": "device_code", "client_id": client_id,
                                            "device_code": data["device_code"]})
        if code < 400 and result.get("access_token"):
            return token_record(result, client_id)
        error = result.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise Fail("Sign-in was declined.")
        if error == "expired_token":
            break
        raise Fail(f"Sign-in failed: {error or f'HTTP {code}'} {result.get('error_description', '')}".strip())
    raise Fail("The sign-in code expired. Start again.")


class Session:
    """A signed-in iBroadcast session. Refreshes its token when needed. Thread safe."""

    def __init__(self, home):
        self.path = home / "tokens.json"
        self.tokens = read_object(self.path)
        if not isinstance(self.tokens.get("access_token"), str) or not self.tokens["access_token"]:
            raise Fail("Not signed in. Run: ibfetch login")
        self.tokens["expires_at"] = number(self.tokens.get("expires_at"))
        self.active = True
        self.lock = threading.Lock()

    def token(self):
        with self.lock:
            if not self.active:
                raise Fail("This session has been signed out.")
            if self.tokens.get("expires_at", 0) - time.time() < 60 and self.tokens.get("refresh_token"):
                self._refresh_locked()
            return self.tokens["access_token"]

    def refresh(self, stale):
        """Get a new token unless another thread already replaced the stale one."""
        with self.lock:
            if not self.active:
                raise Fail("This session has been signed out.")
            if self.tokens["access_token"] == stale:
                self._refresh_locked()
            return self.tokens["access_token"]

    def deactivate(self):
        with self.lock:
            self.active = False

    def _refresh_locked(self):
        refresh_token = self.tokens.get("refresh_token")
        if not refresh_token or not self.tokens.get("client_id"):
            raise Fail("Your iBroadcast session expired. Run: ibfetch login")
        code, data = oauth_post("token", {"grant_type": "refresh_token",
                                          "client_id": self.tokens["client_id"],
                                          "refresh_token": refresh_token})
        if code >= 400 or not data.get("access_token"):
            raise Fail("Your iBroadcast session expired. Run: ibfetch login")
        self.tokens = token_record(data, self.tokens["client_id"], refresh_token)
        write_private(self.path, json.dumps(self.tokens, indent=2).encode())

    def api(self, mode, url=None, timeout=60):
        url = url or f"{API_URL}/{mode}"
        body = {"client": CLIENT, "version": VERSION, "device_name": CLIENT,
                "user_agent": USER_AGENT, "mode": mode}
        token = self.token()
        for attempt in (1, 2):
            data, raw = post_read(url, body, {"Authorization": f"Bearer {token}"}, timeout)
            if data.get("authenticated") is not False:
                break
            if attempt == 2:
                raise Fail("iBroadcast didn't accept the session. Run: ibfetch login")
            token = self.refresh(token)
        if data.get("result") is False:
            raise Fail(data.get("message") or f"iBroadcast refused the {mode} request.")
        return data, raw

    def stream(self, path, track_id, user_id, expires, offset=0):
        """Open one track's audio. The caller closes the response."""
        token = self.token()
        for attempt in (1, 2):
            params = {"Signature": token, "file_id": track_id, "user_id": user_id,
                      "platform": CLIENT, "version": VERSION}
            if expires:
                params["Expires"] = expires
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            response = http("GET", f"{STREAM_URL}{path}?{urllib.parse.urlencode(params)}",
                            headers=headers, timeout=120)
            if status_of(response) not in (401, 403) or attempt == 2:
                return response
            response.close()
            token = self.refresh(token)

    def download(self, track_id, user_id):
        """Ask iBroadcast's download service for one track, as a streamed ZIP. The caller closes it.

        This is the service behind the web player's Download Tracks. It applies the account's
        Download Updated Metadata setting and appends an end marker after the archive.
        """
        token = self.token()
        for attempt in (1, 2):
            form = {"_user_id": text(user_id), "_token": token, "mode": "downloadlibrary",
                    "container_type": "track", "ids": str(track_id), "end_marker": "1"}
            response = http("POST", DOWNLOAD_URL, urllib.parse.urlencode(form).encode(),
                            {"Content-Type": "application/x-www-form-urlencoded"}, 300)
            code = status_of(response)
            if code == 200 and not textual(response):
                return response
            with response:
                answer = as_json(response.read(4096))
            refused = code in (401, 403) or answer.get("authenticated") is False
            if refused and attempt == 1:
                token = self.refresh(token)
                continue
            message = text(answer.get("message")).strip()[:200] or f"HTTP {code}"
            raise ServiceError(f"iBroadcast's download service refused it ({message})",
                               retry=code == 429 or code >= 500)


# -- library --------------------------------------------------------------------

class Table:
    """An iBroadcast library table. Rows stay compact lists, read through the field map."""

    def __init__(self, raw):
        raw = raw if isinstance(raw, dict) else {}
        mapping = raw.get("map") if isinstance(raw.get("map"), dict) else {}
        self.fields = {k: v for k, v in mapping.items() if type(v) is int and v >= 0}
        self.rows = {int(k): v for k, v in raw.items()
                     if str(k).isdigit() and isinstance(v, (list, dict))}

    def get(self, row_id, field, default=None):
        row = self.rows.get(row_id)
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(field, default)
        index = self.fields.get(field)
        if index is None or index >= len(row):
            return default
        return row[index]

    def __contains__(self, row_id):
        return row_id in self.rows

    def __iter__(self):
        return iter(self.rows)


class Library:
    def __init__(self, response, user_id, premium):
        raw = response.get("library", response)
        if not isinstance(raw, dict) or not all(k in raw for k in ("tracks", "albums", "artists")):
            raise Fail("iBroadcast didn't return a complete library. Try again with --refresh.")
        self.artists = Table(raw["artists"])
        self.albums = Table(raw["albums"])
        self.tracks = Table(raw["tracks"])
        self.playlists = Table(raw.get("playlists"))
        self.tags = Table(raw.get("tags"))
        self.expires = text(raw.get("expires"))
        self.user_id = user_id
        self.premium = premium
        self.username = ""
        self._discs = None
        self.plan_lock = threading.Lock()
        self._album_members = self._default_directories = None
        self._path_plans = {}

    def artist_name(self, artist_id):
        return text(self.artists.get(number(artist_id), "name"))

    def album_artist(self, album_id):
        return self.artist_name(self.albums.get(album_id, "artist_id")) or "Unknown Artist"

    def album_title(self, album_id):
        return text(self.albums.get(album_id, "name")) or "Unknown Album"

    def album_year(self, album_id, track_id=None):
        year = number(self.albums.get(album_id, "year"))
        if not year and track_id is not None:
            year = number(self.tracks.get(track_id, "year"))
        return year

    def disc(self, album_id):
        return number(self.albums.get(album_id, "disc")) or 1

    def multi_disc(self, album_id):
        """True when the same artist has more than one disc of this album title."""
        if self._discs is None:
            discs = {}
            for i in self.albums:
                if self.albums.get(i, "trashed"):
                    continue
                key = (number(self.albums.get(i, "artist_id")), fold(self.albums.get(i, "name")))
                discs.setdefault(key, set()).add(self.disc(i))
            self._discs = discs
        key = (number(self.albums.get(album_id, "artist_id")), fold(self.albums.get(album_id, "name")))
        return len(self._discs.get(key, ())) > 1 or self.disc(album_id) > 1

    def mime(self, track_id):
        return text(self.tracks.get(track_id, "type")).strip().lower()

    def format_name(self, track_id):
        mime = self.mime(track_id)
        ext = MIME_EXT.get(mime) or mime.split("/")[-1].replace("x-", "")
        return FORMAT_NAMES.get(ext, ext.upper() or "?")

    def genres(self, track_id):
        out = [text(self.tracks.get(track_id, "genre"))]
        extra = self.tracks.get(track_id, "genres_additional")
        if isinstance(extra, list):
            out += [text(g) for g in extra]
        return [g for g in out if g.strip()]

    def active(self, track_id):
        if self.tracks.get(track_id, "trashed"):
            return False
        album_id = number(self.tracks.get(track_id, "album_id"))
        return not self.albums.get(album_id, "trashed")


def load_library(session, home, refresh=False, quiet=False):
    """Use the cached library while it's current, otherwise download it again."""
    status, _ = session.api("status")
    user = status.get("user") if isinstance(status.get("user"), dict) else {}
    state = status.get("status") if isinstance(status.get("status"), dict) else {}
    user_id = text(user.get("id") or user.get("user_id"))
    premium = user.get("premium") in (True, 1, "1")
    lastmodified = text(state.get("lastmodified"))
    cache, meta_path = home / "library-cache.json.gz", home / "library-cache.meta.json"
    meta = read_object(meta_path)
    expires = number(meta.get("expires"))
    fresh = (meta.get("user_id") == user_id and lastmodified and meta.get("lastmodified") == lastmodified
             and (expires - time.time() > 3600 if expires else time.time() - number(meta.get("fetched_at")) < 6 * 3600))
    if fresh and not refresh and cache.exists():
        try:
            with gzip.open(cache, "rb") as f:
                library = Library(json.load(f), user_id, premium)
            library.username = text(user.get("username"))
            return library
        except (OSError, ValueError, EOFError, Fail):
            pass
    if not quiet:
        print("Downloading your library index from iBroadcast...", file=sys.stderr, flush=True)
    data, raw = session.api("library", url=LIBRARY_URL, timeout=600)
    library = Library(data, user_id, premium)
    library.username = text(user.get("username"))
    got = data.get("status") if isinstance(data.get("status"), dict) else {}
    compressed = gzip.compress(raw, compresslevel=5)
    with session.lock:
        if not session.active:
            raise Fail("This session has been signed out.")
        write_private(cache, compressed)
        write_private(meta_path, json.dumps({"user_id": user_id,
                                             "lastmodified": text(got.get("lastmodified")) or lastmodified,
                                             "expires": library.expires, "fetched_at": time.time()}).encode())
    return library


# -- selecting --------------------------------------------------------------------

def named_ids(table, pats, what):
    ids = {i for i in table if any_match(pats, table.get(i, "name"))}
    if pats and not ids:
        raise Fail(f"No {what} matches {', '.join(repr(p.raw) for p in pats)}.")
    return ids


def select(lib, args):
    """Track IDs that pass every filter, grouped by album and sorted for display."""
    artists, track_artists = patterns(args.artist), patterns(args.track_artist)
    albums, genres, formats = patterns(args.album), patterns(args.genre), patterns(args.format)
    years = [year_range(y) for y in args.year or []]
    album_ids = {number(i) for i in args.album_id or []}
    track_ids = {number(i) for i in args.track_id or []}
    only = None
    for table, pats, what in ((lib.tags, patterns(args.tag), "tag"),
                              (lib.playlists, patterns(args.playlist), "playlist")):
        if pats:
            wanted = set()
            for i in named_ids(table, pats, what):
                wanted.update(number(t) for t in table.get(i, "tracks") or [])
            only = wanted if only is None else only & wanted

    any_filter = any((artists, track_artists, albums, genres, formats, years, album_ids, track_ids,
                      only is not None))
    if not any_filter and not args.all:
        raise Fail("Add at least one filter, like --artist \"4hero\", or use --all for everything.")

    artist_ok, picked = {}, {}
    for track_id in (only if only is not None else lib.tracks):
        if track_id not in lib.tracks or not lib.active(track_id):
            continue
        if track_ids and track_id not in track_ids:
            continue
        album_id = number(lib.tracks.get(track_id, "album_id"))
        if album_ids and album_id not in album_ids:
            continue
        if artists:
            if album_id not in artist_ok:
                artist_ok[album_id] = any_match(artists, lib.album_artist(album_id), args.skip_the)
            if not artist_ok[album_id]:
                continue
        if albums and not any_match(albums, lib.album_title(album_id), args.skip_the):
            continue
        if track_artists and not any_match(track_artists, lib.artist_name(lib.tracks.get(track_id, "artist_id")),
                                           args.skip_the):
            continue
        if years:
            year = lib.album_year(album_id, track_id)
            if not any(low <= year <= high for low, high in years):
                continue
        if genres and not any(any_match(genres, g) for g in lib.genres(track_id)):
            continue
        if formats and not (any_match(formats, lib.format_name(track_id)) or any_match(formats, lib.mime(track_id))):
            continue
        picked.setdefault(album_id, []).append(track_id)
    return sort_groups(lib, picked)


def sort_groups(lib, picked):
    """[(album_id, [track_id, ...]), ...] by artist, album and disc, tracks in album order."""
    def album_key(album_id):
        return (fold(lib.album_artist(album_id)), fold(lib.album_title(album_id)),
                lib.album_year(album_id), lib.disc(album_id), album_id)

    def track_key(track_id):
        return (number(lib.tracks.get(track_id, "track")) or 9999, fold(lib.tracks.get(track_id, "title")), track_id)

    return [(a, sorted(set(picked[a]), key=track_key)) for a in sorted(picked, key=album_key)]


# -- paths --------------------------------------------------------------------------

class Layout:
    FIELDS = ("albumartist", "album", "year", "disc", "track", "disctrack", "title", "artist", "genre", "format")

    def __init__(self, template):
        template = template.replace("\\", "/").strip("/")
        try:
            template.format(**{f: "" for f in self.FIELDS})
        except (KeyError, IndexError, ValueError) as error:
            raise Fail(f"Unknown field in --layout: {error}. Use: {', '.join('{' + f + '}' for f in self.FIELDS)}") from None
        self.template = template

    def relpath(self, lib, album_id, track_id, ext):
        track_no = number(lib.tracks.get(track_id, "track"))
        disc = lib.disc(album_id)
        tn = f"{track_no:02}" if track_no else ""
        values = {
            "albumartist": lib.album_artist(album_id), "album": lib.album_title(album_id),
            "year": lib.album_year(album_id, track_id) or "", "disc": disc, "track": tn,
            "disctrack": f"{disc}-{tn}" if tn and lib.multi_disc(album_id) else tn,
            "title": text(lib.tracks.get(track_id, "title")) or f"Track {track_id}",
            "artist": lib.artist_name(lib.tracks.get(track_id, "artist_id")) or lib.album_artist(album_id),
            "genre": (lib.genres(track_id) or ["Unknown Genre"])[0], "format": lib.format_name(track_id),
        }
        values = {k: safe_name(v, 400) if isinstance(v, str) and v else v for k, v in values.items()}
        parts = []
        for part in self.template.format(**values).split("/"):
            part = re.sub(r"\s*\(\s*\)|\s*\[\s*\]", "", part)  # drop "()" left by an unknown year
            part = re.sub(r"^\s*[-.]\s+", "", part)
            parts.append(safe_name(part))
        parts[-1] = safe_name(parts[-1], 150 - len(ext) - 1) + "." + ext
        return Path(*parts)


def stream_path(lib, track_id, quality):
    path = text(lib.tracks.get(track_id, "file"))
    if not path.startswith("/"):
        return None
    if re.match(r"^/(?:96|128|192|256|320|orig)/", path):
        path = re.sub(r"^/[^/]+/", f"/{quality}/", path, count=1)
    return path


def extension(lib, track_id, quality):
    if quality != "orig":
        return "mp3"
    mime = lib.mime(track_id)
    ext = MIME_EXT.get(mime) or re.sub(r"[^a-z0-9]", "", mime.split("/")[-1].replace("x-", ""))
    return ext or "audio"


# -- downloading ---------------------------------------------------------------------

def plan_jobs(lib, groups, quality, layout, stop=None):
    """Reuse snapshot-local indexes; default layouts only name relevant albums."""
    def cancelled():
        if stop is not None and stop.is_set():
            raise Fail("Download preparation stopped.")

    cancelled()
    with lib.plan_lock:
        cancelled()
        if layout.template == DEFAULT_LAYOUT:
            if lib._album_members is None:
                members = {}
                for track_id in lib.tracks:
                    cancelled()
                    if lib.active(track_id):
                        album_id = number(lib.tracks.get(track_id, "album_id"))
                        members.setdefault(album_id, []).append(track_id)
                lib._album_members = members
            if lib._default_directories is None:
                directories = {}
                for album_id, tracks in lib._album_members.items():
                    cancelled()
                    samples = {}
                    if lib.album_year(album_id):
                        samples[lib.album_year(album_id)] = tracks[0]
                    else:
                        for tid in tracks:
                            samples.setdefault(lib.album_year(album_id, tid), tid)
                    for tid in samples.values():
                        parent = layout.relpath(lib, album_id, tid, "audio").parent
                        directories.setdefault(fold(str(parent)), set()).add(album_id)
                lib._default_directories = directories
            parents = {fold(str(layout.relpath(lib, a, t, "audio").parent))
                       for a, tracks in groups for t in tracks}
            album_ids = set()
            for parent in parents:
                album_ids.update(lib._default_directories.get(parent, ()))
            track_ids = [t for a in album_ids for t in lib._album_members[a]]
            cache_key = (quality, layout.template, tuple(sorted(parents)))
        else:
            # Arbitrary flat/cross-album templates need a global collision map once.
            track_ids, parents = None, None
            cache_key = (quality, layout.template)
        paths = lib._path_plans.get(cache_key)
        if paths is None:
            paths = _plan_paths(lib, quality, layout, track_ids, parents, cancelled)
            if len(lib._path_plans) >= 4:
                lib._path_plans.pop(next(iter(lib._path_plans)))
            lib._path_plans[cache_key] = paths
    cancelled()
    return [(paths[t], stream_path(lib, t, quality), t) for _, tracks in groups for t in tracks]


def _plan_paths(lib, quality, layout, track_ids, parents, cancelled):
    candidates = {}
    counts = {}
    for track_id in (lib.tracks if track_ids is None else track_ids):
        cancelled()
        if not lib.active(track_id):
            continue
        album_id = number(lib.tracks.get(track_id, "album_id"))
        rel = layout.relpath(lib, album_id, track_id, extension(lib, track_id, quality))
        if parents is not None and fold(str(rel.parent)) not in parents:
            continue
        if quality != "orig":
            rel = rel.with_name(f"{rel.stem} [{quality}kbps]{rel.suffix}")
        candidates[track_id] = rel
        key = fold(str(rel))
        counts[key] = counts.get(key, 0) + 1
    reserved, seen, paths = set(counts), set(), {}
    for track_id in sorted(candidates):
        cancelled()
        rel = candidates[track_id]
        if counts[fold(str(rel))] > 1:
            base, attempt = rel, 1
            while True:
                suffix = str(track_id) if attempt == 1 else f"{track_id}-{attempt}"
                rel = base.with_name(f"{base.stem} [{suffix}]{base.suffix}")
                if fold(str(rel)) not in reserved and fold(str(rel)) not in seen:
                    break
                attempt += 1
        seen.add(fold(str(rel)))
        paths[track_id] = rel
    return paths


def file_hasher(path):
    """SHA-256 state of a file's current contents, so appending can continue it."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest


def file_digest(path):
    return file_hasher(path).hexdigest()


def resolve_source(source, quality):
    """The download service has original files only; MP3 bitrates always stream."""
    if source in (None, "", "auto"):
        return "download" if quality == "orig" else "stream"
    if source not in SOURCES:
        raise Fail("Use download or stream as the source.")
    if source == "download" and quality != "orig":
        raise Fail("iBroadcast's download service only delivers original files. Use the stream source for MP3 bitrates.")
    return source


ZIP_TAIL = 64 * 1024  # held back while copying: the archive's directory and end marker follow the audio
EXTENSION_ALIASES = {".aif": ".aiff", ".wave": ".wav", ".oga": ".ogg", ".mp4": ".m4a"}


def copy_zip_entry(response, f, digest, suffix, stop, on_progress=None):
    """Copy the one stored file of a streamed single-track ZIP into f while hashing it.

    iBroadcast streams the archive: sizes and CRC-32 follow the data, then the central
    directory and the end marker. The last ZIP_TAIL bytes are held back until the end so
    only audio is written. Returns the audio length, or None when stopped. Raises
    ServiceError for an unexpected or incomplete archive.
    """
    chunks = iter(lambda: response.read(CHUNK), b"")
    buf = bytearray()

    def need(n):
        while len(buf) < n:
            chunk = next(chunks, None)
            if chunk is None:
                raise ServiceError("the download service's ZIP ended early", retry=True)
            buf.extend(chunk)

    need(30)
    sig, _, flags, method, _, _, crc, csize, _, nlen, xlen = struct.unpack("<IHHHHHIIIHH", bytes(buf[:30]))
    if sig != 0x04034B50:
        raise ServiceError("the download service didn't return a ZIP archive")
    if method != 0 or flags & 1:
        raise ServiceError("the download service returned a compressed or encrypted ZIP entry")
    need(30 + nlen + xlen)
    raw_name = bytes(buf[30:30 + nlen])
    name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", "replace")
    archived_suffix = Path(name.replace("\\", "/")).suffix.lower()
    if EXTENSION_ALIASES.get(archived_suffix, archived_suffix) != EXTENSION_ALIASES.get(suffix.lower(), suffix.lower()):
        raise ServiceError(f"the download service returned {Path(name).suffix or 'a file without extension'}, "
                           f"not {suffix}")
    start = 30 + nlen + xlen
    del buf[:start]
    written, crc_now = 0, 0
    first = True
    while True:
        chunk = b"" if first else next(chunks, None)
        first = False
        if chunk is None:
            break
        if stop.is_set():
            return None
        buf.extend(chunk)
        if len(buf) > ZIP_TAIL:
            data = bytes(buf[:-ZIP_TAIL])
            del buf[:-ZIP_TAIL]
            f.write(data)
            digest.update(data)
            crc_now = zlib.crc32(data, crc_now)
            written += len(data)
            if on_progress:
                on_progress(written, 0, len(data))

    tail = bytes(buf)
    if not tail.endswith(b"EOF\n"):
        raise ServiceError("the download service stopped before its end marker", retry=True)
    tail = tail[:-4]
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        raise ServiceError("the download service's ZIP has no directory", retry=True)
    _, disk, cd_disk, here, total, cd_size, cd_offset, comment = struct.unpack("<IHHHHIIH", tail[end:end + 22])
    if end + 22 + comment != len(tail) or disk or cd_disk:
        raise ServiceError("the download service returned an unexpected ZIP layout")
    if here != 1 or total != 1:
        raise ServiceError("the download service returned more than one file")
    cd = cd_offset - (start + written)  # tail-relative position of the central directory
    if cd_offset == 0xFFFFFFFF or cd < 0 or cd + cd_size != end or cd_size < 46:
        raise ServiceError("the download service returned an unexpected ZIP layout")
    (csig, _, _, _, cmethod, _, _, ccrc, ccsize, cusize, cnlen, _, _, _, _, _,
     local) = struct.unpack("<IHHHHHHIIIHHHHHII", tail[cd:cd + 46])
    if csig != 0x02014B50 or cmethod != 0 or local != 0 or ccsize != cusize or tail[cd + 46:cd + 46 + cnlen] != raw_name:
        raise ServiceError("the download service's ZIP directory doesn't match its file")
    data_end = cd
    if flags & 8:  # a data descriptor follows the data, with or without its signature
        for size_of in (16, 12):
            fields = tail[cd - 12:cd] if cd >= size_of else b""
            signed = size_of == 12 or tail[cd - 16:cd - 12] == b"PK\x07\x08"
            if len(fields) == 12 and signed and struct.unpack("<III", fields) == (ccrc, ccsize, cusize):
                data_end = cd - size_of
                break
        else:
            raise ServiceError("the download service's ZIP sizes don't match", retry=True)
    elif (crc, csize) != (ccrc, ccsize):
        raise ServiceError("the download service's ZIP sizes don't match", retry=True)
    data = tail[:data_end]
    f.write(data)
    digest.update(data)
    crc_now = zlib.crc32(data, crc_now)
    written += len(data)
    if on_progress and data:
        on_progress(written, 0, len(data))
    if written != ccsize or crc_now != ccrc:
        raise ServiceError("the downloaded file failed the ZIP's CRC-32 check", retry=True)
    return written


class TargetBusy(Fail):
    pass


LOCK_ERRORS = (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
_enforced_lock_dirs = set()
_enforced_guard = threading.Lock()


def check_lock_enforcement(directory):
    """Refuse lock directories where a second flock is silently granted, as on macOS SMB shares."""
    if os.name == "nt":
        return
    import fcntl
    key = str(directory)
    with _enforced_guard:
        if key in _enforced_lock_dirs:
            return
        probe = str(directory / "probe.lock")
        first = os.open(probe, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            second = os.open(probe, os.O_RDWR)
            try:
                try:
                    fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in LOCK_ERRORS:
                        raise
                    # Another ibfetch process holds the probe: that is enforcement too.
                else:
                    try:
                        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        if error.errno not in LOCK_ERRORS:
                            raise
                    else:
                        raise Fail(f"File locks are not enforced in {directory}. "
                                   "Keep ibfetch's settings folder on a local disk.")
                    finally:
                        fcntl.flock(first, fcntl.LOCK_UN)
            finally:
                os.close(second)
        finally:
            os.close(first)
        _enforced_lock_dirs.add(key)


@contextmanager
def target_lock(out, rel, stop=None, locks=None):
    """At most 4096 OS-owned slots in a local lock folder; collisions serialize, never unlink.

    Locks live in ibfetch's own settings folder, not beside the audio: network shares
    can grant every flock silently, which would protect nothing.
    """
    if locks is None:
        raise ValueError("target_lock needs ibfetch's local lock folder")
    directory = Path(locks)
    directory.mkdir(parents=True, exist_ok=True)
    check_lock_enforcement(directory)
    key = hashlib.sha256(fold(str((out / rel).resolve())).encode()).hexdigest()[:3]
    fd = os.open(str(directory / ("slot-" + key + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in LOCK_ERRORS:
                    raise
                if stop is None or stop.wait(0.1):
                    raise TargetBusy("another download owns this lock slot; wait for it to finish") from None
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_folder(home):
    return Path(home) / "locks"


MANIFEST_NAME = ".ibfetch.json"
_migrated_record_dirs = set()


class DownloadRecord:
    """One logical record in a folder's atomic manifest, guarded by local OS locks."""

    def __init__(self, audio, locks, partial=False):
        self.audio = Path(audio)
        self.locks = Path(locks)
        self.partial = partial
        self.manifest = self.audio.parent / MANIFEST_NAME
        self.section = "partials" if partial else "files"
        self.legacy = self.audio.with_name(self.audio.name + (".part" if partial else "") + ".ibfetch.json")

    def __eq__(self, other):
        return (isinstance(other, DownloadRecord) and self.audio == other.audio
                and self.locks == other.locks and self.partial == other.partial)

    @contextmanager
    def _locked(self):
        # Separate slot namespace: a target must never deadlock on its own slot.
        with target_lock(self.audio.parent, Path(MANIFEST_NAME), threading.Event(), self.locks / "manifests"):
            yield

    def _load(self):
        try:
            data = json.loads(self.manifest.read_text("utf-8"))
        except FileNotFoundError:
            data = {"version": 1, "files": {}, "partials": {}}
        except (OSError, ValueError) as error:
            raise Fail(f"Cannot read download records in {self.manifest}; preserved ({error.__class__.__name__}).") from None
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                or not isinstance(data.get("files"), dict) or not isinstance(data.get("partials"), dict)
                or any(not isinstance(v, dict) for section in ("files", "partials") for v in data[section].values())):
            raise Fail(f"Invalid download records in {self.manifest}; preserved. Restore the manifest before downloading here.")
        key = str(self.audio.parent.resolve())
        candidates = list(self.audio.parent.glob("*.ibfetch.json")) if key not in _migrated_record_dirs else [self.legacy]
        imported = []
        for path in candidates:
            if path == self.manifest or not path.is_file():
                continue
            raw = path.read_bytes()
            try:
                record = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            identity = record.get("identity") if isinstance(record, dict) else None
            if (not isinstance(identity, dict) or type(identity.get("version")) is not int or identity["version"] != 1
                    or not isinstance(identity.get("user_id"), str)
                    or type(identity.get("track_id")) is not int or identity["track_id"] <= 0
                    or identity.get("quality") not in QUALITIES or not isinstance(identity.get("source"), str)):
                continue
            name = path.name[:-len(".ibfetch.json")]
            partial = name.endswith(".part") and (record.get("completed") is True or "size" not in record)
            if not partial or record.get("completed") is True:
                if (type(record.get("size")) is not int or record["size"] <= 0
                        or not isinstance(record.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                    continue
            section, name = ("partials", name[:-5]) if partial else ("files", name)
            if name in data[section] and data[section][name] != record:
                if section == self.section and name == self.audio.name:
                    raise Fail(f"Conflicting legacy download record for {self.audio.name}; both records preserved.")
                continue
            data[section][name] = record
            imported.append((path, raw))
        if imported:
            self._save(data)
            # A crash here is safe: the next pass sees identical duplicate records.
            for path, raw in imported:
                if path.read_bytes() == raw:
                    path.unlink()
        _migrated_record_dirs.add(key)
        return data

    def _save(self, data):
        if not data["files"] and not data["partials"]:
            try:
                self.manifest.unlink()
            except FileNotFoundError:
                pass
            return
        write_private(self.manifest, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if os.name == "nt":
            import ctypes
            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(self.manifest))
            if attributes != -1:
                ctypes.windll.kernel32.SetFileAttributesW(str(self.manifest), attributes | 2)

    def exists(self):
        with self._locked():
            return self.audio.name in self._load()[self.section]

    def read_text(self, encoding="utf-8"):
        with self._locked():
            data = self._load()[self.section]
            if self.audio.name not in data:
                raise FileNotFoundError(str(self.manifest))
            return json.dumps(data[self.audio.name], ensure_ascii=False)

    def read_bytes(self):
        return self.read_text().encode("utf-8")

    def write_bytes(self, raw):
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            raise Fail("A download record must be an object.")
        with self._locked():
            data = self._load()
            data[self.section][self.audio.name] = record
            self._save(data)

    def write_text(self, value, encoding="utf-8"):
        self.write_bytes(value.encode(encoding))

    def unlink(self):
        with self._locked():
            data = self._load()
            if self.audio.name not in data[self.section]:
                raise FileNotFoundError(str(self.manifest))
            del data[self.section][self.audio.name]
            self._save(data)


def work_root(work, out):
    """Each download folder gets its own subfolder inside the work folder."""
    return Path(work) / hashlib.sha256(fold(str(Path(out).resolve())).encode()).hexdigest()[:16]


def check_work_folder(work, out):
    work_path, out_path = Path(work).resolve(), Path(out).resolve()
    if work_path == out_path or work_path in out_path.parents or out_path in work_path.parents:
        raise Fail("Use a work folder outside the download folder.")
    work_path.mkdir(parents=True, exist_ok=True)


def fetch_track(session, lib, out, rel, path, track_id, stop, on_progress=None, quality="orig",
                verify=False, adopt_existing=False, locks=None, work=None, source="stream"):
    """source: "download" for iBroadcast's download service, "stream" for the streaming server."""
    if source not in SOURCES or (source == "download" and quality != "orig"):
        raise ValueError("the download service only provides original files")
    try:
        with target_lock(out, rel, stop, locks):
            if work:
                return _fetch_via_work(session, lib, out, rel, path, track_id, stop, on_progress,
                                       quality, verify, adopt_existing, Path(work), locks, source)
            return _fetch_track(session, lib, out, rel, path, track_id, stop, on_progress,
                                quality, verify, adopt_existing, locks, source)
    except TargetBusy as error:
        if stop.is_set():
            return "stopped", 0, ""
        return "fail", 0, str(error)


def _same_track(record, lib, track_id, quality):
    recorded = record.get("identity")
    return (isinstance(recorded, dict) and recorded.get("user_id") == text(lib.user_id)
            and recorded.get("track_id") == track_id and recorded.get("quality") == quality)


def record_note(record):
    """Only our saved fallback notice is a success note; other text never authorizes a move."""
    note = record.get("note")
    return note if isinstance(note, str) and note.startswith("Fallback:") else ""


def _fetch_via_work(session, lib, out, rel, path, track_id, stop, on_progress, quality,
                    verify, adopt_existing, work, locks, source="stream"):
    """Download into a local work folder, then move each complete, hashed file to out."""
    target = out / rel
    local_out = work_root(work, out)
    local = local_out / rel
    local_record = DownloadRecord(local, locks)
    if target.exists():
        recovered = _recover_move(lib, track_id, quality, target, local, local_record, local_out)
        if recovered:
            return recovered
        result = _fetch_track(session, lib, out, rel, path, track_id, stop, on_progress,
                              quality, verify, adopt_existing, locks, source)
        if result[0] == "skip" and (not result[2] or result[2].startswith("Fallback:")):
            note = _discard_moved_copy(target, local, local_record, local_out)
            return "skip", 0, " ".join(n for n in (result[2], note) if n)
        return result
    state, nbytes, note = _fetch_track(session, lib, local_out, rel, path, track_id, stop,
                                       on_progress, quality, verify=verify, locks=locks, source=source)
    if state == "skip" and note and not note.startswith(("Recovered:", "Fallback:")):
        return "fail", 0, f"the copy in the work folder can't be moved: {note}"
    if state not in ("ok", "skip"):
        return state, nbytes, note
    record = read_object(local_record)
    saved_note = record_note(record)
    if (not saved_note and source == "download" and not record.get("adopted")
            and record.get("identity", {}).get("route") == "stream"):
        saved_note = ("Fallback: the copy in the work folder was fetched by streaming; "
                      "saved with the tags as uploaded, without iBroadcast's tag edits.")
        write_private(local_record, json.dumps({**record, "note": saved_note}).encode())
    try:
        moved = _move_to_output(local, local_record, target, local_out)
    except Fail as error:
        return "fail", 0, str(error)
    except OSError as error:
        return "fail", 0, ("downloaded to the work folder, but moving it failed "
                           f"({error.strerror or error}); it stays there and moves on the next run")
    return "ok", moved, saved_note or (note if state == "ok" else "")


def _move_to_output(local, local_record, target, local_out):
    record = read_object(local_record)
    info = local.stat()
    if record.get("size") != info.st_size or record.get("mtime_ns") != info.st_mtime_ns:
        raise Fail("the copy in the work folder changed before it could be moved; it is kept there")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".ibfetch-move")
    shutil.copyfile(str(local), str(temporary))
    if temporary.stat().st_size != info.st_size:
        raise OSError(errno.EIO, "the copied file has the wrong size")
    if target.exists():
        temporary.unlink()
        raise Fail("another file appeared at the target during the move; both are kept")
    os.replace(temporary, target)
    moved = target.stat()
    if moved.st_size != info.st_size:
        raise OSError(errno.EIO, "the moved file has the wrong size")
    # Network shares may set their own timestamp on rename; the baseline uses the final one.
    write_private(DownloadRecord(target, local_record.locks), json.dumps({
        "identity": record["identity"], "size": moved.st_size, "mtime_ns": moved.st_mtime_ns,
        "sha256": record["sha256"], "adopted": bool(record.get("adopted")),
        **({"note": record_note(record)} if record_note(record) else {})}).encode())
    _remove_local_copy(local, local_record, local_out)
    return moved.st_size


def _recover_move(lib, track_id, quality, target, local, local_record, local_out):
    """Restore a move interrupted after rename, including replacement of an old baseline."""
    target_record = DownloadRecord(target, local_record.locks)
    if not local.exists():
        return None
    record = read_object(local_record)
    existing = read_object(target_record)
    info = target.stat()
    if target_record.exists():
        if not _same_track(existing, lib, track_id, quality):
            return None
        if (existing.get("sha256") == record.get("sha256") and existing.get("size") == record.get("size") == info.st_size
                and existing.get("mtime_ns") == info.st_mtime_ns and record_note(existing) == record_note(record)
                and existing.get("identity") == record.get("identity")):
            return None  # already registered; the normal stat/verify path handles it
    if (not _same_track(record, lib, track_id, quality) or record.get("size") != info.st_size
            or local.stat().st_size != info.st_size or record.get("sha256") != file_digest(target)):
        return None
    after = target.stat()
    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
        return "skip", 0, "Modified: file changed during move recovery; preserved."
    write_private(target_record, json.dumps({
        "identity": record["identity"], "size": info.st_size, "mtime_ns": info.st_mtime_ns,
        "sha256": record["sha256"], "adopted": bool(record.get("adopted")),
        **({"note": record_note(record)} if record_note(record) else {})}).encode())
    note = _discard_moved_copy(target, local, local_record, local_out)
    return "skip", 0, " ".join(n for n in ("Recovered: an interrupted move from the work folder was completed.",
                                          record_note(record), note) if n)


def _discard_moved_copy(target, local, local_record, local_out):
    """Delete only a matching, unchanged leftover; return a note when preserving it."""
    if not local.exists():
        return ""
    record = read_object(local_record)
    target_record = read_object(DownloadRecord(target, local_record.locks))
    identity, moved_identity = record.get("identity"), target_record.get("identity")
    if (not isinstance(identity, dict) or not isinstance(moved_identity, dict)
            or any(k not in identity or identity[k] != moved_identity.get(k)
                   for k in ("user_id", "track_id", "quality"))
            or not record.get("sha256") or record["sha256"] != target_record.get("sha256")
            or record.get("size") != target_record.get("size")):
        return "Unverified: leftover audio in the work folder has no matching moved baseline; preserved."
    info = local.stat()
    if (record.get("size") != info.st_size or record.get("mtime_ns") != info.st_mtime_ns
            or record["sha256"] != file_digest(local)):
        return "Modified: leftover audio in the work folder changed; preserved."
    try:
        _remove_local_copy(local, local_record, local_out)
    except Fail as error:
        return str(error)
    return ""


def _remove_local_copy(local, local_record, local_out):
    # Recheck immediately before cleanup, including after a leftover's checksum.
    # Fresh downloads keep their streaming digest and do not need an extra read.
    if local.exists():
        record, info = read_object(local_record), local.stat()
        if record.get("size") != info.st_size or record.get("mtime_ns") != info.st_mtime_ns:
            raise Fail("Modified: audio in the work folder changed before cleanup; preserved.")
    for path in (local, local_record):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    parent = local.parent
    while parent != local_out and local_out in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _fetch_track(session, lib, out, rel, path, track_id, stop, on_progress, quality,
                 verify=False, adopt_existing=False, locks=None, source="stream"):
    """Download one file to out/rel, from the download service or by streaming.

    Returns (state, bytes, note) with state ok, skip, fail or stopped.
    on_progress(received, expected, new_bytes) is called while data arrives;
    expected is 0 when the size isn't known in advance.
    The SHA-256 is computed while the data arrives, so completion never reads
    the file again; a resumed .part is read once to continue its checksum.
    Only the streaming server resumes a .part. The download service starts each
    attempt over, and falls back to streaming when it can't deliver the file.
    """
    final = out / rel
    record_path = DownloadRecord(final, locks)
    part = final.with_name(final.name + ".part")
    part_record = DownloadRecord(final, locks, partial=True)
    # "route" tells how the bytes were fetched. Completed files match without it, so a
    # track saved by either route is skipped; partial files must match it to resume.
    identity = {"version": 1, "user_id": text(lib.user_id), "track_id": track_id,
                "quality": quality, "source": text(lib.tracks.get(track_id, "file")), "route": source}

    def save_record(digest, adopted=False, expected=None, note="", provenance=None):
        info = final.stat()
        if expected and (info.st_size, info.st_mtime_ns) != (expected.st_size, expected.st_mtime_ns):
            return False
        write_private(record_path, json.dumps({"identity": provenance if provenance is not None else identity,
                      "size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": digest, "adopted": adopted,
                      **({"note": note} if note else {})}).encode())
        return True

    def matches(record):
        recorded = record.get("identity")
        return isinstance(recorded, dict) and all(recorded.get(k) == identity[k]
                                                  for k in ("user_id", "track_id", "quality"))

    def cleanup_pending(record):
        pending = read_object(part_record)
        if (not part.exists() and pending.get("completed") is True and matches(pending)
                and pending.get("sha256") == record.get("sha256")
                and pending.get("size") == record.get("size")):
            part_record.unlink()

    if final.exists():
        record = read_object(record_path)
        recorded = record.get("identity")
        matching = matches(record)
        info = final.stat()
        if not final.is_file() or not info.st_size:
            return "skip", 0, "Unverified: existing target is empty or not a regular file; preserved."
        if isinstance(recorded, dict) and not matching:
            return "skip", 0, "Unverified: existing file belongs to another track, account or quality; preserved."
        # A completion receipt was saved before rename. A partial identity alone is
        # insufficient: validate the completed length and checksum before recovery.
        pending = read_object(part_record)
        unchanged = matching and record.get("size") == info.st_size and record.get("mtime_ns") == info.st_mtime_ns
        registered_pending = (unchanged and pending.get("sha256") == record.get("sha256")
                              and pending.get("size") == record.get("size")
                              and pending.get("identity") == record.get("identity")
                              and record_note(pending) == record_note(record))
        if ((not record_path.exists() or matching) and not registered_pending
                and not part.exists() and pending.get("completed") is True
                and matches(pending) and pending.get("size") == info.st_size
                and pending.get("sha256") == file_digest(final)):
            if not save_record(pending["sha256"], expected=info, note=record_note(pending), provenance=pending["identity"]):
                return "skip", 0, "Modified: file changed during crash recovery; preserved."
            part_record.unlink()
            return "skip", 0, "Recovered: completed download receipt restored after interruption." + (
                " " + record_note(pending) if record_note(pending) else "")
        if adopt_existing and not unchanged:
            if not save_record(file_digest(final), adopted=True, expected=info,
                               note=record_note(record), provenance=recorded if matching else None):
                return "skip", 0, "Modified: file changed during adoption; baseline left unchanged."
            return "skip", 0, "Adopted: existing file accepted as a local baseline, not remotely verified."
        if not matching:
            return "skip", 0, "Unverified: existing file preserved; use explicit adoption only if you trust it."
        if verify:
            if record.get("sha256") != file_digest(final):
                return "skip", 0, "Modified: checksum differs from the saved baseline; preserved."
            if not save_record(record["sha256"], adopted=bool(record.get("adopted")), expected=info,
                               note=record_note(record), provenance=recorded):
                return "skip", 0, "Modified: file changed during verification; baseline left unchanged."
            cleanup_pending(record)
            return "skip", 0, record_note(record)
        if unchanged:
            cleanup_pending(record)
            return "skip", 0, record_note(record)
        if "mtime_ns" not in record:
            return "skip", 0, "Unverified: older record needs full verification or explicit adoption; preserved."
        return "skip", 0, "Modified: local size or modification time changed; preserved."
    final.parent.mkdir(parents=True, exist_ok=True)

    def without_route(value):
        return {k: v for k, v in value.items() if k != "route"} if isinstance(value, dict) else None

    def discard_part():
        for leftover in (part, part_record):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass

    if part.exists():
        recorded = read_object(part_record).get("identity")
        if without_route(recorded) != without_route(identity):
            return "fail", 0, "partial file has no matching download record; choose another folder"
        if source == "download" or recorded.get("route", "stream") != "stream":
            discard_part()  # this track's own incomplete download; only a streamed one can resume

    hashing = {"state": None, "length": 0}  # checksum of the bytes written to .part so far

    def checksum_for(length):
        if hashing["state"] is None or hashing["length"] != length:
            hashing["state"] = file_hasher(part) if length else hashlib.sha256()
            hashing["length"] = part.stat().st_size if length else 0
        return hashing["state"]

    def finish(note=""):
        length = part.stat().st_size
        if not length:
            return "fail", 0, "iBroadcast returned an empty audio file"
        digest = checksum_for(length).hexdigest()
        write_private(part_record, json.dumps({"identity": identity, "completed": True,
                      "size": length, "sha256": digest, **({"note": note} if note else {})}).encode())
        os.replace(part, final)
        # Network shares may give the renamed file their own timestamp. The content was
        # hashed as it was written under this target's lock, so size and the final stat
        # are the baseline; reading the whole file again would only double the traffic.
        renamed = final.stat()
        if renamed.st_size != length or not save_record(digest, expected=renamed, note=note):
            return "fail", 0, "file changed locally during completion; preserved without a new baseline"
        part_record.unlink()
        return "ok", length, note

    fallback = service_note = ""
    if source == "download":
        note = "failed"
        for attempt in range(3):
            if stop.is_set() or (attempt and stop.wait(3 * attempt)):
                return "stopped", 0, ""
            try:
                response = session.download(track_id, lib.user_id)
            except ServiceError as error:
                note = str(error)
                if error.retry:
                    continue
                break
            except NETWORK_ERRORS as error:
                note = f"network error ({error.__class__.__name__})"
                continue
            digest = hashlib.sha256()
            try:
                with response:
                    write_private(part_record, json.dumps({"identity": identity}).encode())
                    with open(part, "wb") as f:
                        length = copy_zip_entry(response, f, digest, final.suffix, stop, on_progress)
            except ServiceError as error:
                note = str(error)
                if error.retry:
                    continue
                break
            except NETWORK_ERRORS as error:
                note = f"connection dropped ({error.__class__.__name__})"
                continue
            if length is None:
                return "stopped", 0, ""
            hashing["state"], hashing["length"] = digest, length
            return finish()
        # The streaming server still has the original upload: finish the track from there.
        service_note = note
        fallback = f"Fallback: {note}; saved the file as uploaded, without iBroadcast's tag edits."
        identity = dict(identity, route="stream")
        discard_part()
        hashing["state"], hashing["length"] = None, 0

    if not path:
        return "fail", 0, "no stream path in the library, try refreshing the library"
    note = "failed"
    for attempt in range(4):
        if stop.is_set():
            return "stopped", 0, ""
        if attempt and stop.wait(3 * attempt):
            return "stopped", 0, ""
        offset = part.stat().st_size if part.exists() else 0
        try:
            response = session.stream(path, track_id, lib.user_id, lib.expires, offset)
        except NETWORK_ERRORS as error:
            note = f"network error ({error.__class__.__name__})"
            continue
        with response:
            code = status_of(response)
            if code == 416 and offset:
                total = re.fullmatch(r"bytes \*/(\d+)", response.headers.get("Content-Range") or "")
                if total and int(total.group(1)) == offset:
                    return finish(fallback)
                return "fail", 0, "server range does not confirm the partial file is complete"
            if code in (401, 403):
                return "fail", 0, f"iBroadcast refused it (HTTP {code}), refresh the library or sign in again"
            if code == 404:
                return "fail", 0, "not found on iBroadcast (HTTP 404), refresh the library"
            if code not in (200, 206):
                note = f"HTTP {code}"
                continue
            if textual(response):
                return "fail", 0, "iBroadcast returned a non-audio response"
            if code == 200:
                offset = 0  # the server sent the whole file, start over
            expected = number(response.headers.get("Content-Length"))
            if code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range") or "")
                if not match:
                    return "fail", 0, "missing or invalid Content-Range on resumed download"
                start, end, total = map(int, match.groups())
                if start != offset or end < start or end >= total or (expected and expected != end - start + 1):
                    return "fail", 0, "server returned an unexpected byte range"
                expected = end - start + 1
            else:
                total = expected
            received = 0
            try:
                if stop.is_set():
                    return "stopped", 0, ""
                checksum = checksum_for(offset)
                if not part.exists():
                    write_private(part_record, json.dumps({"identity": identity}).encode())
                with open(part, "ab" if offset else "wb") as f:
                    while True:
                        if stop.is_set():
                            return "stopped", 0, ""
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        checksum.update(chunk)
                        hashing["length"] += len(chunk)
                        received += len(chunk)
                        if on_progress:
                            on_progress(offset + received, offset + expected if expected else 0, len(chunk))
            except NETWORK_ERRORS as error:
                note = f"connection dropped ({error.__class__.__name__})"
                continue
        if expected and received != expected:
            note = f"incomplete ({size(received)} of {size(expected)})"
            continue
        if total and offset + received < total:
            note = "server returned only part of the remaining file"
            continue
        return finish(fallback)
    if fallback:
        return "fail", 0, f"{service_note}; streaming also failed: {note}"
    return "fail", 0, note


class Transfer:
    """Downloads a list of jobs with a few workers. snapshot() is safe to call from any thread."""

    def __init__(self, session, lib, jobs, out, workers, on_item=None, quality="orig",
                 verify=False, adopt_existing=False, prepare=None, total=None, valid=None,
                 locks=None, work=None, source="stream"):
        self.session, self.lib, self.jobs, self.out = session, lib, jobs, out
        self.workers, self.on_item = workers, on_item
        self.quality, self.source = quality, source
        self.locks, self.work = locks, work
        self.verify, self.adopt_existing = verify, adopt_existing
        self.prepare, self.valid = prepare, valid
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.total, self.done = len(jobs) if total is None else total, 0
        self.counts = {"ok": 0, "skip": 0, "fail": 0}
        self.bytes = 0        # size of the files finished this run
        self.moved = 0        # bytes received this run, for the speed
        self.live = {}        # rel -> [received, expected]
        self.failures = []
        self.existing = []
        self.notes = []       # saved files with a remark, such as a streaming fallback
        self.recent = deque(maxlen=40)
        self.samples = deque()
        self.futures = []
        self.state = "ready"
        self.started = self.finished = None

    def _work(self, rel, path, track_id):
        def progress(received, expected, new):
            with self.lock:
                self.live[rel] = [received, expected]
                self.moved += new
        with self.lock:  # visible while the download service prepares the file
            self.live[rel] = [0, 0]
        try:
            return fetch_track(self.session, self.lib, self.out, rel, path, track_id, self.stop,
                               progress, self.quality, self.verify, self.adopt_existing,
                               self.locks, self.work, self.source)
        except Fail as error:
            return "fail", 0, str(error)
        except Exception as error:  # one bad file shouldn't stop the rest
            return "fail", 0, f"{error.__class__.__name__}: {error}"
        finally:
            with self.lock:
                self.live.pop(rel, None)

    def _finish(self, rel, state, nbytes, note):
        if state == "stopped":
            return
        with self.lock:
            self.done += 1
            self.counts[state] += 1
            self.bytes += nbytes
            if state == "fail":
                self.failures.append({"name": str(rel), "note": note})
            if state == "skip" and note:
                self.existing.append({"name": str(rel), "note": note})
            if state == "ok" and note:
                self.notes.append({"name": str(rel), "note": note})
            self.recent.appendleft({"name": str(rel), "state": state, "bytes": nbytes, "note": note})
            done = self.done
        if self.on_item:
            self.on_item(done, self.total, state, rel, nbytes, note)

    def run(self):
        self.state, self.started = "planning", time.time()
        try:
            if self.prepare:
                self.jobs = self.prepare(self.stop)
            if self.valid and not self.valid():
                self.stop.set()
            if self.stop.is_set():
                self.finished, self.state = time.time(), "stopped"
                return
            self.total = len(self.jobs)
        except Exception as error:
            if not self.stop.is_set():
                self.total = 1
                self._finish("Download preparation", "fail", 0, str(error))
            self.finished = time.time()
            self.state = "stopped" if self.stop.is_set() else "done"
            return
        self.state = "running"
        pool = ThreadPoolExecutor(max_workers=self.workers)
        self.futures = {pool.submit(self._work, rel, path, tid): rel for rel, path, tid in self.jobs}
        try:
            for future in as_completed(self.futures):
                if future.cancelled():
                    continue
                state, nbytes, note = future.result()
                self._finish(self.futures[future], state, nbytes, note)
        except KeyboardInterrupt:
            self.cancel()
        finally:
            pool.shutdown(wait=True)
            self.finished = time.time()
            self.state = "stopped" if self.stop.is_set() else "done"

    def cancel(self):
        self.stop.set()
        for future in list(self.futures):
            future.cancel()

    def snapshot(self):
        now = time.time()
        with self.lock:
            self.samples.append((now, self.moved))
            while len(self.samples) > 2 and now - self.samples[0][0] > 8:
                self.samples.popleft()
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
            speed = (b1 - b0) / (t1 - t0) if t1 - t0 > 0.5 and self.state == "running" else 0
            elapsed = (self.finished or now) - (self.started or now)
            left = self.total - self.done
            eta = elapsed / self.done * left if self.done >= 3 and self.state == "running" else None
            return {
                "state": self.state, "total": self.total, "done": self.done, **self.counts,
                "bytes": self.bytes + sum(r for r, _ in self.live.values()), "speed": speed,
                "elapsed": elapsed, "eta": eta, "out": str(self.out.resolve()),
                "active": [{"name": str(k), "received": v[0], "expected": v[1]} for k, v in self.live.items()],
                "recent": list(self.recent)[:12], "failures": self.failures[-200:],
                "existing": self.existing[-200:], "existing_count": len(self.existing),
                "notes": self.notes[-200:], "notes_count": len(self.notes), "source": self.source,
            }


def run_downloads(session, lib, groups, args, home=None):
    out = Path(args.out).expanduser()
    work = getattr(args, "work_folder", None)
    work = Path(work).expanduser() if work else None
    if work is not None:
        if not work.is_absolute():
            raise Fail("Use a full path for --work-folder, like ~/ibfetch-work.")
        check_work_folder(work, out)
    source = resolve_source(getattr(args, "source", None), args.quality)
    jobs = plan_jobs(lib, groups, args.quality, Layout(args.layout))
    width = len(str(len(jobs)))

    def report(done, total, state, rel, nbytes, note):
        label = {"ok": "ok  ", "skip": "skip", "fail": "FAIL"}[state]
        extra = (f"  {size(nbytes)}" + (f"  {note}" if note else "")) if state == "ok" else f"  {note}" if note else ""
        print(f"[{done:>{width}}/{total}] {label} {rel}{extra}", flush=True)

    transfer = Transfer(session, lib, jobs, out, args.jobs, on_item=report, quality=args.quality,
                        verify=args.verify, adopt_existing=args.adopt_existing,
                        locks=lock_folder(home) if home is not None else None, work=work, source=source)
    transfer.run()
    if transfer.state == "stopped":
        print("\nStopped. Run the same command again to pick up where it left off.", file=sys.stderr)
        return 130
    c = transfer.counts
    print(f"\nDone in {duration(transfer.finished - transfer.started)}: {c['ok']} downloaded "
          f"({size(transfer.bytes)}), {c['skip']} skipped, {c['fail']} failed.")
    print(f"Saved in {out.resolve()}")
    notes = {label: sum(item["note"].startswith(label + ":") for item in transfer.existing)
             for label in ("Unverified", "Modified", "Adopted", "Recovered")}
    if any(notes.values()):
        print("Existing files: " + ", ".join(f"{count} {label.lower()}" for label, count in notes.items() if count) + ".")
    if notes["Unverified"] or notes["Modified"]:
        print("Audio preserved. Use --verify to check a saved checksum, or --adopt-existing only to trust a local baseline.")
    if transfer.notes:
        count = len(transfer.notes)
        print(f"{count} track{'s' if count != 1 else ''} came from the streaming server, with the tags as uploaded, "
              "not iBroadcast's edits. See the notes for the reason.")
    if transfer.failures:
        print("\nFailed:")
        for item in transfer.failures[:50]:
            print(f"  {item['name']}: {item['note']}")
        if len(transfer.failures) > 50:
            print(f"  ...and {len(transfer.failures) - 50} more")
        print("Check the failure details before retrying; temporary network failures can be retried directly.")
        return 1
    return 0


# -- commands ---------------------------------------------------------------------------

NOTHING = 'Nothing matches these filters. Tip: ibfetch artists "name*" shows how artists are spelled.'


def print_selection(lib, groups, show_tracks):
    total_tracks = total_length = 0
    for album_id, track_ids in groups:
        length = sum(number(lib.tracks.get(t, "length")) for t in track_ids)
        formats = sorted({lib.format_name(t) for t in track_ids})
        year = lib.album_year(album_id, track_ids[0])
        disc = f" [disc {lib.disc(album_id)}]" if lib.multi_disc(album_id) else ""
        print(f"{lib.album_artist(album_id)} - {lib.album_title(album_id)}"
              f"{f' ({year})' if year else ''}{disc}  ·  {len(track_ids)} tracks  ·  "
              f"{'/'.join(formats)}  ·  {duration(length)}  ·  id {album_id}")
        if show_tracks:
            for t in track_ids:
                no = number(lib.tracks.get(t, "track"))
                print(f"    {no:>2}. {lib.tracks.get(t, 'title')}  ({duration(number(lib.tracks.get(t, 'length')))})")
        total_tracks += len(track_ids)
        total_length += length
    print(f"\n{len(groups)} albums, {total_tracks} tracks, {duration(total_length)} of music.")
    return total_tracks


def cmd_login(args, home):
    client_id = args.client_id or os.environ.get("IBROADCAST_CLIENT_ID") or DEFAULT_CLIENT_ID
    data = device_start(client_id)
    print(f"\nOpen  {data['verification_uri']}")
    print(f"Code  {data.get('user_code')}")
    if data.get("verification_uri_complete"):
        print(f"\nOr open this link, the code is already filled in:\n{data['verification_uri_complete']}")
    print("\nWaiting for you to approve ibfetch in iBroadcast...", flush=True)
    tokens = device_poll(client_id, data)
    write_private(home / "tokens.json", json.dumps(tokens, indent=2).encode())
    data, _ = Session(home).api("status")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    premium = user.get("premium") in (True, 1, "1")
    print(f"\nSigned in as {user.get('username') or 'your account'}"
          f"{' (Premium)' if premium else ' (free account: only 128 kbps MP3 downloads)'}.")
    return 0


def sign_out(home):
    """Revoke the sign-in when possible and remove it with the cached library index."""
    path = home / "tokens.json"
    tokens = read_object(path)
    if tokens.get("refresh_token"):
        try:
            oauth_post("revoke", {"client_id": tokens.get("client_id", DEFAULT_CLIENT_ID),
                                  "refresh_token": tokens["refresh_token"]})
        except Fail:
            pass
    for name in ("tokens.json", "library-cache.json.gz", "library-cache.meta.json"):
        try:
            (home / name).unlink()
        except FileNotFoundError:
            pass


def cmd_logout(args, home):
    sign_out(home)
    print("Signed out. The cached library index is removed too.")
    return 0


def cmd_artists(args, home):
    session = Session(home)
    lib = load_library(session, home, args.refresh)
    pats = patterns(args.pattern or ["*"])
    counts = {}
    for album_id in lib.albums:
        if lib.albums.get(album_id, "trashed"):
            continue
        name = lib.album_artist(album_id)
        if any_match(pats, name, args.skip_the):
            tracks = [t for t in lib.albums.get(album_id, "tracks") or [] if number(t) in lib.tracks
                      and lib.active(number(t))]
            if tracks:
                entry = counts.setdefault(name, [0, 0])
                entry[0] += 1
                entry[1] += len(tracks)
    for name in sorted(counts, key=fold):
        albums, tracks = counts[name]
        print(f"{name}  ·  {albums} album{'s' if albums != 1 else ''}, {tracks} tracks")
    print(f"\n{len(counts)} album artists.")
    return 0


def cmd_list(args, home):
    session = Session(home)
    lib = load_library(session, home, args.refresh)
    groups = select(lib, args)
    if not groups:
        print(NOTHING)
        return 1
    print_selection(lib, groups, args.tracks)
    return 0


def cmd_get(args, home):
    if args.quality not in QUALITIES:
        raise Fail(f"Use one of these qualities: {', '.join(QUALITIES)}")
    source = resolve_source(args.source, args.quality)
    session = Session(home)
    lib = load_library(session, home, args.refresh)
    if args.quality != "128" and not lib.premium and not args.dry_run:
        raise Fail("Original files and bitrates other than 128 need iBroadcast Premium. Use --quality 128.")
    groups = select(lib, args)
    if not groups:
        print(NOTHING)
        return 1
    total = sum(len(t) for _, t in groups)
    if args.dry_run:
        print_selection(lib, groups, args.tracks)
        return 0
    if total > CONFIRM_ABOVE and not args.yes:
        print(f"This selects {len(groups)} albums with {total} tracks.")
        if not sys.stdin.isatty():
            raise Fail("Add --yes to download this many tracks without a prompt.")
        if input("Download them all? [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
            print("Nothing downloaded.")
            return 1
    via = {"download": "iBroadcast's download service, with its current tags",
           "stream": "the streaming server, as uploaded"}[source] if args.quality == "orig" else args.quality
    print(f"Downloading {total} tracks from {len(groups)} albums ({via}, {args.jobs} at a time).")
    if args.work_folder:
        print(f"Each finished track moves from the work folder {Path(args.work_folder).expanduser()} to {Path(args.out).expanduser()}.")
    print()
    return run_downloads(session, lib, groups, args, home)


# -- the app (a local page in your browser) --------------------------------------------

GUI_PORT = 8765
QUALITY_LABELS = {"orig": "Original file", "320": "MP3 320 kbps", "256": "MP3 256 kbps",
                  "192": "MP3 192 kbps", "128": "MP3 128 kbps", "96": "MP3 96 kbps"}


def build_meta(lib):
    """What the app needs to fill its artist list and pickers."""
    artists, album_count = {}, 0
    for album_id in lib.albums:
        if lib.albums.get(album_id, "trashed"):
            continue
        tracks = [number(t) for t in lib.albums.get(album_id, "tracks") or []]
        n = sum(1 for t in tracks if t in lib.tracks and lib.active(t))
        if not n:
            continue
        album_count += 1
        entry = artists.setdefault(lib.album_artist(album_id), [0, 0])
        entry[0] += 1
        entry[1] += n
    genres, formats, active = {}, {}, 0
    for track_id in lib.tracks:
        if not lib.active(track_id):
            continue
        active += 1
        genre = text(lib.tracks.get(track_id, "genre")).strip()
        if genre:
            genres[genre] = genres.get(genre, 0) + 1
        fmt = lib.format_name(track_id)
        formats[fmt] = formats.get(fmt, 0) + 1

    def names(table):
        out = set()
        for i in table:
            name = text(table.get(i, "name")).strip()
            if name and table.get(i, "tracks"):
                out.add(name)
        return sorted(out, key=fold)

    return {
        "artists": [[name, a, t] for name, (a, t) in sorted(artists.items(), key=lambda kv: fold(kv[0]))],
        "genres": sorted(genres, key=fold), "formats": sorted(formats, key=lambda f: -formats[f]),
        "tags": names(lib.tags), "playlists": names(lib.playlists),
        "counts": {"tracks": active, "albums": album_count, "artists": len(artists)},
    }


def choose_folder(start):
    """Show the system's folder picker. Returns a path, "" when cancelled, None when unavailable."""
    prompt = "Choose where ibfetch saves your music"
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        command = ["osascript", "-e", script]
    elif os.name == "nt":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.Description = '{prompt}'; $d.SelectedPath = $env:IBFETCH_START;"
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        command = ["powershell", "-NoProfile", "-STA", "-Command", ps]
    elif shutil.which("zenity"):
        command = ["zenity", "--file-selection", "--directory", f"--title={prompt}", f"--filename={start}/"]
    elif shutil.which("kdialog"):
        command = ["kdialog", "--getexistingdirectory", start, "--title", prompt]
    else:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900,
                                env={**os.environ, "IBFETCH_START": start})
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().rstrip("/\\") if result.returncode == 0 else ""


def open_folder(path):
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: the Windows way to open a folder
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    """State behind the browser page. Every method is called from a request thread."""

    def __init__(self, home):
        self.home = home
        self.lock = threading.RLock()
        self.login_generation = self.library_generation = 0
        self.closed = self.signing_out = False
        self.session = None
        self.login = None
        self.login_cancel = None
        self.login_request_id = None
        self.lib = self.meta = None
        self.lib_state, self.lib_error, self.lib_version = "none", "", 0
        self.transfer = None
        self.last_items = []
        self.last_options = {}
        self.settings = {"out": str(Path.home() / "Music" / "iBroadcast"), "quality": "orig", "source": "download",
                         "jobs": 3, "layout": DEFAULT_LAYOUT, "work": "", "appearance": "auto", "palette": "forest",
                         "auto_updates": True}
        saved = read_object(home / "app-settings.json")
        for key in ("out", "layout"):
            if isinstance(saved.get(key), str) and saved[key].strip():
                self.settings[key] = saved[key]
        if isinstance(saved.get("work"), str):
            self.settings["work"] = saved["work"].strip()
        if saved.get("quality") in QUALITIES:
            self.settings["quality"] = saved["quality"]
        if saved.get("source") in SOURCES:
            self.settings["source"] = saved["source"]
        if type(saved.get("jobs")) is int and 1 <= saved["jobs"] <= 8:
            self.settings["jobs"] = saved["jobs"]
        if saved.get("appearance") in ("auto", "light", "dark"):
            self.settings["appearance"] = saved["appearance"]
        if saved.get("palette") in ("forest", "ocean", "warm", "ibroadcast", "amber"):
            self.settings["palette"] = saved["palette"]
        if type(saved.get("auto_updates")) is bool:
            self.settings["auto_updates"] = saved["auto_updates"]
        self.updates = UpdateChecker(home, self.settings["auto_updates"])
        self.updates_started = False
        try:
            self.session = Session(home)
        except Fail:
            pass
        if self.session:
            self.load_library()

    # local appearance
    def start_updates(self):
        self.updates_started = True
        self.updates.check()

    def set_updates(self, body):
        enabled = body.get("automatic")
        if type(enabled) is not bool:
            raise Fail("Choose whether to check automatically.")
        with self.lock:
            settings = {**self.settings, "auto_updates": enabled}
            write_private(self.home / "app-settings.json", json.dumps(settings, indent=2).encode())
            self.settings = settings
            with self.updates.lock:
                self.updates.automatic = enabled
        if self.updates_started:
            self.updates.check()
        return self.updates.snapshot()

    def set_appearance(self, body):
        appearance, palette = body.get("appearance"), body.get("palette")
        if appearance not in ("auto", "light", "dark") or palette not in ("forest", "ocean", "warm", "ibroadcast", "amber"):
            raise Fail("Choose a listed appearance and colour palette.")
        with self.lock:
            settings = {**self.settings, "appearance": appearance, "palette": palette}
            write_private(self.home / "app-settings.json", json.dumps(settings, indent=2).encode())
            self.settings = settings
        return {}

    # library
    def load_library(self, refresh=False):
        with self.lock:
            if self.lib_state == "loading" or not self.session or self.closed or self.signing_out:
                return
            self.lib_state, self.lib_error = "loading", ""
            self.library_generation += 1
            generation, session = self.library_generation, self.session
        threading.Thread(target=self._load, args=(refresh, session, generation), daemon=True).start()

    def _load(self, refresh, session, generation):
        try:
            lib = load_library(session, self.home, refresh, quiet=True)
            meta = build_meta(lib)
        except Exception as error:
            with self.lock:
                if session is self.session and generation == self.library_generation and not self.closed:
                    self.lib_state = "error"
                    self.lib_error = str(error) if isinstance(error, Fail) else f"{error.__class__.__name__}: {error}"
            return
        with self.lock:
            if session is not self.session or generation != self.library_generation or self.closed:
                return
            self.lib, self.meta = lib, meta
            self.lib_state, self.lib_version = "ready", self.lib_version + 1

    def require_lib(self):
        if not self.lib:
            raise Fail("The library isn't loaded yet.")
        return self.lib

    # sign-in
    def start_login(self):
        with self.lock:
            if self.closed or self.signing_out:
                raise Fail("The app is closing or signing out. Please wait.")
            if self.session:
                return
            if self.login_cancel:
                self.login_cancel.set()
            self.login_generation += 1
            generation = self.login_generation
            cancel = self.login_cancel = threading.Event()
            self.login_request_id = secrets.token_urlsafe(18)
            self.login = {"state": "starting", "request_id": self.login_request_id}
        client_id = os.environ.get("IBROADCAST_CLIENT_ID") or DEFAULT_CLIENT_ID
        try:
            data = device_start(client_id)
        except (Fail, OSError) as error:
            with self.lock:
                if not cancel.is_set() and generation == self.login_generation and not self.closed:
                    self.login = {"state": "error", "message": str(error),
                                  "request_id": self.login_request_id}
            raise
        with self.lock:
            if cancel.is_set() or generation != self.login_generation or self.closed:
                return
            self.login = {"state": "waiting", "code": data.get("user_code"),
                          "url": data.get("verification_uri_complete") or data["verification_uri"],
                          "plain_url": data["verification_uri"],
                          "request_id": self.login_request_id,
                          "expires_at": time.time() + (number(data.get("expires_in")) or 900)}
            public = dict(self.login)
        threading.Thread(target=self._poll_login, args=(client_id, data, cancel, generation), daemon=True).start()
        return public

    def _poll_login(self, client_id, data, cancel, generation):
        try:
            tokens = device_poll(client_id, data, cancel)
            with self.lock:
                if cancel.is_set() or generation != self.login_generation or self.closed:
                    return
                write_private(self.home / "tokens.json", json.dumps(tokens, indent=2).encode())
                self.session, self.login = Session(self.home), None
                self.login_request_id = None
        except (Fail, OSError) as error:
            with self.lock:
                if not cancel.is_set() and generation == self.login_generation and not self.closed:
                    self.login = {"state": "error", "message": str(error),
                                  "request_id": self.login_request_id}
            return
        self.load_library()

    def cancel_login(self, request_id=None):
        if request_id is not None and (not isinstance(request_id, str)
                                      or not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", request_id)):
            raise Fail("Invalid sign-in request.")
        with self.lock:
            if request_id is not None and request_id != self.login_request_id:
                return False
            self.login_request_id = None
            self.login_generation += 1
            if self.login_cancel:
                self.login_cancel.set()
            self.login = None
            return True

    def sign_out(self):
        with self.lock:
            if self.transfer and self.transfer.state in ("ready", "planning", "running"):
                raise Fail("Stop the download before you sign out.")
            if self.signing_out:
                raise Fail("Sign-out is already in progress.")
            self.signing_out = True
            self.cancel_login()
            self.library_generation += 1
            session = self.session
            self.session = self.lib = self.meta = self.transfer = None
            self.lib_state, self.lib_error = "none", ""
            self.last_items = []
            self.last_options = {}
        try:
            if session:
                session.deactivate()
            sign_out(self.home)
        finally:
            with self.lock:
                self.signing_out = False

    # browsing
    def select(self, body):
        lib = self.require_lib()

        def one(key):
            value = text(body.get(key)).strip()
            return [value] if value else None

        args = SimpleNamespace(
            artist=one("artist"), track_artist=one("track_artist"), album=one("album"), genre=one("genre"),
            year=one("year"), format=one("format"), tag=one("tag"), playlist=one("playlist"),
            album_id=None, track_id=None, skip_the=bool(body.get("skip_the")), all=bool(body.get("all")))
        albums = []
        for album_id, track_ids in select(lib, args):
            albums.append({
                "id": album_id, "artist": lib.album_artist(album_id), "title": lib.album_title(album_id),
                "year": lib.album_year(album_id, track_ids[0]),
                "disc": lib.disc(album_id) if lib.multi_disc(album_id) else 0,
                "tracks": track_ids, "formats": "/".join(sorted({lib.format_name(t) for t in track_ids})),
                "track_lengths": {t: number(lib.tracks.get(t, "length")) for t in track_ids},
                "length": sum(number(lib.tracks.get(t, "length")) for t in track_ids)})
        return {"albums": albums}

    def album_tracks(self, body):
        lib = self.require_lib()
        out = []
        for t in body.get("tracks") or []:
            t = number(t)
            if t in lib.tracks:
                out.append({"id": t, "no": number(lib.tracks.get(t, "track")),
                            "title": text(lib.tracks.get(t, "title")),
                            "artist": lib.artist_name(lib.tracks.get(t, "artist_id")),
                            "length": number(lib.tracks.get(t, "length")), "format": lib.format_name(t)})
        return {"tracks": out}

    # downloading
    def download(self, body):
        with self.lock:
            lib = self.require_lib()
            if self.closed or self.signing_out:
                raise Fail("The app is closing or signing out.")
            if self.transfer and self.transfer.state in ("ready", "planning", "running"):
                raise Fail("A download is already running.")
            quality = text(body.get("quality") or "orig")
            if quality not in QUALITIES:
                raise Fail("Pick one of the listed qualities.")
            if quality != "128" and not lib.premium:
                raise Fail("Original files and bitrates other than 128 need iBroadcast Premium. Pick MP3 128 kbps.")
            # The source only applies to original files; the setting is kept for the next time.
            chosen = text(body.get("source") or "download")
            if chosen not in SOURCES:
                raise Fail("Pick one of the listed sources.")
            source = resolve_source(chosen if quality == "orig" else "stream", quality)
            jobs = number(body.get("jobs")) or 3
            if not 1 <= jobs <= 8:
                raise Fail("Use 1 to 8 parallel downloads.")
            layout = Layout(text(body.get("layout")).strip() or DEFAULT_LAYOUT)
            out_text = text(body.get("out")).strip()
            if not out_text:
                raise Fail("Choose a download folder.")
            out = Path(out_text).expanduser()
            if not out.is_absolute():
                raise Fail("Use a full folder path, like ~/Music/iBroadcast.")
            work_text = text(body.get("work")).strip()
            work = Path(work_text).expanduser() if work_text else None
            if work is not None and not work.is_absolute():
                raise Fail("Use a full path for the work folder, like ~/ibfetch-work.")
            picked = {}
            for item in body.get("items") or []:
                album_id = number(item.get("album_id"))
                for t in item.get("tracks") or []:
                    t = number(t)
                    if t in lib.tracks and lib.active(t) and number(lib.tracks.get(t, "album_id")) == album_id:
                        picked.setdefault(album_id, []).append(t)
            if not picked:
                raise Fail("The download list is empty.")
            session, generation = self.session, self.library_generation
            total = sum(len(set(tracks)) for tracks in picked.values())

            def valid():
                with self.lock:
                    return (not self.closed and not self.signing_out and self.session is session
                            and self.lib is lib and self.library_generation == generation)

            def prepare(stop):
                plan = plan_jobs(lib, sort_groups(lib, picked), quality, layout, stop)
                if not stop.is_set() and valid():
                    out.mkdir(parents=True, exist_ok=True)
                    if work is not None:
                        check_work_folder(work, out)
                return plan

            transfer = Transfer(session, lib, [], out, jobs, quality=quality, prepare=prepare,
                                total=total, valid=valid, verify=body.get("verify") is True,
                                adopt_existing=body.get("adopt_existing") is True,
                                locks=lock_folder(self.home), work=work, source=source)
            with self.lock:
                self.transfer = transfer
                self.last_items = body.get("items") or []
                self.last_options = {"verify": body.get("verify") is True}
                self.settings.update({"out": out_text, "quality": quality, "source": chosen, "jobs": jobs,
                                      "layout": layout.template, "work": work_text})
            try:
                write_private(self.home / "app-settings.json", json.dumps(self.settings, indent=2).encode())
            except OSError:
                pass
            self.transfer_thread = threading.Thread(target=transfer.run, daemon=True)
            self.transfer_thread.start()
            return {"tracks": total}

    def cancel_download(self):
        if self.transfer:
            self.transfer.cancel()

    def state(self):
        if self.updates_started:
            self.updates.check()
        with self.lock:
            lib, login, transfer = self.lib, self.login, self.transfer
            info = {"signed_in": bool(self.session), "login": login,
                    "session": self.login_generation,
                    "library": {"state": self.lib_state, "error": self.lib_error, "version": self.lib_version,
                                "counts": self.meta["counts"] if self.meta else None},
                    "settings": dict(self.settings)}
        info["user"] = {"name": lib.username, "premium": lib.premium} if lib else None
        info["transfer"] = transfer.snapshot() if transfer else None
        info["can_retry"] = bool(self.last_items)
        info["updates"] = self.updates.snapshot()
        return info

    def shutdown(self):
        with self.updates.lock:
            self.updates.closed = True
        with self.lock:
            self.closed = True
            self.library_generation += 1
            self.cancel_login()
            if self.transfer:
                self.transfer.cancel()
            session = self.session
        if session:
            session.deactivate()


class LocalHTTPServer(ThreadingHTTPServer):
    """Known loopback host: HTTPServer's reverse-DNS lookup is unnecessary."""

    def server_bind(self):
        if self.server_address[0] != "127.0.0.1":
            raise Fail("The local server must bind to 127.0.0.1.")
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def make_handler(app, token):
    page = PAGE.replace("__TOKEN__", token).replace("__VERSION__", VERSION)
    page = page.replace("__IBFETCH_ICON__", "data:image/svg+xml;base64," + IBFETCH_ICON)
    for kind, encoded in POWERED_LOGOS.items():
        page = page.replace("__POWERED_" + kind.upper() + "__", "data:image/svg+xml;base64," + encoded)
    page = page.encode()
    script_policy = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'"
                             for script in re.findall(rb"<script>(.*?)</script>", page, re.S))
    cookie_name = "ibfetch"

    def current_page():
        with app.lock:
            appearance = app.settings.get("appearance", "auto")
            palette = app.settings.get("palette", "forest")
        if appearance not in ("auto", "light", "dark"):
            appearance = "auto"
        if palette not in ("forest", "ocean", "warm", "ibroadcast", "amber"):
            palette = "forest"
        return page.replace(b"__APPEARANCE__", appearance.encode()).replace(b"__PALETTE__", palette.encode())

    class Handler(BaseHTTPRequestHandler):
        server_version = "ibfetch"
        port = 0

        def log_message(self, *args):
            pass

        def host_ok(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

        def send(self, code, body, kind="application/json; charset=utf-8", extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def cookie_ok(self):
            cookies = self.headers.get("Cookie") or ""
            return any(part.strip() == f"{cookie_name}={token}" for part in cookies.split(";"))

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, b"Forbidden", "text/plain")
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                if query.get("t") == [token] or self.cookie_ok():
                    return self.send(200, current_page(), "text/html; charset=utf-8", {
                        "Set-Cookie": f"{cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict",
                        "Content-Security-Policy": f"default-src 'none'; script-src {script_policy}; "
                                                   "style-src 'unsafe-inline'; connect-src 'self'; img-src data:",
                        "Referrer-Policy": "no-referrer"})
                return self.send(403, b"Open the link that ibfetch printed in your terminal.", "text/plain")
            if self.headers.get("X-Ibfetch-Token") != token:
                return self.send(403, {"error": "Forbidden"})
            if url.path == "/api/state":
                return self.send(200, app.state())
            if url.path == "/api/meta":
                return self.send(200, app.meta or {})
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.host_ok() or self.headers.get("X-Ibfetch-Token") != token:
                return self.send(403, {"error": "Forbidden"})
            length = number(self.headers.get("Content-Length"))
            if length < 0:
                return self.send(400, {"error": "Bad request."})
            if length > 64 * 1024 * 1024:
                return self.send(413, {"error": "Too much data."})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.send(400, {"error": "Bad request."})
            body = body if isinstance(body, dict) else {}
            path = urllib.parse.urlparse(self.path).path
            try:
                result = self.route(path, body)
            except Fail as error:
                return self.send(400, {"error": str(error)})
            except Exception as error:
                return self.send(500, {"error": f"{error.__class__.__name__}: {error}"})
            if result is None:
                return self.send(404, {"error": "Not found"})
            self.send(200, result)

        def route(self, path, body):
            if path == "/api/updates/check":
                return app.updates.check(manual=True)
            if path == "/api/updates/settings":
                return app.set_updates(body)
            if path == "/api/appearance":
                return app.set_appearance(body)
            if path == "/api/login":
                return {"login": app.start_login()}
            if path == "/api/login/cancel":
                app.cancel_login(body.get("request_id"))
                return {}
            if path == "/api/logout":
                app.sign_out()
                return {}
            if path == "/api/library":
                app.load_library(refresh=bool(body.get("refresh")))
                return {}
            if path == "/api/select":
                return app.select(body)
            if path == "/api/album":
                return app.album_tracks(body)
            if path == "/api/download":
                return app.download(body)
            if path == "/api/retry":
                return app.download({**app.settings, **app.last_options, "items": app.last_items})
            if path == "/api/cancel":
                app.cancel_download()
                return {}
            if path == "/api/choose-folder":
                start = text(body.get("start")) or app.settings["out"]
                start = str(Path(start).expanduser())
                chosen = choose_folder(start if Path(start).is_dir() else str(Path.home()))
                return {"path": chosen, "available": chosen is not None}
            if path == "/api/open-folder":
                target = Path(text(body.get("path")) or app.settings["out"]).expanduser()
                if not target.is_dir():
                    raise Fail("That folder doesn't exist yet.")
                open_folder(target)
                return {}
            if path == "/api/quit":
                app.shutdown()
                threading.Timer(0.3, self.server.shutdown).start()  # let this answer go out first
                return {}
            return None

    return Handler


def cmd_gui(args, home):
    app = App(home)
    token = secrets.token_urlsafe(18)
    handler = make_handler(app, token)
    port = getattr(args, "port", GUI_PORT)
    try:
        server = LocalHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = LocalHTTPServer(("127.0.0.1", 0), handler)  # that port is taken: use any free one
    handler.port = server.server_address[1]
    server.daemon_threads = True
    app.start_updates()
    url = f"http://127.0.0.1:{handler.port}/?t={token}"
    print(f"ibfetch is running at\n  {url}\n\nKeep this window open while you use it. Press Ctrl+C to quit.",
          flush=True)
    if not getattr(args, "no_browser", False):
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
    print("\nibfetch stopped.")
    return 0


def cmd_desktop(home):
    """Private parent-owned host: one readiness message, EOF requests shutdown."""
    app = App(home)
    token = secrets.token_urlsafe(24)
    handler = make_handler(app, token)
    server = LocalHTTPServer(("127.0.0.1", 0), handler)
    handler.port = server.server_address[1]
    server.daemon_threads = True
    app.start_updates()
    serving = threading.Event()

    def parent_input():
        sys.stdin.buffer.read(1)
        app.shutdown()
        serving.wait()
        # shutdown must run in a different thread from serve_forever.
        server.shutdown()

    threading.Thread(target=parent_input, daemon=True).start()
    print(json.dumps({"event": "ready", "app": CLIENT, "protocol": 1, "version": VERSION,
                      "url": f"http://127.0.0.1:{handler.port}/?t={token}"}), flush=True)
    try:
        serving.set()
        server.serve_forever()
    finally:
        app.shutdown()
        server.server_close()
    return 0


def add_filters(p):
    f = p.add_argument_group("filters (combine freely, repeat an option to match any of its values)")
    f.add_argument("-a", "--artist", action="append", metavar="NAME",
                   help='album artist, wildcards * and ?: "4hero", "A*", "*floyd*"')
    f.add_argument("-r", "--track-artist", action="append", metavar="NAME",
                   help="track artist, also finds tracks on compilations")
    f.add_argument("-A", "--album", action="append", metavar="TITLE", help="album title, wildcards allowed")
    f.add_argument("-g", "--genre", action="append", metavar="GENRE", help="genre, wildcards allowed")
    f.add_argument("-y", "--year", action="append", metavar="YEARS", help="1997, 1990-1999 or 2010-")
    f.add_argument("-f", "--format", action="append", metavar="FORMAT", help="file format: flac, mp3, m4a...")
    f.add_argument("-t", "--tag", action="append", metavar="TAG", help="iBroadcast tag name")
    f.add_argument("-p", "--playlist", action="append", metavar="NAME", help="playlist name")
    f.add_argument("--album-id", action="append", metavar="ID", help="album ID (see the list output)")
    f.add_argument("--track-id", action="append", metavar="ID", help="track ID")
    f.add_argument("--skip-the", action="store_true", help='let "B*" also match "The Beatles"')
    f.add_argument("--all", action="store_true", help="no filter: the whole library")
    p.add_argument("--tracks", action="store_true", help="also list the tracks of each album")
    p.add_argument("--refresh", action="store_true", help="download the library index again")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ibfetch", description="Download a chosen part of your iBroadcast library.",
        epilog='Run ibfetch without a command to open the app in your browser. '
               'Example on the command line: ibfetch get --artist "A*" --format flac -o ~/Music/iBroadcast')
    parser.add_argument("--version", action="version", version=f"ibfetch {VERSION}")
    parser.add_argument("--home", help="folder for the sign-in and library cache (default: your config folder)")
    parser.add_argument("--desktop-host", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("gui", help="open the app in your browser (also what plain ibfetch does)")
    p.add_argument("--port", type=int, default=GUI_PORT, help=f"local port (default {GUI_PORT})")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser, just print the link")

    p = sub.add_parser("login", help="sign in to iBroadcast")
    p.add_argument("--client-id", help="your own iBroadcast OAuth client ID")
    sub.add_parser("logout", help="sign out and remove the cached library index")

    p = sub.add_parser("artists", help="list album artists and how many albums each has")
    p.add_argument("pattern", nargs="*", help='name pattern, like "A*"')
    p.add_argument("--skip-the", action="store_true", help='let "B*" also match "The Beatles"')
    p.add_argument("--refresh", action="store_true", help="download the library index again")

    p = sub.add_parser("list", help="show which albums and tracks a filter selects")
    add_filters(p)

    p = sub.add_parser("get", help="download the selected tracks")
    add_filters(p)
    p.add_argument("-o", "--out", default="iBroadcast", help="download folder (default: ./iBroadcast)")
    p.add_argument("-q", "--quality", default="orig", choices=QUALITIES,
                   help="orig = uploaded format using the selected source (default), or an MP3 bitrate")
    p.add_argument("--source", choices=SOURCES,
                   help="for original files: download = iBroadcast's download service, faster and with the tags "
                        "you edited in iBroadcast (default); stream = the file exactly as uploaded, resumable")
    p.add_argument("-j", "--jobs", type=int, default=3, choices=range(1, 9), metavar="1-8",
                   help="parallel downloads (default 3)")
    p.add_argument("--layout", default=DEFAULT_LAYOUT,
                   help=f"folder and file names (default: {DEFAULT_LAYOUT}). Fields: "
                        + ", ".join("{" + f + "}" for f in Layout.FIELDS))
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would download, download nothing")
    p.add_argument("--yes", action="store_true", help=f"don't ask before downloading more than {CONFIRM_ABOVE} tracks")
    p.add_argument("--verify", action="store_true", help="hash existing files to check their saved SHA-256 baseline")
    p.add_argument("--adopt-existing", action="store_true", help="explicitly trust existing matching-path files as local baselines")
    p.add_argument("--work-folder", metavar="PATH",
                   help="download to this local folder first and move each finished track to --out (useful for a NAS)")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # names in any script, on any console
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    home = home_dir(args.home)
    commands = {"login": cmd_login, "logout": cmd_logout, "artists": cmd_artists,
                "list": cmd_list, "get": cmd_get, "gui": cmd_gui, None: cmd_gui}
    try:
        if args.desktop_host:
            return cmd_desktop(home)
        return commands[args.command](args, home)
    except Fail as error:
        print(f"ibfetch: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # output piped into head or less that closed early
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0



IBFETCH_ICON = 'PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDI0IDEwMjQiIHJvbGU9ImltZyIgYXJpYS1sYWJlbGxlZGJ5PSJ0aXRsZSI+CiAgPHRpdGxlIGlkPSJ0aXRsZSI+aWJmZXRjaCDigJQgbXVzaWMgZG93bmxvYWRzPC90aXRsZT4KICA8cmVjdCB4PSIyNCIgeT0iMjQiIHdpZHRoPSI5NzYiIGhlaWdodD0iOTc2IiByeD0iMjE4IiBmaWxsPSIjMWUzMDI3Ii8+CiAgPHJlY3QgeD0iMjgiIHk9IjI4IiB3aWR0aD0iOTY4IiBoZWlnaHQ9Ijk2OCIgcng9IjIxNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjNDY2NjRlIiBzdHJva2Utd2lkdGg9IjgiLz4KICA8cGF0aCBkPSJNNDI4IDI3NiA2MjggMjMyVjMzMEw0MjggMzc2WiIgZmlsbD0iI2QyZTU5ZSIvPgogIDxwYXRoIGQ9Ik00MjggMzA0VjU3MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZDJlNTllIiBzdHJva2Utd2lkdGg9IjY2IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4KICA8ZWxsaXBzZSBjeD0iMzUyIiBjeT0iNTc4IiByeD0iMTA2IiByeT0iNzgiIHRyYW5zZm9ybT0icm90YXRlKC0xOCAzNTIgNTc4KSIgZmlsbD0iI2QyZTU5ZSIvPgogIDxwYXRoIGQ9Ik02NzYgNDE0VjYzMk02MDAgNTYyIDY3NiA2MzggNzUyIDU2MiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjOTFjYmFmIiBzdHJva2Utd2lkdGg9IjYyIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KICA8cGF0aCBkPSJNMjMwIDcyMlY3NzBRMjMwIDgxMCAyNzAgODEwSDc1NFE3OTQgODEwIDc5NCA3NzBWNzIyIiBmaWxsPSJub25lIiBzdHJva2U9IiM5MWNiYWYiIHN0cm9rZS13aWR0aD0iNTAiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K'

POWERED_LOGOS = {'light': 'PHN2ZyB4bWw6c3BhY2U9InByZXNlcnZlIiBzdHlsZT0iZW5hYmxlLWJhY2tncm91bmQ6bmV3IDAgMCAzMzguNyA3OS41OyIgdmlld0JveD0iMCAwIDMzOC43IDc5LjUiIHk9IjBweCIgeD0iMHB4IiB4bWxuczp4bGluaz0iaHR0cDovL3d3dy53My5vcmcvMTk5OS94bGluayIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIiB2ZXJzaW9uPSIxLjEiPgo8c3R5bGUgdHlwZT0idGV4dC9jc3MiPgoJLnN0MHtkaXNwbGF5Om5vbmU7fQoJLnN0MXtkaXNwbGF5OmlubGluZTt9Cgkuc3Qye2ZpbGw6I0RDRERERTt9Cgkuc3Qze2ZpbGw6IzkzOTU5ODt9Cgkuc3Q0e2ZpbGw6IzU4NTk1Qjt9Cgkuc3Q1e2ZpbGw6IzhDOTM5ODt9Cgkuc3Q2e2ZpbGw6IzI4MjgyOTt9Cgkuc3Q3e2ZpbGw6IzZENkU3MTt9Cgkuc3Q4e2ZpbGw6I0E3QTlBQzt9Cgkuc3Q5e2ZpbGw6IzQxNDA0Mjt9Cjwvc3R5bGU+CjxnIGNsYXNzPSJzdDAiIGlkPSJMYXllcl8xIj4KCTxnIGNsYXNzPSJzdDEiPgoJCTxnPgoJCQk8cmVjdCBoZWlnaHQ9IjIzLjIiIHdpZHRoPSI5LjMiIGNsYXNzPSJzdDIiIHk9IjMyLjQiIHg9IjAuNiI+PC9yZWN0PgoJCTwvZz4KCTwvZz4KCTxnIGNsYXNzPSJzdDEiPgoJCTxnPgoJCQk8cGF0aCBkPSJNMTQ3LjcsMjcuOGMtMS4yLTEuOS00LjEtNC40LTkuMi00LjRjLTguMywwLTE1LjYsNi40LTE1LjYsMTYuNmMwLDguOSw1LjgsMTYuNiwxNS42LDE2LjYKCQkJCWMzLjYsMCw3LjUtMS40LDkuMi00LjR2My40aDkuM1YyNC41aC05LjNWMjcuOHogTTE0Ny43LDQwLjVjLTAuMSwxLjQtMC41LDIuNy0xLjMsNGMtMS4yLDEuOS0zLjIsMy41LTYuNSwzLjUKCQkJCWMtNS42LDAtNy43LTQuOS03LjctNy44di0wLjFjMC0zLjUsMi40LTgsNy43LThjMy45LDAsNi4xLDIuMiw3LjEsNC4zQzE0Ny41LDM3LjcsMTQ3LjgsMzkuMSwxNDcuNyw0MC41eiIgY2xhc3M9InN0MiI+PC9wYXRoPgoJCTwvZz4KCTwvZz4KCTxnIGNsYXNzPSJzdDEiPgoJCTxnPgoJCQk8cGF0aCBkPSJNMTg5LjMsMjMuOHYzLjRjLTEtMS0zLjYtMy43LTkuMS0zLjdjLTcuOSwwLTE1LjQsNi0xNS40LDE2LjVjMCw5LjQsNi4yLDE2LjYsMTUuNiwxNi42CgkJCQljNi4zLDAsOC41LTMuMSw5LjQtNC40djMuNGg4LjhWMTQuMWgtOS4zVjIzLjh6IE0xODcuNiw0NS42Yy0xLjUsMS41LTMuNSwyLjQtNS44LDIuNGMtNC40LDAtNy44LTMuMi03LjgtOHYtMC4xCgkJCQljMC0zLjYsMi41LTcuOCw3LjktNy44YzIuMiwwLDQuMiwwLjgsNS42LDIuMmMxLjQsMS4zLDIuMywzLjMsMi4zLDUuNUMxODkuOSw0Mi4xLDE4OS4xLDQ0LjEsMTg3LjYsNDUuNnoiIGNsYXNzPSJzdDIiPjwvcGF0aD4KCQk8L2c+Cgk8L2c+Cgk8ZyBjbGFzcz0ic3QxIj4KCQk8Zz4KCQkJPHBhdGggZD0iTTIyMi41LDQ3LjljLTQuNCwwLTcuNC0zLjQtNy40LTcuOWMwLTIuMiwwLjctNC4zLDItNS43YzEuNC0xLjUsMy4yLTIuMyw1LjQtMi4yYzMuOSwwLDUuNywyLjIsNi43LDQuNGg5LjQKCQkJCWMtMS42LTcuNi04LjItMTMuMS0xNi41LTEzLjFjLTkuMywwLTE2LjQsNy40LTE2LjQsMTYuNWMwLDkuMiw3LjIsMTYuNiwxNi42LDE2LjZjOC40LDAsMTQuNi01LjgsMTYuMy0xMi44aC05LjUKCQkJCUMyMjguMiw0NS43LDIyNi4zLDQ3LjksMjIyLjUsNDcuOXoiIGNsYXNzPSJzdDIiPjwvcGF0aD4KCQk8L2c+Cgk8L2c+Cgk8ZyBjbGFzcz0ic3QxIj4KCQk8Zz4KCQkJPHBhdGggZD0iTTI3MC43LDI3LjhjLTEuMi0xLjktNC4xLTQuNC05LjItNC40Yy04LjMsMC0xNS42LDYuNC0xNS42LDE2LjZjMCw4LjksNS44LDE2LjYsMTUuNiwxNi42CgkJCQljMy42LDAsNy41LTEuNCw5LjItNC40djMuNGg5LjNWMjQuNWgtOS4zVjI3Ljh6IE0yNzAuNyw0MC41Yy0wLjEsMS40LTAuNSwyLjctMS4zLDRjLTEuMiwxLjktMy4yLDMuNS02LjUsMy41CgkJCQljLTUuNiwwLTcuNy00LjktNy43LTcuOHYtMC4xYzAtMy41LDIuMy04LDcuNy04YzMuOSwwLDYsMi4yLDcuMSw0LjNDMjcwLjUsMzcuNywyNzAuOCwzOS4xLDI3MC43LDQwLjV6IiBjbGFzcz0ic3QyIj48L3BhdGg+CgkJPC9nPgoJPC9nPgoJPGcgY2xhc3M9InN0MSI+CgkJPGc+CgkJCTxwYXRoIGQ9Ik0zMDIuOCwzNmMtMi41LTAuNy00LjgtMS43LTQuOC0zLjFjMC0wLjksMC43LTIuMSwyLjYtMi4xYzIuNywwLDIuOSwxLjcsMi45LDIuNGg5LjEKCQkJCWMtMC44LTguMi03LjgtOS44LTExLjktOS44Yy02LjQsMC0xMi4xLDQuMS0xMi4xLDEwLjVjMCw0LjIsMy40LDYuOSw3LjQsOC4xYzUuOSwyLjMsOC4yLDIuNiw4LjIsNC43YzAsMS41LTEuNywyLjQtMy4yLDIuNAoJCQkJYy0wLjQsMC0zLjEsMC0zLjUtMi44aC05LjJjMC45LDguMiw4LDEwLjMsMTIuNywxMC4zYzYuOSwwLDEyLjctNCwxMi43LTEwLjlDMzEzLjcsMzkuMSwzMDguMSwzNy41LDMwMi44LDM2eiIgY2xhc3M9InN0MiI+PC9wYXRoPgoJCTwvZz4KCTwvZz4KCTxnIGNsYXNzPSJzdDEiPgoJCTxnPgoJCQk8cG9seWdvbiBwb2ludHM9IjMzNC4yLDI0LjUgMzM0LjIsMTQuMSAzMjQuOSwxNC4xIDMyNC45LDI0LjUgMzIwLjMsMjQuNSAzMjAuMywzMS42IDMyNC45LDMxLjYgMzI0LjksNTUuNiAKCQkJCTMzNC4yLDU1LjYgMzM0LjIsMzEuNiAzMzguNywzMS42IDMzOC43LDI0LjUiIGNsYXNzPSJzdDIiPjwvcG9seWdvbj4KCQk8L2c+Cgk8L2c+Cgk8ZyBjbGFzcz0ic3QxIj4KCQk8Zz4KCQkJPHBhdGggZD0iTTEwLjYsMjNjMCwyLjktMi40LDUuMy01LjMsNS4zQzIuNCwyOC4zLDAsMjYsMCwyM2MwLTIuOSwyLjQtNS4zLDUuMy01LjNDOC4yLDE3LjcsMTAuNiwyMC4xLDEwLjYsMjN6IiBjbGFzcz0ic3QzIj48L3BhdGg+CgkJPC9nPgoJPC9nPgoJPGcgY2xhc3M9InN0MSI+CgkJPGc+CgkJCTxwYXRoIGQ9Ik03OS42LDI1LjhjMCwwLTEtMS4yLTIuNS0xLjhjLTEuMS0wLjUtMi4zLTAuNi0yLjMtMC42Yy01LjQsMC03LDIuOC03LjksNC41di0zLjVoLTguOHYzMS4xaDkuM1YzOC45CgkJCQljMC00LjQsMi4yLTYuMSw3LTYuMWgwLjNMNzkuNiwyNS44eiIgY2xhc3M9InN0MiI+PC9wYXRoPgoJCTwvZz4KCTwvZz4KCTxnIGNsYXNzPSJzdDEiPgoJCTxnPgoJCQk8cGF0aCBkPSJNMTE0LjMsMzBjMi4zLDMsMy40LDYuNywzLjQsMTAuMmMwLDcuNi01LjQsMTYuNS0xNi43LDE2LjVjLTExLjMsMC0xNi44LTguOS0xNi44LTE2LjV2LTAuMQoJCQkJYzAtMy40LDEuMS03LjEsMy40LTEwLjJsMCwwYzIuNy0zLjYsNy4yLTYuMywxMy4zLTYuM0MxMDcuMSwyMy44LDExMS41LDI2LjQsMTE0LjMsMzBMMTE0LjMsMzB6IE0xMDcuOCwzNy4xCgkJCQljLTEuMS0yLjgtMy44LTQuNy02LjktNC43Yy0zLDAtNS43LDEuOS02LjksNC43TDk0LDM3LjRjLTAuMywwLjktMC41LDEuOC0wLjUsMi44djBjMCw0LjUsMy40LDcuOCw3LjUsNy44YzQsMCw3LjUtMy4zLDcuNS03LjgKCQkJCWMwLTEtMC4yLTItMC41LTIuOEwxMDcuOCwzNy4xeiIgY2xhc3M9InN0MiI+PC9wYXRoPgoJCTwvZz4KCTwvZz4KCTxnIGNsYXNzPSJzdDEiPgoJCTxwYXRoIGQ9Ik0xMDEuMSw2OS4yYy03LjQsMC0xNC40LTIuOS0xOS42LTguMWw0LjMtNC4zYzguMiw4LjIsMjIuNCw4LjIsMzAuNiwwbDQuMyw0LjMKCQkJQzExNS41LDY2LjMsMTA4LjYsNjkuMiwxMDEuMSw2OS4yeiIgY2xhc3M9InN0MyI+PC9wYXRoPgoJPC9nPgoJPGcgY2xhc3M9InN0MSI+CgkJPHBhdGggZD0iTTExNi41LDIzLjNjLTguMi04LjItMjIuNS04LjItMzAuNiwwTDgxLjUsMTlDOTIsOC41LDExMC4zLDguNSwxMjAuOCwxOUwxMTYuNSwyMy4zeiIgY2xhc3M9InN0MyI+PC9wYXRoPgoJPC9nPgoJPGcgY2xhc3M9InN0MSI+CgkJPHBhdGggZD0iTTEwMS4xLDc5LjVjLTkuOCwwLTE5LTMuOC0yNi0xMC44bDQuMy00LjNjNS44LDUuOCwxMy41LDksMjEuNiw5czE1LjktMy4yLDIxLjYtOWw0LjMsNC4zCgkJCUMxMjAuMiw3NS43LDExMSw3OS41LDEwMS4xLDc5LjV6IiBjbGFzcz0ic3Q0Ij48L3BhdGg+Cgk8L2c+Cgk8ZyBjbGFzcz0ic3QxIj4KCQk8cGF0aCBkPSJNMTIyLjgsMTUuMWMtNS44LTUuOC0xMy41LTktMjEuNi05Yy04LjIsMC0xNS45LDMuMi0yMS42LDlsLTQuMy00LjNDODIuMSwzLjgsOTEuMywwLDEwMS4xLDBzMTksMy44LDI2LDEwLjcKCQkJTDEyMi44LDE1LjF6IiBjbGFzcz0ic3Q0Ij48L3BhdGg+Cgk8L2c+Cgk8ZyBjbGFzcz0ic3QxIj4KCQk8cGF0aCBkPSJNNDMuMiwzMy42YzMuMy0xLjksNC43LTUuMSw0LjctOC4yYzAtMy41LTEuMy02LjItMy42LTcuOWMtMi4zLTEuOC01LjctMi44LTkuOS0yLjhoLTE1djQwLjhoMTMuNAoJCQljNi45LDAsOS42LTAuNCwxMy4zLTMuNWMyLjUtMiwzLjgtNS42LDMuOC04LjlDNDkuOSw0MS40LDQ5LjgsMzYsNDMuMiwzMy42eiBNMjkuOSwyMy45aDMuN2MwLjgsMCwxLjksMCwyLjgsMC40CgkJCWMwLjksMC40LDEuNiwxLjIsMS42LDIuN2MwLDEuNi0wLjcsMi40LTEuNywyLjljLTAuOSwwLjQtMiwwLjQtMi44LDAuNGgtMy43VjIzLjl6IE0zNy44LDQ1LjljLTEuMSwwLjUtMi40LDAuNS0zLjEsMC41aC00LjkKCQkJdi03LjRoNC45YzAuOCwwLDIuMSwwLDMuMiwwLjRjMS4xLDAuNCwyLDEuMywyLDMuMUM0MCw0NC40LDM5LDQ1LjQsMzcuOCw0NS45eiIgY2xhc3M9InN0MiI+PC9wYXRoPgoJPC9nPgoJPGcgY2xhc3M9InN0MSI+CgkJPHBhdGggZD0iTTI2Ni45LDYzLjdjMC44LTAuMSwxLjktMC4yLDMuMi0wLjJjMS42LDAsMi45LDAuNCwzLjYsMWMwLjcsMC42LDEuMSwxLjUsMS4xLDIuNmMwLDEuMS0wLjMsMi0xLDIuNwoJCQljLTAuOSwwLjktMi4zLDEuNC0zLjksMS40Yy0wLjUsMC0xLDAtMS4zLTAuMXY1aC0xLjdWNjMuN3ogTTI2OC42LDY5LjhjMC40LDAuMSwwLjgsMC4xLDEuNCwwLjFjMiwwLDMuMi0xLDMuMi0yLjcKCQkJYzAtMS43LTEuMi0yLjUtMy0yLjVjLTAuNywwLTEuMywwLjEtMS42LDAuMVY2OS44eiIgY2xhc3M9InN0NSI+PC9wYXRoPgoJCTxwYXRoIGQ9Ik0yODUsNzEuNmMwLDMuMy0yLjQsNC44LTQuNiw0LjhjLTIuNSwwLTQuNC0xLjgtNC40LTQuNmMwLTMsMi00LjgsNC42LTQuOEMyODMuMiw2Ni45LDI4NSw2OC44LDI4NSw3MS42egoJCQkgTTI3Ny42LDcxLjdjMCwyLDEuMiwzLjUsMi44LDMuNWMxLjYsMCwyLjgtMS41LDIuOC0zLjVjMC0xLjUtMC44LTMuNS0yLjgtMy41UzI3Ny42LDY5LjksMjc3LjYsNzEuN3oiIGNsYXNzPSJzdDUiPjwvcGF0aD4KCQk8cGF0aCBkPSJNMjg3LjYsNjcuMWwxLjIsNC42YzAuMywxLDAuNSwxLjksMC43LDIuOWgwLjFjMC4yLTAuOSwwLjUtMS45LDAuOC0yLjlsMS41LTQuNmgxLjRsMS40LDQuNQoJCQljMC4zLDEuMSwwLjYsMiwwLjgsMi45aDAuMWMwLjItMC45LDAuNC0xLjksMC43LTIuOWwxLjMtNC41aDEuN2wtMyw5aC0xLjVsLTEuNC00LjNjLTAuMy0xLTAuNi0xLjktMC44LTNoMGMtMC4yLDEuMS0wLjUsMi0wLjgsMwoJCQlsLTEuNSw0LjNoLTEuNWwtMi44LTlIMjg3LjZ6IiBjbGFzcz0ic3Q1Ij48L3BhdGg+CgkJPHBhdGggZD0iTTMwMS44LDcxLjljMCwyLjIsMS41LDMuMSwzLjIsMy4xYzEuMiwwLDEuOS0wLjIsMi42LTAuNWwwLjMsMS4yYy0wLjYsMC4zLTEuNiwwLjYtMy4xLDAuNgoJCQljLTIuOSwwLTQuNi0xLjgtNC42LTQuNXMxLjYtNC45LDQuMy00LjljMywwLDMuOCwyLjYsMy44LDQuM2MwLDAuMywwLDAuNi0wLjEsMC44SDMwMS44eiBNMzA2LjgsNzAuOGMwLTEtMC40LTIuNy0yLjMtMi43CgkJCWMtMS43LDAtMi41LDEuNS0yLjYsMi43SDMwNi44eiIgY2xhc3M9InN0NSI+PC9wYXRoPgoJCTxwYXRoIGQ9Ik0zMTAuNSw2OS45YzAtMS4xLDAtMi0wLjEtMi44aDEuNWwwLjEsMS44aDAuMWMwLjQtMS4yLDEuNC0yLDIuNi0yYzAuMiwwLDAuMywwLDAuNSwwLjF2MS41CgkJCWMtMC4yLDAtMC4zLTAuMS0wLjYtMC4xYy0xLjIsMC0yLDAuOS0yLjMsMi4xYzAsMC4yLTAuMSwwLjUtMC4xLDAuOHY0LjhoLTEuN1Y2OS45eiIgY2xhc3M9InN0NSI+PC9wYXRoPgoJCTxwYXRoIGQ9Ik0zMTcuNSw3MS45YzAsMi4yLDEuNSwzLjEsMy4yLDMuMWMxLjIsMCwxLjktMC4yLDIuNi0wLjVsMC4zLDEuMmMtMC42LDAuMy0xLjYsMC42LTMuMSwwLjYKCQkJYy0yLjksMC00LjYtMS44LTQuNi00LjVzMS42LTQuOSw0LjMtNC45YzMsMCwzLjgsMi42LDMuOCw0LjNjMCwwLjMsMCwwLjYtMC4xLDAuOEgzMTcuNXogTTMyMi40LDcwLjhjMC0xLTAuNC0yLjctMi4zLTIuNwoJCQljLTEuNywwLTIuNSwxLjUtMi42LDIuN0gzMjIuNHoiIGNsYXNzPSJzdDUiPjwvcGF0aD4KCQk8cGF0aCBkPSJNMzM0LjIsNjIuOXYxMC45YzAsMC44LDAsMS43LDAuMSwyLjNoLTEuNWwtMC4xLTEuNmgwYy0wLjUsMS0xLjYsMS44LTMuMiwxLjhjLTIuMiwwLTQtMS44LTQtNC42CgkJCWMwLTMsMS45LTQuOCw0LjItNC44YzEuNCwwLDIuNCwwLjcsMi44LDEuNGgwdi01LjRIMzM0LjJ6IE0zMzIuNSw3MC44YzAtMC4yLDAtMC41LTAuMS0wLjdjLTAuMi0xLTEuMi0xLjktMi40LTEuOQoJCQljLTEuNywwLTIuOCwxLjUtMi44LDMuNWMwLDEuOCwwLjksMy4zLDIuNywzLjNjMS4xLDAsMi4yLTAuNywyLjUtMmMwLjEtMC4yLDAuMS0wLjQsMC4xLTAuN1Y3MC44eiIgY2xhc3M9InN0NSI+PC9wYXRoPgoJPC9nPgo8L2c+CjxnIGlkPSJMYXllcl8yIj4KCTxnPgoJCTxnPgoJCQk8Zz4KCQkJCTxyZWN0IGhlaWdodD0iMjMuMSIgd2lkdGg9IjkuMyIgY2xhc3M9InN0NiIgeT0iMzIuNiIgeD0iMS4zIj48L3JlY3Q+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxnPgoJCQkJPHBhdGggZD0iTTE0OC4xLDI4Yy0xLjItMS45LTQuMS00LjQtOS4yLTQuNGMtOC4zLDAtMTUuNiw2LjQtMTUuNiwxNi42YzAsOC45LDUuOCwxNi42LDE1LjYsMTYuNgoJCQkJCWMzLjYsMCw3LjUtMS40LDkuMi00LjR2My40aDkuM3YtMzFoLTkuM1YyOHogTTE0OC4xLDQwLjdjLTAuMSwxLjQtMC41LDIuNy0xLjMsNGMtMS4yLDEuOS0zLjIsMy41LTYuNSwzLjUKCQkJCQljLTUuNiwwLTcuNy00LjktNy43LTcuOHYtMC4xYzAtMy41LDIuNC04LDcuNy04YzMuOSwwLDYuMSwyLjIsNy4xLDQuM0MxNDcuOSwzNy45LDE0OC4yLDM5LjMsMTQ4LjEsNDAuN3oiIGNsYXNzPSJzdDYiPjwvcGF0aD4KCQkJPC9nPgoJCTwvZz4KCQk8Zz4KCQkJPGc+CgkJCQk8cGF0aCBkPSJNMTg5LjYsMjR2My40Yy0xLTEtMy42LTMuNy05LjEtMy43Yy03LjksMC0xNS40LDYtMTUuNCwxNi41YzAsOS4zLDYuMiwxNi42LDE1LjUsMTYuNgoJCQkJCWM2LjMsMCw4LjUtMy4xLDkuNC00LjN2My40aDguOFYxNC4zaC05LjNWMjR6IE0xODcuOSw0NS43Yy0xLjUsMS41LTMuNSwyLjQtNS44LDIuNGMtNC40LDAtNy44LTMuMi03LjgtNy45VjQwCgkJCQkJYzAtMy42LDIuNS03LjgsNy45LTcuOGMyLjIsMCw0LjEsMC44LDUuNSwyLjJjMS40LDEuMywyLjMsMy4zLDIuMyw1LjVDMTkwLjIsNDIuMiwxODkuNCw0NC4yLDE4Ny45LDQ1Ljd6IiBjbGFzcz0ic3Q2Ij48L3BhdGg+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxnPgoJCQkJPHBhdGggZD0iTTIyMi43LDQ4LjFjLTQuNCwwLTcuNC0zLjQtNy40LTcuOWMwLTIuMiwwLjctNC4zLDItNS43YzEuNC0xLjUsMy4yLTIuMyw1LjQtMi4yYzMuOSwwLDUuNywyLjIsNi43LDQuNAoJCQkJCWg5LjRjLTEuNi03LjYtOC4yLTEzLTE2LjQtMTNjLTkuMywwLTE2LjQsNy40LTE2LjQsMTYuNWMwLDkuMiw3LjIsMTYuNiwxNi42LDE2LjZjOC40LDAsMTQuNi01LjgsMTYuMy0xMi44aC05LjUKCQkJCQlDMjI4LjQsNDUuOCwyMjYuNSw0OC4xLDIyMi43LDQ4LjF6IiBjbGFzcz0ic3Q2Ij48L3BhdGg+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxnPgoJCQkJPHBhdGggZD0iTTI3MC44LDI4Yy0xLjItMS45LTQuMS00LjQtOS4yLTQuNGMtOC4zLDAtMTUuNiw2LjQtMTUuNiwxNi42YzAsOC45LDUuOCwxNi42LDE1LjYsMTYuNgoJCQkJCWMzLjYsMCw3LjUtMS40LDkuMi00LjR2My40aDkuM3YtMzFoLTkuM1YyOHogTTI3MC44LDQwLjdjLTAuMSwxLjQtMC41LDIuNy0xLjMsNGMtMS4yLDEuOS0zLjIsMy41LTYuNSwzLjUKCQkJCQljLTUuNiwwLTcuNy00LjktNy43LTcuOHYtMC4xYzAtMy41LDIuMy04LDcuNy04YzMuOSwwLDYsMi4yLDcsNC4zQzI3MC43LDM3LjksMjcwLjksMzkuMywyNzAuOCw0MC43eiIgY2xhc3M9InN0NiI+PC9wYXRoPgoJCQk8L2c+CgkJPC9nPgoJCTxnPgoJCQk8Zz4KCQkJCTxwYXRoIGQ9Ik0zMDIuOSwzNi4yYy0yLjUtMC43LTQuNy0xLjctNC43LTMuMWMwLTAuOSwwLjctMi4xLDIuNi0yLjFjMi43LDAsMi45LDEuNywyLjksMi40aDkuMQoJCQkJCWMtMC44LTguMi03LjgtOS44LTExLjktOS44Yy02LjQsMC0xMiw0LjEtMTIsMTAuNWMwLDQuMiwzLjQsNi44LDcuNCw4LjFjNS45LDIuMyw4LjIsMi42LDguMiw0LjdjMCwxLjUtMS43LDIuNC0zLjIsMi40CgkJCQkJYy0wLjQsMC0zLjEsMC0zLjUtMi44aC05LjJjMC44LDguMiw3LjksMTAuMywxMi42LDEwLjNjNi45LDAsMTIuNi00LDEyLjYtMTAuOEMzMTMuOCwzOS4yLDMwOC4yLDM3LjYsMzAyLjksMzYuMnoiIGNsYXNzPSJzdDYiPjwvcGF0aD4KCQkJPC9nPgoJCTwvZz4KCQk8Zz4KCQkJPGc+CgkJCQk8cG9seWdvbiBwb2ludHM9IjMzNC4yLDI0LjcgMzM0LjIsMTQuMyAzMjQuOSwxNC4zIDMyNC45LDI0LjcgMzIwLjMsMjQuNyAzMjAuMywzMS43IDMyNC45LDMxLjcgMzI0LjksNTUuNyAKCQkJCQkzMzQuMiw1NS43IDMzNC4yLDMxLjcgMzM4LjcsMzEuNyAzMzguNywyNC43IiBjbGFzcz0ic3Q2Ij48L3BvbHlnb24+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxnPgoJCQkJPHBhdGggZD0iTTExLjIsMjMuMmMwLDIuOS0yLjQsNS4zLTUuMyw1LjNjLTIuOSwwLTUuMy0yLjQtNS4zLTUuM2MwLTIuOSwyLjQtNS4zLDUuMy01LjMKCQkJCQlDOC45LDE3LjksMTEuMiwyMC4zLDExLjIsMjMuMnoiIGNsYXNzPSJzdDciPjwvcGF0aD4KCQkJPC9nPgoJCTwvZz4KCQk8Zz4KCQkJPGc+CgkJCQk8cGF0aCBkPSJNODAuMSwyNmMwLDAtMS0xLjItMi41LTEuOGMtMS4xLTAuNS0yLjMtMC42LTIuMy0wLjZjLTUuNCwwLTcsMi44LTcuOSw0LjV2LTMuNWgtOC43djMxSDY4VjM5CgkJCQkJYzAtNC40LDIuMi02LjEsNy02LjFoMC4zTDgwLjEsMjZ6IiBjbGFzcz0ic3Q2Ij48L3BhdGg+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxnPgoJCQkJPHBhdGggZD0iTTExNC44LDMwLjJjMi4zLDMsMy40LDYuNywzLjQsMTAuMmMwLDcuNi01LjQsMTYuNS0xNi43LDE2LjVjLTExLjMsMC0xNi43LTguOS0xNi43LTE2LjV2LTAuMQoJCQkJCWMwLTMuNCwxLjEtNy4xLDMuNC0xMC4ybDAsMGMyLjctMy42LDcuMS02LjIsMTMuMy02LjJDMTA3LjYsMjMuOSwxMTIsMjYuNSwxMTQuOCwzMC4yTDExNC44LDMwLjJ6IE0xMDguMywzNy4yCgkJCQkJYy0xLjEtMi44LTMuOC00LjctNi44LTQuN2MtMywwLTUuNywxLjktNi45LDQuN2wtMC4xLDAuMmMtMC4zLDAuOS0wLjUsMS44LTAuNSwyLjh2MGMwLDQuNSwzLjQsNy44LDcuNSw3LjgKCQkJCQljNCwwLDcuNC0zLjMsNy40LTcuOGMwLTEtMC4yLTEuOS0wLjUtMi44TDEwOC4zLDM3LjJ6IiBjbGFzcz0ic3Q2Ij48L3BhdGg+CgkJCTwvZz4KCQk8L2c+CgkJPGc+CgkJCTxwYXRoIGQ9Ik0xMDEuNiw2OS4zYy03LjQsMC0xNC40LTIuOS0xOS42LTguMWw0LjMtNC4zYzguMiw4LjIsMjIuNCw4LjIsMzAuNiwwbDQuMyw0LjMKCQkJCUMxMTYsNjYuNCwxMDksNjkuMywxMDEuNiw2OS4zeiIgY2xhc3M9InN0NyI+PC9wYXRoPgoJCTwvZz4KCQk8Zz4KCQkJPHBhdGggZD0iTTExNi45LDIzLjVjLTguMi04LjItMjIuNC04LjItMzAuNiwwTDgyLDE5LjFjMTAuNS0xMC41LDI4LjctMTAuNSwzOS4yLDBMMTE2LjksMjMuNXoiIGNsYXNzPSJzdDciPjwvcGF0aD4KCQk8L2c+CgkJPGc+CgkJCTxwYXRoIGQ9Ik0xMDEuNiw3OS42Yy05LjgsMC0xOS0zLjgtMjUuOS0xMC43bDQuMy00LjNjNS44LDUuOCwxMy40LDguOSwyMS42LDguOWM4LjIsMCwxNS44LTMuMiwyMS42LTguOWw0LjMsNC4zCgkJCQlDMTIwLjYsNzUuOCwxMTEuNCw3OS42LDEwMS42LDc5LjZ6IiBjbGFzcz0ic3Q4Ij48L3BhdGg+CgkJPC9nPgoJCTxnPgoJCQk8cGF0aCBkPSJNMTIzLjIsMTUuM2MtNS44LTUuOC0xMy40LTguOS0yMS42LTguOWMtOC4yLDAtMTUuOCwzLjItMjEuNiw4LjlsLTQuMy00LjNDODIuNiw0LDkxLjgsMC4yLDEwMS42LDAuMgoJCQkJYzkuOCwwLDE5LDMuOCwyNS45LDEwLjdMMTIzLjIsMTUuM3oiIGNsYXNzPSJzdDgiPjwvcGF0aD4KCQk8L2c+CgkJPGc+CgkJCTxwYXRoIGQ9Ik00My44LDMzLjhjMy4zLTEuOSw0LjctNS4xLDQuNy04LjJjMC0zLjUtMS4zLTYuMi0zLjYtNy45Yy0yLjMtMS44LTUuNy0yLjgtOS45LTIuOEgyMHY0MC44aDEzLjQKCQkJCWM2LjksMCw5LjYtMC40LDEzLjMtMy41YzIuNS0yLDMuOC01LjYsMy44LTguOUM1MC41LDQxLjUsNTAuMywzNi4xLDQzLjgsMzMuOHogTTMwLjQsMjRoMy43YzAuOCwwLDEuOSwwLDIuOCwwLjQKCQkJCWMwLjksMC40LDEuNiwxLjIsMS42LDIuN2MwLDEuNi0wLjcsMi40LTEuNywyLjljLTAuOSwwLjQtMiwwLjQtMi44LDAuNGgtMy43VjI0eiBNMzguNCw0NmMtMS4xLDAuNS0yLjQsMC41LTMuMSwwLjVoLTQuOXYtNy40CgkJCQloNC45YzAuOCwwLDIuMSwwLDMuMiwwLjRjMS4xLDAuNCwyLDEuMywyLDMuMUM0MC41LDQ0LjUsMzkuNiw0NS41LDM4LjQsNDZ6IiBjbGFzcz0ic3Q2Ij48L3BhdGg+CgkJPC9nPgoJPC9nPgoJPGc+CgkJPHBhdGggZD0iTTI2Nyw2NGMwLjgtMC4xLDEuOS0wLjIsMy4yLTAuMmMxLjYsMCwyLjgsMC40LDMuNiwxYzAuNywwLjYsMS4xLDEuNSwxLjEsMi42YzAsMS4xLTAuMywyLTEsMi43CgkJCWMtMC45LDAuOS0yLjMsMS40LTMuOSwxLjRjLTAuNSwwLTEsMC0xLjMtMC4xdjVIMjY3VjY0eiBNMjY4LjcsNzBjMC40LDAuMSwwLjgsMC4xLDEuNCwwLjFjMiwwLDMuMi0wLjksMy4yLTIuNwoJCQljMC0xLjctMS4yLTIuNS0zLTIuNWMtMC43LDAtMS4zLDAuMS0xLjYsMC4xVjcweiIgY2xhc3M9InN0OSI+PC9wYXRoPgoJCTxwYXRoIGQ9Ik0yODUsNzEuOGMwLDMuMy0yLjQsNC44LTQuNiw0LjhjLTIuNSwwLTQuNC0xLjgtNC40LTQuNmMwLTMsMi00LjgsNC42LTQuOEMyODMuMiw2Ny4xLDI4NSw2OSwyODUsNzEuOHoKCQkJIE0yNzcuNyw3MS45YzAsMiwxLjIsMy41LDIuOCwzLjVjMS42LDAsMi44LTEuNSwyLjgtMy41YzAtMS41LTAuOC0zLjUtMi44LTMuNVMyNzcuNyw3MC4yLDI3Ny43LDcxLjl6IiBjbGFzcz0ic3Q5Ij48L3BhdGg+CgkJPHBhdGggZD0iTTI4Ny43LDY3LjNsMS4yLDQuNmMwLjMsMSwwLjUsMS45LDAuNywyLjloMC4xYzAuMi0wLjksMC41LTEuOSwwLjgtMi44bDEuNS00LjZoMS40bDEuNCw0LjUKCQkJYzAuMywxLjEsMC42LDIsMC44LDIuOWgwLjFjMC4yLTAuOSwwLjQtMS45LDAuNy0yLjlsMS4zLTQuNWgxLjdsLTMsOWgtMS41bC0xLjQtNC4zYy0wLjMtMS0wLjYtMS45LTAuOC0zaDBjLTAuMiwxLjEtMC41LDItMC44LDMKCQkJbC0xLjUsNC4zaC0xLjVsLTIuOC05SDI4Ny43eiIgY2xhc3M9InN0OSI+PC9wYXRoPgoJCTxwYXRoIGQ9Ik0zMDEuOCw3Mi4xYzAsMi4yLDEuNSwzLjEsMy4yLDMuMWMxLjIsMCwxLjktMC4yLDIuNi0wLjVsMC4zLDEuMmMtMC42LDAuMy0xLjYsMC42LTMuMSwwLjYKCQkJYy0yLjgsMC00LjUtMS44LTQuNS00LjVzMS42LTQuOSw0LjMtNC45YzMsMCwzLjgsMi42LDMuOCw0LjJjMCwwLjMsMCwwLjYtMC4xLDAuOEgzMDEuOHogTTMwNi44LDcxYzAtMS0wLjQtMi43LTIuMy0yLjcKCQkJYy0xLjcsMC0yLjQsMS41LTIuNiwyLjdIMzA2Ljh6IiBjbGFzcz0ic3Q5Ij48L3BhdGg+CgkJPHBhdGggZD0iTTMxMC41LDcwLjJjMC0xLjEsMC0yLTAuMS0yLjhoMS41bDAuMSwxLjhoMC4xYzAuNC0xLjIsMS40LTIsMi42LTJjMC4yLDAsMC4zLDAsMC41LDAuMXYxLjUKCQkJYy0wLjIsMC0wLjMtMC4xLTAuNi0wLjFjLTEuMiwwLTIsMC45LTIuMywyLjFjMCwwLjItMC4xLDAuNS0wLjEsMC44djQuOGgtMS43VjcwLjJ6IiBjbGFzcz0ic3Q5Ij48L3BhdGg+CgkJPHBhdGggZD0iTTMxNy41LDcyLjFjMCwyLjIsMS41LDMuMSwzLjIsMy4xYzEuMiwwLDEuOS0wLjIsMi42LTAuNWwwLjMsMS4yYy0wLjYsMC4zLTEuNiwwLjYtMy4xLDAuNgoJCQljLTIuOCwwLTQuNi0xLjgtNC42LTQuNXMxLjYtNC45LDQuMy00LjljMywwLDMuOCwyLjYsMy44LDQuMmMwLDAuMywwLDAuNi0wLjEsMC44SDMxNy41eiBNMzIyLjQsNzFjMC0xLTAuNC0yLjctMi4zLTIuNwoJCQljLTEuNywwLTIuNCwxLjUtMi42LDIuN0gzMjIuNHoiIGNsYXNzPSJzdDkiPjwvcGF0aD4KCQk8cGF0aCBkPSJNMzM0LjEsNjMuMVY3NGMwLDAuOCwwLDEuNywwLjEsMi4zaC0xLjVsLTAuMS0xLjZoMGMtMC41LDEtMS42LDEuOC0zLjIsMS44Yy0yLjIsMC00LTEuOC00LTQuNgoJCQljMC0zLDEuOS00LjgsNC4xLTQuOGMxLjQsMCwyLjQsMC43LDIuOCwxLjRoMHYtNS40SDMzNC4xeiBNMzMyLjQsNzFjMC0wLjIsMC0wLjUtMC4xLTAuN2MtMC4yLTEtMS4yLTEuOS0yLjQtMS45CgkJCWMtMS43LDAtMi44LDEuNS0yLjgsMy41YzAsMS44LDAuOSwzLjMsMi43LDMuM2MxLjEsMCwyLjItMC43LDIuNS0yYzAuMS0wLjIsMC4xLTAuNCwwLjEtMC43VjcxeiIgY2xhc3M9InN0OSI+PC9wYXRoPgoJPC9nPgo8L2c+Cjwvc3ZnPgo=', 'dark': 'PHN2ZyB4bWw6c3BhY2U9InByZXNlcnZlIiBzdHlsZT0iZW5hYmxlLWJhY2tncm91bmQ6bmV3IDAgMCAzMzguNyA3OS41OyIgdmlld0JveD0iMCAwIDMzOC43IDc5LjUiIHk9IjBweCIgeD0iMHB4IiB4bWxuczp4bGluaz0iaHR0cDovL3d3dy53My5vcmcvMTk5OS94bGluayIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIiBpZD0iTGF5ZXJfMSIgdmVyc2lvbj0iMS4xIj4KPHN0eWxlIHR5cGU9InRleHQvY3NzIj4KCS5zdDB7ZmlsbDojRENERERFO30KCS5zdDF7ZmlsbDojOTM5NTk4O30KCS5zdDJ7ZmlsbDojNTg1OTVCO30KCS5zdDN7ZmlsbDojOEM5Mzk4O30KPC9zdHlsZT4KPGc+Cgk8Zz4KCQk8cmVjdCBoZWlnaHQ9IjIzLjIiIHdpZHRoPSI5LjMiIGNsYXNzPSJzdDAiIHk9IjMyLjQiIHg9IjAuNiI+PC9yZWN0PgoJPC9nPgo8L2c+CjxnPgoJPGc+CgkJPHBhdGggZD0iTTE0Ny43LDI3LjhjLTEuMi0xLjktNC4xLTQuNC05LjItNC40Yy04LjMsMC0xNS42LDYuNC0xNS42LDE2LjZjMCw4LjksNS44LDE2LjYsMTUuNiwxNi42CgkJCWMzLjYsMCw3LjUtMS40LDkuMi00LjR2My40aDkuM1YyNC41aC05LjNWMjcuOHogTTE0Ny43LDQwLjVjLTAuMSwxLjQtMC41LDIuNy0xLjMsNGMtMS4yLDEuOS0zLjIsMy41LTYuNSwzLjUKCQkJYy01LjYsMC03LjctNC45LTcuNy03Ljh2LTAuMWMwLTMuNSwyLjQtOCw3LjctOGMzLjksMCw2LjEsMi4yLDcuMSw0LjNDMTQ3LjUsMzcuNywxNDcuOCwzOS4xLDE0Ny43LDQwLjV6IiBjbGFzcz0ic3QwIj48L3BhdGg+Cgk8L2c+CjwvZz4KPGc+Cgk8Zz4KCQk8cGF0aCBkPSJNMTg5LjMsMjMuOHYzLjRjLTEtMS0zLjYtMy43LTkuMS0zLjdjLTcuOSwwLTE1LjQsNi0xNS40LDE2LjVjMCw5LjQsNi4yLDE2LjYsMTUuNiwxNi42CgkJCWM2LjMsMCw4LjUtMy4xLDkuNC00LjR2My40aDguOFYxNC4xaC05LjNWMjMuOHogTTE4Ny42LDQ1LjZjLTEuNSwxLjUtMy41LDIuNC01LjgsMi40Yy00LjQsMC03LjgtMy4yLTcuOC04di0wLjEKCQkJYzAtMy42LDIuNS03LjgsNy45LTcuOGMyLjIsMCw0LjIsMC44LDUuNiwyLjJjMS40LDEuMywyLjMsMy4zLDIuMyw1LjVDMTg5LjksNDIuMSwxODkuMSw0NC4xLDE4Ny42LDQ1LjZ6IiBjbGFzcz0ic3QwIj48L3BhdGg+Cgk8L2c+CjwvZz4KPGc+Cgk8Zz4KCQk8cGF0aCBkPSJNMjIyLjUsNDcuOWMtNC40LDAtNy40LTMuNC03LjQtNy45YzAtMi4yLDAuNy00LjMsMi01LjdjMS40LTEuNSwzLjItMi4zLDUuNC0yLjJjMy45LDAsNS43LDIuMiw2LjcsNC40aDkuNAoJCQljLTEuNi03LjYtOC4yLTEzLjEtMTYuNS0xMy4xYy05LjMsMC0xNi40LDcuNC0xNi40LDE2LjVjMCw5LjIsNy4yLDE2LjYsMTYuNiwxNi42YzguNCwwLDE0LjYtNS44LDE2LjMtMTIuOGgtOS41CgkJCUMyMjguMiw0NS43LDIyNi4zLDQ3LjksMjIyLjUsNDcuOXoiIGNsYXNzPSJzdDAiPjwvcGF0aD4KCTwvZz4KPC9nPgo8Zz4KCTxnPgoJCTxwYXRoIGQ9Ik0yNzAuNywyNy44Yy0xLjItMS45LTQuMS00LjQtOS4yLTQuNGMtOC4zLDAtMTUuNiw2LjQtMTUuNiwxNi42YzAsOC45LDUuOCwxNi42LDE1LjYsMTYuNgoJCQljMy42LDAsNy41LTEuNCw5LjItNC40djMuNGg5LjNWMjQuNWgtOS4zVjI3Ljh6IE0yNzAuNyw0MC41Yy0wLjEsMS40LTAuNSwyLjctMS4zLDRjLTEuMiwxLjktMy4yLDMuNS02LjUsMy41CgkJCWMtNS42LDAtNy43LTQuOS03LjctNy44di0wLjFjMC0zLjUsMi4zLTgsNy43LThjMy45LDAsNiwyLjIsNy4xLDQuM0MyNzAuNSwzNy43LDI3MC44LDM5LjEsMjcwLjcsNDAuNXoiIGNsYXNzPSJzdDAiPjwvcGF0aD4KCTwvZz4KPC9nPgo8Zz4KCTxnPgoJCTxwYXRoIGQ9Ik0zMDIuOCwzNmMtMi41LTAuNy00LjgtMS43LTQuOC0zLjFjMC0wLjksMC43LTIuMSwyLjYtMi4xYzIuNywwLDIuOSwxLjcsMi45LDIuNGg5LjEKCQkJYy0wLjgtOC4yLTcuOC05LjgtMTEuOS05LjhjLTYuNCwwLTEyLjEsNC4xLTEyLjEsMTAuNWMwLDQuMiwzLjQsNi45LDcuNCw4LjFjNS45LDIuMyw4LjIsMi42LDguMiw0LjdjMCwxLjUtMS43LDIuNC0zLjIsMi40CgkJCWMtMC40LDAtMy4xLDAtMy41LTIuOGgtOS4yYzAuOSw4LjIsOCwxMC4zLDEyLjcsMTAuM2M2LjksMCwxMi43LTQsMTIuNy0xMC45QzMxMy43LDM5LjEsMzA4LjEsMzcuNSwzMDIuOCwzNnoiIGNsYXNzPSJzdDAiPjwvcGF0aD4KCTwvZz4KPC9nPgo8Zz4KCTxnPgoJCTxwb2x5Z29uIHBvaW50cz0iMzM0LjIsMjQuNSAzMzQuMiwxNC4xIDMyNC45LDE0LjEgMzI0LjksMjQuNSAzMjAuMywyNC41IDMyMC4zLDMxLjYgMzI0LjksMzEuNiAzMjQuOSw1NS42IAoJCQkzMzQuMiw1NS42IDMzNC4yLDMxLjYgMzM4LjcsMzEuNiAzMzguNywyNC41IiBjbGFzcz0ic3QwIj48L3BvbHlnb24+Cgk8L2c+CjwvZz4KPGc+Cgk8Zz4KCQk8cGF0aCBkPSJNMTAuNiwyM2MwLDIuOS0yLjQsNS4zLTUuMyw1LjNDMi40LDI4LjMsMCwyNiwwLDIzYzAtMi45LDIuNC01LjMsNS4zLTUuM0M4LjIsMTcuNywxMC42LDIwLjEsMTAuNiwyM3oiIGNsYXNzPSJzdDEiPjwvcGF0aD4KCTwvZz4KPC9nPgo8Zz4KCTxnPgoJCTxwYXRoIGQ9Ik03OS42LDI1LjhjMCwwLTEtMS4yLTIuNS0xLjhjLTEuMS0wLjUtMi4zLTAuNi0yLjMtMC42Yy01LjQsMC03LDIuOC03LjksNC41di0zLjVoLTguOHYzMS4xaDkuM1YzOC45CgkJCWMwLTQuNCwyLjItNi4xLDctNi4xaDAuM0w3OS42LDI1Ljh6IiBjbGFzcz0ic3QwIj48L3BhdGg+Cgk8L2c+CjwvZz4KPGc+Cgk8Zz4KCQk8cGF0aCBkPSJNMTE0LjMsMzBjMi4zLDMsMy40LDYuNywzLjQsMTAuMmMwLDcuNi01LjQsMTYuNS0xNi43LDE2LjVjLTExLjMsMC0xNi44LTguOS0xNi44LTE2LjV2LTAuMQoJCQljMC0zLjQsMS4xLTcuMSwzLjQtMTAuMmwwLDBjMi43LTMuNiw3LjItNi4zLDEzLjMtNi4zQzEwNy4xLDIzLjgsMTExLjUsMjYuNCwxMTQuMywzMEwxMTQuMywzMHogTTEwNy44LDM3LjEKCQkJYy0xLjEtMi44LTMuOC00LjctNi45LTQuN2MtMywwLTUuNywxLjktNi45LDQuN0w5NCwzNy40Yy0wLjMsMC45LTAuNSwxLjgtMC41LDIuOHYwYzAsNC41LDMuNCw3LjgsNy41LDcuOGM0LDAsNy41LTMuMyw3LjUtNy44CgkJCWMwLTEtMC4yLTItMC41LTIuOEwxMDcuOCwzNy4xeiIgY2xhc3M9InN0MCI+PC9wYXRoPgoJPC9nPgo8L2c+CjxnPgoJPHBhdGggZD0iTTEwMS4xLDY5LjJjLTcuNCwwLTE0LjQtMi45LTE5LjYtOC4xbDQuMy00LjNjOC4yLDguMiwyMi40LDguMiwzMC42LDBsNC4zLDQuMwoJCUMxMTUuNSw2Ni4zLDEwOC42LDY5LjIsMTAxLjEsNjkuMnoiIGNsYXNzPSJzdDEiPjwvcGF0aD4KPC9nPgo8Zz4KCTxwYXRoIGQ9Ik0xMTYuNSwyMy4zYy04LjItOC4yLTIyLjUtOC4yLTMwLjYsMEw4MS41LDE5QzkyLDguNSwxMTAuMyw4LjUsMTIwLjgsMTlMMTE2LjUsMjMuM3oiIGNsYXNzPSJzdDEiPjwvcGF0aD4KPC9nPgo8Zz4KCTxwYXRoIGQ9Ik0xMDEuMSw3OS41Yy05LjgsMC0xOS0zLjgtMjYtMTAuOGw0LjMtNC4zYzUuOCw1LjgsMTMuNSw5LDIxLjYsOXMxNS45LTMuMiwyMS42LTlsNC4zLDQuMwoJCUMxMjAuMiw3NS43LDExMSw3OS41LDEwMS4xLDc5LjV6IiBjbGFzcz0ic3QyIj48L3BhdGg+CjwvZz4KPGc+Cgk8cGF0aCBkPSJNMTIyLjgsMTUuMWMtNS44LTUuOC0xMy41LTktMjEuNi05Yy04LjIsMC0xNS45LDMuMi0yMS42LDlsLTQuMy00LjNDODIuMSwzLjgsOTEuMywwLDEwMS4xLDBzMTksMy44LDI2LDEwLjcKCQlMMTIyLjgsMTUuMXoiIGNsYXNzPSJzdDIiPjwvcGF0aD4KPC9nPgo8Zz4KCTxwYXRoIGQ9Ik00My4yLDMzLjZjMy4zLTEuOSw0LjctNS4xLDQuNy04LjJjMC0zLjUtMS4zLTYuMi0zLjYtNy45Yy0yLjMtMS44LTUuNy0yLjgtOS45LTIuOGgtMTV2NDAuOGgxMy40CgkJYzYuOSwwLDkuNi0wLjQsMTMuMy0zLjVjMi41LTIsMy44LTUuNiwzLjgtOC45QzQ5LjksNDEuNCw0OS44LDM2LDQzLjIsMzMuNnogTTI5LjksMjMuOWgzLjdjMC44LDAsMS45LDAsMi44LDAuNAoJCWMwLjksMC40LDEuNiwxLjIsMS42LDIuN2MwLDEuNi0wLjcsMi40LTEuNywyLjljLTAuOSwwLjQtMiwwLjQtMi44LDAuNGgtMy43VjIzLjl6IE0zNy44LDQ1LjljLTEuMSwwLjUtMi40LDAuNS0zLjEsMC41aC00Ljl2LTcuNAoJCWg0LjljMC44LDAsMi4xLDAsMy4yLDAuNGMxLjEsMC40LDIsMS4zLDIsMy4xQzQwLDQ0LjQsMzksNDUuNCwzNy44LDQ1Ljl6IiBjbGFzcz0ic3QwIj48L3BhdGg+CjwvZz4KPGc+Cgk8cGF0aCBkPSJNMjY2LjksNjMuN2MwLjgtMC4xLDEuOS0wLjIsMy4yLTAuMmMxLjYsMCwyLjksMC40LDMuNiwxYzAuNywwLjYsMS4xLDEuNSwxLjEsMi42YzAsMS4xLTAuMywyLTEsMi43CgkJYy0wLjksMC45LTIuMywxLjQtMy45LDEuNGMtMC41LDAtMSwwLTEuMy0wLjF2NWgtMS43VjYzLjd6IE0yNjguNiw2OS44YzAuNCwwLjEsMC44LDAuMSwxLjQsMC4xYzIsMCwzLjItMSwzLjItMi43CgkJYzAtMS43LTEuMi0yLjUtMy0yLjVjLTAuNywwLTEuMywwLjEtMS42LDAuMVY2OS44eiIgY2xhc3M9InN0MyI+PC9wYXRoPgoJPHBhdGggZD0iTTI4NSw3MS42YzAsMy4zLTIuNCw0LjgtNC42LDQuOGMtMi41LDAtNC40LTEuOC00LjQtNC42YzAtMywyLTQuOCw0LjYtNC44QzI4My4yLDY2LjksMjg1LDY4LjgsMjg1LDcxLjZ6CgkJIE0yNzcuNiw3MS43YzAsMiwxLjIsMy41LDIuOCwzLjVjMS42LDAsMi44LTEuNSwyLjgtMy41YzAtMS41LTAuOC0zLjUtMi44LTMuNVMyNzcuNiw2OS45LDI3Ny42LDcxLjd6IiBjbGFzcz0ic3QzIj48L3BhdGg+Cgk8cGF0aCBkPSJNMjg3LjYsNjcuMWwxLjIsNC42YzAuMywxLDAuNSwxLjksMC43LDIuOWgwLjFjMC4yLTAuOSwwLjUtMS45LDAuOC0yLjlsMS41LTQuNmgxLjRsMS40LDQuNQoJCWMwLjMsMS4xLDAuNiwyLDAuOCwyLjloMC4xYzAuMi0wLjksMC40LTEuOSwwLjctMi45bDEuMy00LjVoMS43bC0zLDloLTEuNWwtMS40LTQuM2MtMC4zLTEtMC42LTEuOS0wLjgtM2gwYy0wLjIsMS4xLTAuNSwyLTAuOCwzCgkJbC0xLjUsNC4zaC0xLjVsLTIuOC05SDI4Ny42eiIgY2xhc3M9InN0MyI+PC9wYXRoPgoJPHBhdGggZD0iTTMwMS44LDcxLjljMCwyLjIsMS41LDMuMSwzLjIsMy4xYzEuMiwwLDEuOS0wLjIsMi42LTAuNWwwLjMsMS4yYy0wLjYsMC4zLTEuNiwwLjYtMy4xLDAuNgoJCWMtMi45LDAtNC42LTEuOC00LjYtNC41czEuNi00LjksNC4zLTQuOWMzLDAsMy44LDIuNiwzLjgsNC4zYzAsMC4zLDAsMC42LTAuMSwwLjhIMzAxLjh6IE0zMDYuOCw3MC44YzAtMS0wLjQtMi43LTIuMy0yLjcKCQljLTEuNywwLTIuNSwxLjUtMi42LDIuN0gzMDYuOHoiIGNsYXNzPSJzdDMiPjwvcGF0aD4KCTxwYXRoIGQ9Ik0zMTAuNSw2OS45YzAtMS4xLDAtMi0wLjEtMi44aDEuNWwwLjEsMS44aDAuMWMwLjQtMS4yLDEuNC0yLDIuNi0yYzAuMiwwLDAuMywwLDAuNSwwLjF2MS41CgkJYy0wLjIsMC0wLjMtMC4xLTAuNi0wLjFjLTEuMiwwLTIsMC45LTIuMywyLjFjMCwwLjItMC4xLDAuNS0wLjEsMC44djQuOGgtMS43VjY5Ljl6IiBjbGFzcz0ic3QzIj48L3BhdGg+Cgk8cGF0aCBkPSJNMzE3LjUsNzEuOWMwLDIuMiwxLjUsMy4xLDMuMiwzLjFjMS4yLDAsMS45LTAuMiwyLjYtMC41bDAuMywxLjJjLTAuNiwwLjMtMS42LDAuNi0zLjEsMC42CgkJYy0yLjksMC00LjYtMS44LTQuNi00LjVzMS42LTQuOSw0LjMtNC45YzMsMCwzLjgsMi42LDMuOCw0LjNjMCwwLjMsMCwwLjYtMC4xLDAuOEgzMTcuNXogTTMyMi40LDcwLjhjMC0xLTAuNC0yLjctMi4zLTIuNwoJCWMtMS43LDAtMi41LDEuNS0yLjYsMi43SDMyMi40eiIgY2xhc3M9InN0MyI+PC9wYXRoPgoJPHBhdGggZD0iTTMzNC4yLDYyLjl2MTAuOWMwLDAuOCwwLDEuNywwLjEsMi4zaC0xLjVsLTAuMS0xLjZoMGMtMC41LDEtMS42LDEuOC0zLjIsMS44Yy0yLjIsMC00LTEuOC00LTQuNgoJCWMwLTMsMS45LTQuOCw0LjItNC44YzEuNCwwLDIuNCwwLjcsMi44LDEuNGgwdi01LjRIMzM0LjJ6IE0zMzIuNSw3MC44YzAtMC4yLDAtMC41LTAuMS0wLjdjLTAuMi0xLTEuMi0xLjktMi40LTEuOQoJCWMtMS43LDAtMi44LDEuNS0yLjgsMy41YzAsMS44LDAuOSwzLjMsMi43LDMuM2MxLjEsMCwyLjItMC43LDIuNS0yYzAuMS0wLjIsMC4xLTAuNCwwLjEtMC43VjcwLjh6IiBjbGFzcz0ic3QzIj48L3BhdGg+CjwvZz4KPC9zdmc+Cg=='}

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="ibfetch-token" content="__TOKEN__">
<meta name="ibfetch-appearance" content="__APPEARANCE__">
<meta name="ibfetch-palette" content="__PALETTE__">
<script>
(() => {
  const root = document.documentElement;
  const appearance = document.querySelector('meta[name="ibfetch-appearance"]').content;
  const palette = document.querySelector('meta[name="ibfetch-palette"]').content;
  root.dataset.appearance = ["auto", "light", "dark"].includes(appearance) ? appearance : "auto";
  root.dataset.palette = ["forest", "ocean", "warm", "ibroadcast", "amber"].includes(palette) ? palette : "forest";
  const dark = root.dataset.appearance === "dark" || (root.dataset.appearance === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "dark" : "light";
})();
</script>
<title>ibfetch</title>
<style>
/* Tagcast colour roles, adapted to ibfetch; appearance and palette are independent. */
:root {
  --bg: #f7f8f4;
  --panel: #fbfcf9;
  --raised: #ffffff;
  --ink: #203329;
  --muted: #68746c;
  --line: #e0e5dd;
  --accent: #2d654d;
  --accent-fill: #2d654d;
  --accent-soft: #e5ecd9;
  --focus: #759345;
  --shell: #1e3027;
  --shell-ink: #dce7de;
  --shell-muted: #b5c6b8;
  --shell-active: #344b3d;
  --shell-line: #3a4e40;
  --shell-accent: #d2e59e;
  --window: #1e3027;
  --window-ink: #d2e59e;
  --window-dim: #b5c6b8;
  --window-track: #344b3d;
  --window-stats: #dce7de;
  --primary-ink: #ffffff;
  --red: #b9302a;
}
:root { --round: ui-rounded, "SF Pro Rounded", system-ui, sans-serif; color-scheme: light; }
:root[data-theme="dark"] {
  --bg: #121915;
  --panel: #18211c;
  --raised: #1b2520;
  --ink: #d3ebde;
  --muted: #a7b3ab;
  --line: #393b37;
  --accent: #91cbaf;
  --accent-fill: #91cbaf;
  --accent-soft: #2b2e26;
  --focus: #8fae60;
  --shell: #1e3027;
  --shell-ink: #dce7de;
  --shell-muted: #b5c6b8;
  --shell-active: #344b3d;
  --shell-line: #3a4e40;
  --shell-accent: #d2e59e;
  --window: #1e3027;
  --window-ink: #d2e59e;
  --window-dim: #b5c6b8;
  --window-track: #344b3d;
  --window-stats: #dce7de;
  --primary-ink: #1b2520;
  --red: #e5574b;
}
:root[data-theme="dark"] { color-scheme: dark; }
:root[data-palette="ocean"] {
  --bg: #f5f8fa;
  --panel: #f0f5f8;
  --raised: #ffffff;
  --ink: #213746;
  --muted: #536b7a;
  --line: #d7e3e9;
  --accent: #246276;
  --accent-fill: #246276;
  --accent-soft: #e5f0f4;
  --focus: #559bac;
  --shell: #172d38;
  --shell-ink: #e8f3f7;
  --shell-muted: #b2c8d3;
  --shell-active: #294958;
  --shell-line: #435e6b;
  --shell-accent: #9bdce7;
  --window: #172d38;
  --window-ink: #9bdce7;
  --window-dim: #b2c8d3;
  --window-track: #294958;
  --window-stats: #e8f3f7;
  --primary-ink: #ffffff;
  --red: #b9302a;
}
:root[data-palette="ocean"][data-theme="dark"] {
  --bg: #101f28;
  --panel: #162731;
  --raised: #1a2d38;
  --ink: #e0eef3;
  --muted: #afc4cf;
  --line: #344956;
  --accent: #90d9e6;
  --accent-fill: #286b7b;
  --accent-soft: #233f4c;
  --focus: #67b8cb;
  --shell: #172d38;
  --shell-ink: #e8f3f7;
  --shell-muted: #b2c8d3;
  --shell-active: #294958;
  --shell-line: #435e6b;
  --shell-accent: #9bdce7;
  --window: #172d38;
  --window-ink: #9bdce7;
  --window-dim: #b2c8d3;
  --window-track: #294958;
  --window-stats: #e8f3f7;
  --primary-ink: #ffffff;
  --red: #e5574b;
}
:root[data-palette="warm"] {
  --bg: #faf7f2;
  --panel: #f7f1e8;
  --raised: #fffefa;
  --ink: #3b2c23;
  --muted: #76614f;
  --line: #e6dcd0;
  --accent: #805334;
  --accent-fill: #805334;
  --accent-soft: #f0e4d6;
  --focus: #b18459;
  --shell: #33281f;
  --shell-ink: #f7eee3;
  --shell-muted: #d0bfae;
  --shell-active: #574233;
  --shell-line: #735b48;
  --shell-accent: #efc99e;
  --window: #33281f;
  --window-ink: #efc99e;
  --window-dim: #d0bfae;
  --window-track: #574233;
  --window-stats: #f7eee3;
  --primary-ink: #ffffff;
  --red: #b9302a;
}
:root[data-palette="warm"][data-theme="dark"] {
  --bg: #211a15;
  --panel: #2a211b;
  --raised: #30251d;
  --ink: #f2e8dc;
  --muted: #c7b6a5;
  --line: #564336;
  --accent: #e6bf96;
  --accent-fill: #865632;
  --accent-soft: #483425;
  --focus: #d2a16e;
  --shell: #33281f;
  --shell-ink: #f7eee3;
  --shell-muted: #d0bfae;
  --shell-active: #574233;
  --shell-line: #735b48;
  --shell-accent: #efc99e;
  --window: #33281f;
  --window-ink: #efc99e;
  --window-dim: #d0bfae;
  --window-track: #574233;
  --window-stats: #f7eee3;
  --primary-ink: #ffffff;
  --red: #e5574b;
}
:root[data-palette="ibroadcast"] {
  --bg: #f4f7fb;
  --panel: #edf3fa;
  --raised: #ffffff;
  --ink: #1c3048;
  --muted: #53677f;
  --line: #d5dfeb;
  --accent: #1c559b;
  --accent-fill: #1c559b;
  --accent-soft: #e3edf9;
  --focus: #518ec5;
  --shell: #162234;
  --shell-ink: #ecedf1;
  --shell-muted: #b7bec9;
  --shell-active: #184e8e;
  --shell-line: #30445f;
  --shell-accent: #a0dafa;
  --window: #162234;
  --window-ink: #a0dafa;
  --window-dim: #b7bec9;
  --window-track: #184e8e;
  --window-stats: #ecedf1;
  --primary-ink: #ffffff;
  --red: #b9302a;
}
:root[data-palette="ibroadcast"][data-theme="dark"] {
  --bg: #0d1828;
  --panel: #162234;
  --raised: #1e2d43;
  --ink: #eceef2;
  --muted: #b1bdcd;
  --line: #2d405b;
  --accent: #9bd9f8;
  --accent-fill: #1c559b;
  --accent-soft: #213d60;
  --focus: #5c99d6;
  --shell: #162234;
  --shell-ink: #ecedf1;
  --shell-muted: #b7bec9;
  --shell-active: #184e8e;
  --shell-line: #30445f;
  --shell-accent: #a0dafa;
  --window: #162234;
  --window-ink: #a0dafa;
  --window-dim: #b7bec9;
  --window-track: #184e8e;
  --window-stats: #ecedf1;
  --primary-ink: #ffffff;
  --red: #e5574b;
}
/* Amber preserves the original neutral grey surfaces and orange accents. */
:root[data-palette="amber"] {
  --bg: #e2e4df;
  --panel: #eff0ec;
  --raised: #fafaf8;
  --ink: #1c2024;
  --muted: #5c636a;
  --line: #c6cac2;
  --accent: #b8730c;
  --accent-fill: #e9a23a;
  --accent-soft: #f3e1bd;
  --focus: #b8730c;
  --shell: #eff0ec;
  --shell-ink: #1c2024;
  --shell-muted: #5c636a;
  --shell-active: #f3e1bd;
  --shell-line: #c6cac2;
  --shell-accent: #e9a23a;
  --window: #15181b;
  --window-ink: #f2a93b;
  --window-dim: #7d6233;
  --window-track: #2a2f34;
  --window-stats: #c9b48e;
  --primary-ink: #241600;
}
:root[data-palette="amber"][data-theme="dark"] {
  --bg: #16181a;
  --panel: #1f2225;
  --raised: #272b2f;
  --ink: #e5e6e2;
  --muted: #969ca2;
  --line: #353a3f;
  --accent: #f0a63a;
  --accent-fill: #f0a63a;
  --accent-soft: #43361d;
  --focus: #f0a63a;
  --shell: #1f2225;
  --shell-ink: #e5e6e2;
  --shell-muted: #969ca2;
  --shell-active: #43361d;
  --shell-line: #353a3f;
  --shell-accent: #f0a63a;
  --window: #0e1012;
  --window-ink: #f2a93b;
  --window-dim: #7d6233;
  --window-track: #23272b;
  --window-stats: #c9b48e;
  --primary-ink: #241600;
}
:root[data-palette="amber"] #crate input { background: var(--raised); }
:root[data-palette="amber"] #crate .artist:hover:not([aria-pressed=true]) { background: var(--raised); }
:root[data-palette="amber"] #crate #letters button[aria-pressed=true] { color: var(--primary-ink); }
:root[data-palette="amber"] #crate :focus-visible { outline-color: var(--focus); }
:root[data-palette="amber"] .tally b { color: #d9c59f; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
button, input, select { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
[hidden] { display: none !important; }

button {
  border: 1px solid var(--line); background: var(--raised); border-radius: 6px;
  padding: 6px 12px; cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--muted); }
button:disabled { opacity: .45; cursor: default; }
button.primary {
  background: var(--accent-fill); border-color: var(--accent-fill); color: var(--primary-ink); font-weight: 600;
}
button.primary:hover:not(:disabled) { filter: brightness(1.06); border-color: var(--accent-fill); }
button.primary:disabled { background: var(--raised); border-color: var(--line); color: var(--muted); opacity: 1; font-weight: 500; }
button.quiet { border-color: transparent; background: transparent; color: var(--muted); }
button.quiet:hover:not(:disabled) { color: var(--ink); border-color: var(--line); }
input[type=text], input[type=search], select {
  width: 100%; padding: 6px 9px; border: 1px solid var(--line); border-radius: 6px; background: var(--raised);
  min-width: 0;
}
input::placeholder { color: var(--muted); opacity: .8; }
label { display: block; }
.field > span:first-child { display: block; font-size: 12px; color: var(--muted); margin-bottom: 3px; }

/* header */
.top {
  height: 52px; display: flex; align-items: center; gap: 12px; padding: 0 16px;
  border-bottom: 1px solid var(--line); background: var(--panel);
}
.brand { font: 600 19px/1 var(--round); letter-spacing: -.01em; margin-right: 6px; }
.brand b { color: var(--accent); font-weight: 600; }
.brand { display: flex; align-items: center; gap: 6px; }
.brand img { width: 25px; height: 25px; }
.brand-block { flex: 0 0 auto; }
.maker { display: block; color: var(--muted); font-size: 10px; margin-top: 4px; }
.powered { display: inline-block; flex: 0 0 auto; line-height: 0; }
.powered img { width: 132px; height: auto; }
.powered .powered-dark { display: none; }
:root[data-theme="dark"] .powered .powered-light { display: none; }
:root[data-theme="dark"] .powered .powered-dark { display: inline; }
.update-wrap { position: relative; }
.appearance-panel.update-panel { width: 310px; max-width: calc(100vw - 28px); }
.update-panel p { margin: 0; font-size: 13px; color: var(--muted); }
.update-panel .check { display: flex; gap: 8px; align-items: flex-start; }
.update-panel .row-gap { display: flex; gap: 8px; flex-wrap: wrap; }
.update-panel a { font-size: 13px; }
.update-panel .project-links a { color: var(--accent); }
#updateBtn.available { color: var(--accent); background: var(--accent-soft); }
#libInfo { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.spacer { flex: 1; }
#account { color: var(--muted); white-space: nowrap; }
.appearance { position: relative; }
.appearance-panel {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 5; width: 240px;
  padding: 14px; background: var(--raised); border: 1px solid var(--line); border-radius: 8px;
  box-shadow: 0 12px 32px #0003; display: grid; gap: 12px;
}
.badge {
  display: inline-block; font-size: 12px; padding: 1px 7px; border-radius: 10px; margin-left: 4px;
  background: var(--accent-soft); color: var(--ink);
}

/* sign-in */
.center { min-height: calc(100% - 52px); display: grid; place-items: center; padding: 24px; }
.signin { max-width: 460px; width: 100%; }
.signin details { margin-top: 20px; }
.signin summary { cursor: pointer; color: var(--muted); }
.signin details p { margin-top: 14px; }
.signin h1 { font: 600 28px/1.15 var(--round); margin: 0 0 10px; }
.signin p { color: var(--muted); margin: 0 0 18px; }
.code {
  font: 300 40px/1 var(--round); letter-spacing: .06em; font-variant-numeric: tabular-nums;
  background: var(--window); color: var(--window-ink); border-radius: 10px; padding: 18px 20px; margin: 0 0 14px;
  text-align: center; text-shadow: 0 0 14px color-mix(in srgb, var(--window-ink) 35%, transparent);
}
.row-gap { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
a.button {
  display: inline-block; text-decoration: none; padding: 6px 12px; border-radius: 6px;
  background: var(--accent-fill); color: var(--primary-ink); font-weight: 600;
}
.error { color: var(--red); }

/* main grid */
#app {
  display: grid; grid-template-columns: 250px minmax(0, 1fr) 350px; height: calc(100% - 52px);
}
#app > * { min-height: 0; overflow: auto; }

/* artists */
#crate { border-right: 1px solid var(--line); background: var(--panel); display: flex; flex-direction: column; overflow: hidden; }
#crate { background: var(--shell); color: var(--shell-ink); border-color: var(--shell-line); }
#crate input { background: var(--shell-active); color: var(--shell-ink); border-color: var(--shell-line); }
#crate input::placeholder, #crate .artist .count, #crate .more { color: var(--shell-muted); }
#crate :focus-visible { outline-color: var(--shell-accent); }
.crate-head { padding: 12px 12px 8px; }
.crate-head h2, #deck h2 { font: 600 15px/1.2 var(--round); margin: 0 0 8px; }
#letters { display: flex; flex-wrap: wrap; gap: 2px; padding: 0 10px 8px; border-bottom: 1px solid var(--line); }
#letters button {
  padding: 2px 0; width: 25px; font-size: 12px; border: 0; background: transparent; color: var(--muted); border-radius: 4px;
}
#letters button.all { width: auto; padding: 2px 7px; }
#letters button[aria-pressed=true] { background: var(--accent-fill); color: var(--primary-ink); font-weight: 600; }
#crate #letters { border-color: var(--shell-line); }
#crate #letters button { color: var(--shell-muted); }
#crate #letters button[aria-pressed=true] { background: var(--shell-accent); color: var(--shell); }
#artistList { overflow: auto; flex: 1; padding: 6px 6px 16px; }
.artist {
  display: flex; width: 100%; justify-content: space-between; gap: 8px; text-align: left;
  border: 0; background: transparent; padding: 5px 8px; border-radius: 5px;
}
.artist:hover { background: var(--raised); }
.artist[aria-pressed=true] { background: var(--accent-soft); }
#crate .artist:hover, #crate .artist[aria-pressed=true] { background: var(--shell-active); }
.artist .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artist .count { color: var(--muted); font-variant-numeric: tabular-nums; }
.artist.every { font-weight: 600; margin-bottom: 4px; }
.more { color: var(--muted); padding: 6px 8px; margin: 0; font-size: 13px; }

/* browse */
#browse { display: flex; flex-direction: column; overflow: hidden; }
#filters { padding: 12px 16px 10px; border-bottom: 1px solid var(--line); }
.fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 10px; }
.filter-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.check { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.hint { color: var(--muted); font-size: 13px; margin-left: auto; }
.resultbar {
  display: flex; align-items: center; gap: 8px; padding: 8px 16px; border-bottom: 1px solid var(--line);
  background: var(--panel);
}
#resultInfo { flex: 1; color: var(--muted); }
#results { flex: 1; overflow: auto; position: relative; }
.grid-row {
  display: grid; grid-template-columns: 34px minmax(110px, 1fr) minmax(150px, 1.6fr) 50px 54px 86px 66px;
  align-items: center; gap: 0 10px; padding: 0 16px 0 8px; min-height: 34px;
}
.head {
  position: sticky; top: 0; z-index: 1; background: var(--bg); color: var(--muted); font-size: 12px;
  border-bottom: 1px solid var(--line);
}
.row { border-bottom: 1px solid color-mix(in srgb, var(--line) 55%, transparent); cursor: default; user-select: none; }
.row:hover { background: var(--panel); }
.row.on { background: var(--accent-soft); box-shadow: inset 3px 0 var(--accent); }
.row.inq .c-album::before {
  content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
  margin-right: 7px; vertical-align: 1px;
}
.row > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.c-chk { display: grid; place-items: center; }
.expand {
  border: 0; background: none; padding: 0; width: 20px; margin-right: 4px; color: var(--muted); cursor: pointer;
  font-size: 11px; line-height: 1; transition: transform .15s;
}
.expand:hover { color: var(--ink); }
.expand[aria-expanded=true] { transform: rotate(90deg); }
.disc { color: var(--muted); font-size: 12px; margin-left: 6px; }
.tracklist { padding: 4px 16px 10px 66px; background: var(--panel); border-bottom: 1px solid var(--line); }
.tracklist ol { margin: 0; padding: 0; list-style: none; }
.tracklist li { display: grid; grid-template-columns: 28px 1fr auto auto; gap: 10px; padding: 2px 0; color: var(--muted); }
.tracklist li b { font-weight: 400; color: var(--ink); }
.tracklist .ta { margin-left: 10px; }
.empty { padding: 48px 24px; color: var(--muted); max-width: 520px; margin: 0 auto; text-align: center; }
.empty strong { display: block; color: var(--ink); font: 600 18px/1.3 var(--round); margin-bottom: 6px; }

/* deck */
#deck { border-left: 1px solid var(--line); background: var(--panel); padding: 14px 14px 24px; display: flex; flex-direction: column; gap: 14px; }
.deck-head { display: flex; align-items: baseline; justify-content: space-between; }
#queueInfo { color: var(--muted); font-size: 13px; }
#queueList { list-style: none; margin: 0; padding: 0; max-height: 34vh; overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--raised); }
#queueList:empty { display: none; }
#queueList li { display: grid; grid-template-columns: 1fr auto auto; gap: 8px; align-items: center; padding: 6px 6px 6px 10px; border-bottom: 1px solid var(--line); }
#queueList li:last-child { border-bottom: 0; }
.q-album { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.q-artist { display: block; font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.q-n { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 13px; }
.q-x { border: 0; background: none; color: var(--muted); padding: 2px 6px; font-size: 16px; line-height: 1; }
.q-x:hover { color: var(--red); }
.settings { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.settings .wide { grid-column: 1 / -1; }
.source-help { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
.field .folder, details .folder { display: flex; gap: 6px; }
details summary { cursor: pointer; color: var(--muted); font-size: 13px; }
details p { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
#downloadBtn { padding: 10px 14px; font-size: 15px; }
.premium-note { color: var(--muted); font-size: 12px; margin-top: 4px; }

/* the transfer window */
.window {
  background: var(--window); color: var(--window-ink); border-radius: 12px; padding: 14px 16px 12px;
  box-shadow: inset 0 0 0 1px #000, inset 0 1px 0 rgba(255,255,255,.06), 0 1px 0 rgba(255,255,255,.4);
}
.window-top { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--window-dim); }
.lamp { width: 8px; height: 8px; border-radius: 50%; background: #4a1a16; }
.lamp.on { background: #ff5446; box-shadow: 0 0 8px #ff5446; animation: blink 1.6s ease-in-out infinite; }
.lamp.done { background: var(--window-ink); box-shadow: 0 0 6px var(--window-ink); }
@keyframes blink { 50% { opacity: .45; } }
.counter {
  font: 300 46px/1.05 var(--round); font-variant-numeric: tabular-nums; letter-spacing: .02em;
  text-shadow: 0 0 14px color-mix(in srgb, var(--window-ink) 35%, transparent); margin: 6px 0 8px; white-space: nowrap;
}
.counter small { font-size: 20px; color: var(--window-dim); text-shadow: none; }
.meter { position: relative; height: 12px; background: repeating-linear-gradient(90deg, var(--window-track) 0 7px, transparent 7px 9px); }
.meter .fill {
  position: absolute; inset: 0 auto 0 0; width: 0;
  background: repeating-linear-gradient(90deg, var(--window-ink) 0 7px, transparent 7px 9px);
  filter: drop-shadow(0 0 4px color-mix(in srgb, var(--window-ink) 50%, transparent)); transition: width .4s linear;
}
.window-stats { margin-top: 9px; font-size: 13px; color: var(--window-stats); font-variant-numeric: tabular-nums; }
.tally { display: flex; gap: 14px; margin-top: 4px; font-size: 12px; color: var(--window-dim); }
.tally b { color: var(--window-stats); font-weight: 500; }
.active { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.active li { font-size: 12px; }
.active .nm { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.mini { height: 3px; background: var(--line); border-radius: 2px; margin-top: 3px; overflow: hidden; }
.mini i { display: block; height: 100%; background: var(--accent-fill); }
.mini.busy i { width: 35% !important; animation: busy 1.4s ease-in-out infinite alternate; }
@keyframes busy { from { transform: translateX(0); } to { transform: translateX(186%); } }
.recent { list-style: none; margin: 0; padding: 0; font-size: 12px; color: var(--muted); }
.recent li { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 1px 0; }
.recent .st { display: inline-block; width: 86px; color: var(--ink); }
.recent .fail { color: var(--red); }
.fails { font-size: 12px; }
.fails ul { margin: 6px 0 0; padding-left: 16px; color: var(--muted); max-height: 160px; overflow: auto; }

/* toast */
#notice {
  position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); max-width: min(560px, 92vw);
  background: var(--ink); color: var(--bg); padding: 9px 14px; border-radius: 8px; font-size: 13px;
  box-shadow: 0 6px 24px rgba(0,0,0,.25); z-index: 10;
}
#notice.bad { background: var(--red); color: #fff; }
.bye { text-align: center; color: var(--muted); }
.bye strong { display: block; color: var(--ink); font: 600 22px/1.3 var(--round); }

@media (max-width: 1180px) {
  #app { grid-template-columns: 230px minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) auto; }
  #deck { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--line); max-height: 46vh; }
  .fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .appearance-panel.update-panel { position: fixed; top: 110px; left: 10px; right: 10px; width: auto; }
  body { height: auto; }
  #app { display: block; height: auto; }
  #crate { max-height: 50vh; border-right: 0; border-bottom: 1px solid var(--line); }
  #browse { overflow: visible; }
  #results { max-height: 70vh; }
  #deck { max-height: none; }
  .grid-row { grid-template-columns: 30px minmax(0, 1fr) minmax(0, 1.3fr) 44px; padding: 0 10px 0 4px; }
  .grid-row > :nth-child(n+5) { display: none; }
  .top { gap: 8px; padding: 0 10px; }
  .top { flex-wrap: wrap; height: auto; min-height: 52px; padding-block: 8px; }
  #account { max-width: 110px; overflow: hidden; text-overflow: ellipsis; }
  #libInfo, #refreshBtn { display: none; }
  .hint { margin-left: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .lamp.on { animation: none; }
  .mini.busy i { animation: none; width: 100% !important; opacity: .45; }
  .meter .fill { transition: none; }
}
</style>
</head>
<body>
<header class="top">
  <div class="brand-block"><div class="brand"><img src="__IBFETCH_ICON__" alt=""><span>ib<b>fetch</b></span></div><span class="maker">by Cyberdelia</span></div>
  <a class="powered" href="https://www.ibroadcast.com/" target="_blank" rel="noopener noreferrer" aria-label="Powered by iBroadcast">
    <img class="powered-light" src="__POWERED_LIGHT__" alt="Powered by iBroadcast">
    <img class="powered-dark" src="__POWERED_DARK__" alt="Powered by iBroadcast">
  </a>
  <span id="libInfo"></span>
  <button id="refreshBtn" class="quiet" hidden title="Download the library index again">Refresh library</button>
  <span class="spacer"></span>
  <div class="update-wrap">
    <button id="updateBtn" class="quiet" aria-expanded="false" aria-controls="updatePanel">ibfetch __VERSION__</button>
    <div id="updatePanel" class="appearance-panel update-panel" hidden>
      <strong>ibfetch <span id="currentVersion">__VERSION__</span></strong>
      <p>Independent app by Cyberdelia. Not made or endorsed by iBroadcast.</p>
      <p id="updateStatus" role="status" aria-live="polite">Updates are checked automatically, at most once a day.</p>
      <label class="check"><input type="checkbox" id="autoUpdates" checked> Automatically check for updates</label>
      <div class="row-gap"><button id="checkUpdates">Check for updates</button>
        <a id="updateLink" class="button" href="https://github.com/cyberdeliaAI/ibfetch/releases/latest" target="_blank" rel="noopener noreferrer">Releases &amp; downloads</a></div>
      <p>No account or library data is sent. Installation is manual; quit ibfetch before upgrading.</p>
      <div class="row-gap project-links">
        <a href="https://github.com/cyberdeliaAI/ibfetch/actions/workflows/tests.yml" target="_blank" rel="noopener noreferrer">Tests</a>
        <a href="https://github.com/cyberdeliaAI/ibfetch/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">MIT license</a>
      </div>
    </div>
  </div>
  <div class="appearance">
    <button id="themeBtn" class="quiet" aria-expanded="false" aria-controls="themeMenu">◐ Theme ▾</button>
    <div id="themeMenu" class="appearance-panel" hidden>
      <label class="field"><span>Appearance</span><select id="appearanceChoice">
        <option value="auto">Auto · follow system</option><option value="light">Light</option><option value="dark">Dark</option>
      </select></label>
      <label class="field"><span>Colour</span><select id="paletteChoice">
        <option value="forest">Forest</option><option value="ocean">Ocean</option>
        <option value="warm">Warm</option><option value="ibroadcast">iBroadcast inspired</option>
        <option value="amber">Amber · Original</option>
      </select></label>
    </div>
  </div>
  <span id="account"></span>
  <button id="signoutBtn" class="quiet" hidden>Sign out</button>
  <button id="quitBtn" class="quiet">Quit</button>
</header>

<main id="signin" class="center" hidden>
  <div class="signin">
    <h1>Sign in to iBroadcast</h1>
    <div id="signinIdle">
      <p>Sign in on the official iBroadcast website in a separate window. Approve the connection and ibfetch continues automatically. Your password stays on iBroadcast.</p>
      <button id="signinBtn" class="primary">Sign in with iBroadcast</button>
    </div>
    <div id="signinWait" hidden>
      <p id="signinStatus" role="status">Opening the iBroadcast sign-in window…</p>
      <div class="row-gap">
        <button id="signinOpen" class="primary">Open sign-in window</button>
        <button id="signinCancel" class="quiet">Cancel</button>
      </div>
      <details id="signinFallback">
        <summary>Use your browser or a code instead</summary>
        <p>If the sign-in window is unavailable, open iBroadcast in your browser. Enter this code if requested:</p>
        <div class="code" id="signinCode"></div>
        <a id="signinLink" class="button" href="#" target="_blank" rel="noopener noreferrer">Open iBroadcast in your browser ↗</a>
      </details>
    </div>
    <p id="signinError" class="error" role="alert" hidden></p>
  </div>
</main>

<main id="app" hidden>
  <aside id="crate" aria-label="Artists">
    <div class="crate-head">
      <h2>Artists</h2>
      <input id="artistFind" type="search" placeholder="Find an artist" aria-label="Find an artist" autocomplete="off">
    </div>
    <nav id="letters" aria-label="First letter"></nav>
    <div id="artistList"></div>
  </aside>

  <section id="browse" aria-label="Albums">
    <form id="filters" autocomplete="off">
      <div class="fields">
        <label class="field"><span>Album artist</span><input id="fArtist" type="text" placeholder="4hero or A*"></label>
        <label class="field"><span>Album</span><input id="fAlbum" type="text" placeholder="Title or Two*"></label>
        <label class="field"><span>Track artist</span><input id="fTrackArtist" type="text" placeholder="Also on compilations"></label>
        <label class="field"><span>Year</span><input id="fYear" type="text" placeholder="1998 or 1990-1999"></label>
        <label class="field"><span>Genre</span><input id="fGenre" type="text" list="genreList" placeholder="Any genre"></label>
        <label class="field"><span>Format</span><select id="fFormat"><option value="">Any format</option></select></label>
        <label class="field"><span>Tag</span><select id="fTag"><option value="">Any tag</option></select></label>
        <label class="field"><span>Playlist</span><select id="fPlaylist"><option value="">Any playlist</option></select></label>
      </div>
      <datalist id="genreList"></datalist>
      <div class="filter-actions">
        <button type="submit" class="primary">Search</button>
        <button type="button" id="clearBtn" class="quiet">Clear</button>
        <label class="check"><input id="fSkipThe" type="checkbox"> Ignore “The” at the start</label>
        <span class="hint">* matches anything, so A* finds every name starting with A.</span>
      </div>
    </form>
    <div class="resultbar">
      <span id="resultInfo" aria-live="polite"></span>
      <button id="addSelBtn" disabled>Add selected</button>
      <button id="addAllBtn" disabled>Add all</button>
    </div>
    <div id="results"></div>
  </section>

  <aside id="deck" aria-label="Download">
    <div>
      <div class="deck-head"><h2>Download list</h2><button id="clearQueueBtn" class="quiet" hidden>Empty list</button></div>
      <div id="queueInfo"></div>
    </div>
    <ul id="queueList"></ul>
    <div class="settings">
      <label class="field wide"><span>Save to</span>
        <span class="folder"><input id="outDir" type="text" spellcheck="false"><button type="button" id="chooseBtn">Choose</button></span>
      </label>
      <label class="field"><span>Quality</span><select id="quality"></select></label>
      <label class="field"><span>At the same time</span><select id="jobs"></select></label>
      <label class="field wide" id="sourceField"><span>Original files from</span><select id="source">
        <option value="download">Download service: faster, your edits</option>
        <option value="stream">Streaming: exactly as uploaded</option>
      </select><p class="source-help">Updated tags require Download Updated Metadata to be enabled in iBroadcast.
        Existing files are preserved when you change source.</p></label>
      <details class="wide">
        <summary>Folder and file names</summary>
        <input id="layout" type="text" spellcheck="false" aria-label="Folder and file names" style="margin-top:6px">
        <p>Fields: {albumartist} {album} {year} {disc} {track} {disctrack} {title} {artist} {genre} {format}. A slash makes a folder.</p>
      </details>
      <details class="wide" id="workBox">
        <summary>Work folder</summary>
        <span class="folder" style="margin-top:6px"><input id="workDir" type="text" spellcheck="false" placeholder="Off: save directly" aria-label="Work folder"><button type="button" id="chooseWorkBtn">Choose</button></span>
        <p>Optional. Tracks download to this folder on your computer first, and each finished track then moves to Save to. Useful when Save to is a NAS.</p>
      </details>
      <p id="premiumNote" class="premium-note wide" hidden>Free account: downloads are MP3 128 kbps. Original files need iBroadcast Premium.</p>
      <details class="wide"><summary>Existing files</summary>
        <label class="check"><input id="verifyFiles" type="checkbox"> Verify existing files with SHA-256</label>
        <label class="check"><input id="adoptFiles" type="checkbox"> Adopt existing files I trust</label>
        <p>Normally, size and modification time are checked. Changed or unknown files are preserved and skipped.
        Adoption accepts matching-path files as local baselines; it does not verify their track or bitrate with iBroadcast.</p>
      </details>
    </div>
    <button id="downloadBtn" class="primary" disabled>Download</button>

    <section id="transfer" hidden aria-live="polite">
      <div class="window">
        <div class="window-top"><span id="lamp" class="lamp"></span><span id="tState">Downloading</span></div>
        <div class="counter" id="counter"></div>
        <div class="meter" role="progressbar" aria-label="Download progress" aria-valuemin="0" aria-valuemax="100" id="meter"><div class="fill" id="meterFill"></div></div>
        <div class="window-stats" id="tStats"></div>
        <div class="tally" id="tTally"></div>
      </div>
      <div class="row-gap" style="margin-top:10px">
        <button id="stopBtn">Stop</button>
        <button id="openBtn">Open folder</button>
        <button id="retryBtn" hidden>Try again</button>
      </div>
      <ul class="active" id="tActive" style="margin-top:12px"></ul>
      <ul class="recent" id="tRecent" style="margin-top:10px"></ul>
      <details class="fails" id="tFails" hidden><summary></summary><ul></ul></details>
      <details class="fails" id="tExisting" hidden><summary></summary><ul></ul></details>
      <details class="fails" id="tNotes" hidden><summary></summary><ul></ul></details>
    </section>
  </aside>
</main>

<main id="bye" class="center" hidden>
  <div class="bye"><strong>ibfetch has stopped</strong>You can close this tab. Run ibfetch again to come back.</div>
</main>

<div id="notice" role="status" hidden></div>

<script>
"use strict";
const TOKEN = document.querySelector('meta[name="ibfetch-token"]').content;
const $ = (id) => document.getElementById(id);
const QUALITY = [["orig", "Original file"], ["320", "MP3 320 kbps"], ["256", "MP3 256 kbps"],
                 ["192", "MP3 192 kbps"], ["128", "MP3 128 kbps"], ["96", "MP3 96 kbps"]];
const LETTERS = ["All", "#", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"];

let S = null, meta = null, metaVersion = 0, settingsApplied = false, misses = 0;
let searchGeneration = 0, sessionVersion = null, metadataLoadingVersion = null, downloadStarting = false;
let updatePreferenceRevision = 0, updatePreferencePending = false;
let results = [], checked = new Set(), lastClick = -1, letter = "All", activeArtist = null;
const queue = new Map();
const expanded = new Map();
const themeMedia = matchMedia("(prefers-color-scheme: dark)");
let themeSave = Promise.resolve();

function showTheme() {
  const root = document.documentElement;
  const appearance = root.dataset.appearance, palette = root.dataset.palette;
  root.dataset.theme = appearance === "dark" || (appearance === "auto" && themeMedia.matches) ? "dark" : "light";
  $("appearanceChoice").value = appearance;
  $("paletteChoice").value = palette;
  $("themeBtn").textContent = `${{auto: "◐", light: "☀", dark: "☾"}[appearance]} Theme ▾`;
  $("themeBtn").title = `${$("paletteChoice").options[$("paletteChoice").selectedIndex]?.text || palette} · ${appearance}`;
}

function setThemeMenu(open, restoreFocus = false) {
  $("themeMenu").hidden = !open;
  $("themeBtn").setAttribute("aria-expanded", String(open));
  if (restoreFocus) $("themeBtn").focus();
}

function changeTheme() {
  const root = document.documentElement;
  root.dataset.appearance = $("appearanceChoice").value;
  root.dataset.palette = $("paletteChoice").value;
  showTheme();
  const choice = {appearance: root.dataset.appearance, palette: root.dataset.palette};
  // Serialize saves so a slow earlier request cannot overwrite the latest choice.
  themeSave = themeSave.catch(() => {}).then(() => api("/api/appearance", choice))
    .catch(e => notice(`Could not save the theme: ${e.message}`, true));
  return themeSave;
}

async function api(path, body) {
  const opt = { headers: { "X-Ibfetch-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty answer */ }
  if (!res.ok) throw new Error(data.error || `ibfetch answered with HTTP ${res.status}.`);
  return data;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fold = (s) => String(s ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
const num = (n) => Number(n || 0).toLocaleString();
function clock(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function span(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
  if (h) return `${num(h)} h ${m} min`;
  if (m) return `${m} min`;
  return `${sec} s`;
}
function size(b) {
  b = b || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return i ? `${b.toFixed(b < 10 ? 1 : 0)} ${units[i]}` : `${b} B`;
}
const base = (p) => String(p).split(/[\\/]/).pop();

let noticeTimer = 0;
function notice(text, bad = false) {
  const el = $("notice");
  el.textContent = text;
  el.className = bad ? "bad" : "";
  el.setAttribute("role", bad ? "alert" : "status");
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 4000);
}

/* polling: one loop, poll() just refreshes now */
let stopped = false, statePollSerial = 0, loginRevision = 0;
async function poll() {
  if (stopped) return;
  const updateRevision = updatePreferenceRevision;
  const authRevision = loginRevision, serial = ++statePollSerial;
  try {
    const data = await api("/api/state");
    if (stopped || serial !== statePollSerial || authRevision !== loginRevision) return;
    if ((updatePreferencePending || updateRevision !== updatePreferenceRevision) && S?.updates && data.updates)
      data.updates.automatic = S.updates.automatic;
    S = data;
    misses = 0;
    render();
    // Read the completed state first: closing a popup after approval must never undo sign-in.
    if (!signInDesktop() && !S.signed_in && S.login?.state === "waiting"
        && signInFlow.popup?.closed) await cancelSignIn();
  } catch (e) {
    if (stopped || serial !== statePollSerial || authRevision !== loginRevision) return;
    if (++misses >= 3) { stopped = true; showBye(); }
  }
}
async function loop() {
  await poll();
  if (stopped) return;
  const busy = S && ((S.transfer && ["ready", "planning", "running"].includes(S.transfer.state)) || S.login || S.library.state === "loading");
  setTimeout(loop, busy ? 800 : 2500);
}

function showBye() {
  ++signInFlow.serial; loginRevision++;
  closeSignInChild(); signInFlow.requestId = ""; signInFlow.opening = false;
  setThemeMenu(false);
  setUpdatePanel(false);
  for (const id of ["app", "signin"]) $(id).hidden = true;
  $("bye").hidden = false;
  document.querySelectorAll(".top button, #account, #libInfo").forEach((el) => { el.hidden = true; });
}

function render() {
  renderUpdates(S.updates);
  if (sessionVersion !== S.session) {
    sessionVersion = S.session;
    searchGeneration++;
    results = []; checked.clear(); expanded.clear(); queue.clear();
    meta = null; metaVersion = 0; metadataLoadingVersion = null;
    $("artistList").innerHTML = "";
    $("letters").innerHTML = "";
    $("resultInfo").textContent = "";
    renderQueue();
  }
  const signed = S.signed_in;
  $("signin").hidden = signed;
  $("app").hidden = !signed;
  $("signoutBtn").hidden = !signed;
  $("refreshBtn").hidden = !signed || S.library.state === "loading";
  renderLogin();
  const lib = S.library;
  $("libInfo").textContent = !signed ? "" :
    lib.state === "loading" ? "Loading your library index" :
    lib.state === "error" ? "The library didn't load" :
    lib.counts ? `${num(lib.counts.tracks)} tracks in ${num(lib.counts.albums)} albums` : "";
  $("account").innerHTML = S.user ? `${esc(S.user.name || "Signed in")}<span class="badge">${S.user.premium ? "Premium" : "Free"}</span>` : "";
  if (signed && lib.state === "ready" && lib.version !== metaVersion) loadMeta(lib.version);
  if (signed && lib.state !== "ready" && !results.length) renderResultsEmpty();
  if (!settingsApplied && S.settings) applySettings(S.settings);
  if (S.user) {
    const free = !S.user.premium;
    $("premiumNote").hidden = !free;
    for (const opt of $("quality").options) opt.disabled = free && opt.value !== "128";
    if (free && $("quality").value !== "128") $("quality").value = "128";
    updateSource();
  }
  renderTransfer();
  updateDownloadButton();
}

function updateURL(data) {
  return data?.available && /^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$/.test(data.latest || "")
    ? `https://github.com/cyberdeliaAI/ibfetch/releases/tag/v${data.latest}` : "https://github.com/cyberdeliaAI/ibfetch/releases/latest";
}
function renderUpdates(data) {
  if (!data) return;
  const available = data.available && updateURL(data).includes("/tag/");
  $("currentVersion").textContent = data.current;
  $("updateBtn").textContent = available ? `Update available · ${data.latest}` : `ibfetch ${data.current}`;
  $("updateBtn").className = available ? "quiet available" : "quiet";
  $("autoUpdates").checked = data.automatic;
  $("checkUpdates").disabled = data.checking;
  $("updateLink").href = updateURL(data);
  $("updateLink").textContent = available ? `Download ${data.latest}` : "Releases & downloads";
  $("updateStatus").textContent = data.checking ? "Checking for updates…" :
    data.error ? (available ? `Last check failed. Last known update: ${data.latest}.` : "Could not check for updates. Try again or view GitHub releases.") :
    available ? `Version ${data.latest} is available. You are running ${data.current}.` :
    data.latest ? `You are running ${data.current}. Latest stable release: ${data.latest}.` :
    data.automatic ? "Updates are checked automatically, at most once a day." : "Automatic checks are off. You can still check manually.";
}
function setUpdatePanel(open, focus = false) {
  $("updatePanel").hidden = !open;
  $("updateBtn").setAttribute("aria-expanded", String(open));
  if (open) setThemeMenu(false);
  if (focus) $("updateBtn").focus();
}
async function checkUpdates() {
  $("checkUpdates").disabled = true;
  try { const data = await api("/api/updates/check", {}); if (S) S.updates = data; renderUpdates(data); }
  catch (error) { notice("Could not check for updates. Try again later.", true); }
  finally { $("checkUpdates").disabled = !!S?.updates?.checking; }
}
async function saveUpdatePreference() {
  const enabled = $("autoUpdates").checked;
  const previous = S?.updates?.automatic;
  updatePreferenceRevision++;
  updatePreferencePending = true;
  if (S?.updates) S.updates.automatic = enabled;
  $("autoUpdates").disabled = true;
  try {
    const data = await api("/api/updates/settings", {automatic: enabled});
    if (S) S.updates = data;
    renderUpdates(data);
  } catch (error) { if (S?.updates) S.updates.automatic = previous; renderUpdates(S?.updates); notice(error.message, true); }
  finally { updatePreferencePending = false; updatePreferenceRevision++; $("autoUpdates").disabled = false; }
}

// A provider window around the existing device flow; tokens stay in Python.
const signInFlow = {serial: 0, requestId: "", popup: null, opening: false, message: ""};
function signInDesktop() { return typeof window.ibfetchSignIn?.open === "function"; }
function signInVerificationUrl(login) {
  for (const value of [login?.url, login?.plain_url]) {
    if (typeof value !== "string" || value.length > 8192) continue;
    try {
      const url = new URL(value);
      if (url.origin === "https://oauth.ibroadcast.com" && !url.username && !url.password) return url.href;
    } catch { /* keep the manual code available for malformed provider links */ }
  }
  return "https://oauth.ibroadcast.com/device";
}
function reserveSignInPopup() {
  if (signInDesktop()) return null;
  try {
    // Reserve before the asynchronous request so normal browser popup blockers allow it.
    const popup = window.open("about:blank", "_blank", "popup,width=620,height=800,resizable=yes,scrollbars=yes");
    if (popup) {
      popup.opener = null;
      popup.document.title = "Sign in to iBroadcast";
      popup.document.body.textContent = "Opening iBroadcast sign-in…";
    }
    return popup;
  } catch { return null; }
}
function closeSignInChild() {
  if (signInFlow.requestId && signInDesktop()) window.ibfetchSignIn.close(signInFlow.requestId);
  try { signInFlow.popup?.close(); } catch { /* already closed */ }
  signInFlow.popup = null;
}
async function openSignInChild(login, serial, reserved = null) {
  if (serial !== signInFlow.serial) { try { reserved?.close(); } catch {} return; }
  signInFlow.requestId = login.request_id || "";
  let opened = false;
  if (signInDesktop() && signInFlow.requestId) {
    try { opened = await window.ibfetchSignIn.open(signInFlow.requestId) === true; } catch { /* browser fallback */ }
  } else {
    signInFlow.popup = reserved;
    try {
      if (reserved && !reserved.closed) { reserved.location.replace(signInVerificationUrl(login)); opened = true; }
    } catch { try { reserved?.close(); } catch {} signInFlow.popup = null; }
  }
  if (serial !== signInFlow.serial) return;
  signInFlow.opening = false;
  signInFlow.message = opened ? "Approve the connection in the iBroadcast sign-in window. ibfetch continues automatically."
    : "The sign-in window could not be opened. Use your browser or the code below.";
  $("signinFallback").open = !opened;
  renderLogin();
}
async function beginSignIn() {
  if (signInFlow.opening) return;
  const reserved = reserveSignInPopup(), serial = ++signInFlow.serial;
  closeSignInChild(); signInFlow.requestId = ""; signInFlow.popup = reserved;
  signInFlow.opening = true; signInFlow.message = ""; loginRevision++;
  if (S) S.login = null;
  renderLogin();
  try {
    const {login} = await api("/api/login", {});
    if (serial !== signInFlow.serial) {
      try { reserved?.close(); } catch {}
      if (login?.request_id) api("/api/login/cancel", {request_id: login.request_id}).catch(() => {});
      return;
    }
    if (!login || login.state !== "waiting") { signInFlow.opening = false; closeSignInChild(); await poll(); return; }
    if (S) S.login = login;
    loginRevision++;
    await openSignInChild(login, serial, reserved);
    poll();
  } catch (error) {
    try { reserved?.close(); } catch {}
    if (serial !== signInFlow.serial) return;
    signInFlow.opening = false; signInFlow.popup = null;
    if (S) S.login = {state: "error", message: error.message};
    loginRevision++; renderLogin();
  }
}
async function reopenSignIn() {
  const login = S?.login;
  if (login?.state !== "waiting" || signInFlow.opening) return;
  if (!signInDesktop() && signInFlow.popup && !signInFlow.popup.closed) {
    try { signInFlow.popup.focus(); return; } catch { /* reopen below */ }
  }
  const reserved = reserveSignInPopup(), serial = ++signInFlow.serial;
  signInFlow.opening = true;
  await openSignInChild(login, serial, reserved);
}
async function cancelSignIn() {
  const id = signInFlow.requestId || S?.login?.request_id, serial = ++signInFlow.serial;
  loginRevision++; closeSignInChild(); signInFlow.requestId = ""; signInFlow.opening = false;
  try {
    // A start still in flight cancels its own bound request when its reply arrives.
    if (id) await api("/api/login/cancel", {request_id: id});
    if (serial !== signInFlow.serial) return;
    if (S) S.login = null;
    loginRevision++; renderLogin(); poll();
  } catch (error) { if (serial === signInFlow.serial) { renderLogin(); notice(error.message, true); } }
}
function renderLogin() {
  const login = S?.login, waiting = login?.state === "waiting" && !S?.signed_in;
  const starting = (signInFlow.opening || login?.state === "starting") && !S?.signed_in;
  if (S?.signed_in || (!starting && !waiting)) {
    closeSignInChild(); signInFlow.requestId = "";
    if (S?.signed_in) { signInFlow.serial++; signInFlow.opening = false; }
  }
  $("signinIdle").hidden = !!(waiting || starting);
  $("signinWait").hidden = !(waiting || starting);
  $("signinBtn").disabled = !!starting;
  $("signinOpen").disabled = !!starting;
  $("signinFallback").hidden = !waiting;
  $("signinError").hidden = login?.state !== "error";
  if (login?.state === "error") $("signinError").textContent = login.message;
  $("signinStatus").textContent = starting && !waiting ? "Contacting iBroadcast…" :
    signInFlow.message || "Waiting for approval. Open the sign-in window, or use your browser below.";
  if (waiting) {
    $("signinCode").textContent = login.code || "";
    $("signinLink").href = signInVerificationUrl(login);
  }
}
if (typeof window.ibfetchSignIn?.onClosed === "function") window.ibfetchSignIn.onClosed(event => {
  if (event?.requestId !== signInFlow.requestId) return;
  if (["load-failed", "browser-needed"].includes(event.reason)) {
    signInFlow.opening = false;
    signInFlow.message = event.reason === "browser-needed" ? "This sign-in needs your browser. Use the link or code below."
      : "The sign-in page could not load. Try your browser or the code below.";
    $("signinFallback").open = true; renderLogin();
  } else cancelSignIn();
});

/* library index */
async function loadMeta(version) {
  if (metadataLoadingVersion === version) return;
  metadataLoadingVersion = version;
  const session = S.session;
  let loaded;
  try { loaded = await api("/api/meta"); } catch (e) {
    if (session === S.session) notice(e.message, true);
    return;
  } finally {
    if (metadataLoadingVersion === version) metadataLoadingVersion = null;
  }
  if (session !== S.session || version !== S.library.version || !S.signed_in) return;
  meta = loaded;
  metaVersion = version;
  const fill = (id, label, items) => {
    const sel = $(id), keep = sel.value;
    sel.innerHTML = `<option value="">${label}</option>` + items.map((v) => `<option>${esc(v)}</option>`).join("");
    if (items.includes(keep)) sel.value = keep;
  };
  fill("fFormat", "Any format", meta.formats);
  fill("fTag", "Any tag", meta.tags);
  fill("fPlaylist", "Any playlist", meta.playlists);
  $("fTag").disabled = !meta.tags.length;
  $("fPlaylist").disabled = !meta.playlists.length;
  $("genreList").innerHTML = meta.genres.map((g) => `<option value="${esc(g)}">`).join("");
  renderLetters();
  renderArtists();
  if (!results.length) renderResultsEmpty();
}

function letterOf(name) {
  let f = fold(name);
  if ($("fSkipThe").checked && f.startsWith("the ")) f = f.slice(4);
  const c = f.charAt(0).toUpperCase();
  return c >= "A" && c <= "Z" ? c : "#";
}

function renderLetters() {
  $("letters").innerHTML = LETTERS.map((l) =>
    `<button type="button" class="${l === "All" ? "all" : ""}" data-letter="${l}" aria-pressed="${l === letter}">${l}</button>`).join("");
}

function renderArtists() {
  if (!meta) return;
  const q = fold($("artistFind").value);
  let list = meta.artists;
  if (q) list = list.filter((a) => fold(a[0]).includes(q));
  else if (letter !== "All") list = list.filter((a) => letterOf(a[0]) === letter);
  const cap = 500;
  let html = "";
  if (!q && letter !== "All" && letter !== "#" && list.length) {
    const albums = list.reduce((n, a) => n + a[1], 0);
    html += `<button type="button" class="artist every" data-pattern="${letter}*" aria-pressed="${activeArtist === letter + "*"}">` +
            `<span class="name">Every artist on ${letter}</span><span class="count">${num(albums)}</span></button>`;
  }
  for (const [name, albums] of list.slice(0, cap)) {
    html += `<button type="button" class="artist" data-name="${esc(name)}" aria-pressed="${activeArtist === name}">` +
            `<span class="name">${esc(name)}</span><span class="count">${num(albums)}</span></button>`;
  }
  if (list.length > cap) html += `<p class="more">${num(list.length - cap)} more. Type a name to narrow it down.</p>`;
  if (!list.length) html = `<p class="more">No artist matches.</p>`;
  $("artistList").innerHTML = html;
}

/* searching */
function filters() {
  return {
    artist: $("fArtist").value, album: $("fAlbum").value, track_artist: $("fTrackArtist").value,
    year: $("fYear").value, genre: $("fGenre").value, format: $("fFormat").value, tag: $("fTag").value,
    playlist: $("fPlaylist").value, skip_the: $("fSkipThe").checked,
  };
}

async function search() {
  const generation = ++searchGeneration;
  const f = filters();
  const any = ["artist", "album", "track_artist", "year", "genre", "format", "tag", "playlist"].some((k) => String(f[k]).trim());
  if (!any) { notice("Pick an artist or fill in a field first."); return; }
  if (!S || S.library.state !== "ready") { notice("The library is still loading."); return; }
  $("resultInfo").textContent = "Searching";
  try {
    const r = await api("/api/select", f);
    if (generation !== searchGeneration) return;
    results = r.albums;
  } catch (e) {
    if (generation !== searchGeneration) return;
    results = [];
    $("resultInfo").textContent = "";
    notice(e.message, true);
    renderResultsEmpty(e.message);
    return;
  }
  checked = new Set(); lastClick = -1; expanded.clear();
  renderResults();
}

function renderResultsEmpty(message) {
  const loading = S && S.library.state === "loading";
  const failed = S && S.library.state === "error";
  $("results").innerHTML = `<div class="empty">` + (
    loading ? `<strong>Loading your library index</strong>The first time takes a little while. After that ibfetch keeps a copy and only fetches it again when your library changes.` :
    failed ? `<strong>The library didn't load</strong>${esc(S.library.error)} Use Refresh library to try again.` :
    message ? `<strong>Nothing to show</strong>${esc(message)}` :
    `<strong>Find the music you want</strong>Pick an artist on the left, or fill in a field above and press Search. Then add albums to the download list.`) + `</div>`;
  $("addSelBtn").disabled = $("addAllBtn").disabled = true;
}

function renderResults() {
  if (!results.length) {
    renderResultsEmpty("No albums match. Check the spelling, or use * for any text, like *floyd*.");
    $("resultInfo").textContent = "No albums";
    return;
  }
  const head = `<div class="grid-row head" role="row"><div class="c-chk"><input type="checkbox" id="allBox" aria-label="Select all albums"></div>` +
    `<div>Artist</div><div>Album</div><div class="num">Year</div><div class="num">Tracks</div><div>Format</div><div class="num">Length</div></div>`;
  const rows = results.map((a, i) => rowHtml(a, i)).join("");
  $("results").innerHTML = head + rows;
  $("results").scrollTop = 0;
  updateResultBar();
}

function rowHtml(a, i) {
  const on = checked.has(a.id), inq = queue.has(a.id);
  return `<div class="grid-row row${on ? " on" : ""}${inq ? " inq" : ""}" data-i="${i}" role="row">` +
    `<div class="c-chk"><input type="checkbox" data-i="${i}" ${on ? "checked" : ""} aria-label="Select ${esc(a.title)}"></div>` +
    `<div title="${esc(a.artist)}">${esc(a.artist)}</div>` +
    `<div class="c-album" title="${esc(a.title)}"><button type="button" class="expand" data-i="${i}" aria-expanded="${expanded.has(a.id)}" aria-label="Show the tracks of ${esc(a.title)}">&#9656;</button>${esc(a.title)}` +
    `${a.disc ? `<span class="disc">disc ${a.disc}</span>` : ""}</div>` +
    `<div class="num">${a.year || ""}</div><div class="num">${a.tracks.length}</div><div>${esc(a.formats)}</div>` +
    `<div class="num">${clock(a.length)}</div></div>`;
}

function refreshRow(i) {
  const old = document.querySelector(`.row[data-i="${i}"]`);
  if (!old) return;
  const a = results[i];
  old.className = `grid-row row${checked.has(a.id) ? " on" : ""}${queue.has(a.id) ? " inq" : ""}`;
  old.querySelector("input").checked = checked.has(a.id);
}

function updateResultBar() {
  const n = results.length;
  const tracks = results.reduce((t, a) => t + a.tracks.length, 0);
  const sel = results.filter((a) => checked.has(a.id));
  $("resultInfo").textContent = sel.length
    ? `${num(sel.length)} of ${num(n)} albums selected`
    : `${num(n)} album${n === 1 ? "" : "s"}, ${num(tracks)} tracks, ${span(results.reduce((t, a) => t + a.length, 0))}`;
  $("addSelBtn").disabled = !sel.length;
  $("addSelBtn").textContent = sel.length ? `Add ${num(sel.length)} selected` : "Add selected";
  $("addAllBtn").disabled = !n;
  $("addAllBtn").textContent = n ? `Add all ${num(n)}` : "Add all";
  const box = $("allBox");
  if (box) {
    box.checked = sel.length === n && n > 0;
    box.indeterminate = sel.length > 0 && sel.length < n;
  }
}

async function toggleTracks(i) {
  const a = results[i];
  const row = document.querySelector(`.row[data-i="${i}"]`);
  const next = row.nextElementSibling;
  if (expanded.has(a.id)) {
    expanded.delete(a.id);
    if (next && next.classList.contains("tracklist")) next.remove();
    row.querySelector(".expand").setAttribute("aria-expanded", "false");
    return;
  }
  expanded.set(a.id, true);
  row.querySelector(".expand").setAttribute("aria-expanded", "true");
  let tracks = [];
  try { tracks = (await api("/api/album", { tracks: a.tracks })).tracks; } catch (e) { notice(e.message, true); return; }
  if (!expanded.has(a.id)) return;
  const box = document.createElement("div");
  box.className = "tracklist";
  box.innerHTML = `<ol>` + tracks.map((t) =>
    `<li><span>${t.no || ""}</span><span><b>${esc(t.title)}</b>${t.artist && t.artist !== a.artist ? `<span class="ta">${esc(t.artist)}</span>` : ""}</span><span>${esc(t.format)}</span><span>${clock(t.length)}</span></li>`).join("") + `</ol>`;
  row.after(box);
}

/* the download list */
function addToQueue(albums) {
  let added = 0, tracks = 0;
  for (const a of albums) {
    const have = queue.get(a.id);
    if (have) {
      const merged = new Set([...have.tracks, ...a.tracks]);
      tracks += merged.size - have.tracks.length;
      have.tracks = [...merged];
      Object.assign(have.track_lengths, a.track_lengths);
      have.length = have.tracks.reduce((n, id) => n + (have.track_lengths[id] || 0), 0);
    } else {
      queue.set(a.id, { id: a.id, artist: a.artist, title: a.title, year: a.year, disc: a.disc,
                       tracks: [...a.tracks], track_lengths: {...a.track_lengths}, length: a.length });
      added++; tracks += a.tracks.length;
    }
  }
  checked.clear();
  results.forEach((_, i) => refreshRow(i));
  updateResultBar();
  renderQueue();
  notice(added ? `Added ${num(added)} album${added === 1 ? "" : "s"} with ${num(tracks)} tracks.` :
         tracks ? `Added ${num(tracks)} more tracks.` : "Those albums are already in the list.");
}

function renderQueue() {
  const items = [...queue.values()];
  const tracks = items.reduce((n, a) => n + a.tracks.length, 0);
  $("queueInfo").textContent = items.length
    ? `${num(items.length)} album${items.length === 1 ? "" : "s"}, ${num(tracks)} tracks, ${span(items.reduce((n, a) => n + a.length, 0))}`
    : "Empty. Add albums from the results.";
  $("clearQueueBtn").hidden = !items.length;
  const shown = items.slice(0, 400);
  $("queueList").innerHTML = shown.map((a) =>
    `<li><span><span class="q-album">${esc(a.title)}${a.disc ? ` <span class="disc">disc ${a.disc}</span>` : ""}</span>` +
    `<span class="q-artist">${esc(a.artist)}${a.year ? `, ${a.year}` : ""}</span></span>` +
    `<span class="q-n">${a.tracks.length}</span>` +
    `<button type="button" class="q-x" data-id="${a.id}" aria-label="Remove ${esc(a.title)}" title="Remove">×</button></li>`).join("") +
    (items.length > shown.length ? `<li><span class="q-artist">and ${num(items.length - shown.length)} more albums</span></li>` : "");
  updateDownloadButton();
}

function queueTracks() {
  let n = 0;
  for (const a of queue.values()) n += a.tracks.length;
  return n;
}

function updateDownloadButton() {
  const n = queueTracks();
  const running = downloadStarting || (S && S.transfer && ["ready", "planning", "running"].includes(S.transfer.state));
  const btn = $("downloadBtn");
  btn.disabled = !n || running || !S || S.library.state !== "ready";
  btn.textContent = running ? "Downloading" : n ? `Download ${num(n)} track${n === 1 ? "" : "s"}` : "Download";
}

function applySettings(s) {
  settingsApplied = true;
  $("quality").innerHTML = QUALITY.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("jobs").innerHTML = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option value="${n}">${n} file${n === 1 ? "" : "s"}</option>`).join("");
  $("outDir").value = s.out;
  $("quality").value = s.quality;
  $("jobs").value = String(s.jobs);
  $("layout").value = s.layout;
  $("workDir").value = s.work || "";
  $("workBox").open = !!s.work;
  $("source").value = s.source === "stream" ? "stream" : "download";
  updateSource();
}

/* The download service only has original files; bitrates always stream. */
function updateSource() {
  $("sourceField").hidden = $("quality").value !== "orig";
}

async function startDownload() {
  if (downloadStarting) return;
  const n = queueTracks();
  if (!n) return;
  if (n > 1000 && !confirm(`Download ${num(n)} tracks?`)) return;
  if ($("adoptFiles").checked && !confirm("Trust existing files at these paths as local baselines? This does not verify their track or bitrate with iBroadcast. Their audio will be preserved.")) return;
  downloadStarting = true;
  updateDownloadButton();
  try {
    await api("/api/download", {
      items: [...queue.values()].map((a) => ({ album_id: a.id, tracks: a.tracks })),
      out: $("outDir").value, quality: $("quality").value, jobs: Number($("jobs").value), layout: $("layout").value,
      verify: $("verifyFiles").checked, adopt_existing: $("adoptFiles").checked, work: $("workDir").value,
      source: $("source").value,
    });
  } catch (e) { notice(e.message, true); return; }
  finally { downloadStarting = false; updateDownloadButton(); }
  queue.clear();
  renderQueue();
  results.forEach((_, i) => refreshRow(i));
  poll();
}

/* the transfer window */
function renderTransfer() {
  const t = S.transfer;
  $("transfer").hidden = !t;
  if (!t) return;
  const running = ["ready", "planning", "running"].includes(t.state);
  const w = String(t.total).length;
  $("counter").innerHTML = `${String(t.done).padStart(w, "0")}<small> / ${t.total}</small>`;
  const pct = t.total ? Math.round(t.done / t.total * 1000) / 10 : 0;
  $("meterFill").style.width = `${pct}%`;
  $("meter").setAttribute("aria-valuenow", String(pct));
  $("lamp").className = `lamp${running ? " on" : t.state === "done" ? " done" : ""}`;
  $("tState").textContent = t.state === "planning" || t.state === "ready" ? "Preparing download" : running ? "Downloading" : t.state === "done" ? "Finished" : "Stopped";
  $("tStats").textContent = running
    ? `${size(t.bytes)}${t.speed ? ` at ${size(t.speed)}/s` : ""}${t.eta ? `, about ${span(t.eta)} left` : ""}`
    : t.state === "done" ? `Done in ${span(t.elapsed)}, ${size(t.bytes)} saved` : `Stopped after ${span(t.elapsed)}. Try again to carry on.`;
  $("tTally").innerHTML = `<span><b>${num(t.ok)}</b> saved</span><span><b>${num(t.skip)}</b> skipped</span><span><b>${num(t.fail)}</b> failed</span>`;
  $("stopBtn").hidden = !running;
  $("retryBtn").hidden = running || !(t.fail || t.state === "stopped") || !S.can_retry;
  $("retryBtn").textContent = t.state === "stopped" ? "Carry on" : "Retry failed";
  $("tActive").innerHTML = running ? t.active.map((a) =>
    `<li><span class="nm">${esc(base(a.name))}</span><div class="mini${a.expected ? "" : " busy"}"><i style="width:${a.expected ? Math.min(100, a.received / a.expected * 100) : 0}%"></i></div></li>`).join("") : "";
  const label = { ok: "Saved", skip: "Skipped", fail: "Failed" };
  $("tRecent").innerHTML = t.recent.slice(0, running ? 6 : 4).map((r) =>
    `<li title="${esc(r.name)}${r.note ? `: ${esc(r.note)}` : ""}"><span class="st ${r.state === "fail" ? "fail" : ""}">${label[r.state]}</span>${esc(base(r.name))}</li>`).join("");
  const fails = $("tFails");
  fails.hidden = !t.failures.length;
  if (t.failures.length) {
    fails.querySelector("summary").textContent = `${num(t.fail)} failed, show which`;
    fails.querySelector("ul").innerHTML = t.failures.map((f) => `<li>${esc(f.name)}: ${esc(f.note)}</li>`).join("");
  }
  const existing = $("tExisting"), notes = t.existing || [];
  existing.hidden = !notes.length;
  if (notes.length) {
    existing.querySelector("summary").textContent = `${num(t.existing_count)} existing file${t.existing_count === 1 ? "" : "s"}: details`;
    existing.querySelector("ul").innerHTML = notes.map(f => `<li>${esc(f.name)}: ${esc(f.note)}</li>`).join("");
  }
  const remarks = $("tNotes"), saved = t.notes || [];
  remarks.hidden = !saved.length;
  if (saved.length) {
    remarks.querySelector("summary").textContent = `${num(t.notes_count)} saved with a note: details`;
    remarks.querySelector("ul").innerHTML = saved.map(f => `<li>${esc(f.name)}: ${esc(f.note)}</li>`).join("");
  }
}

/* events */
$("updateBtn").addEventListener("click", () => setUpdatePanel($("updatePanel").hidden));
$("checkUpdates").addEventListener("click", checkUpdates);
$("autoUpdates").addEventListener("change", saveUpdatePreference);
$("themeBtn").addEventListener("click", () => setThemeMenu($("themeMenu").hidden));
$("appearanceChoice").addEventListener("change", changeTheme);
$("paletteChoice").addEventListener("change", changeTheme);
document.addEventListener("click", e => {
  if (!e.target.closest(".appearance")) setThemeMenu(false);
  if (!e.target.closest(".update-wrap")) setUpdatePanel(false);
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("themeMenu").hidden) { e.preventDefault(); setThemeMenu(false, true); }
  if (e.key === "Escape" && !$("updatePanel").hidden) { e.preventDefault(); setUpdatePanel(false, true); }
});
if (themeMedia.addEventListener) themeMedia.addEventListener("change", showTheme);
else themeMedia.addListener(showTheme);
showTheme();
if (typeof window !== "undefined") window.addEventListener("beforeunload", e => {
  const busy = S && S.transfer && ["ready", "planning", "running"].includes(S.transfer.state);
  if (!stopped && (queue.size || downloadStarting || busy)) {
    e.preventDefault(); e.returnValue = "";
  }
});
$("signinBtn").addEventListener("click", beginSignIn);
$("signinOpen").addEventListener("click", reopenSignIn);
$("signinCancel").addEventListener("click", cancelSignIn);
$("signoutBtn").addEventListener("click", async () => {
  if (!confirm("Sign out? ibfetch forgets your sign-in and its copy of the library index.")) return;
  try {
    await api("/api/logout", {});
    queue.clear(); results = []; meta = null; metaVersion = 0;
    renderQueue(); poll();
  } catch (e) { notice(e.message, true); }
});
$("refreshBtn").addEventListener("click", async () => {
  try { await api("/api/library", { refresh: true }); notice("Fetching the library index again."); poll(); } catch (e) { notice(e.message, true); }
});
$("quitBtn").addEventListener("click", async () => {
  const running = S && S.transfer && ["ready", "planning", "running"].includes(S.transfer.state);
  if (running && !confirm("A download is running. Stop it and quit? You can carry on later.")) return;
  await api("/api/quit", {}).catch(() => {});
  stopped = true;
  showBye();
});

$("letters").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-letter]");
  if (!b) return;
  letter = b.dataset.letter;
  $("artistFind").value = "";
  renderLetters();
  renderArtists();
  $("artistList").scrollTop = 0;
});
$("artistFind").addEventListener("input", renderArtists);
$("artistList").addEventListener("click", (e) => {
  const b = e.target.closest("button.artist");
  if (!b) return;
  activeArtist = b.dataset.pattern || b.dataset.name;
  $("fArtist").value = activeArtist;
  renderArtists();
  search();
});
$("fSkipThe").addEventListener("change", renderArtists);
$("filters").addEventListener("submit", (e) => { e.preventDefault(); activeArtist = $("fArtist").value; renderArtists(); search(); });
$("clearBtn").addEventListener("click", () => {
  searchGeneration++;
  for (const id of ["fArtist", "fAlbum", "fTrackArtist", "fYear", "fGenre"]) $(id).value = "";
  for (const id of ["fFormat", "fTag", "fPlaylist"]) $(id).value = "";
  activeArtist = null;
  results = []; checked.clear();
  renderArtists();
  renderResultsEmpty();
  $("resultInfo").textContent = "";
});

$("results").addEventListener("click", (e) => {
  if (e.target.id === "allBox") {
    const on = e.target.checked;
    checked = on ? new Set(results.map((a) => a.id)) : new Set();
    results.forEach((_, i) => refreshRow(i));
    updateResultBar();
    return;
  }
  const exp = e.target.closest(".expand");
  if (exp) { toggleTracks(Number(exp.dataset.i)); return; }
  const row = e.target.closest(".row[data-i]");
  if (!row) return;
  const i = Number(row.dataset.i);
  const box = row.querySelector("input");
  const on = e.target === box ? box.checked : !checked.has(results[i].id);
  const [from, to] = e.shiftKey && lastClick >= 0 ? [Math.min(lastClick, i), Math.max(lastClick, i)] : [i, i];
  for (let j = from; j <= to; j++) {
    if (on) checked.add(results[j].id); else checked.delete(results[j].id);
    refreshRow(j);
  }
  lastClick = i;
  updateResultBar();
});
$("addSelBtn").addEventListener("click", () => addToQueue(results.filter((a) => checked.has(a.id))));
$("addAllBtn").addEventListener("click", () => addToQueue(results));
$("queueList").addEventListener("click", (e) => {
  const b = e.target.closest(".q-x");
  if (!b) return;
  queue.delete(Number(b.dataset.id));
  renderQueue();
  const i = results.findIndex((a) => a.id === Number(b.dataset.id));
  if (i >= 0) refreshRow(i);
});
$("clearQueueBtn").addEventListener("click", () => {
  queue.clear();
  renderQueue();
  results.forEach((_, i) => refreshRow(i));
});
async function chooseFolderInto(input, button) {
  notice("A folder window is open. It can sit behind your browser.");
  $(button).disabled = true;
  try {
    const r = await api("/api/choose-folder", { start: $(input).value || $("outDir").value });
    if (!r.available) notice("This system has no folder picker. Type the folder path instead.");
    else if (r.path) { $(input).value = r.path; $("notice").hidden = true; }
    else $("notice").hidden = true;
  } catch (e) { notice(e.message, true); }
  $(button).disabled = false;
}
$("chooseBtn").addEventListener("click", () => chooseFolderInto("outDir", "chooseBtn"));
$("chooseWorkBtn").addEventListener("click", () => chooseFolderInto("workDir", "chooseWorkBtn"));
$("quality").addEventListener("change", updateSource);
$("downloadBtn").addEventListener("click", startDownload);
$("stopBtn").addEventListener("click", async () => { await api("/api/cancel", {}).catch((e) => notice(e.message, true)); poll(); });
$("openBtn").addEventListener("click", async () => {
  try { await api("/api/open-folder", { path: S.transfer ? S.transfer.out : $("outDir").value }); } catch (e) { notice(e.message, true); }
});
$("retryBtn").addEventListener("click", async () => {
  try { await api("/api/retry", {}); poll(); } catch (e) { notice(e.message, true); }
});

renderQueue();
loop();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
