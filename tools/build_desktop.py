"""Build a standalone app with bundled Python, then smoke-test it."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_app(executable, version):
    resources = (executable.parent.parent / "Resources" if sys.platform == "darwin"
                 else executable.parent / "resources")
    for relative in ("LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/Electron.txt",
                     "licenses/LICENSES.chromium.html", "licenses/PyInstaller.txt", "licenses/Python-3.13.txt",
                     "licenses/certifi.txt"):
        if not (resources / relative).is_file():
            raise RuntimeError("Missing bundled license: " + relative)
    bundle = resources / "backend/_internal/certificates/cacert.pem"
    if not ssl.create_default_context(cafile=str(bundle)).get_ca_certs():
        raise RuntimeError("The bundled backend has no trusted HTTPS roots")
    with tempfile.TemporaryDirectory(prefix="ibfetch-desktop-check-") as tmp:
        home = Path(tmp)
        report = home / "report.json"
        settings = home / "settings"
        settings.mkdir()
        (settings / "app-settings.json").write_text(json.dumps({"appearance":"dark", "palette":"ocean", "auto_updates":False}), encoding="utf-8")
        env = dict(os.environ, IBFETCH_HOME=str(home / "settings"),
                   IBFETCH_ELECTRON_PROFILE=str(home / "profile"), IBFETCH_SMOKE_REPORT=str(report))
        subprocess.run([str(executable)], env=env, check=True, timeout=90)
        result = json.loads(report.read_text("utf-8"))
        if (not result.get("ok") or result.get("version") != version or not result.get("isolated")
                or result.get("palette") != "ocean" or result.get("theme") != "dark"):
            raise RuntimeError("Desktop smoke check failed")
        print("Standalone window and isolated renderer verified.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()
    version = json.loads((ROOT / "electron/package.json").read_text())["version"]
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    node = shutil.which("node")
    if not npm or not node:
        parser.error("Node.js and npm are required only for desktop builds")
    from PIL import Image
    import certifi
    assets = ROOT / "build/assets"
    assets.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ROOT / "assets/ibfetch-icon.png").convert("RGBA")
    icon.save(assets / "ibfetch.png")
    icon.save(assets / "ibfetch.icns")
    icon.save(assets / "ibfetch.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
                    "--add-data", certifi.where() + os.pathsep + "certificates",
                    "--runtime-hook", str(ROOT / "tools/desktop_certificates.py"),
                    "--name", "ibfetch-server", "--distpath", str(ROOT / "build/backend"),
                    "--workpath", str(ROOT / "build/pyinstaller"), "--specpath", str(ROOT / "build"),
                    str(ROOT / "ibfetch.py")], check=True, cwd=ROOT)
    if not args.skip_install:
        subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], check=True,
                       cwd=ROOT / "electron")
    subprocess.run([node, "node_modules/electron/install.js"], check=True, cwd=ROOT / "electron")
    licenses = ROOT / "build/licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "licenses").glob("*.txt"):
        shutil.copy2(source, licenses / source.name)
    for package, target in (("pyinstaller", "PyInstaller.txt"), ("pyinstaller-hooks-contrib", "PyInstaller-hooks.txt"),
                            ("certifi", "certifi.txt")):
        distribution = metadata.distribution(package)
        source = next(f for f in distribution.files if "/licenses/" in str(f) and Path(f).name in ("COPYING.txt", "LICENSE"))
        shutil.copy2(distribution.locate_file(source), licenses / target)
    for name in ("LICENSE", "LICENSES.chromium.html"):
        shutil.copy2(ROOT / "electron/node_modules/electron/dist" / name,
                     licenses / ("Electron.txt" if name == "LICENSE" else name))
    subprocess.run([npm, "run", "pack"], check=True, cwd=ROOT / "electron",
                   env=dict(os.environ, CSC_IDENTITY_AUTO_DISCOVERY="false"))
    output = ROOT / "electron/out"
    if sys.platform == "darwin":
        directory = next(output.glob("mac*/ibfetch.app"))
        executable = directory / "Contents/MacOS/ibfetch"
    else:
        directory = output / ("win-unpacked" if os.name == "nt" else "linux-unpacked")
        executable = directory / ("ibfetch.exe" if os.name == "nt" else "ibfetch")
    check_app(executable, version)
    packages = ROOT / "dist"
    packages.mkdir(exist_ok=True)
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    name = f"ibfetch-{version}-{system}-{arch}"
    if sys.platform == "darwin":
        archive = packages / (name + ".zip")
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(directory), str(archive)], check=True)
    elif os.name == "nt":
        archive = Path(shutil.make_archive(str(packages / name), "zip", directory))
    else:
        archive = packages / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as packed:
            packed.add(directory, arcname="ibfetch")
    with tempfile.TemporaryDirectory(prefix="ibfetch-extracted-") as tmp:
        destination = Path(tmp)
        if sys.platform == "darwin":
            subprocess.run(["ditto", "-x", "-k", str(archive), str(destination)], check=True)
            extracted = destination / "ibfetch.app/Contents/MacOS/ibfetch"
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(destination / "ibfetch.app")], check=True)
        elif os.name == "nt":
            shutil.unpack_archive(str(archive), str(destination))
            extracted = destination / "ibfetch.exe"
        else:
            shutil.unpack_archive(str(archive), str(destination), filter="data")
            extracted = destination / "ibfetch/ibfetch"
        check_app(extracted, version)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Verified standalone archive: {archive}")


if __name__ == "__main__":
    main()
