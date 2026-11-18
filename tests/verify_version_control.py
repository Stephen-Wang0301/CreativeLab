"""Check privacy boundaries, archive validation, drift protection and code recovery."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment'))
import manage
import server_release as release
from version_policy import allowed, check_contents, source_files


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='creative-version-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.old = {'run.py': b'old = True\n', 'study/http.py': b'old = True\n'}
        release.replace_source(self.root, {}, self.old)

    def test_private_paths_and_traversal_are_rejected(self):
        for name in ('README.md', 'data/sample.json', 'private/server.json', '.runtime/token',
                     'backups/site.zip', 'runtime/python.exe', 'deployment/sync-baseline.json',
                     '../run.py', '/run.py', 'study/../../data/key', 'study/a.key', 'study/.env',
                     'study\\http.py', 'C:/run.py'):
            with self.subTest(name=name):
                self.assertFalse(allowed(name))
        for name in ('run.py', 'study/web/research.js', '.github/README.md', 'deployment/manage.py'):
            self.assertTrue(allowed(name))

    def test_credential_checks_report_names_without_values(self):
        secret = b'fictional-key-for-unit-test'
        result = check_contents([('run.py', b'x=' + secret)], {secret})
        self.assertEqual(result[0][0], 'run.py')
        self.assertNotIn(secret.decode(), str(result))
        for value in (b'ghp' + b'_' + b'x' * 36, b'#' + b'invite=' + b'x' * 16):
            self.assertTrue(check_contents([('run.py', value)]))

    def test_actual_git_ignore_excludes_private_files(self):
        project = Path(__file__).resolve().parents[1]
        (self.root / '.gitignore').write_bytes((project / '.gitignore').read_bytes())
        for name in ('README.md', 'data/_system/researcher.key', 'private/server.json',
                     '.runtime/cache.json', 'deployment/sync-baseline.json', 'deployment/cloudflared.yml'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        names = source_files(self.root)
        self.assertIn('run.py', names)
        self.assertIn('study/http.py', names)
        self.assertTrue(all(allowed(name) for name in names))

    def test_deleted_private_file_is_still_blocked_in_git_history(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        for key, value in [('user.name', 'Version test'), ('user.email', 'test@example.invalid'),
                           ('core.autocrlf', 'false')]:
            subprocess.run(['git', '-C', str(self.root), 'config', key, value], check=True)
        private = self.root / 'README.md'
        private.write_text('fictional private operator guide')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'temporary test fixture'], check=True)
        private.unlink()
        subprocess.run(['git', '-C', str(self.root), 'add', '-u'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'remove temporary private file'], check=True)
        with patch.object(manage, 'BASE', self.root), redirect_stdout(io.StringIO()) as captured:
            with self.assertRaisesRegex(RuntimeError, 'Private-source check failed'):
                manage.check(SimpleNamespace(staged=True, history=True))
        self.assertIn('README.md', captured.getvalue())
        self.assertNotIn('fictional private operator guide', captured.getvalue())

    def test_archive_traversal_is_blocked_before_extraction(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('../outside.py', 'bad')
        with self.assertRaises(RuntimeError):
            release.read_archive(archive)
        self.assertFalse((self.root.parent / 'outside.py').exists())

    def test_bad_hashes_and_duplicate_entries_are_rejected(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('release.json', json.dumps({'files': {'run.py': 'wrong'}}))
            for name, content in self.old.items():
                bundle.writestr('source/' + name, content)
        with self.assertRaisesRegex(RuntimeError, 'hashes'):
            release.read_archive(archive, release=True)

    def test_server_drift_stops_before_backup_or_service_stop(self):
        with patch.object(release, 'service') as service, patch.object(release, 'preflight') as preflight:
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                release.apply(self.root, 'creative-study', self.old, {}, {})
            service.assert_not_called()
            preflight.assert_not_called()

    def test_failed_deployment_restores_code_and_preserves_live_data(self):
        data = self.root / 'data/participant.txt'
        data.parent.mkdir()
        data.write_bytes(b'newest answers')
        private = self.root / 'private/cloudflared.token'
        private.parent.mkdir()
        private.write_bytes(b'private token remains')
        (self.root / '.runtime').mkdir()
        manifest = {'id': '20261118T120000000000Z-0123456789', 'kind': 'working-test'}
        new = {'run.py': b'new = True\n', 'study/http.py': b'new = True\n', 'study/new.py': b'x=1\n'}
        with patch.object(release, 'preflight'), patch.object(release, 'service'), \
             patch.object(release.subprocess, 'run', return_value=SimpleNamespace(stdout='{"archive":"private-backup"}')), \
             patch.object(release, 'wait_healthy', side_effect=[RuntimeError('failed start'), None]):
            with self.assertRaisesRegex(RuntimeError, 'Previous code is running'):
                release.apply(self.root, 'creative-study', new, manifest, release.fingerprints(self.old))
        self.assertEqual(release.source(self.root), self.old)
        self.assertEqual(data.read_bytes(), b'newest answers')
        self.assertEqual(private.read_bytes(), b'private token remains')
        self.assertFalse((self.root / 'study/new.py').exists())

    def test_removed_source_files_do_not_remove_data(self):
        data = self.root / 'data/record.json'
        data.parent.mkdir()
        data.write_bytes(b'{}')
        release.replace_source(self.root, self.old, {'run.py': b'updated'})
        self.assertFalse((self.root / 'study/http.py').exists())
        self.assertEqual(data.read_bytes(), b'{}')

    def test_incompatible_rollback_cannot_restart_frozen_timers_or_unseal_restarted_attempts(self):
        path = self.root / 'data/_system/study.sqlite3'
        path.parent.mkdir(parents=True)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE participants(data TEXT)')
            db.execute('INSERT INTO participants VALUES (?)', ('{"phase":"main_draft","pause":{"remaining":12}}',))
            db.commit()
        with patch.object(release, 'preflight') as preflight, patch.object(release, 'service') as service:
            with self.assertRaisesRegex(RuntimeError, 'cannot read paused/restarted'):
                release.apply(self.root, 'creative-study', self.old, {}, release.fingerprints(self.old))
            preflight.assert_not_called()
            service.assert_not_called()
        release.check_data_compatibility(self.root, {'study/participation.py': b'', 'study/engine.py': b"TERMINAL={'restarted'}"})


if __name__ == '__main__':
    unittest.main(verbosity=2)
