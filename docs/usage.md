# Using ibfetch 1.8.1

The project builds one Rust application. The native window uses egui. The same
binary also provides the terminal commands and serves the existing browser UI
from a loopback-only Rust server. Python, Node.js and Electron are not runtime
requirements.

Build with stable Rust 1.95 or newer and your operating system's build tools:

```sh
cargo run --locked
cargo run --locked -- gui
cargo run --locked -- get --help
cargo run --locked -- get --artist "Björk" --dry-run
cargo test --locked --all-targets
```

The native window and the browser page offer the same controls: the five colour
themes in light and dark, track lists under each album, and a transfer window with
the download speed over the last seconds and the time left. The window uses the
system interface font where it finds one (San Francisco on macOS, Segoe UI on
Windows) and egui's bundled font elsewhere.

With `--no-default-features`, builds omit the graphical toolkit and native folder
picker. The terminal and the loopback browser app still work. On Linux, the
desktop build uses your system's X11 or Wayland, OpenGL, D-Bus and TLS libraries.
On macOS, run `scripts/make-rust-macos-app.sh` for a locally signed standalone
app at `target/release/ibfetch.app`.

## Set up sign-in and a profile

New sign-ins in 1.8.1 use ibfetch's own iBroadcast registration.
Version 1.8.0 shipped with the registration shared with Tagcast. Existing
sessions keep using the client ID saved at sign-in. To switch an existing
session to the ibfetch registration, sign out, then sign in with 1.8.1 or later.
A custom build can override it with `IBROADCAST_CLIENT_ID` at build time or
runtime. The CLI also accepts `ibfetch login --client-id YOUR_ID`.

By default, the app uses the existing `ibfetch` folder under
`~/Library/Application Support` on macOS, `%APPDATA%` on Windows, and
`$XDG_CONFIG_HOME` or `~/.config` on Linux. Use `--home` or `IBFETCH_HOME`
to select another profile. The earlier standalone Rust build's
`IBFETCH_RUST_HOME` override also works when `IBFETCH_HOME` is unset.
To try 1.8.0 in isolation, use a disposable profile, sign in and choose a
separate output folder. To reuse an early Rust test profile, point `--home`
at its existing directory.

Stop older writers before using the same output folder. This also applies to
two profiles that use different local lock directories.

The Rust engine reads the existing account, settings and download records. It
checks the account on a library load and keeps its own desktop window state. A
completed record identifies an account, track and quality. A partial also needs
the original stream path, route, size and the stored validators. The work folder
uses the existing local lock and manifest conventions. Audio is published with an
exclusive rename where supported, or with an atomic hard link on other Unix
filesystems. A server share that supports neither operation fails safely. Validate
this on supported macOS, Windows and Linux mounts before using it for a large run.

Desktop sign-in opens the official iBroadcast page in the system browser. The
app shows the device code and waits for approval. Use **Switch User** on the
iBroadcast page when changing the browser's remembered account. The native
version shows release notes and links to downloads. Install new versions
manually. The update check reads stable releases from the official ibfetch repository.
Get the platform downloads and checksums from the [1.8.0 release](https://github.com/cyberdeliaAI/ibfetch/releases/tag/v1.8.0).

The fixture suite checks path selection, both download routes, existing data,
work-folder moves, browser authorization and account isolation with disposable
folders and loopback HTTP. When moving an existing library on a network share,
try a small selection first and check its filenames and manifest before starting a large batch.
