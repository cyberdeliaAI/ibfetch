# Desktop installation and builds

## Choose a download

Get a complete archive from [Releases](https://github.com/cyberdeliaAI/ibfetch/releases/latest):

| Platform | Architecture | Archive suffix |
| --- | --- | --- |
| macOS 13+ | Apple Silicon | macos-arm64.zip |
| macOS 13+ | Intel | macos-x64.zip |
| Windows 10/11 | x64 | windows-x64.zip |
| Linux desktop | x64, tested on Ubuntu 22.04 | linux-x64.tar.gz |

Extract the whole archive and keep its contents together. Open ibfetch.app on
macOS, ibfetch.exe on Windows, or ibfetch/ibfetch on Linux. Python is bundled;
Node.js/npm are only needed to build the app. For other architectures or a
terminal-only system, use the Python/browser or CLI version in the README.

Sign-in approval opens in your system browser. Settings use ibfetch's normal
platform directory; the Electron profile is separate. Closing the app stops its
owned backend. Saved music and sign-in settings survive an upgrade/uninstall.

## First launch

### macOS

The app is ad-hoc signed, without Apple Developer ID signing or notarization.
After attempting to open a trusted download, open **System Settings > Privacy &
Security**, find ibfetch's notice and choose **Open Anyway**. Confirm this app in
the next prompt. Keep the overall system security settings enabled. Follow
[Apple's instructions](https://support.apple.com/en-gb/102445).

For Homebrew, use `brew install --cask cyberdeliaAI/apps/ibfetch`. This installs
ibfetch.app and links the bundled `ibfetch-cli`. Stop an older copy and move a
manually installed app aside before Homebrew installation; do not force-overwrite
it. The formula alternative (`brew install --formula cyberdeliaAI/apps/ibfetch`)
provides the Python/browser and terminal command `ibfetch`.

### Windows

Right-click the ZIP and use **Extract All**, then open ibfetch.exe in the
extracted folder. The app is not Authenticode signed. Review the publisher/source
and checksums if Windows shows an unrecognized-publisher warning. A managed
computer may prohibit unsigned apps. Do not disable system-wide protections.

### Linux

Extract and run from a graphical desktop:

```sh
tar -xzf ibfetch-1.3.1-linux-x64.tar.gz
./ibfetch/ibfetch
```

The Linux build is tested on Ubuntu 22.04 with a virtual display. Other desktop
distributions need compatible glibc and Chromium desktop libraries. For a
headless machine, use Python/CLI; do not disable Electron's sandbox.

## Verify a download

Each release archive has a .sha256 companion. Keep both in the same folder.

macOS:

```sh
shasum -a 256 -c ibfetch-1.3.1-macos-arm64.zip.sha256
```

Linux:

```sh
sha256sum -c ibfetch-1.3.1-linux-x64.tar.gz.sha256
```

Windows PowerShell:

```powershell
Get-FileHash .\ibfetch-1.3.1-windows-x64.zip -Algorithm SHA256
Get-Content .\ibfetch-1.3.1-windows-x64.zip.sha256
```

Compare the 64-character digest exactly. A checksum checks download integrity;
it does not replace publisher signing.

## Bundled command line

The same frozen backend also accepts CLI commands. The Mac cask links it as
`ibfetch-cli`. From an extracted app you can run:

```sh
# macOS
./ibfetch.app/Contents/Resources/backend/ibfetch-server --version
# Linux
./ibfetch/resources/backend/ibfetch-server --version
```

On Windows, run `.\resources\backend\ibfetch-server.exe --version` in PowerShell
from the extracted folder. `gui --no-browser` starts the browser server and prints
its private local link; `get --help` lists download options.

## Build from source

Use Python 3.13 or 3.14 and Node.js 24/npm on the target operating system:

```sh
python -m venv .build-venv
# macOS/Linux: source .build-venv/bin/activate
# Windows PowerShell: .build-venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python tools/build_desktop.py
```

Linux needs a display; CI uses `xvfb-run -a python tools/build_desktop.py`.
The script builds Python with PyInstaller, installs locked Electron dependencies,
packages the app, Mozilla CA certificates and runtime notices, validates its trusted
HTTPS roots, tests its isolated renderer, archives it,
checks the extracted archive, and writes its SHA-256 companion. macOS also checks
the ad-hoc signature. Pillow draws the original icon at build time only.

The Desktop builds workflow makes all four native archives. Releases use verified
archives from a successful run; Python/browser ZIPs contain the standalone file,
MIT license and documentation. Build logs and automated tests use temporary settings
and synthetic data, never a real music account. Local HTTPS tests verify trusted
connections and rejection of incorrect hostnames/untrusted certificates. The desktop
interpreter uses its bundled roots unless SSL_CERT_FILE or SSL_CERT_DIR is explicitly
configured; it does not depend on the build machine's Python certificate paths.

## Runtime security and ownership

- One Electron instance owns a fresh random-port loopback backend; unrelated
  browser servers are never reused or killed.
- The renderer has sandbox, context isolation and web security enabled, with Node
  integration, webviews and arbitrary navigation disabled. There is no preload/IPC
  bridge. Only its own backend and external iBroadcast approval are allowed.
- The shared page uses hashed inline scripts, a private per-run URL, authenticated
  API calls and no-store/no-referrer headers. Credentials stay in Python.
- Close warns for an unsent queue or download, then requests graceful shutdown.
  If a read remains stuck after five seconds, only the owned child is terminated.
  Matching partial downloads resume later. This is not a power-loss guarantee.
- Build fuses disable RunAsNode and Node environment/inspect injection. Native
  tests keep the renderer sandbox enabled.

See [Electron's security guidance](https://www.electronjs.org/docs/latest/tutorial/security).
