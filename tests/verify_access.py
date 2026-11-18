"""Personal attempt isolation, researcher recovery and dashboard request contracts."""

import copy
import io
import json
from pathlib import Path
import re
import secrets
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_flow import Clock, Client, DRAFT, HEADERS
from study.engine import Study
from study.http import create_apps
from study.research import Research
from study.storage import Store
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment'))
from backup import snapshot_data
import tempfile


class AccessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='creative-access-')
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.addCleanup(self.store.db.close)
        self.clock = Clock()
        self.study = Study(self.store, clock=self.clock)
        self.public, self.admin = create_apps(self.study, {'local_url': 'http://127.0.0.1:8765'})
        self.research = Research(self.study)
        self.owner = self.admin.test_client()
        self.assertEqual(self.owner.post('/research/api/login', headers=HEADERS,
            json={'key': self.store.admin_key}).status_code, 200)

    def issue(self):
        result = self.owner.post('/research/api/pilot/invitations', headers=HEADERS,
                                 json={'revision': self.study.settings('pilot')['active']['id']})
        self.assertEqual(result.status_code, 200)
        return result.get_json()

    def headers(self, invitation):
        return {**HEADERS, 'X-Study-Invitation': invitation['token']}

    def body(self, invitation):
        return {'invite': invitation['token'], 'nonce': secrets.token_hex(16), 'consent': True, 'adult': True}

    def client(self):
        return Client(self.public, self.study.settings('pilot')['invite'])

    def recover(self, pid, serial=None):
        bundle = self.research.detail('pilot', pid)
        return self.owner.post('/research/api/pilot/person/' + pid + '/recover', headers=HEADERS,
            json={'expected_serial': bundle['progress']['serial'] if serial is None else serial,
                  'reason': '隔离验证：测试者联系研究人员后协助继续。'})

    def test_two_invitations_in_one_browser_are_independent_and_reopen_same_attempt(self):
        first, second = self.issue(), self.issue()
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(first['label'], first['participant_code'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM participants').fetchone()[0], 0)
        browser = self.public.test_client()
        a = browser.post('/p/pilot/api/join', headers=self.headers(first), json=self.body(first)).get_json()
        b = browser.post('/p/pilot/api/join', headers=self.headers(second), json=self.body(second)).get_json()
        self.assertEqual(a['view']['participant_code'], 'S0-V0001-P000001')
        self.assertEqual(b['view']['participant_code'], 'S0-V0001-P000002')
        self.assertNotEqual(a['view']['id'], b['view']['id'])
        for invitation, answer in ((first, a), (second, b), (first, a)):
            state = browser.get('/p/pilot/api/state', headers=self.headers(invitation)).get_json()
            self.assertEqual(state['id'], answer['view']['id'])
        guest = self.public.test_client()
        reopened = guest.post('/p/pilot/api/entry', headers=self.headers(first), json={'invite': first['token']})
        self.assertEqual(reopened.get_json()['view']['id'], a['view']['id'])
        self.assertEqual(browser.get('/p/pilot/api/state', headers=self.headers(second)).get_json()['id'], b['view']['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM participants').fetchone()[0], 2)

    def test_new_personal_invite_does_not_reuse_existing_legacy_cookie(self):
        browser = self.public.test_client()
        legacy = {'invite': self.study.settings('pilot')['invite'], 'nonce': secrets.token_hex(16), 'consent': True, 'adult': True}
        original = browser.post('/p/pilot/api/join', headers=HEADERS, json=legacy).get_json()['view']
        invitation = self.issue()
        entry = browser.post('/p/pilot/api/entry', headers=self.headers(invitation), json={'invite': invitation['token']})
        self.assertIn('landing', entry.get_json())
        fresh = browser.post('/p/pilot/api/join', headers=self.headers(invitation), json=self.body(invitation)).get_json()['view']
        self.assertNotEqual(fresh['id'], original['id'])
        self.assertEqual(browser.get('/p/pilot/api/state').get_json()['id'], original['id'])
        self.assertEqual(browser.get('/p/pilot/api/state', headers=self.headers(invitation)).get_json()['id'], fresh['id'])

    def test_readable_label_and_forged_signature_cannot_authorize_enrollment(self):
        invitation = self.issue()
        client = self.public.test_client()
        for value in (invitation['label'], invitation['token'][:-1] + ('0' if invitation['token'][-1] != '0' else '1')):
            response = client.post('/p/pilot/api/join', headers=HEADERS,
                json={'invite': value, 'nonce': secrets.token_hex(16), 'consent': True, 'adult': True})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM participants').fetchone()[0], 0)

    def test_personal_double_submit_is_idempotent_and_old_unused_invite_cannot_enter_new_version(self):
        invitation = self.issue()
        a, b = self.public.test_client(), self.public.test_client()
        first = a.post('/p/pilot/api/join', headers=self.headers(invitation), json=self.body(invitation)).get_json()
        second = b.post('/p/pilot/api/join', headers=self.headers(invitation), json=self.body(invitation)).get_json()
        self.assertEqual(first['view']['id'], second['view']['id'])
        self.assertEqual(first['resume']['code'], second['resume']['code'])
        unused = self.issue()
        protocol = self.study.settings('pilot')['active']['protocol']
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': False})
        self.study.change_settings('pilot', {'op': 'revision', 'protocol': protocol})
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': True})
        response = self.public.test_client().post('/p/pilot/api/join', headers=self.headers(unused), json=self.body(unused))
        self.assertEqual(response.status_code, 409)
        reopened = a.post('/p/pilot/api/entry', headers=self.headers(invitation), json={'invite': invitation['token']})
        self.assertEqual(reopened.get_json()['view']['version_code'], '0.1')

    def test_incomplete_recovery_preserves_partial_work_history_and_identity(self):
        participant = self.client(); credentials = participant.join(); pid = credentials['id']
        participant.act('choose', intent=participant.state['intentions'][0]['id'], experience='some')
        participant.act('start')
        partial = {'title': '已保存的半成品', 'concept': '尚未写完', 'elements': ['窗户']}
        participant.act('save', content=partial)
        self.clock.now = participant.state['deadline']
        participant.get()
        self.assertEqual(participant.state['phase'], 'incomplete')
        before = self.research.detail('pilot', pid)
        old = copy.deepcopy(before['responses'])
        timeout_ids = [v['id'] for v in before['versions'] if v['type'] == 'frozen_timeout']
        result = self.recover(pid)
        self.assertEqual(result.status_code, 200, result.get_json())
        after = self.research.detail('pilot', pid)
        self.assertEqual(after['progress']['participant_code'], before['progress']['participant_code'])
        self.assertEqual(after['progress']['version_code'], '0.1')
        self.assertEqual(after['progress']['recovery_count'], 1)
        snapshot = next(v for v in after['versions'] if v['type'] == 'admin_recovery_snapshot')
        self.assertEqual(snapshot['contents'], old)
        self.assertEqual([v['id'] for v in after['versions'] if v['type'] == 'frozen_timeout'], timeout_ids)
        participant.get(); self.assertEqual(participant.state['phase'], 'main_draft')
        self.assertIsNone(participant.state['deadline'])
        participant.act('start')
        self.assertEqual(participant.state['work'], partial)
        participant.act('submit', content=DRAFT)
        self.assertEqual(participant.state['phase'], 'main_edit')

    def test_active_edit_recovery_keeps_group_and_saved_edit_and_stale_requests_fail(self):
        participant = self.client(); pid = participant.join()['id']; participant.draft()
        edited = {'title': '正在编辑的稿件', 'concept': '保留这个修改', 'elements': ['屋顶', '窗户']}
        participant.act('save', content=edited)
        before = self.research.detail('pilot', pid)
        stale = before['progress']['serial']
        participant.act('save', content={**edited, 'concept': '最新一次修改'})
        self.assertEqual(self.recover(pid, stale).status_code, 409)
        original_group = before['responses']['main']['group']
        self.assertEqual(self.recover(pid).status_code, 200)
        participant.get(); participant.act('start')
        self.assertEqual(participant.state['work']['concept'], '最新一次修改')
        after = self.research.detail('pilot', pid)
        self.assertEqual(after['responses']['main']['group'], original_group)
        self.assertEqual(sum(e['type'] == 'main_randomized' for e in after['events']), 1)

    def test_intentional_withdrawal_is_not_reopened_and_admin_routes_require_login(self):
        participant = self.client(); pid = participant.join()['id']
        participant.act('withdraw', reason='自行结束本次体验')
        self.assertEqual(self.recover(pid).status_code, 409)
        anonymous = self.admin.test_client()
        self.assertEqual(anonymous.get('/research/api/pilot/dashboard').status_code, 401)
        self.assertEqual(anonymous.post('/research/api/pilot/invitations', headers=HEADERS,json={}).status_code,401)
        self.assertEqual(anonymous.post('/research/api/pilot/person/'+pid+'/recover',headers=HEADERS,json={}).status_code,401)

    def test_dashboard_is_one_compact_read_and_protocol_page_keeps_full_editor(self):
        compact = self.owner.get('/research/api/pilot/dashboard?page=overview')
        self.assertEqual(compact.status_code, 200)
        data = compact.get_json()
        self.assertEqual(data['overview']['batch']['version_code'], '0.1')
        self.assertNotIn('protocol', data['settings']['active'])
        self.assertNotIn('draft', data['settings'])
        self.assertIn('Server-Timing', compact.headers)
        self.assertEqual(compact.headers['Cache-Control'], 'no-store')
        full = self.owner.get('/research/api/pilot/dashboard?page=protocol').get_json()
        self.assertIn('protocol', full['settings']['active'])
        self.assertIn('draft', full['settings'])
        self.assertIn('contact', self.study.settings('pilot')['active']['protocol'])
        with patch('study.storage.os.fsync') as fsync:
            self.store.sync_catalog()
            fsync.assert_not_called()

    def test_personal_tokens_are_reconstructed_and_not_in_raw_exports(self):
        invitation = self.issue()
        browser = self.public.test_client()
        browser.post('/p/pilot/api/join', headers=self.headers(invitation), json=self.body(invitation))
        listed = self.owner.get('/research/api/pilot/invitations?revision=1').get_json()['invitations']
        self.assertEqual(listed, [])
        pid = self.store.db.execute('SELECT id FROM participants').fetchone()[0]
        link = self.owner.post('/research/api/pilot/person/' + pid + '/continue-link', headers=HEADERS, json={})
        self.assertEqual(link.get_json()['token'], invitation['token'])
        columns = {r['name'] for r in self.store.db.execute('PRAGMA table_info(invitations)')}
        self.assertNotIn('token', columns)
        response = self.owner.get('/research/api/pilot/raw.zip?revision=1')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            for name in archive.namelist():
                self.assertNotIn(invitation['token'].encode(), archive.read(name))
        response.close()

    def test_unused_and_used_invitations_survive_private_backup(self):
        used, unused = self.issue(), self.issue()
        browser = self.public.test_client()
        browser.post('/p/pilot/api/join', headers=self.headers(used), json=self.body(used))
        destination = self.store.root / 'private-snapshot'
        # Keep the output outside live data, as in the production backup path.
        with tempfile.TemporaryDirectory(prefix='creative-invite-backup-') as folder:
            destination = Path(folder) / 'data'
            snapshot_data(self.store.root, destination)
            restored = Store(destination)
            try:
                study = Study(restored, clock=self.clock)
                for invitation in (used, unused):
                    row = study.access.resolve(restored.db, 'pilot', invitation['token'])
                    self.assertIsNotNone(row)
                    self.assertEqual(row['label'], invitation['label'])
                self.assertEqual(restored.db.execute('SELECT count(*) FROM participants').fetchone()[0], 1)
            finally:
                restored.db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
