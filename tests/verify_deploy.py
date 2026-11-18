"""Check that 004 deployment copies records and respects existing directories."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run import exclusive_root
from study.deploy import INSTALL_DIRS, INSTALL_FILES, PROJECT, install


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='creative-deploy-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.source = root / 'staging' / PROJECT.name
        self.source.mkdir(parents=True)
        for name in INSTALL_FILES:
            (self.source / name).write_text('version-004', encoding='ascii')
        for name in INSTALL_DIRS:
            (self.source / name).mkdir()
            (self.source / name / 'example').write_text('local files', encoding='ascii')
        self.development = root / 'Development'
        self.target = self.development / 'Projects' / 'python_project' / 'PhDWorking' / PROJECT.name
        self.target.parent.mkdir(parents=True)
        self.data = root / 'old' / 'data'
        (self.data / '_system').mkdir(parents=True)
        (self.data / '_system' / 'study.sqlite3').write_bytes(b'database')
        (self.data / 'quick-demo').mkdir()
        (self.data / 'quick-demo' / 'sample.json').write_bytes(b'{"state":"complete"}')

    def deploy(self, source=None):
        return install(source or self.source, self.target, self.development, self.data, exclusive_root)

    def test_copies_records_and_inventory_and_can_rerun(self):
        (self.development / 'anaconda3' / 'envs' / 'private-environment').mkdir(parents=True)
        path, _, report = self.deploy()
        self.assertEqual(path, self.target)
        self.assertEqual((path / 'data' / '_system' / 'study.sqlite3').read_bytes(), b'database')
        self.assertTrue((self.data / 'quick-demo' / 'sample.json').exists())
        self.assertFalse((path / 'data' / '.server.lock').exists())
        content = report.read_text(encoding='utf-8')
        self.assertIn('PhDWorking', content)
        self.assertIn('anaconda3', content)
        self.assertNotIn('private-environment', content)
        (path / 'data' / 'quick-demo' / 'sample.json').write_bytes(b'current')
        self.deploy()
        self.assertEqual((path / 'data' / 'quick-demo' / 'sample.json').read_bytes(), b'current')

    def test_existing_directory_is_preserved(self):
        self.target.mkdir()
        existing = self.target / 'other-project.txt'
        existing.write_text('existing', encoding='ascii')
        with self.assertRaisesRegex(RuntimeError, '目标已存在'):
            self.deploy()
        self.assertEqual(existing.read_text(encoding='ascii'), 'existing')
        self.assertFalse((self.target / 'run.py').exists())

    def test_quick_session_in_use_blocks_migration(self):
        with exclusive_root(self.data / 'quick-demo'):
            with self.assertRaises(RuntimeError):
                self.deploy()
        self.assertFalse(self.target.exists())
        self.deploy()
        self.assertTrue(self.target.exists())

    def test_direct_extraction_can_import_old_data(self):
        self.source.rename(self.target)
        self.deploy(self.target)
        self.assertEqual((self.target / 'data' / '_system' / 'study.sqlite3').read_bytes(), b'database')

    def test_failed_copy_leaves_no_partial_target(self):
        with patch('study.deploy.shutil.copy2', side_effect=OSError('write failure')):
            with self.assertRaises(OSError):
                self.deploy()
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
