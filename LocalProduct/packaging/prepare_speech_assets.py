"""Explicit developer tool for pinned offline speech assets.

The installed application never invokes this tool or downloads models. Run it
from a source distribution to prepare assets before building the package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import urllib.request
import zipfile

PRODUCT = Path(__file__).resolve().parents[1]
WHEEL_URL = 'https://files.pythonhosted.org/packages/c0/4c/deb0861f7da9696f8a255f1731bb73e9412cca29c4b3888a3fcb2a930a59/vosk-0.3.45-py3-none-win_amd64.whl'
WHEEL_SHA256 = '6994ddc68556c7e5730c3b6f6bad13320e3519b13ce3ed2aa25a86724e7c10ac'
MODEL_URL = 'https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip'
# Pinned after the initial HTTPS download from the model author's official site.
# Unlike the wheel hash, this is not a separately published upstream checksum.
MODEL_SHA256 = '961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4'
LICENSE_URLS = {
    'Vosk-Apache-2.0.txt': 'https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING',
    'GCC-GPL-3.0.txt': 'https://raw.githubusercontent.com/gcc-mirror/gcc/master/COPYING3',
    'GCC-Runtime-Exception-3.1.txt': 'https://raw.githubusercontent.com/gcc-mirror/gcc/master/COPYING.RUNTIME',
    'MinGW-winpthreads-COPYING.txt': 'https://raw.githubusercontent.com/mingw-w64/mingw-w64/master/mingw-w64-libraries/winpthreads/COPYING',
}


def no_links(path, regular=False):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Links are not allowed in staging paths')
        if item == path and regular and (not item.is_file() or info.st_nlink != 1):
            raise ValueError('Expected a regular unlinked file')


def fetch(url, path, limit, expected=None, *, offline=False):
    no_links(path, path.exists())
    if path.exists():
        if path.stat().st_size > limit:
            raise ValueError('Cached file exceeds size limit')
        body = path.read_bytes()
    elif offline:
        raise ValueError('A required verified cache file is missing')
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('Download size limit exceeded')
    digest = hashlib.sha256(body).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError('Pinned SHA-256 mismatch')
    if not path.exists():
        with path.open('xb') as output:
            output.write(body)
    return body, digest


def prepare(target, cache, *, offline=False):
    target, cache = Path(target).absolute(), Path(cache).absolute()
    no_links(target)
    no_links(cache)
    if target.exists():
        if not target.is_dir():
            raise ValueError('Target must be a directory')
        # Source ZIPs contain only receipts/licenses. Complete those safely;
        # refuse any previous runtime tree or unrelated target content.
        metadata = {'manifest.json'} | {'licenses/' + name for name in LICENSE_URLS}
        for item in target.rglob('*'):
            no_links(item, item.is_file())
            if item.is_file() and item.relative_to(target).as_posix() not in metadata:
                raise ValueError('Target already contains runtime assets or unrelated content')
    cache.mkdir(parents=True, exist_ok=True)
    wheel, wheel_hash = fetch(WHEEL_URL, cache / 'vosk-0.3.45-py3-none-win_amd64.whl',
                              20 * 1024**2, WHEEL_SHA256, offline=offline)
    model, model_hash = fetch(MODEL_URL, cache / 'vosk-model-small-ru-0.22.zip',
                              65 * 1024**2, MODEL_SHA256, offline=offline)
    licenses = {}
    for name, url in LICENSE_URLS.items():
        body, digest = fetch(url, cache / name, 65536, offline=offline)
        licenses[name] = (body, url, digest)
    # Validate every member before writing any extracted file.
    payload = {}
    import io
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for item in archive.infolist():
            parts = PurePosixPath(item.filename).parts
            if len(parts) == 2 and parts[0] == 'vosk' and parts[1].lower().endswith('.dll'):
                if (not re.fullmatch(r'[A-Za-z0-9_+.\-]+', parts[1]) or item.file_size > 50 * 1024**2
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError('Invalid native wheel member')
                name = 'native/' + parts[1]
                if name in payload:
                    raise ValueError('Duplicate native member')
                payload[name] = archive.read(item)
    with zipfile.ZipFile(io.BytesIO(model)) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(item.file_size for item in members) > 256 * 1024**2:
            raise ValueError('Model expansion exceeds limits')
        for item in members:
            parts = PurePosixPath(item.filename).parts
            if (not parts or parts[0] != 'vosk-model-small-ru-0.22'
                    or any(not re.fullmatch(r'[A-Za-z0-9_+.\-]+', part) or part in ('.', '..') for part in parts)
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('Invalid model member')
            if not item.is_dir():
                name = '/'.join(('model', *parts[1:]))
                if name in payload:
                    raise ValueError('Duplicate model member')
                payload[name] = archive.read(item)
    if not {'native/libvosk.dll', 'model/am/final.mdl', 'model/conf/mfcc.conf'} <= set(payload):
        raise ValueError('Required runtime assets are missing')
    for name, (body, _, _) in licenses.items():
        payload['licenses/' + name] = body
    target.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, body in sorted(payload.items()):
        path = target.joinpath(*PurePosixPath(name).parts)
        no_links(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            no_links(path, True)
            if path.read_bytes() != body:
                raise ValueError('Existing license differs from pinned preparation')
        else:
            with path.open('xb') as output:
                output.write(body)
        files[name] = {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    manifest = {
        'schema': 1, 'engine': 'vosk', 'version': '0.3.45', 'language': 'ru',
        'model': 'vosk-model-small-ru-0.22', 'license': 'Apache-2.0', 'runtime_network': False,
        'dependency_licenses': {'libgcc_s_seh-1.dll': 'GPL-3.0-with-GCC-exception',
                                'libstdc++-6.dll': 'GPL-3.0-with-GCC-exception',
                                'libwinpthread-1.dll': 'MinGW-winpthreads-COPYING'},
        'sources': {
            'wheel': {'url': WHEEL_URL, 'sha256': wheel_hash, 'published_sha256_verified': True},
            'model': {'url': MODEL_URL, 'sha256': model_hash, 'published_sha256_verified': False,
                      'pinned_sha256_verified': True},
            'licenses': {name: {'url': url, 'sha256': digest} for name, (_, url, digest) in licenses.items()},
        },
        'files': files,
    }
    manifest_path = target / 'manifest.json'
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    if manifest_path.exists():
        no_links(manifest_path, True)
        if json.loads(manifest_path.read_text(encoding='utf-8')) != manifest:
            raise ValueError('Existing receipt differs from pinned preparation')
    else:
        with manifest_path.open('x', encoding='utf-8') as output:
            output.write(manifest_text)
    return {'prepared': True, 'files': len(files), 'bytes': sum(x['size'] for x in files.values()),
            'model_sha256': model_hash, 'wheel_sha256': wheel_hash}


def main():
    parser = argparse.ArgumentParser(description='Prepare pinned offline Russian speech assets; explicit developer action only.')
    parser.add_argument('--target', type=Path, default=PRODUCT / 'assets' / 'speech')
    parser.add_argument('--cache', type=Path, default=PRODUCT / 'build' / 'asr-downloads')
    parser.add_argument('--offline', action='store_true', help='Use existing cache only; never contact the network')
    args = parser.parse_args()
    print(json.dumps(prepare(args.target, args.cache, offline=args.offline), ensure_ascii=True))


if __name__ == '__main__':
    main()
