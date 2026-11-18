"""Pause/restart timing, reserved IDs, privacy and feature release compatibility."""
import concurrent.futures
import copy
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_flow import Client, Clock, DRAFT, HEADERS
from study.engine import Study
from study.http import create_apps
from study.storage import Store
from study.research import Research


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='creative-pause-')
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'data')
        self.addCleanup(self.store.db.close)
        self.clock = Clock()
        self.study = Study(self.store, clock=self.clock)
        self.public, self.admin, *_ = create_apps(self.study, {'local_url': 'http://localhost:8765'})
        self.research = Research(self.study)
        self.client = Client(self.public, self.study.settings('pilot')['invite'])
        self.pid = self.client.join()['id']

    def write(self):
        c = self.client
        c.act('choose', intent=c.state['intentions'][0]['id'], experience='some')
        c.act('start')

    def test_pause_saves_unsent_draft_freezes_and_survives_application_restart(self):
        self.write()
        deadline = self.client.state['deadline']
        self.clock.now += 12.25
        self.client.act('pause', content=DRAFT)
        self.assertEqual(self.client.state['work'], DRAFT)
        remaining = deadline - self.clock.now
        self.assertEqual(self.client.state['pause']['remaining'], remaining)
        self.assertIsNone(self.client.state['deadline'])
        self.clock.now += 10_000
        self.study.tick()
        self.client.get()
        self.assertEqual(self.client.state['phase'], 'main_draft')
        self.client.act('submit', content=DRAFT, status=409)
        # Rebuild the application around the same durable DB, not a browser timer.
        Study(self.store, clock=self.clock)
        self.client.get()
        self.client.act('continue')
        self.assertEqual(self.client.state['deadline'], self.clock.now + remaining)
        self.clock.now += 5
        self.client.act('submit', content=DRAFT)
        bundle = self.research.detail('pilot', self.pid)
        saved = next(e for e in bundle['events'] if e['type'] == 'phase_submitted')
        self.assertEqual(saved['details']['effective_seconds'], 17.25)
        self.assertEqual(bundle['progress']['pause_seconds'], 10_000)

    def test_archive_exit_keeps_paused_record_and_continue_link_does_not_recover(self):
        self.write()
        self.client.act('pause', content=DRAFT)
        view = self.client.act('archive_exit')
        invite = view['continuation_invitation']
        self.assertTrue(view['pause']['archived'])
        browser = self.public.test_client()
        self.clock.now += 20_000
        headers = {**HEADERS, 'X-Study-Invitation': invite['token']}
        entry = browser.post('/p/pilot/api/entry', headers=headers, json={'invite': invite['token']}).get_json()['view']
        self.assertEqual(entry['id'], self.pid)
        self.assertTrue(entry['pause'])
        self.assertEqual(entry['work'], DRAFT)
        self.assertEqual(entry['recovery_count'], 0)
        before = self.store.db.execute('SELECT data FROM participants WHERE id=?', (self.pid,)).fetchone()[0]
        self.study.access.create('pilot', participant=self.pid)
        self.assertEqual(self.store.db.execute('SELECT data FROM participants WHERE id=?', (self.pid,)).fetchone()[0], before)
        self.assertFalse(self.study.recovery.preview(self.store.db.execute('SELECT * FROM participants').fetchone(), json.loads(before))['allowed'])

    def test_restart_requires_server_wait_preserves_old_and_allocates_after_admin(self):
        self.write()
        self.client.act('pause', content=DRAFT)
        original = copy.deepcopy(self.research.detail('pilot', self.pid)['responses'])
        self.client.act('restart_prepare')
        challenge = self.client.state['restart_challenge']['token']
        self.client.act('restart_confirm', challenge=challenge, status=409)
        self.clock.now += 9.99
        self.client.act('restart_confirm', challenge=challenge, status=409)
        admin_invite = self.study.access.create('pilot')
        self.assertEqual(admin_invite['participant_code'], 'S0-V0001-P000002')
        self.clock.now += .02
        command = secrets.token_hex(16)
        view = self.client.act('restart_confirm', challenge=challenge, command=command)
        invitation = view['restart_invitation']
        self.assertEqual(invitation['participant_code'], 'S0-V0001-P000003')
        self.assertEqual(view['phase'], 'restarted')
        again = self.client.act('restart_confirm', challenge=challenge, command=command)
        self.assertEqual(again['restart_invitation']['id'], invitation['id'])
        old = self.research.detail('pilot', self.pid)
        self.assertEqual(old['responses']['work'], DRAFT)
        self.assertEqual(old['versions'][-1]['type'], 'restart_snapshot')
        self.assertEqual(old['versions'][-1]['contents']['main'], original['main'])
        self.assertFalse(old['recovery']['allowed'])
        self.client.act('start', status=409)
        other = Client(self.public, invitation['token'])
        credentials = other.join()
        self.assertEqual(other.state['participant_code'], 'S0-V0001-P000003')
        self.assertEqual(other.state['phase'], 'main_brief')
        new = self.research.detail('pilot', credentials['id'])
        self.assertEqual(new['responses']['work'], {})
        self.assertEqual(new['responses']['restart_from'], self.pid)
        self.assertNotEqual(credentials['id'], self.pid)
        self.assertIsNone(self.research.score_records('pilot', None)[0]['h1'])

    def test_cancel_and_stale_tabs_cannot_restart_or_overwrite_paused_draft(self):
        self.write()
        old_seq = self.client.state['seq']
        self.client.act('pause', content=DRAFT)
        self.client.act('save', seq=old_seq, content={**DRAFT, 'title': '旧页面'}, status=409)
        self.client.act('restart_prepare')
        challenge = self.client.state['restart_challenge']['token']
        self.client.act('restart_cancel')
        self.clock.now += 11
        self.client.act('restart_confirm', challenge=challenge, status=409)
        self.client.act('continue')
        self.assertEqual(self.client.state['work'], DRAFT)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM participants').fetchone()[0], 1)

    def test_all_entry_paths_use_one_counter_and_unused_list_is_paginated(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            issued = list(pool.map(lambda _: self.study.access.create('pilot'), range(21)))
        codes = {v['participant_code'] for v in issued}
        self.assertEqual(len(codes), 21)
        self.assertNotIn(self.client.state['participant_code'], codes)
        self.assertEqual(len(self.study.access.list('pilot', 1)), 11)  # ten + has-more sentinel
        invitation = issued[0]
        other = Client(self.public, invitation['token']); other.join()
        self.assertNotIn(invitation['id'], [v['id'] for v in self.study.access.list('pilot', 1, 10)])
        self.assertEqual(other.state['participant_code'], invitation['participant_code'])
        self.assertIsNotNone(self.study.access.resolve(self.store.db, 'pilot', invitation['token']))

    def test_feature_release_does_not_publish_draft_renumber_or_rewrite_snapshots(self):
        settings = self.study.settings('pilot')
        protocol = self.store.root / 'batches/pilot/v0.1/protocol.json'
        identity = self.store.participant_path(self.store.db.execute('SELECT * FROM participants').fetchone()) / 'identity.json'
        before = protocol.read_bytes(), identity.read_bytes()
        draft = copy.deepcopy(settings['active']['protocol']); draft['contact'] = '尚未发布的私有草稿。'
        self.study.change_settings('pilot', {'op': 'save_draft', 'protocol': draft, 'editor_version': settings['editor_version']})
        Study(self.store, clock=self.clock)
        settings = self.study.settings('pilot')
        self.assertEqual(settings['active']['release_code'], '0.1.1')
        self.assertEqual(settings['next_version'], '0.2.1')
        self.assertNotEqual(settings['active']['protocol']['contact'], draft['contact'])
        self.assertEqual(settings['saved_draft']['contact'], draft['contact'])
        self.assertEqual(self.research.overview('pilot', '0.1.1')['enrolled'], 1)
        self.assertEqual((protocol.read_bytes(), identity.read_bytes()), before)
        self.assertTrue((protocol.parent / 'releases/0.1.1.json').is_file())

    def test_pause_preserves_condition_restore_dialog_and_untimed_form_fields(self):
        c = self.client
        c.draft()
        before = self.research.detail('pilot', self.pid)['responses']['main']
        c.act('pause', content=DRAFT)
        self.clock.now += 21
        c.act('continue')
        after = self.research.detail('pilot', self.pid)['responses']['main']
        self.assertEqual(after, before)
        c.act('submit', content=DRAFT)
        c.act('pause', page_draft={'changed': 'no', 'reason': '未提交的回顾', 'unexpected': '丢弃'})
        c.get(); self.assertEqual(c.state['page_draft'], {'changed': 'no', 'reason': '未提交的回顾'})
        c.act('continue')
        c.act('post', **c.state['page_draft'])
        self.assertEqual(c.state['phase'], 'timing_intro')


if __name__ == '__main__':
    unittest.main(verbosity=2)
