"""Packaging metadata/license checks; no runtime build, compiler, UI or network."""
from importlib import metadata
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'packaging' / 'build_beta.py'
SPEC = importlib.util.spec_from_file_location('build_beta_license_tests', SCRIPT)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class FakeDistribution:
    def __init__(self, base, version='7.41.0', layout='modern', content=b'Test COPYING license\n'):
        self.base = Path(base)
        self.version = version
        self.metadata = {'Name': 'pyinstaller'}
        directory = f'pyinstaller-{version}.dist-info'
        self.files = [PurePosixPath(directory, 'METADATA')]
        relative = PurePosixPath(directory, *(['licenses'] if layout == 'modern' else []), 'COPYING.txt')
        self.files.append(relative)
        self.license = self.locate_file(relative)
        self.license.parent.mkdir(parents=True, exist_ok=True)
        self.license.write_bytes(content)

    def locate_file(self, entry):
        return self.base.joinpath(*entry.parts)


class BuildLicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def resolve(self, distribution):
        with patch.object(BUILD.metadata, 'distribution', return_value=distribution) as lookup:
            found = BUILD.pyinstaller_license_info()
        lookup.assert_called_once_with('pyinstaller')
        return found

    def test_modern_layout_uses_actual_metadata_version(self):
        installed = FakeDistribution(self.base, version='7.41.0', layout='modern')
        stale = self.base / 'pyinstaller-6.22.3.dist-info' / 'COPYING.txt'
        stale.parent.mkdir()
        stale.write_text('Stale unrelated version', encoding='utf-8')
        version, source = self.resolve(installed)
        self.assertEqual(version, '7.41.0')
        self.assertEqual(source, installed.license)
        self.assertNotEqual(source, stale)

    def test_legacy_layout_is_read_from_distribution_record(self):
        installed = FakeDistribution(self.base, version='6.17.0', layout='legacy')
        self.assertEqual(self.resolve(installed), ('6.17.0', installed.license))

    def test_metadata_version_mismatch_with_recorded_directory_fails(self):
        installed = FakeDistribution(self.base, version='6.22.3')
        installed.version = '7.41.0'
        with self.assertRaisesRegex(RuntimeError, 'version does not match'):
            self.resolve(installed)

    def test_missing_recorded_license_fails_instead_of_omitting_it(self):
        installed = FakeDistribution(self.base)
        installed.license.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing or empty'):
            self.resolve(installed)

    def test_unrecorded_license_in_another_version_is_not_used(self):
        installed = FakeDistribution(self.base)
        installed.files = [installed.files[0]]
        stale = self.base / 'pyinstaller-6.22.3.dist-info' / 'COPYING.txt'
        stale.parent.mkdir()
        stale.write_text('Stale license', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'exactly one recorded'):
            self.resolve(installed)

    def test_license_from_other_dist_info_is_not_used(self):
        installed = FakeDistribution(self.base)
        installed.files[1] = PurePosixPath('different-1.0.dist-info/licenses/COPYING.txt')
        with self.assertRaisesRegex(RuntimeError, 'exactly one recorded'):
            self.resolve(installed)

    def test_empty_license_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'missing or empty'):
            self.resolve(FakeDistribution(self.base, content=b''))

    def test_missing_file_record_fails_closed(self):
        installed = FakeDistribution(self.base)
        installed.files = None
        with self.assertRaisesRegex(RuntimeError, 'one recorded dist-info'):
            self.resolve(installed)

    def test_ambiguous_legacy_and_modern_licenses_fail_closed(self):
        installed = FakeDistribution(self.base)
        installed.files.append(PurePosixPath('pyinstaller-7.41.0.dist-info/COPYING.txt'))
        with self.assertRaisesRegex(RuntimeError, 'exactly one recorded'):
            self.resolve(installed)

    def test_no_pyinstaller_distribution_fails_with_clear_error(self):
        with patch.object(BUILD.metadata, 'distribution', side_effect=metadata.PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, 'distribution metadata is required'):
                BUILD.pyinstaller_license_info()

    def test_notice_uses_current_interpreter_and_installed_tool_version(self):
        target = self.base / 'NOTICE.txt'
        with patch.object(BUILD.platform, 'python_implementation', return_value='CPython'), \
             patch.object(BUILD.platform, 'python_version', return_value='3.14.9'):
            BUILD.write_build_notice(target, '7.41.0')
        notice = target.read_text(encoding='utf-8')
        self.assertIn('Build interpreter: CPython 3.14.9', notice)
        self.assertIn('Packaging tool: PyInstaller 7.41.0', notice)
        self.assertIn('see RELEASE-PYTHON-RUNTIME-RU.txt for provenance and remaining license-mapping limits', notice)
        self.assertNotIn('Python license includes its bundled third-party notices', notice)
        self.assertNotIn('3.13.3', notice)
        self.assertNotIn('6.22.3', notice)

    def test_missing_license_preflight_does_not_run_build_or_make_directories(self):
        installed = FakeDistribution(self.base)
        installed.license.unlink()
        with patch.object(BUILD.metadata, 'distribution', return_value=installed), \
             patch.object(BUILD.sys, 'argv', ['build_beta.py']), \
             patch.object(BUILD, 'run') as run, \
             patch.object(BUILD, 'BUILD', self.base / 'never-created-build'), \
             patch.object(BUILD, 'RELEASES', self.base / 'never-created-releases'):
            with self.assertRaisesRegex(RuntimeError, 'missing or empty'):
                BUILD.main()
        run.assert_not_called()
        self.assertFalse((self.base / 'never-created-build').exists())
        self.assertFalse((self.base / 'never-created-releases').exists())


class BuildRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.build = self.base / 'build'
        self.runtime = self.build / 'pyinstaller-dist' / 'ProstranstvoCore'
        self.core = self.runtime / 'ProstranstvoCore.exe'
        self.runtime.mkdir(parents=True)
        self.core.write_bytes(b'Fixture only; never executed')
        self.addCleanup(patch.stopall)
        patch.object(BUILD, 'BUILD', self.build).start()

    @staticmethod
    def report(python='3.13.18', implementation='cpython', **changes):
        report = {
            'status': 'ok', 'version': BUILD.VERSION, 'check': 'runtime_metadata_only',
            'runtime': {'implementation': implementation, 'python': python,
                        'openssl': 'test', 'sqlite': 'test', 'zlib': 'test'},
        }
        report.update(changes)
        return report

    def expected(self, python='3.13.18'):
        return self.report(python=python)['runtime']

    def result(self, report=None, *, returncode=0, stdout=None):
        return SimpleNamespace(returncode=returncode, stdout=(
            json.dumps(self.report() if report is None else report) if stdout is None else stdout
        ), stderr='')

    def test_final_baseline_and_future_patch_are_accepted(self):
        for version in ('3.13.18', '3.13.19'):
            with self.subTest(version=version):
                self.assertEqual(BUILD.validate_python_runtime('cpython', version), version)

    def test_old_other_line_non_cpython_and_prerelease_are_rejected(self):
        for implementation, version in (
            ('cpython', '3.13.3'), ('cpython', '3.13.17'), ('cpython', '3.14.0'),
            ('cpython', '3.12.99'), ('pypy', '3.13.18'), ('CPython', '3.13.18'),
            ('cpython', '3.13.18rc1'), ('cpython', '3.13.19a1'),
            ('cpython', '3.13.19.dev1'), ('cpython', '3.13.18+'),
            ('cpython', '3.13.018'), ('cpython', None), ('cpython', 31318),
        ):
            with self.subTest(implementation=implementation, version=version):
                with self.assertRaisesRegex(RuntimeError, 'final CPython 3.13.18'):
                    BUILD.validate_python_runtime(implementation, version)

    def test_build_interpreter_preflight_uses_actual_implementation_and_version(self):
        with patch.object(BUILD.sys, 'version', '3.13.18 (fixture)'):
            self.assertEqual(BUILD.build_python_version(), '3.13.18')
        with patch.object(BUILD.sys, 'version', '3.13.3 (fixture)'):
            with self.assertRaisesRegex(RuntimeError, 'Build interpreter'):
                BUILD.build_python_version()

    def test_build_runtime_metadata_uses_loaded_standard_libraries(self):
        with (patch.object(BUILD, 'build_python_version', return_value='3.13.18'),
              patch.object(BUILD.ssl, 'OPENSSL_VERSION', 'fixture OpenSSL'),
              patch.object(BUILD.sqlite3, 'sqlite_version', 'fixture SQLite'),
              patch.object(BUILD.zlib, 'ZLIB_RUNTIME_VERSION', 'fixture zlib')):
            found = BUILD.build_runtime_info()
        self.assertEqual(found, {'implementation': 'cpython', 'python': '3.13.18',
                                 'openssl': 'fixture OpenSSL', 'sqlite': 'fixture SQLite',
                                 'zlib': 'fixture zlib'})

    def test_old_build_interpreter_fails_before_build_or_directory_creation(self):
        build = self.base / 'never-created-build'
        releases = self.base / 'never-created-releases'
        with (patch.object(BUILD, 'pyinstaller_license_info', return_value=('6.22.3', self.base / 'COPYING')),
             patch.object(BUILD.sys, 'argv', ['build_beta.py']),
             patch.object(BUILD.sys, 'version', '3.13.3 (fixture)'),
             patch.object(BUILD, 'BUILD', build), patch.object(BUILD, 'RELEASES', releases),
             patch.object(BUILD, 'runtime_modules') as modules, patch.object(BUILD, 'run') as run):
            with self.assertRaisesRegex(RuntimeError, 'Build interpreter'):
                BUILD.main()
        modules.assert_not_called()
        run.assert_not_called()
        self.assertFalse(build.exists())
        self.assertFalse(releases.exists())

    def test_license_failure_still_precedes_python_baseline_failure(self):
        with (patch.object(BUILD, 'pyinstaller_license_info', side_effect=RuntimeError('license first')),
             patch.object(BUILD.sys, 'argv', ['build_beta.py']),
             patch.object(BUILD, 'build_python_version') as interpreter,
             patch.object(BUILD, 'run') as run):
            with self.assertRaisesRegex(RuntimeError, 'license first'):
                BUILD.main()
        interpreter.assert_not_called()
        run.assert_not_called()

    def test_frozen_probe_uses_only_owned_path_fixed_argv_hidden_and_bounded(self):
        with patch.object(BUILD.subprocess, 'run', return_value=self.result()) as probe:
            self.assertEqual(BUILD.verify_frozen_runtime(self.expected()), self.runtime)
        args, kwargs = probe.call_args
        self.assertEqual(args, ([str(self.core), 'self-test', '--runtime-only'],))
        self.assertEqual(kwargs['cwd'], self.runtime)
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(kwargs['timeout'], 45)
        self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_missing_frozen_executable_is_not_launched(self):
        self.core.unlink()
        with patch.object(BUILD.subprocess, 'run') as probe:
            with self.assertRaisesRegex(RuntimeError, 'Owned frozen Core is missing'):
                BUILD.verify_frozen_runtime(self.expected())
        probe.assert_not_called()

    def test_invalid_json_and_nonobject_reports_fail_closed(self):
        for stdout in ('not JSON', '', '[]', 'null', '1'):
            with self.subTest(stdout=stdout), patch.object(
                    BUILD.subprocess, 'run', return_value=self.result(stdout=stdout)):
                with self.assertRaises(RuntimeError):
                    BUILD.verify_frozen_runtime(self.expected())

    def test_legacy_core_missing_runtime_is_rejected(self):
        report = self.report()
        del report['runtime']
        with patch.object(BUILD.subprocess, 'run', return_value=self.result(report)):
            with self.assertRaisesRegex(RuntimeError, 'runtime metadata is missing'):
                BUILD.verify_frozen_runtime(self.expected())

    def test_runtime_only_marker_is_required(self):
        for marker in (None, 'some other self-test'):
            report = self.report()
            if marker is None:
                del report['check']
            else:
                report['check'] = marker
            with self.subTest(marker=marker), patch.object(
                    BUILD.subprocess, 'run', return_value=self.result(report)):
                with self.assertRaisesRegex(RuntimeError, 'runtime-only check marker'):
                    BUILD.verify_frozen_runtime(self.expected())

    def test_failed_self_test_or_wrong_product_version_is_rejected(self):
        for result in (
            self.result(returncode=1), self.result(self.report(version='0.1.14-stale')),
            self.result(self.report(status='failed')),
        ):
            with self.subTest(result=result), patch.object(BUILD.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError):
                    BUILD.verify_frozen_runtime(self.expected())

    def test_old_prerelease_non_cpython_and_mismatched_frozen_python_are_rejected(self):
        for report in (
            self.report(python='3.13.3'), self.report(python='3.13.18rc1'),
            self.report(python='3.14.0'), self.report(implementation='pypy'),
            self.report(python='3.13.19'), self.report(runtime={}), self.report(runtime=None),
        ):
            with self.subTest(report=report), patch.object(
                    BUILD.subprocess, 'run', return_value=self.result(report)):
                with self.assertRaises(RuntimeError):
                    BUILD.verify_frozen_runtime(self.expected())

    def test_matching_future_frozen_patch_is_accepted(self):
        with patch.object(BUILD.subprocess, 'run', return_value=self.result(self.report(python='3.13.19'))):
            self.assertEqual(BUILD.verify_frozen_runtime(self.expected('3.13.19')), self.runtime)

    def test_frozen_libraries_must_match_actual_build_runtime(self):
        for library in ('openssl', 'sqlite', 'zlib'):
            for value in ('different fixture library', None):
                report = self.report()
                if value is None:
                    del report['runtime'][library]
                else:
                    report['runtime'][library] = value
                with self.subTest(library=library, value=value), patch.object(
                        BUILD.subprocess, 'run', return_value=self.result(report)):
                    with self.assertRaisesRegex(RuntimeError, f'Frozen Core {library} runtime'):
                        BUILD.verify_frozen_runtime(self.expected())

    def test_probe_timeout_process_and_decode_errors_fail_closed(self):
        for error in (
            subprocess.TimeoutExpired([str(self.core), 'self-test', '--runtime-only'], 45),
            OSError('fixture launch failure'), UnicodeError('fixture output decode failure'),
        ):
            with self.subTest(error=error), patch.object(BUILD.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, 'bounded runtime-only probe'):
                    BUILD.verify_frozen_runtime(self.expected())

    def test_failed_reuse_probe_preserves_existing_payload_and_does_not_assemble(self):
        stage = self.build / 'payload'
        stage.mkdir()
        sentinel = stage / 'keep.txt'
        sentinel.write_text('preserved', encoding='utf-8')
        releases = self.base / 'never-created-releases'
        with (patch.object(BUILD, 'pyinstaller_license_info', return_value=('6.22.3', self.base / 'COPYING')),
             patch.object(BUILD.sys, 'argv', ['build_beta.py', '--reuse-runtime']),
             patch.object(BUILD, 'build_runtime_info', return_value=self.expected()),
             patch.object(BUILD, 'runtime_modules', return_value=[]),
             patch.object(BUILD, 'RELEASES', releases), patch.object(BUILD, 'run') as build,
             patch.object(BUILD.subprocess, 'run', return_value=self.result(self.report(python='3.13.3'))),
             patch.object(BUILD.shutil, 'rmtree') as remove,
             patch.object(BUILD.shutil, 'copytree') as copy):
            with self.assertRaisesRegex(RuntimeError, 'Frozen Core runtime'):
                BUILD.main()
        build.assert_not_called()
        remove.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserved')
        self.assertFalse(releases.exists())

    def test_both_full_and_reuse_modes_probe_before_first_payload_write(self):
        for reuse in (False, True):
            events = []
            argv = ['build_beta.py'] + (['--reuse-runtime'] if reuse else [])
            def fake_build(_):
                events.append('pyinstaller')
            def fake_probe(*args, **kwargs):
                events.append('probe')
                return self.result()
            def stop_at_stage(*args, **kwargs):
                events.append('stage')
                raise RuntimeError('fixture stops before stage copy')
            with self.subTest(reuse=reuse), \
                 patch.object(BUILD, 'pyinstaller_license_info', return_value=('6.22.3', self.base / 'COPYING')), \
                 patch.object(BUILD.sys, 'argv', argv), \
                 patch.object(BUILD, 'build_runtime_info', return_value=self.expected()), \
                 patch.object(BUILD, 'runtime_modules', return_value=[]), \
                 patch.object(BUILD, 'RELEASES', self.base / 'releases'), \
                 patch.object(BUILD, 'run', side_effect=fake_build), \
                 patch.object(BUILD.subprocess, 'run', side_effect=fake_probe), \
                 patch.object(BUILD.shutil, 'copytree', side_effect=stop_at_stage):
                with self.assertRaisesRegex(RuntimeError, 'fixture stops before stage copy'):
                    BUILD.main()
            self.assertEqual(events, (['pyinstaller'] if not reuse else []) + ['probe', 'stage'])


if __name__ == '__main__':
    unittest.main()
