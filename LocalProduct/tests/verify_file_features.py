"""Record reproducible local fixture checks; never touches personal files."""
import datetime
import hashlib
import json
from pathlib import Path
import sys
import unittest

PRODUCT = Path(__file__).resolve().parents[1]


def main():
    suite = unittest.defaultTestLoader.discover(str(PRODUCT / 'tests'))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    files = ('engine.py', 'app.py', 'folder_policy.py', 'folder_ui.py', 'local_rules.py',
             'rules_ui.py', 'indexed_archives.py', 'archive_ui.py')
    report = {
        'version': '0.1.5',
        'verified_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'suite': {'total': result.testsRun, 'passed': result.testsRun - len(result.skipped) - len(result.errors) - len(result.failures),
                  'failures': len(result.failures), 'errors': len(result.errors),
                  'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped]},
        'source_files_sha256': {name: hashlib.sha256((PRODUCT / 'src' / name).read_bytes()).hexdigest() for name in files},
        'scope': 'Isolated local fixtures, including Tk and archive round trips. No personal files archived or moved.',
        'actual_windows_reboot_tested': False,
        'second_pc_or_clean_vm_tested': False,
        'real_telegram_delivery_tested': False,
        'original_files_removed_by_archiver': False,
    }
    (PRODUCT / 'file-features-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
