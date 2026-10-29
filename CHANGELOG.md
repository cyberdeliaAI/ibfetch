# Changelog

## 1.3.0

- First public release under the MIT license, with macOS Apple Silicon/Intel,
  Windows x64, Linux x64 and Python/browser downloads.
- Original files come from iBroadcast's download service by default, with the tags you
  edited in iBroadcast (per the account's Download Updated Metadata setting). In testing
  this was about three times faster than streaming.
- One ZIP per track, unpacked while it arrives. The end marker, ZIP sizes and CRC-32 are
  checked before a file is saved; a failed check downloads the track again.
- After three attempts a track falls back to streaming and is listed with a note.
- `--source stream` and "Streaming: exactly as uploaded" keep byte-for-byte uploads with resume.
  MP3 bitrates always stream.
- Finished files are recognised whichever source saved them.
- Tracks show a moving bar while the download service prepares them.
- Keep streaming-fallback notes and actual download provenance through work-folder
  retries, interrupted completion, verification and later skips.
- Accept equivalent AIFF, WAV, OGG and MP4/M4A filename extensions from the download
  service; different containers still trigger the safe fallback.
- Recover interrupted replacements even when an older baseline remains, using the
  completed receipt's identity, length and SHA-256 without overwriting existing audio.

## 1.2.1

- Prepare publication under the MIT license, with a clean history.
- Prepare standalone macOS Apple Silicon/Intel, Windows x64 and Linux x64 downloads,
  plus Python/browser and CLI instructions and a portable Python ZIP.
- Prepare a Homebrew desktop cask and Python/browser formula for cyberdeliaAI/apps.
- Include runtime licenses, download checksums and screenshots with synthetic data.
- Document that updated embedded metadata export is not supported by the current streaming downloader.

## 1.2.0

- One hidden .ibfetch.json per music folder for completed/partial download records.
- Safely consolidate recognized older per-track records when a folder is used.
- Preserve verification, partial resume, work-folder recovery and local modifications.
- Local OS locks protect parallel writers; NAS output and a local work folder are supported.

## Earlier development

- Asynchronous download planning, stable collision names and cached path plans.
- Standalone Electron interface alongside the Python/browser and command line.
- Forest, Ocean, Warm, iBroadcast-inspired and Amber palettes with saved light/dark appearance.
- Optional checksum verification and explicit adoption of trusted existing files.
