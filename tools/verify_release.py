"""Validate a complete release and bind both notarization reports to the actual archives."""
import argparse
import hashlib
import json
from pathlib import Path
import re

TARGETS = {"macos-arm64": ".zip", "macos-x64": ".zip", "windows-x64": ".zip",
           "linux-x64": ".tar.gz", "python": ".zip"}


def verify_release(root, version):
    if not re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version):
        raise ValueError("Expected a stable numeric version")
    checks = {}
    for target, suffix in TARGETS.items():
        name = "ibfetch-" + version + "-" + target + suffix
        archive = root / name
        if not archive.is_file() or not archive.stat().st_size:
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


def write_checksums(root, checks):
    content = "".join(value + "  " + name + "\n" for name, value in sorted(checks.items()))
    path = root / "SHA256SUMS"
    if path.exists() and path.read_text() != content:
        raise ValueError("Existing release checksums differ; use a new staging directory")
    path.write_text(content, encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        checks = verify_release(args.directory, args.version)
        write_checksums(args.directory, checks)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("Verified all five archives and matching Apple verification for both Mac downloads.")


if __name__ == "__main__":
    main()
