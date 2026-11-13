"""Refresh only docs/source archives after acceptance; keep verified runtime."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_archive(target, pairs):
    temporary = target.with_suffix(target.suffix + ".pending")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, name in pairs:
            archive.write(path, name)
    os.replace(temporary, target)


def main():
    receipt = json.loads((ROOT / "build-receipt.json").read_text(encoding="utf-8"))
    build_root = (ROOT / "build").resolve()
    stage = Path(receipt["runtime"]).resolve()
    target = Path(receipt["directory"]).resolve()
    if not stage.is_relative_to(build_root) or not target.is_relative_to(build_root):
        raise ValueError("Build path is outside this module's workspace")
    for name in ("START-HERE-RU.txt", "module-manifest.json", "verification.json"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, stage / name)
    for folder in ("docs", "research", "contracts", "examples"):
        shutil.copytree(ROOT / folder, stage / folder, dirs_exist_ok=True)
    files = {path.relative_to(stage).as_posix(): {"sha256": digest(path), "size": path.stat().st_size}
             for path in sorted(stage.rglob("*")) if path.is_file() and path.name != "files-manifest.json"}
    (stage / "files-manifest.json").write_text(json.dumps({"version": "0.1.0", "files": files}, indent=2), encoding="utf-8")
    portable = Path(receipt["portable"])
    source_zip = Path(receipt["source"])
    if portable.parent.resolve() != target or source_zip.parent.resolve() != target:
        raise ValueError("Archive paths are outside this build")
    write_archive(portable, [(path, path.relative_to(stage).as_posix())
                             for path in sorted(stage.rglob("*")) if path.is_file()])
    source_files = [ROOT / "run_learning.py", ROOT / "START-HERE-RU.txt", ROOT / "module-manifest.json"]
    for folder in ("src", "tests", "packaging", "docs", "research", "contracts", "examples"):
        source_files.extend(path for path in (ROOT / folder).rglob("*")
                            if path.is_file() and path.suffix in {".py", ".cs", ".ps1", ".txt", ".json"}
                            and "__pycache__" not in path.parts)
    write_archive(source_zip, [(path, path.relative_to(ROOT).as_posix()) for path in sorted(set(source_files))])
    receipt.update(portable_sha256=digest(portable), source_sha256=digest(source_zip), file_count=len(files))
    (ROOT / "build-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"runtime_unchanged": True, **receipt}, indent=2))


if __name__ == "__main__":
    main()
