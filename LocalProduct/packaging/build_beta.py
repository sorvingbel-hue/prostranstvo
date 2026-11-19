"""Rebuild the clean Windows x64 beta; never packages the personal deployment."""
import argparse
import hashlib
import json
import runpy
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

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

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--reuse-runtime', action='store_true', help='Only for reassembling unchanged Python code')
    args = p.parse_args()
    BUILD.mkdir(exist_ok=True)
    RELEASES.mkdir(exist_ok=True)
    if not args.reuse_runtime:
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
             '--name', 'ProstranstvoCore', '--distpath', BUILD / 'pyinstaller-dist',
             '--workpath', BUILD / 'pyinstaller-work', '--specpath', BUILD, PRODUCT / 'src' / 'local_main.py'])
    runtime = BUILD / 'pyinstaller-dist' / 'ProstranstvoCore'
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
    licenses = stage / 'licenses'
    licenses.mkdir()
    python_base = Path(sys.base_prefix)
    shutil.copy2(python_base / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    tk_license = python_base / 'tcl' / 'tk8.6' / 'license.terms'
    if tk_license.exists():
        shutil.copy2(tk_license, licenses / 'Tk-license.terms')
    shutil.copy2(PRODUCT / 'packaging' / 'licenses' / 'Tcl-license.terms', licenses / 'Tcl-license.terms')
    import PyInstaller
    pi_license = Path(PyInstaller.__file__).parent.parent / 'pyinstaller-6.22.3.dist-info' / 'licenses' / 'COPYING.txt'
    if not pi_license.exists():
        pi_license = Path(PyInstaller.__file__).parent.parent / 'pyinstaller-6.22.3.dist-info' / 'COPYING.txt'
    if pi_license.exists():
        shutil.copy2(pi_license, licenses / 'PyInstaller-COPYING.txt')
    (licenses / 'NOTICE.txt').write_text(f'Beta {VERSION} bundles CPython 3.13.3, Tcl/Tk 8.6, SQLite and the PyInstaller 6.22.3 bootloader. Python license includes its bundled third-party notices. Runtime libraries are included only to run this application. This private beta is unsigned. No public redistribution license for the application source is granted yet.\n', encoding='utf-8')
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
