"""Fixed local entry points used by native launchers and stdio MCP."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from paths import APP_ROOT, DATA_ROOT, DEFAULT_ALLOWED_ROOTS
from version import VERSION

SELF_TEST_MODULES = (
    'app', 'engine', 'activity_agent', 'history_schedule', 'family_messages_ui', 'family_agent',
    'family_telegram', 'kitten_messenger', 'cat_api', 'cat_brain', 'cat_knowledge', 'cat_ai_ui',
    'cat_dialogue', 'cat_study', 'family_context', 'family_context_ui', 'cat_voice', 'cat_speech',
    'speech_worker', 'pet_tricks', 'pet_tricks_server', 'pet_tricks_launch', 'desktop_pet',
    'study_core', 'study_windows', 'study_ui', 'study_guard', 'study_resume', 'study_runtime',
    'study_shell', 'folder_policy', 'folder_ui', 'local_rules', 'rules_ui', 'indexed_archives',
    'archive_ui', 'application_launcher', 'browser_access', 'agent_apps', 'agent_control',
    'headless_browser', 'browser_session', 'browser_login_ui', 'parent_apps', 'codex_pause',
    'session_control', 'power_control', 'power_ui', 'maintenance_advisor', 'maintenance_ui',
    'maintenance_sequence', 'storage_audit', 'system_broker',
    'storage_tasks', 'storage_ui', 'security_report', 'security_ui', 'security_mcp',
)

class GuiLock:
    def __enter__(self):
        key = hashlib.sha256(os.path.normcase(str(DATA_ROOT.absolute())).encode('utf-8')).hexdigest()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.CreateMutexW(None, False, 'Global\\ProstranstvoLocalBeta-GUI-' + key)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.existed = ctypes.get_last_error() == 183
        return self
    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)

def prepare_uninstall():
    # Only the beta's named current-user entries are touched. Data stays intact.
    from activity_startup import set_startup_enabled as activity_startup
    from family_startup import set_startup_enabled as family_startup
    from history_schedule import helper, HistoryArchive
    from activity_core import ActivityStore
    from family_messages import MessageStore
    from family_telegram import TelegramSettings
    from study_resume import StudyResume
    activity_startup(False)
    family_startup(False)
    study = StudyResume()
    lease = study.read()
    if lease is not None:
        study.clear(lease['session_id'])
    study.disable_startup()
    if (DATA_ROOT / 'activity' / 'config.json').exists():
        ActivityStore().set_config(enabled=False)
    if (DATA_ROOT / 'family-messages' / 'telegram-config.json').exists():
        store = MessageStore()
        TelegramSettings(store.root).disable()
    if (DATA_ROOT / 'browser-history' / 'schedule-config.json').exists():
        HistoryArchive().set_config(enabled=False)
    helper('remove')
    return {'prepared': True, 'data_preserved': str(DATA_ROOT)}

def main():
    parser = argparse.ArgumentParser(description='Пространство Local Beta', allow_abbrev=False)
    parser.add_argument('mode', choices=('ui', 'messages', 'reply', 'kitten', 'pet', 'activity-agent', 'family-agent', 'history-collect', 'mcp', 'family-mcp', 'security-mcp', 'study-resume', 'status', 'self-test', 'prepare-uninstall', 'speech-transcribe', 'tricks', 'power-worker', 'storage-worker'))
    parser.add_argument('--activity', action='store_true')
    parser.add_argument('--messages', action='store_true')
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--ready-stdout', action='store_true')
    parser.add_argument('--reply-to')
    parser.add_argument('--mini', action='store_true')
    parser.add_argument('--greet', action='store_true')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--serve-only', action='store_true')
    parser.add_argument('--request-id')
    parser.add_argument('--worker-id')
    parser.add_argument('--task-id')
    parser.add_argument('--no-gui', action='store_true', help='Self-test imports/offline logic without creating Tk')
    parser.add_argument('--runtime-only', action='store_true', help='Self-test runtime metadata only; no assets or Tk')
    args = parser.parse_args()
    if args.no_gui and args.mode != 'self-test':
        parser.error('--no-gui is only supported for self-test')
    if args.runtime_only:
        if args.mode != 'self-test':
            parser.error('--runtime-only is only supported for self-test')
        if any((args.activity, args.messages, args.setup, args.background, args.ready_stdout,
                args.reply_to, args.mini, args.greet, args.open, args.serve_only,
                args.request_id, args.worker_id, args.task_id)):
            parser.error('Runtime metadata self-test accepts no other action options')
    if args.mode == 'storage-worker':
        if not args.task_id or not re.fullmatch(r'[0-9a-f]{32}', args.task_id):
            parser.error('Storage worker requires an exact task ID')
        if any((args.activity, args.messages, args.setup, args.background, args.ready_stdout,
                args.reply_to, args.mini, args.greet, args.open, args.serve_only,
                args.request_id, args.worker_id)):
            parser.error('Storage worker accepts only --task-id')
    elif args.task_id is not None:
        parser.error('Storage task ID is only supported for storage-worker')
    if args.mode == 'security-mcp' and any((args.activity, args.messages, args.setup,
            args.background, args.ready_stdout, args.reply_to, args.mini, args.greet,
            args.open, args.serve_only, args.request_id, args.worker_id)):
        parser.error('Security MCP accepts no command-line options')
    if args.mode == 'power-worker':
        if (not args.request_id or not re.fullmatch(r'[0-9a-f]{32}', args.request_id) or
                not args.worker_id or not re.fullmatch(r'[0-9a-f]{32}', args.worker_id)):
            parser.error('Power worker requires exact request and worker IDs')
        if any((args.activity, args.messages, args.setup, args.background, args.ready_stdout,
                args.reply_to, args.mini, args.greet, args.open, args.serve_only)):
            parser.error('Power worker accepts only --request-id and --worker-id')
    elif args.request_id is not None or args.worker_id is not None:
        parser.error('Power worker IDs are only supported for power-worker')
    if args.mode != 'tricks' and (args.open or args.serve_only):
        parser.error('Editor arguments are only supported for tricks')
    if args.mode == 'tricks' and any((args.activity, args.messages, args.setup, args.background, args.reply_to, args.mini)):
        parser.error('Tricks accepts only --open, --serve-only and --ready-stdout')
    if args.mode == 'tricks' and args.open and args.serve_only:
        parser.error('Choose one editor launch mode')
    if args.mini and args.mode != 'kitten':
        parser.error('--mini is only supported for kitten')
    if args.greet and (args.mode != 'kitten' or args.mini):
        parser.error('--greet is only supported for kitten without --mini')
    if args.mode == 'kitten' and any((args.activity, args.messages, args.setup, args.background, args.ready_stdout, args.reply_to)):
        parser.error('Kitten accepts only --mini')
    if args.mode == 'pet' and any((args.activity, args.messages, args.setup, args.background, args.ready_stdout, args.reply_to)):
        parser.error('Pet accepts no arguments')
    if args.mode == 'speech-transcribe' and any((args.activity, args.messages, args.setup, args.background, args.ready_stdout, args.reply_to, args.mini)):
        parser.error('Speech worker accepts no arguments')
    if args.reply_to and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args.reply_to):
        parser.error('Invalid reply ID')
    if args.mode == 'storage-worker':
        from storage_tasks import main as storage_main
        return storage_main(['--worker', args.task_id])
    elif args.mode == 'power-worker':
        from power_control import PowerControl
        value = PowerControl().run_worker(args.request_id, args.worker_id)
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value.get('state') in {'dispatched', 'cancelled'} else 1
    elif args.mode == 'ui':
        with GuiLock() as lock:
            if lock.existed:
                ctypes.windll.user32.MessageBoxW(None, 'Окно Local Beta уже открыто. Выберите его на панели задач.', 'Пространство Local Beta', 0x40)
                return 0
            from app import App
            App().mainloop()
    elif args.mode == 'messages':
        from family_messages_ui import messages_window
        messages_window(args.setup).mainloop()
    elif args.mode == 'reply':
        from family_messages_ui import compose_window
        compose_window(args.reply_to, args.ready_stdout).mainloop()
    elif args.mode == 'kitten':
        from kitten_messenger import main as kitten_main
        options = ['--mini'] if args.mini else ['--greet'] if args.greet else []
        return kitten_main(options)
    elif args.mode == 'tricks':
        from pet_tricks_server import main as tricks_main
        options = ['--serve-only'] if args.serve_only else ['--open']
        if args.ready_stdout:
            options.append('--ready-stdout')
        return tricks_main(options)
    elif args.mode == 'speech-transcribe':
        from speech_worker import main as speech_main
        return speech_main()
    elif args.mode == 'pet':
        from desktop_pet import main as pet_main
        return pet_main([])
    elif args.mode == 'activity-agent':
        from activity_agent import main as activity_main
        sys.argv = [sys.argv[0]] + (['--background'] if args.background else [])
        activity_main()
    elif args.mode == 'family-agent':
        from family_agent import run
        run()
    elif args.mode == 'history-collect':
        from history_schedule import HistoryArchive
        value = HistoryArchive().collect()
        print(json.dumps(value, ensure_ascii=False))
        return 3 if value.get('phase') == 'partial' else 0
    elif args.mode == 'mcp':
        import mcp_server  # imports start the stdio loop; no GUI or cloud service
    elif args.mode == 'family-mcp':
        from family_mcp import main as family_main
        family_main()
    elif args.mode == 'security-mcp':
        from security_mcp import main as security_main
        return security_main()
    elif args.mode == 'study-resume':
        from study_runtime import main as study_main
        return study_main(['--resume'])
    elif args.mode == 'status':
        from engine import Executor
        print(json.dumps({'product': 'prostranstvo-local-beta', 'version': VERSION, 'app': str(APP_ROOT), 'data': str(DATA_ROOT), 'status': Executor().status()}, ensure_ascii=False))
    elif args.mode == 'self-test':
        # No executor, browser, credentials, startup entries or network are used.
        import ssl
        import sqlite3
        import zlib
        runtime = {'implementation': sys.implementation.name, 'python': sys.version.split()[0],
                   'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version,
                   'zlib': zlib.ZLIB_RUNTIME_VERSION}
        if args.runtime_only:
            # Build preflight runs before resources are copied beside the Core.
            print(json.dumps({'status': 'ok', 'version': VERSION, 'runtime': runtime,
                              'check': 'runtime_metadata_only', 'tk_tested': False,
                              'data_created': False}))
            return 0
        import importlib
        import tkinter as tk
        from tkinter import ttk
        # Static imports also make these inert modules discoverable by the frozen build.
        import application_launcher, browser_access, agent_apps, agent_control
        import headless_browser, browser_session, browser_login_ui, parent_apps, codex_pause
        import session_control, power_control, power_ui, maintenance_advisor, maintenance_ui
        import maintenance_sequence, storage_audit, system_broker
        import storage_tasks, storage_ui, security_report, security_ui, security_mcp
        for module in SELF_TEST_MODULES:
            importlib.import_module(module)
        tk_version = None
        if not args.no_gui:
            root = tk.Tk()
            try:
                root.withdraw()
                ttk.Frame(root).pack()
                root.update_idletasks()
                tk_version = root.tk.call('info', 'patchlevel')
            finally:
                root.destroy()
        from cat_brain import CatBrain
        from cat_dialogue import CatDialogue
        from cat_knowledge import ApprovedKnowledge
        knowledge = ApprovedKnowledge()
        assert knowledge.search('числитель знаменатель')[0]['id'] == 'fraction_parts'
        brain = CatBrain(CatDialogue(), knowledge=knowledge)
        assert brain.reply('Давай познакомимся')['intent'] == 'introduction'
        brain.reply('Меня зовут Тестик')
        assert 'Тестик' in brain.reply('Как меня зовут?')['text']
        assert 'Roblox' in brain.reply('Мне нравится Roblox')['text']
        assert brain.last_metadata['api_attempted'] is False
        print(json.dumps({'status': 'ok', 'version': VERSION, 'tk': tk_version,
            'tk_tested': not args.no_gui, 'imports_checked': list(SELF_TEST_MODULES), 'data_created': False,
            'runtime': runtime,
            'cat_ai': {'offline_default': True, 'approved_cards': len(knowledge._records),
                       'session_name': True, 'no_network_calls': True}}))
    elif args.mode == 'prepare-uninstall':
        print(json.dumps(prepare_uninstall(), ensure_ascii=False))
    return 0

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except Exception as error:
        # Technical events only: never write exception text, replies or tokens.
        try:
            import datetime
            from activity_core import no_links
            log = DATA_ROOT / 'logs' / 'core-errors.jsonl'
            no_links(log, log.exists())
            log.parent.mkdir(parents=True, exist_ok=True)
            no_links(log, log.exists())
            with open(log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'type': type(error).__name__}) + '\n')
        except Exception:
            pass
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
