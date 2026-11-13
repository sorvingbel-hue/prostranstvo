"""Reproducible local beta regression report. No real Telegram sends."""
import datetime
import hashlib
import json
from pathlib import Path
import runpy
import unittest

PRODUCT = Path(__file__).resolve().parents[1]


def main():
    (PRODUCT / 'build').mkdir(exist_ok=True)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(str(PRODUCT / 'tests')))
    files = [PRODUCT / 'src' / name for name in (
        'app.py', 'local_main.py', 'desktop_pet.py', 'kitten_messenger.py', 'cat_dialogue.py', 'cat_study.py', 'family_context.py', 'family_context_ui.py', 'cat_voice.py',
        'cat_speech.py', 'speech_worker.py', 'pet_tricks.py', 'pet_tricks_server.py', 'pet_tricks_launch.py', 'family_messages_ui.py', 'family_bridge.py', 'family_telegram.py',
        'rules_ui.py', 'archive_ui.py', 'study_windows.py')]
    files += [PRODUCT / 'native' / 'DesktopPet.cs', PRODUCT / 'native' / 'PetPurr.cs', PRODUCT / 'native' / 'PetAmbience.cs', PRODUCT / 'native' / 'PetTricks.cs', PRODUCT / 'native' / 'KittenVoice.cs', PRODUCT / 'native' / 'ModeLauncher.cs',
              PRODUCT / 'assets' / 'kitten-sprites-v1.png', PRODUCT / 'assets' / 'kitten-sprites-v2.png', PRODUCT / 'assets' / 'kitten-portrait-v2.png',
              PRODUCT / 'assets' / 'cat-dialogues-ru-v1.json', PRODUCT / 'assets' / 'speech' / 'manifest.json',
              PRODUCT / 'assets' / 'blockly' / 'asset-manifest.json', PRODUCT / 'assets' / 'tricks' / 'index.html',
              PRODUCT / 'assets' / 'tricks' / 'editor.js', PRODUCT / 'assets' / 'tricks' / 'editor.css']
    report = {
        'version': runpy.run_path(str(PRODUCT / 'src' / 'version.py'))['VERSION'],
        'verified_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'unit_suite': {'total': result.testsRun, 'passed': result.testsRun - len(result.errors) - len(result.failures) - len(result.skipped),
                       'errors': len(result.errors), 'failures': len(result.failures),
                       'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped]},
        'source_sha256': {p.relative_to(PRODUCT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'live_pet_visual_acceptance': False,
        'live_pet_visual_limit': 'Computer Use found the diagnostic window; app approval timed out before state capture. Own renderer preview was inspected separately.',
        'real_telegram_delivery_tested': False,
        'private_parent_bot_pairing_required': True,
        'personal_messages_sent_by_tests': False,
        'desktop_content_or_titles_read_by_pet': False,
        'paid_ai_calls_during_normal_pet_or_message_operation': False,
    }
    (PRODUCT / 'kitten-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
