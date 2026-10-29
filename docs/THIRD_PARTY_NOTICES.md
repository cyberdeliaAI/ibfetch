# Third-party notices

ibfetch's own code and documentation are MIT licensed; see the root LICENSE.
The standalone desktop downloads include other software under its own terms.

- **Python / standard library:** Python Software Foundation license and retained
  historical terms. Copies for the bundled Python 3.13/3.14 build versions are in
  licenses/Python-3.13.txt and licenses/Python-3.14.txt. Python itself is not modified;
  PyInstaller packages the interpreter and selected modules with ibfetch.
- **PyInstaller:** GPL-2.0-or-later with its Bootloader Exception; runtime hooks have
  their separately stated terms. The installed build version's full COPYING.txt
  and hooks-contrib license are included in desktop resources/licenses.
- **Electron:** MIT. Its license and Chromium/Node dependency notices
  (LICENSES.chromium.html) are copied from the installed Electron distribution
  into resources/licenses for each native build.
- **certifi / Mozilla CA certificates:** MPL-2.0. Only the trusted root certificate
  data is bundled; the normal Python/browser version still needs no third-party packages.
  The installed certifi license is copied into desktop resources/licenses/certifi.txt.
- **OpenSSL, libffi, zlib and Expat:** license copies are retained in licenses/ and
  desktop resources/licenses for runtime libraries that a native build may bundle.
  Platform system libraries retain their respective licenses.

The Mac location is ibfetch.app/Contents/Resources; Windows/Linux use resources
inside the extracted app directory. Electron and Python are not part of the
standalone Python source ZIP; that version uses your installed interpreter.

Upstream sources: [Python](https://github.com/python/cpython/blob/3.13/LICENSE),
[PyInstaller](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt),
[Electron](https://github.com/electron/electron/blob/v44.9.0/LICENSE),
[OpenSSL](https://github.com/openssl/openssl/blob/openssl-3.0/LICENSE.txt),
[libffi](https://github.com/libffi/libffi/blob/master/LICENSE),
[zlib](https://github.com/madler/zlib/blob/master/LICENSE), and
[Expat](https://github.com/libexpat/libexpat/blob/master/expat/COPYING).
