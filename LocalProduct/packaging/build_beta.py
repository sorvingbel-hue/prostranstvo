"""Rebuild the clean Windows x64 beta; never packages the personal deployment."""
import argparse
import hashlib
from importlib import metadata
import json
import re
import runpy
from pathlib import Path, PurePosixPath
import platform
import shutil
import sqlite3
import ssl
import subprocess
import sys
import zipfile
import zlib

PRODUCT = Path(__file__).resolve().parents[1]
BUILD = PRODUCT / 'build'
RELEASES = PRODUCT / 'releases'
CSC = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
VERSION = runpy.run_path(str(PRODUCT / 'src' / 'version.py'))['VERSION']
SOURCES = ['app.py', 'daily_plan.py', 'daily_plan_ui.py', 'daily_plan_animation.py', 'daily_plan_guide.py', 'daily_plan_guide_art.py', 'engine.py', 'activity_core.py', 'activity_agent.py', 'activity_ui.py', 'file_audit.py', 'file_audit_ui.py',
    'activity_control.py', 'activity_startup.py', 'browser_history.py', 'browser_history_ui.py',
    'history_schedule.py', 'family_messages.py', 'family_telegram.py', 'family_agent.py', 'desktop_pet.py', 'kitten_messenger.py', 'cat_dialogue.py', 'cat_study.py', 'cat_api.py', 'cat_brain.py', 'cat_knowledge.py', 'cat_ai_ui.py', 'family_context.py', 'family_context_ui.py', 'cat_voice.py',
    'cat_speech.py', 'speech_worker.py', 'pet_tricks.py', 'pet_tricks_server.py', 'pet_tricks_launch.py', 'family_messages_ui.py', 'family_startup.py', 'family_bridge.py', 'family_mcp.py', 'mcp_server.py', 'system_broker.py', 'study_core.py', 'study_windows.py', 'study_ui.py', 'study_guard.py', 'study_resume.py', 'study_runtime.py', 'study_shell.py', 'codex_pause.py', 'folder_policy.py', 'folder_ui.py', 'local_rules.py', 'rules_ui.py', 'indexed_archives.py', 'archive_ui.py', 'paths.py', 'local_main.py', 'version.py']

SOURCES += ['application_launcher.py', 'browser_access.py', 'agent_apps.py']
SOURCES += ['headless_browser.py', 'agent_control.py', 'parent_apps.py', 'session_control.py', 'power_control.py', 'power_ui.py']
SOURCES += ['browser_session.py', 'browser_login_ui.py']
SOURCES += ['maintenance_advisor.py', 'maintenance_ui.py', 'maintenance_sequence.py']
SOURCES += ['storage_audit.py']
SOURCES += ['storage_tasks.py', 'storage_ui.py', 'security_report.py', 'security_ui.py', 'security_mcp.py']

MINIMUM_PYTHON_PATCH = 18
FROZEN_CHECK_TIMEOUT = 45

def run(args):
    subprocess.run([str(x) for x in args], check=True, cwd=PRODUCT)

def compile_cs(source, output, references, define=None, resource=None, windowed=False, extra_sources=()):
    args = [CSC, '/nologo', '/platform:x64', '/target:' + ('winexe' if windowed else 'exe'), '/out:' + str(output)]
    args += ['/reference:' + r for r in references]
    if define:
        args += ['/define:' + define]
    if resource:
        args += ['/resource:' + str(resource) + ',payload.zip']
    run(args + [source, *extra_sources])

def zip_tree(root, target):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(root.rglob('*')):
            if file.is_file():
                archive.write(file, file.relative_to(root).as_posix())

def hash_file(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def runtime_modules():
    """Bundle the reviewed inventory, including modules used by dynamic entry points."""
    expected = set(SOURCES)
    actual = {path.name for path in (PRODUCT / 'src').glob('*.py')}
    if len(expected) != len(SOURCES) or expected != actual:
        raise RuntimeError('Reviewed source inventory must match src exactly without duplicates')
    return [Path(name).stem for name in SOURCES if name != 'local_main.py']

def pyinstaller_license_info():
    """Resolve COPYING from the active distribution's RECORD, never a glob."""
    try:
        distribution = metadata.distribution('pyinstaller')
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError('PyInstaller distribution metadata is required to package its license') from exc
    version = distribution.version
    if not isinstance(version, str) or not version or any(ord(c) < 32 for c in version):
        raise RuntimeError('PyInstaller distribution has an invalid version')
    if str(distribution.metadata.get('Name', '')).lower() != 'pyinstaller':
        raise RuntimeError('Unexpected distribution returned for PyInstaller')
    entries = []
    for entry in distribution.files or ():
        path = PurePosixPath(str(entry))
        if path.is_absolute() or '..' in path.parts or '\\' in str(entry):
            continue
        entries.append((entry, path.parts))
    roots = {parts[0] for _, parts in entries if len(parts) == 2 and parts[1] == 'METADATA'
             and parts[0].lower().startswith('pyinstaller-') and parts[0].lower().endswith('.dist-info')}
    if len(roots) != 1:
        raise RuntimeError('PyInstaller must have one recorded dist-info metadata directory')
    root = next(iter(roots))
    if root.lower() != f'pyinstaller-{version}.dist-info'.lower():
        raise RuntimeError('PyInstaller version does not match its recorded dist-info directory')
    matches = [entry for entry, parts in entries if parts in
               ((root, 'licenses', 'COPYING.txt'), (root, 'COPYING.txt'))]
    if len(matches) != 1:
        raise RuntimeError('PyInstaller must have exactly one recorded COPYING.txt license')
    license_path = Path(distribution.locate_file(matches[0]))
    if not license_path.is_file() or license_path.stat().st_size == 0:
        raise RuntimeError('Required PyInstaller COPYING.txt license is missing or empty')
    return version, license_path

def write_build_notice(destination, pyinstaller_version):
    # Describe the current build toolchain accurately even when --reuse-runtime is used.
    destination.write_text(
        f'Beta {VERSION} includes Python, Tcl/Tk 8.6, SQLite and the PyInstaller bootloader. '
        f'Build interpreter: {platform.python_implementation()} {platform.python_version()}. '
        f'Packaging tool: PyInstaller {pyinstaller_version}. '
        'Bundled license texts are included; see RELEASE-PYTHON-RUNTIME-RU.txt for provenance and remaining license-mapping limits. '
        'Runtime libraries are included only to run this application. '
        'This private beta is unsigned. No public redistribution license for the application source is granted yet.\n',
        encoding='utf-8')

def validate_python_runtime(implementation, version, *, context='Python runtime'):
    """Accept only final CPython releases on the reviewed 3.13 security baseline."""
    match = re.fullmatch(r'3\.13\.(0|[1-9][0-9]*)', version) if isinstance(version, str) else None
    if implementation != 'cpython' or match is None or int(match.group(1)) < MINIMUM_PYTHON_PATCH:
        raise RuntimeError(f'{context} must be final CPython 3.13.{MINIMUM_PYTHON_PATCH} or newer on the 3.13 line')
    return version


def build_python_version():
    return validate_python_runtime(sys.implementation.name, sys.version.split()[0], context='Build interpreter')


def build_runtime_info():
    return {'implementation': sys.implementation.name, 'python': build_python_version(),
            'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version,
            'zlib': zlib.ZLIB_RUNTIME_VERSION}


def verify_frozen_runtime(expected_runtime):
    """Probe only this build's owned Core before touching the payload stage."""
    runtime = BUILD / 'pyinstaller-dist' / 'ProstranstvoCore'
    core = runtime / 'ProstranstvoCore.exe'
    expected_core = BUILD.resolve() / 'pyinstaller-dist' / 'ProstranstvoCore' / 'ProstranstvoCore.exe'
    if not core.is_file() or core.resolve() != expected_core:
        raise RuntimeError('Owned frozen Core is missing or resolves outside its fixed build path')
    try:
        result = subprocess.run(
            [str(core), 'self-test', '--runtime-only'], cwd=runtime,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='strict', shell=False, check=False,
            timeout=FROZEN_CHECK_TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise RuntimeError('Frozen Core self-test could not complete within its bounded runtime-only probe') from error
    if result.returncode != 0:
        raise RuntimeError('Frozen Core runtime-only self-test returned a failure')
    try:
        report = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError('Frozen Core self-test did not return valid JSON') from error
    if not isinstance(report, dict) or report.get('status') != 'ok' or report.get('version') != VERSION:
        raise RuntimeError('Frozen Core self-test status or product version does not match this build')
    if report.get('check') != 'runtime_metadata_only':
        raise RuntimeError('Frozen Core self-test runtime-only check marker is missing or invalid')
    frozen = report.get('runtime')
    if not isinstance(frozen, dict):
        raise RuntimeError('Frozen Core self-test runtime metadata is missing')
    frozen_python = validate_python_runtime(
        frozen.get('implementation'), frozen.get('python'), context='Frozen Core runtime',
    )
    if frozen_python != expected_runtime['python']:
        raise RuntimeError('Frozen Core Python version does not match the build interpreter')
    for library in ('openssl', 'sqlite', 'zlib'):
        if frozen.get(library) != expected_runtime[library]:
            raise RuntimeError(f'Frozen Core {library} runtime does not match the build interpreter')
    return runtime


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--reuse-runtime', action='store_true', help='Only for reassembling unchanged Python code')
    args = p.parse_args()
    pyinstaller_version, pi_license = pyinstaller_license_info()
    expected_runtime = build_runtime_info()
    modules = runtime_modules()
    if not args.reuse_runtime:
        BUILD.mkdir(exist_ok=True)
        # importlib-based entry points are invisible to ordinary import analysis.
        hidden = [argument for name in modules for argument in ('--hidden-import', name)]
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
             '--name', 'ProstranstvoCore', '--distpath', BUILD / 'pyinstaller-dist',
             '--workpath', BUILD / 'pyinstaller-work', '--specpath', BUILD,
             '--paths', PRODUCT / 'src', *hidden, PRODUCT / 'src' / 'local_main.py'])
    runtime = verify_frozen_runtime(expected_runtime)
    RELEASES.mkdir(exist_ok=True)
    stage = BUILD / 'payload'
    if stage.exists():
        assert stage.resolve() == (PRODUCT / 'build' / 'payload').resolve()
        shutil.rmtree(stage)
    shutil.copytree(runtime, stage)
    native = PRODUCT / 'native'
    for define, name in [('UI', 'Prostranstvo'), ('MESSAGES', 'FamilyMessages'), ('FAMILY', 'FamilyMessageAgent'), ('ACTIVITY', 'ActivityAgent'), ('HISTORY', 'HistoryCollector'), ('KITTEN', 'KittenMessenger')]:
        compile_cs(native / 'ModeLauncher.cs', stage / (name + '.exe'), ['System.Windows.Forms.dll'], define=define, windowed=define != 'HISTORY')
    compile_cs(native / 'PashaMessage.cs', stage / 'PashaMessage.exe', ['System.Windows.Forms.dll', 'System.Drawing.dll', 'Microsoft.CSharp.dll'], windowed=True)
    compile_cs(native / 'DesktopPet.cs', stage / 'NativeDesktopPet.exe', ['System.Windows.Forms.dll', 'System.Drawing.dll', 'System.Web.Extensions.dll'], windowed=True, extra_sources=[native / 'PetPurr.cs', native / 'PetAmbience.cs', native / 'PetTricks.cs'])
    compile_cs(native / 'KittenVoice.cs', stage / 'KittenVoice.exe', ['System.Windows.Forms.dll', 'Microsoft.CSharp.dll'], windowed=True)
    shutil.copytree(PRODUCT / 'assets', stage / 'assets')
    compile_cs(native / 'HistorySchedule.cs', stage / 'HistorySchedule.exe', ['Microsoft.CSharp.dll', 'System.Web.Extensions.dll'])
    for name in ['START-HERE-RU.txt', 'CODEX-SETUP-RU.txt', 'PRIVACY-LOCAL.txt', 'CHANGELOG-RU.txt', 'FAMILY-PET-CHAT-RU.txt', 'STUDY-MODE-RU.txt', 'FILES-RULES-ARCHIVES-RU.txt', 'KITTEN-RU.txt', 'KITTEN-TRICKS-RU.txt', 'KITTEN-REUSE-RESEARCH-RU.txt', 'KITTEN-ART-PROMPT.txt', 'KITTEN-ART-V2-PROMPTS.txt', 'OFFLINE-SPEECH-RU.txt', 'CHILD-DIALOGUE-SOURCES-RU.txt', 'CAT-AI-RU.txt']:
        shutil.copy2(PRODUCT / 'docs' / name, stage / name)
    shutil.copy2(PRODUCT / 'docs' / 'AGENT-CONTROL-RU.txt', stage / 'AGENT-CONTROL-RU.txt')
    shutil.copy2(PRODUCT / 'docs' / 'WINDOWS-HEALTH-RU.txt', stage / 'WINDOWS-HEALTH-RU.txt')
    shutil.copy2(PRODUCT / 'docs' / 'STORAGE-AUDIT-RU.txt', stage / 'STORAGE-AUDIT-RU.txt')
    for name in ('SECURITY-REPORT-RU.txt', 'SECURITY-CHANNEL-RU.txt', 'STORAGE-TASKS-RU.txt', 'PRODUCT-ROADMAP-RU.md', 'RELEASE-PYTHON-RUNTIME-RU.txt'):
        shutil.copy2(PRODUCT / 'docs' / name, stage / name)
    licenses = stage / 'licenses'
    licenses.mkdir()
    python_base = Path(sys.base_prefix)
    shutil.copy2(python_base / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    tk_license = python_base / 'tcl' / 'tk8.6' / 'license.terms'
    if tk_license.exists():
        shutil.copy2(tk_license, licenses / 'Tk-license.terms')
    shutil.copy2(PRODUCT / 'packaging' / 'licenses' / 'Tcl-license.terms', licenses / 'Tcl-license.terms')
    shutil.copy2(pi_license, licenses / 'PyInstaller-COPYING.txt')
    write_build_notice(licenses / 'NOTICE.txt', pyinstaller_version)
    manifest = {'product': 'prostranstvo-local-beta', 'version': VERSION, 'files': {}}
    for file in sorted(stage.rglob('*')):
        if file.is_file():
            manifest['files'][file.relative_to(stage).as_posix()] = {'size': file.stat().st_size, 'sha256': hash_file(file)}
    (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    payload = BUILD / 'payload.zip'
    zip_tree(stage, payload)
    setup = RELEASES / f'Prostranstvo-Local-Beta-{VERSION}-Setup.exe'
    compile_cs(PRODUCT / 'packaging' / 'SetupLocalBeta.cs', setup,
        ['System.Windows.Forms.dll', 'System.Drawing.dll', 'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll', 'System.Web.Extensions.dll'], resource=payload, windowed=True)
    portable = RELEASES / f'Prostranstvo-Local-Beta-{VERSION}-Portable.zip'
    shutil.copy2(payload, portable)
    source_zip = RELEASES / f'Prostranstvo-Local-Beta-{VERSION}-Source.zip'
    allowed = [PRODUCT / 'src' / name for name in SOURCES]
    for folder in ['native', 'packaging', 'docs', 'tests']:
        allowed += [x for x in (PRODUCT / folder).glob('*') if x.suffix in ('.py', '.cs', '.txt', '.md', '.js') and x.is_file()]
    allowed += [PRODUCT / 'packaging' / 'licenses' / 'Tcl-license.terms']
    allowed += [PRODUCT / 'assets' / 'kitten-sprites-v1.png', PRODUCT / 'assets' / 'kitten-sprites-v2.png',
                PRODUCT / 'assets' / 'kitten-portrait-v2.png', PRODUCT / 'assets' / 'cat-dialogues-ru-v1.json',
                PRODUCT / 'assets' / 'cat-knowledge-approved-v1.json']
    allowed += [x for x in (PRODUCT / 'assets' / 'speech').glob('*') if x.is_file() and x.suffix in ('.json', '.txt', '.md')]
    allowed += [x for x in (PRODUCT / 'assets' / 'speech' / 'licenses').rglob('*') if x.is_file() and x.suffix in ('.txt', '.md')]
    allowed += [x for x in (PRODUCT / 'assets' / 'tricks').glob('*') if x.is_file() and x.suffix in ('.html', '.js', '.css', '.json')]
    allowed += [x for x in (PRODUCT / 'assets' / 'blockly').glob('*') if x.is_file() and x.suffix in ('.json', '.txt', '.md')]
    allowed += [PRODUCT / 'assets' / 'blockly' / 'LICENSE']
    allowed += list((PRODUCT / 'assets' / 'agent-browser').glob('*.mjs'))
    with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(set(allowed)):
            archive.write(file, file.relative_to(PRODUCT).as_posix())
    artifacts = [setup, portable, source_zip]
    checksums = ''.join(hash_file(x) + '  ' + x.name + '\n' for x in artifacts)
    (RELEASES / f'SHA256SUMS-{VERSION}.txt').write_text(checksums, encoding='ascii')
    (RELEASES / 'SHA256SUMS.txt').write_text(checksums, encoding='ascii')
    print(json.dumps({'version': VERSION, 'files': len(manifest['files']), 'artifacts': [{'name': x.name, 'bytes': x.stat().st_size, 'sha256': hash_file(x)} for x in artifacts]}, indent=2))

if __name__ == '__main__':
    main()
