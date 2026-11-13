"""Create a unique clean portable build. Never packages personal learning data."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = ROOT / "build" / stamp
    target.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
                    "--name", "LearningModuleCore", "--paths", str(ROOT / "src"),
                    "--collect-submodules", "learning_module", "--distpath", str(target / "dist"),
                    "--workpath", str(target / "work"), "--specpath", str(target),
                    str(ROOT / "run_learning.py")], check=True, cwd=ROOT)
    stage = target / "dist" / "LearningModuleCore"
    csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    subprocess.run([str(csc), "/nologo", "/target:winexe", "/platform:x64",
                    "/reference:System.Windows.Forms.dll", "/out:" + str(stage / "LearningModule.exe"),
                    str(ROOT / "packaging" / "LearningLauncher.cs")], check=True)
    for name in ("START-HERE-RU.txt", "module-manifest.json"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / "docs", stage / "docs")
    shutil.copytree(ROOT / "contracts", stage / "contracts")
    shutil.copytree(ROOT / "research", stage / "research")
    shutil.copytree(ROOT / "examples", stage / "examples")
    licenses = stage / "licenses"
    licenses.mkdir()
    base = Path(sys.base_prefix)
    shutil.copy2(base / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    tk_license = base / "tcl" / "tk8.6" / "license.terms"
    if tk_license.exists():
        shutil.copy2(tk_license, licenses / "Tk-license.terms")
    tcl_license = ROOT.parent / "LocalProduct" / "packaging" / "licenses" / "Tcl-license.terms"
    if tcl_license.exists():
        shutil.copy2(tcl_license, licenses / "Tcl-license.terms")
    import PyInstaller
    for license_path in Path(PyInstaller.__file__).parent.parent.glob("pyinstaller-*.dist-info/licenses/COPYING.txt"):
        shutil.copy2(license_path, licenses / "PyInstaller-COPYING.txt")
        break
    (licenses / "NOTICE.txt").write_text("Local learning prototype 0.1.0. Bundles CPython, SQLite, Tcl/Tk and the PyInstaller bootloader; see bundled licenses. This is a private unsigned Windows beta; clean-device acceptance and a commercial licensing review remain separate.\n", encoding="utf-8")
    files = {path.relative_to(stage).as_posix(): {"sha256": digest(path), "size": path.stat().st_size}
             for path in sorted(stage.rglob("*")) if path.is_file()}
    (stage / "files-manifest.json").write_text(json.dumps({"version": "0.1.0", "files": files}, indent=2), encoding="utf-8")
    portable = target / "LearningModule-0.1.0-Portable.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    source_zip = target / "LearningModule-0.1.0-Source.zip"
    source_files = [ROOT / "run_learning.py", ROOT / "START-HERE-RU.txt", ROOT / "module-manifest.json"]
    for folder in ("src", "tests", "packaging", "docs", "research", "contracts", "examples"):
        source_files.extend(path for path in (ROOT / folder).rglob("*")
                            if path.is_file() and path.suffix in {".py", ".cs", ".ps1", ".txt", ".json"}
                            and "__pycache__" not in path.parts)
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(set(source_files)):
            archive.write(path, path.relative_to(ROOT).as_posix())
    receipt = {"version": "0.1.0", "directory": str(target), "runtime": str(stage),
               "portable": str(portable), "portable_sha256": digest(portable),
               "source": str(source_zip), "source_sha256": digest(source_zip),
               "file_count": len(files)}
    (ROOT / "build-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
