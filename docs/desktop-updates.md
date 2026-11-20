# Desktop updates

## User flow

The header shows the running version and **Update available** when a newer stable
release is found. Open it for release notes, **Later**, a fixed official release
link and the installation controls available for your copy. Choose **Update
settings** to reach manual checks, the automatic-check opt-out and project links
even while a newer release is available. Supported Mac copies
also offer **Skip this version**; manual checks can still reveal a skipped version. Notes are rendered as a
bounded Markdown subset; raw HTML and images never execute or load.

The existing anonymous daily release check and its opt-out stay available in all
versions. Release checks do not send iBroadcast account information. On supported
Mac copies, **Download update** downloads and verifies the package, then **Restart
and install** asks for explicit native confirmation. An optional automatic-download
checkbox is off by default. Manual release checks do not implicitly start an
automatic download. **Later**, closing the dialog and ordinary Quit do not install.

Finish or stop active downloads and empty the pending download list before
installation. A pending sign-in also blocks installation. The app checks again
after confirmation, closes its own backend and waits for a clean exit before
handing off to the native updater. A forced shutdown aborts installation. Existing
music, saved sign-in and app preferences are retained; unsent download lists are
not persisted across restarts.

## Platform routes

| Copy | Upgrade route |
| --- | --- |
| Official Developer ID signed/notarized Mac ZIP, arm64 or x64 | Verified full ZIP download and confirmed native restart/install |
| Homebrew Mac cask | `brew upgrade --cask cyberdeliaAI/apps/ibfetch` |
| Homebrew Python formula | `brew upgrade --formula cyberdeliaAI/apps/ibfetch` |
| Windows portable ZIP | Official release link; close and replace the extracted folder |
| Linux portable tar.gz | Official release link; close and replace the extracted folder |
| Python/browser/CLI | Official Python ZIP or source checkout; stop and replace/update the files |
| Source/ad-hoc desktop builds | Manual replacement; native installation disabled |

The 1.7.0 client is the first ibfetch native-updater client. A 1.6.0 Mac installation
needs a manual upgrade once. Adding updater metadata does not enable native
installation in an older client. Homebrew is detected conservatively when an
ibfetch cask exists in a standard Homebrew Caskroom; that keeps a separately opened
copy on the manual route too. Current Windows/Linux artifacts remain portable;
there is no NSIS/DEB installer in this release.

## Package and release contract

Runtime desktop dependency: pinned `electron-updater` 6.8.12. The Python/browser
runtime still needs only the standard library. Runtime npm notices are bundled
with the desktop archive and listed in its `licenses/npm/packages.json`.

The isolated updater session strips cookies, authorization and staging identifiers
and permits HTTPS only. Native installation accepts numeric stable versions newer
than the current version, fixed GitHub release directories and the exact Mac ZIP
filenames. Each file needs a valid bounded size and SHA-512 checksum. Credentials,
iBroadcast tokens and arbitrary URLs/paths/commands never cross the update bridge.
Native macOS code-signature verification remains enabled.

The release staging command is:

```sh
python tools/verify_release.py RELEASE_DIRECTORY 1.7.0 --prepare-update-feed
```

Place the archive-bound Apple reports under `RELEASE_DIRECTORY/proofs/` as
`macos-arm64.json` and `macos-x64.json`. It validates all five archives, their SHA-256 sidecars and both archive-bound Apple
signing/notarization proofs before writing `latest-mac.yml` and its SHA-256 sidecar.
The feed contains both verified Mac ZIPs, their byte lengths and SHA-512 digests.
`SHA256SUMS` covers the five archives and the feed. Upload and verify this complete
set before publishing a new release. Never add newly generated metadata to an
older published release or replace immutable historical assets.

## Verification limits

Unit regressions cover metadata rejection, package checksum failures, preferences,
manual-check behavior, bridge ownership, deferred installation and clean shutdown.
An isolated native fixture exercises release-note controls through the real preload,
IPC and pointer input in light/dark and narrow/wide windows, with mock packages;
it never requests provider credentials or executes an installer. Every native
build runs this fixture before and after extraction, plus the mock sign-in checks.

These checks are not proof of an actual installed signed version being replaced by
another signed version. That requires a separate two-version upgrade validation
on a disposable installation when a subsequent signed release exists. Do not
claim that scenario has been verified based on mock UI or checksum tests alone.
