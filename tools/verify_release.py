"""Validate a complete release and bind both notarization reports to the actual archives."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re

TARGETS = {"macos-arm64": ".zip", "macos-x64": ".zip", "windows-x64": ".zip",
           "linux-x64": ".tar.gz", "python": ".zip"}


def verify_archives(root, version):
    if not re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version):
        raise ValueError("Expected a stable numeric version")
    checks = {}
    for target, suffix in TARGETS.items():
        name = "ibfetch-" + version + "-" + target + suffix
        archive = root / name
        if archive.is_symlink() or not archive.is_file() or not archive.stat().st_size:
            raise ValueError("Missing release archive: " + name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if (archive.with_name(name + ".sha256")).read_text().split() != [digest, name]:
            raise ValueError("Archive checksum does not match: " + name)
        if target.startswith("macos-"):
            report = json.loads((root / "proofs" / (target + ".json")).read_text())
            if (not isinstance(report, dict) or any(report.get(key) is not True for key in
                    ("developer_id", "hardened_runtime", "notarization_ticket", "gatekeeper"))
                    or report.get("archive") != name or report.get("sha256") != digest):
                raise ValueError("Missing or mismatched Apple verification: " + name)
        checks[name] = digest
    return checks



def feed_required(version):
    return tuple(int(part) for part in version.split('.')) >= (1, 7, 0)


def mac_update_feed(root, version):
    files = []
    for arch in ('arm64', 'x64'):
        name = 'ibfetch-' + version + '-macos-' + arch + '.zip'
        path = root / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2 * 1024**3:
            raise ValueError('Invalid native update package: ' + name)
        digest = hashlib.sha512()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        files.append({'url': name, 'sha512': base64.b64encode(digest.digest()).decode('ascii'), 'size': path.stat().st_size})
    return {'version': version, 'files': files, 'path': files[0]['url'], 'sha512': files[0]['sha512']}


def prepare_update_feed(root, version):
    # Validate the whole archive set and both hash-bound Apple proofs FIRST.
    verify_archives(root, version)
    if not feed_required(version):
        raise ValueError('Do not add updater metadata to historical releases.')
    path = root / 'latest-mac.yml'
    sidecar = root / 'latest-mac.yml.sha256'
    if path.is_symlink() or sidecar.is_symlink():
        raise ValueError('Update metadata must be regular files.')
    content = json.dumps(mac_update_feed(root, version), indent=2) + '\n'
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest() + '  latest-mac.yml\n'
    for target, value in ((path, content), (sidecar, checksum)):
        if target.exists() and target.read_text('utf-8') != value:
            raise ValueError('Existing update metadata differs; use a new staging directory.')
    path.write_text(content, encoding='utf-8')
    sidecar.write_text(checksum, encoding='ascii')


def verify_release(root, version):
    checks = verify_archives(root, version)
    if feed_required(version):
        path = root / 'latest-mac.yml'
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
            raise ValueError('Missing or invalid native update metadata.')
        if json.loads(path.read_text('utf-8')) != mac_update_feed(root, version):
            raise ValueError('Update metadata does not match verified Mac archives.')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar = root / 'latest-mac.yml.sha256'
        if sidecar.is_symlink() or sidecar.read_text().split() != [digest, path.name]:
            raise ValueError('Update metadata checksum does not match.')
        checks[path.name] = digest
    return checks


def write_checksums(root, checks):
    content = "".join(value + "  " + name + "\n" for name, value in sorted(checks.items()))
    path = root / "SHA256SUMS"
    if path.is_symlink() or path.exists() and path.read_text() != content:
        raise ValueError("Existing release checksums differ; use a new staging directory")
    path.write_text(content, encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("version")
    parser.add_argument('--prepare-update-feed', action='store_true', help='Create the Mac feed only after all archive/Apple proofs pass')
    args = parser.parse_args()
    try:
        if args.prepare_update_feed:
            prepare_update_feed(args.directory, args.version)
        checks = verify_release(args.directory, args.version)
        write_checksums(args.directory, checks)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("Verified all five archives, both matching Apple proofs and required native update metadata.")


if __name__ == "__main__":
    main()
