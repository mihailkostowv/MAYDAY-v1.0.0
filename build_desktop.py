from pathlib import Path
import argparse
import hashlib
import importlib.metadata
from io import BytesIO
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"


def build():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "mayday.db")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("Build this release using 64-bit Python on Windows.")
    if not args.database.is_file():
        raise SystemExit(f"Archive not found: {args.database}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / "build/desktop"
    scratch.mkdir(parents=True, exist_ok=True)
    snapshot = scratch / "mayday.db"

    with sqlite3.connect(
        args.database.resolve().as_uri() + "?mode=ro", uri=True
    ) as source:
        with sqlite3.connect(snapshot) as destination:
            source.backup(destination)
            assert destination.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            count = destination.execute("SELECT count(*) FROM events").fetchone()[0]

    import resvg_py
    from PIL import Image

    icon = ROOT / "static/assets/mayday.ico"
    png = resvg_py.svg_to_bytes(
        svg_path=str(ROOT / "static/assets/logo.svg"), width=256, height=256
    )
    Image.open(BytesIO(png)).save(
        icon,
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )

    version_file = scratch / "version-info.txt"
    version_tuple = tuple(int(part) for part in VERSION.split(".")) + (0,)
    version_file.write_text(
        "VSVersionInfo(ffi=FixedFileInfo(filevers=" + repr(version_tuple)
        + ", prodvers=" + repr(version_tuple)
        + ", mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), kids=["
        + "StringFileInfo([StringTable('040904B0', ["
        + "StringStruct('ProductName','MAYDAY'), StringStruct('FileDescription','MAYDAY aviation history globe'),"
        + "StringStruct('OriginalFilename','MAYDAY.exe'), StringStruct('FileVersion'," + repr(VERSION)
        + "), StringStruct('ProductVersion'," + repr(VERSION)
        + ")])]), VarFileInfo([VarStruct('Translation',[1033,1200])])])",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--name",
            "MAYDAY",
            "--icon",
            str(icon),
            "--version-file",
            str(version_file),
            "--distpath",
            str(output),
            "--workpath",
            str(scratch / "pyinstaller"),
            "--specpath",
            str(scratch),
            "--add-data",
            f"{ROOT / 'static'};static",
            "--add-data",
            f"{snapshot};.",
            "--collect-submodules",
            "uvicorn",
            "--collect-all",
            "webview",
            "--hidden-import",
            "webview.platforms.edgechromium",
            "--exclude-module",
            "PyQt5",
            "--exclude-module",
            "PyQt6",
            "--exclude-module",
            "PySide2",
            "--exclude-module",
            "PySide6",
            str(ROOT / "desktop.py"),
        ],
        check=True,
        cwd=ROOT,
    )

    bundle = output / "MAYDAY"
    for filename in ["README.md", "DESKTOP.md", "DATA-SOURCES.md"]:
        shutil.copy2(ROOT / filename, bundle / filename)
    licenses = bundle / "LICENSES"
    licenses.mkdir(exist_ok=True)
    for notice in (ROOT / "desktop-licenses").glob("*.txt"):
        shutil.copy2(notice, licenses / notice.name)
    versions = {}

    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        versions[name] = dist.version
        for item in dist.files or []:
            if any(
                word in item.name.lower() for word in ["license", "copying", "notice"]
            ) and ".dist-info" in str(item):
                original = Path(dist.locate_file(item))
                if original.is_file():
                    target = licenses / name / item.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    manifest = {
        "version": VERSION,
        "platform": "Windows x64",
        "python": platform.python_version(),
        "records": count,
        "database_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "build_packages": versions,
    }
    (bundle / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    archive = output / f"MAYDAY-{VERSION}-windows-x64.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(output))
    with ZipFile(archive) as package:
        assert package.testzip() is None
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    print(
        f"Ready: {archive} ({archive.stat().st_size / 1024**2:.1f} MB), {count:,} records"
    )


if __name__ == "__main__":
    build()
