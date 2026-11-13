"""Fetch one pinned official Blockly package; runtime never downloads anything."""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import urllib.request

VERSION = '13.4.0'
INTEGRITY = 'sha512-cWZJV5nocL+VWOG9Vfm6j8shdOLGyvKkyoGOS2bk9u6PKb4qqLp50NeL8YoNMTEi7fHmY37eBYT1yC6Y7/yZ7w=='
TARBALL = 'https://registry.npmjs.org/blockly/-/blockly-' + VERSION + '.tgz'
PRODUCT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PRODUCT.parent / 'WindowsCompanion' / 'assets' / 'blockly'


def no_links(path):
    for entry in (path, *path.parents):
        try: value = entry.lstat()
        except FileNotFoundError: continue
        if entry.is_symlink() or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked asset path refused')


def selected(name):
    if name in ('blockly_compressed.js', 'msg/ru.js', 'LICENSE', 'NOTICE', 'NOTICE.txt', 'LICENSE.txt'):
        return True
    p = PurePosixPath(name)
    return (len(p.parts) == 2 and p.parts[0] == 'media' and
            p.suffix.lower() in ('.svg', '.png', '.gif', '.mp3', '.wav') and
            re.fullmatch(r'[A-Za-z0-9_.-]+', p.name) is not None)


def prepare(output=DEFAULT_OUTPUT):
    output = Path(output).absolute()
    no_links(output)
    with urllib.request.urlopen(TARBALL, timeout=30) as response:
        body = response.read(12 * 1024**2 + 1)
    if len(body) > 12 * 1024**2:
        raise ValueError('Package too large')
    actual = 'sha512-' + base64.b64encode(hashlib.sha512(body).digest()).decode('ascii')
    if not hmac.compare_digest(actual, INTEGRITY):
        raise ValueError('Pinned npm integrity mismatch')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 1500 or sum(x.size for x in members) > 40 * 1024**2:
            raise ValueError('Unexpected package inventory')
        for entry in members:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.name or ':' in entry.name:
                raise ValueError('Unsafe package member')
            if not name.parts or name.parts[0] != 'package':
                raise ValueError('Unexpected package prefix')
            relative = PurePosixPath(*name.parts[1:]).as_posix()
            if not selected(relative):
                continue
            if not entry.isfile() or entry.size > 5 * 1024**2 or relative in files:
                raise ValueError('Unsafe selected member')
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError('Missing selected member')
            files[relative] = stream.read()
    if not {'blockly_compressed.js', 'msg/ru.js'} <= files.keys() or not any(x.startswith('LICENSE') for x in files):
        raise ValueError('Missing runtime or license')
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'version': 1, 'package': 'blockly', 'package_version': VERSION,
                'license': 'Apache-2.0', 'source': TARBALL, 'integrity': INTEGRITY,
                'archive_sha256': hashlib.sha256(body).hexdigest(), 'files': {}}
    for name, data in sorted(files.items()):
        target = output.joinpath(*PurePosixPath(name).parts)
        no_links(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError('Asset target is not a regular file')
        descriptor, temporary = tempfile.mkstemp(prefix='.blockly-', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            no_links(target)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        manifest['files'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    # Relative member names only; no machine/user paths in the redistributable receipt.
    receipt = output / 'asset-manifest.json'
    no_links(receipt)
    if receipt.exists() and receipt.stat().st_nlink != 1:
        raise ValueError('Linked receipt refused')
    receipt.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = prepare(args.output)
    print(json.dumps({'package': result['package'], 'version': result['package_version'],
                      'integrity_verified': True, 'files': len(result['files']),
                      'runtime_bytes': sum(v['size'] for v in result['files'].values())}))


if __name__ == '__main__': main()
