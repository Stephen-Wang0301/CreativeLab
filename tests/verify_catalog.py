"""Verify per-version retention, naming, migration, resumption and backup coverage."""

from contextlib import closing
import copy
import io
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run import dependencies
dependencies()
from study.engine import Study, StudyError
from study.research import Research
from study.storage import Store
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment'))
from backup import snapshot_data


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='creative-catalog-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / 'data'
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.study = Study(self.store, clock=lambda: 1_800_000_000)
        self.research = Research(self.study)

    def join(self, mode='pilot'):
        return self.study.join(mode, {'consent': True, 'adult': True,
            'invite': self.study.settings(mode)['invite'], 'nonce': secrets.token_hex(16)})

    def publish(self, mode='pilot', contact=None):
        settings = self.study.settings(mode)
        if settings['recruiting']:
            self.study.change_settings(mode, {'op': 'recruiting', 'value': False})
        config = copy.deepcopy(settings['active']['protocol'] if settings['active'] else settings['draft'])
        if contact:
            config['contact'] = contact
        if mode == 'formal':
            config['contact'] = '请通过研究组公布的邮箱联系。'
            config['consent'] = '正式研究知情说明：自愿参加，可随时退出。'
        answer = self.study.change_settings(mode, {'op': 'revision', 'protocol': config,
            'editor_version': settings['editor_version'], 'confirmed': True})
        self.study.change_settings(mode, {'op': 'recruiting', 'value': True})
        return answer['active']

    def test_versions_keep_separate_participants_and_frozen_protocols(self):
        pid1, recovery, token = self.join()
        original = self.store.db.execute('SELECT data FROM participants WHERE id=?', (pid1,)).fetchone()[0]
        snapshot = self.root / 'batches/pilot/v0.1/protocol.json'
        before = snapshot.read_bytes()
        self.publish(contact='这是第二版的联系方式。')
        pid2, _, token2 = self.join()
        self.assertEqual(self.research.overview('pilot', '0.1')['enrolled'], 1)
        self.assertEqual(self.research.overview('pilot', '0.2')['enrolled'], 1)
        self.assertEqual(self.research.overview('pilot', None)['batch']['version_code'], '0.2')
        old, new = self.study.view('pilot', token), self.study.view('pilot', token2)
        self.assertEqual(old['version_code'], '0.1')
        self.assertEqual(new['version_code'], '0.2')
        self.assertEqual(old['participant_code'], 'S0-V0001-P000001')
        self.assertEqual(new['participant_code'], 'S0-V0002-P000001')
        self.assertNotEqual(pid1, pid2)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(self.store.db.execute('SELECT data FROM participants WHERE id=?', (pid1,)).fetchone()[0], original)
        for code, version in [(old['participant_code'], '0.1'), (new['participant_code'], '0.2')]:
            folder = self.root / 'batches/pilot' / ('v' + version) / 'participants' / code
            self.assertTrue((folder / 'identity.json').is_file())
            self.assertEqual(json.loads((folder / 'progress.json').read_text())['version_code'], version)
        resumed = self.study.resume('pilot', {'id': old['participant_code'], 'code': recovery})
        self.assertEqual(self.study.view('pilot', resumed)['version_code'], '0.1')
        resumed2 = self.study.resume('pilot', {'id': pid1, 'code': recovery})
        self.assertEqual(self.study.view('pilot', resumed2)['participant_code'], old['participant_code'])

    def test_drafts_do_not_allocate_versions_and_formal_starts_at_one(self):
        settings = self.study.settings('pilot')
        for number in range(3):
            config = copy.deepcopy(settings['active']['protocol'])
            config['contact'] = f'草稿调整第 {number} 次。'
            settings = self.study.change_settings('pilot', {'op': 'save_draft', 'protocol': config,
                'editor_version': settings['editor_version']})
        self.assertEqual(settings['active']['version_code'], '0.1')
        self.assertEqual(settings['next_version'], '0.2.1')
        self.assertEqual(len(settings['revisions']), 1)
        formal = self.publish('formal')
        self.assertEqual(formal['version_code'], '1.1')
        _, _, token = self.join('formal')
        self.assertEqual(self.study.view('formal', token)['participant_code'], 'S1-V0001-P000001')
        self.assertEqual(self.research.overview('pilot', '0.1')['enrolled'], 0)
        with self.assertRaises(StudyError):
            self.research.overview('pilot', '1.1')

    def test_sequence_is_integer_not_decimal_and_never_reuses_versions(self):
        for _ in range(10):
            self.publish()
        versions = self.study.settings('pilot')['revisions']
        self.assertEqual(versions[0]['version_code'], '0.11')
        self.assertEqual(versions[1]['version_code'], '0.10')
        self.assertEqual(len({v['version_code'] for v in versions}), 11)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('DELETE FROM revisions WHERE id=1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE revisions SET protocol='{}' WHERE id=1")

    def test_cap_applies_to_each_version_and_identity_cannot_move(self):
        config = copy.deepcopy(self.study.settings('pilot')['active']['protocol'])
        config['cap'] = 1
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': False})
        self.study.change_settings('pilot', {'op': 'revision', 'protocol': config})
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': True})
        pid, _, _ = self.join()
        with self.assertRaises(StudyError):
            self.join()
        self.publish()
        self.join()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('UPDATE participants SET revision=1 WHERE id=?', (pid,))

    def test_backup_includes_all_batches_and_unpublished_draft(self):
        self.join()
        self.publish()
        self.join()
        settings = self.study.settings('pilot')
        draft = copy.deepcopy(settings['active']['protocol'])
        draft['contact'] = '尚未发布的草稿也必须备份。'
        self.study.change_settings('pilot', {'op': 'save_draft', 'protocol': draft,
            'editor_version': settings['editor_version']})
        destination = Path(self.folder.name) / 'snapshot'
        counts = snapshot_data(self.root, destination)
        self.assertEqual([batch['participants'] for batch in counts['.']['batches']], [1, 1])
        self.assertTrue((destination / 'batches/pilot/v0.1/protocol.json').exists())
        self.assertTrue((destination / 'batches/pilot/v0.2/protocol.json').exists())
        with closing(sqlite3.connect(destination / '_system/study.sqlite3')) as db:
            self.assertEqual(json.loads(db.execute('SELECT protocol FROM protocol_drafts').fetchone()[0])['contact'], draft['contact'])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_legacy_migration_preserves_ids_keys_answers_images_and_drafts(self):
        pid, recovery, token = self.join()
        row = dict(self.store.db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone())
        version = self.store.db.execute('SELECT * FROM revisions WHERE id=1').fetchone()
        legacy = Path(self.folder.name) / 'legacy-data'
        (legacy / '_system').mkdir(parents=True)
        for name in ('server.key', 'researcher.key'):
            (legacy / '_system' / name).write_bytes((self.root / '_system' / name).read_bytes())
        fields = [key for key in row if key not in {'participant_no', 'participant_code'}]
        with closing(sqlite3.connect(legacy / '_system/study.sqlite3')) as db, db:
            db.executescript('''CREATE TABLE modes (id TEXT PRIMARY KEY,revision INTEGER,recruiting INTEGER,invite TEXT);
                CREATE TABLE revisions(id INTEGER PRIMARY KEY,mode TEXT,created REAL,protocol TEXT,image_provider TEXT);
                CREATE TABLE protocol_drafts(mode TEXT PRIMARY KEY,token TEXT,protocol TEXT);
                CREATE TABLE participants(id TEXT PRIMARY KEY,mode TEXT,revision INTEGER,created REAL,updated REAL,
                    token TEXT,resume TEXT,nonce TEXT,blind TEXT,data TEXT,serial INTEGER,mirrored INTEGER);''')
            db.execute('INSERT INTO revisions VALUES (?,?,?,?,?)', tuple(version[k] for k in ('id','mode','created','protocol','image_provider')))
            db.execute('INSERT INTO modes VALUES (?,?,?,?)', ('pilot',1,1,self.study.settings('pilot')['invite']))
            db.execute('INSERT INTO participants ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')', [row[k] for k in fields])
            db.execute('INSERT INTO protocol_drafts VALUES (?,?,?)', ('pilot','private-draft-token',version['protocol']))
        image = legacy / 'pilot' / pid / 'images/legacy.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'legacy image bytes')
        migrated = Store(legacy)
        try:
            self.assertEqual(dict(migrated.db.execute('SELECT * FROM participants').fetchone())['data'], row['data'])
            self.assertEqual(migrated.admin_key, self.store.admin_key)
            self.assertEqual(migrated.db.execute('SELECT token FROM protocol_drafts').fetchone()[0], 'private-draft-token')
            new = migrated.db.execute('SELECT * FROM participants').fetchone()
            self.assertEqual(new['id'], pid)
            self.assertEqual(new['resume'], row['resume'])
            self.assertEqual(new['participant_code'], 'S0-V0001-P000001')
            self.assertEqual((migrated.participant_path(new) / 'images/legacy.png').read_bytes(), image.read_bytes())
            self.assertTrue(image.exists())
            resumed = Study(migrated).resume('pilot', {'id': pid, 'code': recovery})
            self.assertEqual(Study(migrated).view('pilot', resumed)['version_code'], '0.1')
        finally:
            migrated.db.close()
        again = Store(legacy)
        try:
            self.assertEqual(again.db.execute('SELECT participant_code FROM participants').fetchone()[0], 'S0-V0001-P000001')
        finally:
            again.db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
