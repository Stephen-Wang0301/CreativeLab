"""End-to-end checks using temporary data. Run with: python tests/verify_flow.py."""

import base64
import collections
import csv
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run import dependencies

dependencies()
from study.engine import Study
from study.http import create_apps
from study.images import ImageCandidate
from study.research import Research, csv_bytes
from study.storage import Store


HEADERS = {'X-Study-Request': '1'}
DRAFT = {'title': '人工构思', 'concept': '使用不同角度构成开放空间的层次。',
         'elements': ['折叠画面', '开放窗户']}
IDEAS = {'idea1': '采用网格排列不同活动', 'idea2': '用手写路线表示空间移动'}
FINAL = {'title': '定稿主题', 'concept': '从开放的窗口观察展览并指引路线。'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Client:
    def __init__(self, app, invite, mode='pilot'):
        self.client = app.test_client()
        self.base = f'/p/{mode}/api/'
        self.invite = invite
        self.nonce = secrets.token_hex(16)
        self.state = None

    def join(self):
        response = self.client.post(self.base + 'join', headers=HEADERS,
                                    json={'adult': True, 'consent': True,
                                          'nonce': self.nonce, 'invite': self.invite})
        if response.status_code != 200:
            raise AssertionError(response.get_json())
        data = response.get_json()
        self.state = data['view']
        return data['resume']

    def get(self):
        self.state = self.client.get(self.base + 'state').get_json()
        return self.state

    def act(self, op, status=200, **values):
        body = {'seq': self.state['seq'], 'op': op,
                'edit_version': self.state['edit_version'], 'command': secrets.token_hex(16), **values}
        response = self.client.post(self.base + 'action', json=body, headers=HEADERS)
        if response.status_code != status:
            raise AssertionError((op, response.status_code, response.get_json()))
        if status == 200:
            self.state = response.get_json()
        return response.get_json()

    def draft(self, intent=0):
        self.act('choose', intent=self.state['intentions'][intent]['id'], experience='some')
        self.act('start')
        self.act('save', content=DRAFT)
        self.act('submit', content=DRAFT)
        self.act('start')

    def post(self):
        self.act('post', changed='no', reason='沿用窗户意象。')
        self.act('choose', intent=self.state['intentions'][0]['id'])

    def read_suggestion(self, clock):
        assert 'suggestion' not in self.state
        self.act('start')
        assert 'suggestion' in self.state
        assert 'features' not in self.state
        self.act('submit', status=409)
        clock.now = self.state['deadline']
        self.get()


class Verify(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='study-check-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.clock = Clock()
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.study = Study(self.store, clock=self.clock)
        self.init_apps()

    def init_apps(self):
        self.public, self.admin = create_apps(self.study, {'local_url': 'http://127.0.0.1:8765'})
        self.research = Research(self.study)

    def client(self):
        return Client(self.public, self.study.settings('pilot')['invite'])

    def auth_admin(self):
        client = self.admin.test_client()
        result = client.post('/research/api/login', headers=HEADERS, json={'key': self.store.admin_key})
        self.assertEqual(result.status_code, 200)
        return client

    def test_auth_and_routes(self):
        public = self.public.test_client()
        admin = self.admin.test_client()
        with public.get('/p/pilot/') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(public.get('/research/api/health').status_code, 404)
        self.assertEqual(admin.get('/research/api/health').status_code, 401)
        self.assertIn(admin.get('/research/', base_url='http://external.example').status_code, (400, 403))
        self.assertEqual(admin.get('/research/', environ_base={'REMOTE_ADDR': '10.0.0.8'}).status_code, 403)
        self.assertFalse(public.get('/p/formal/api/landing').get_json()['open'])
        self.assertEqual(public.post('/p/pilot/api/join', json={}).status_code, 415)
        self.assertEqual(public.post('/p/pilot/api/join', json={}, headers={
            **HEADERS, 'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.auth_admin().get('/research/api/health').status_code, 200)

    def test_public_admin_requires_https_and_expiring_login(self):
        origin = 'https://creative-study.wmt.cc.cd'
        public, _ = create_apps(self.study, {
            'local_url': 'http://127.0.0.1:8765', 'admin_url': origin + '/research/',
        })
        client = public.test_client()
        with client.get('/research/', base_url=origin) as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/research/api/health', base_url=origin).status_code, 401)
        self.assertEqual(client.get('/research/', base_url=origin.replace('https:', 'http:')).status_code, 403)
        self.assertIn(client.get('/research/', base_url='https://other.example').status_code, (400, 403))
        self.assertEqual(client.get('/research/', base_url=origin,
                                    environ_base={'REMOTE_ADDR': '10.0.0.8'}).status_code, 403)
        self.assertEqual(client.post('/research/api/login', base_url=origin, headers=HEADERS,
                                     json={'key': 'incorrect'}).status_code, 403)
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=int(time.time()) - 9 * 3600):
            response = client.post('/research/api/login', base_url=origin, headers=HEADERS,
                                   json={'key': self.store.admin_key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/research/api/health', base_url=origin).status_code, 401)
        response = client.post('/research/api/login', base_url=origin, headers=HEADERS,
                               json={'key': self.store.admin_key})
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Max-Age=28800', cookie)
        self.assertEqual(client.get('/research/api/health', base_url=origin).status_code, 200)
        before = client.get('/research/api/pilot/settings', base_url=origin).get_json()
        protocol = {**before['active']['protocol'], 'title': 'HTTPS 后台编辑草稿'}
        body = {'op': 'save_draft', 'protocol': protocol, 'editor_version': before['editor_version']}
        response = client.post('/research/api/pilot/settings', base_url=origin, headers=HEADERS, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/research/api/pilot/settings', base_url=origin).get_json()['saved_draft'], protocol)
        self.assertEqual(client.post('/research/api/pilot/settings', base_url=origin,
                                     headers={**HEADERS, 'Origin': 'https://other.example'},
                                     json=body).status_code, 403)
        self.assertEqual(client.post('/research/api/logout', base_url=origin, headers=HEADERS,
                                     json={}).status_code, 200)
        self.assertEqual(client.get('/research/api/health', base_url=origin).status_code, 401)
        with client.get('/p/pilot/', base_url=origin) as response:
            self.assertEqual(response.status_code, 200)

    def test_main_and_timing_blocks_full_flow(self):
        main = collections.defaultdict(list)
        timing = collections.defaultdict(list)
        for index in range(32):
            participant = self.client()
            code = participant.join()
            pid = code['id']
            self.assertNotIn('draft', participant.state['intentions'][0])
            participant.draft(intent=0 if index < 16 else 1)
            result = self.research.detail('pilot', pid)['responses']['main']
            group = result['group']
            main[result['brief'], result['intent']].append(group)
            self.assertEqual(result['ai_id'] == result['intent'], group in (1, 2))
            self.assertNotIn('group', participant.state)
            self.assertNotIn('friction', participant.state)
            self.assertNotIn('suggestion', participant.state)
            edited = {**DRAFT, 'title': '恢复前保存的编辑'}
            participant.act('save', content=edited)
            participant.act('reference', reference='own')
            participant.act('restore_open')
            if group in (2, 4):
                self.assertEqual(participant.state['work'], edited)
                self.assertEqual(participant.state['restore_step'], 1)
                participant.act('restore_confirm', status=409)
                participant.act('restore_select')
                participant.act('restore_confirm')
            self.assertEqual(participant.state['work'], DRAFT)
            participant.act('submit', content=DRAFT)
            self.assertEqual(participant.state['phase'], 'main_post')
            participant.post()
            stored = self.research.detail('pilot', pid)['responses']['timing']
            timing[group, stored['brief'], stored['suggestion']].append(stored['entry'])
            if stored['entry'] == 'early':
                self.assertEqual(participant.state['phase'], 'timing_suggestion')
                participant.read_suggestion(self.clock)
            self.assertEqual(participant.state['phase'], 'timing_ideas')
            self.assertNotIn('suggestion', participant.act('start'))
            participant.act('submit', content=IDEAS)
            if stored['entry'] == 'late':
                participant.read_suggestion(self.clock)
            self.assertEqual(participant.state['phase'], 'timing_final')
            self.assertNotIn('suggestion', participant.state)
            self.assertIn('suggestion', participant.act('start'))
            participant.act('submit', content=FINAL)
            self.assertEqual(participant.state['phase'], 'done')
            folder = self.path / 'batches' / 'pilot' / 'v0.1' / 'participants' / participant.state['participant_code']
            self.assertEqual(json.loads((folder / 'progress.json').read_text(encoding='utf-8'))['image_status'], 'not_configured')
            versions = [json.loads(line) for line in (folder / 'versions.jsonl').read_text(encoding='utf-8').splitlines()]
            before = next(x for x in versions if x['type'] == 'before_restore')
            after = next(x for x in versions if x['type'] == 'restored_original')
            self.assertEqual(before['contents'], edited)
            self.assertEqual(after['parent'], before['id'])
            self.assertEqual(after['source'], result['original_version'])
            text = (folder / 'responses.json').read_text(encoding='utf-8')
            self.assertNotIn(code['code'], text)
        self.assertEqual(len(main), 4)
        for assignments in main.values():
            self.assertEqual(collections.Counter(assignments), {1: 2, 2: 2, 3: 2, 4: 2})
        self.assertEqual(len(timing), 16)
        for assignments in timing.values():
            self.assertEqual(sorted(assignments), ['early', 'late'])
        overview = self.research.overview('pilot', None)
        self.assertEqual(overview['completed'], 32)
        self.assertEqual(overview['unsynced'], [])

        admin = self.auth_admin()
        raw = admin.get('/research/api/pilot/raw.zip')
        self.assertEqual(raw.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(raw.data)) as archive:
            self.assertEqual(sum(name.endswith('/versions.jsonl') for name in archive.namelist()), 32)
            self.assertIn('ratings_long.csv', archive.namelist())
        raw.close()
        packet = admin.get('/research/api/pilot/packet/h3')
        self.assertEqual(packet.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(packet.data)) as archive:
            content = archive.read('artifacts.csv').decode('utf-8-sig')
            self.assertEqual(next(csv.reader(io.StringIO(content))), ['artifact_id', 'intention', 'final'])
            self.assertNotIn('P-', content)
        packet.close()
        with self.store.lock:
            eligible = self.research.packet_items('pilot', None, 'h3')
        group_scores = {1: 5, 2: 5, 3: 6, 4: 4}
        rows = [{'artifact_id': item['artifact_id'],
                 'score': group_scores[json.loads(row['data'])['main']['group']]}
                for row, item in eligible]
        content = csv_bytes(['artifact_id', 'score'], rows).decode('utf-8-sig')
        for rater in ('A', 'B', 'C'):
            response = admin.post('/research/api/pilot/ratings', headers=HEADERS,
                                  json={'task': 'h3', 'rater': rater, 'csv': content})
            self.assertEqual(response.status_code, 200, response.get_json())
        analysis = admin.get('/research/api/pilot/analysis').get_json()
        self.assertEqual(analysis['contrasts'][2]['estimate'], 2)
        self.assertIsNone(analysis['contrasts'][3]['estimate'])
        self.assertEqual(admin.get('/research/api/pilot/packet/h4').status_code, 404)
        self.assertEqual(admin.get('/research/api/pilot/analysis.csv').status_code, 200)

    def test_timeouts_and_resume_after_restart(self):
        participant = self.client()
        code = participant.join()
        participant.act('choose', intent=participant.state['intentions'][0]['id'], experience='none')
        participant.act('start')
        participant.act('save', content=DRAFT)
        self.clock.now = participant.state['deadline'] + 60
        reopened = Store(self.path)
        self.addCleanup(reopened.db.close)
        study = Study(reopened, clock=self.clock)
        new_public, _ = create_apps(study, {})
        with new_public.test_client() as browser:
            state = browser.post('/p/pilot/api/resume', headers=HEADERS, json=code).get_json()['view']
            self.assertEqual(state['phase'], 'main_edit')
            self.assertIsNone(state['deadline'])
            self.assertNotIn('ai', state)
        fresh = self.client()
        fresh.join()
        fresh.act('choose', intent=fresh.state['intentions'][0]['id'], experience='skip')
        fresh.act('start')
        self.clock.now = fresh.state['deadline'] + 1
        self.study.tick()
        self.assertEqual(fresh.get()['phase'], 'incomplete')

    def test_stale_version_and_idempotent_command(self):
        participant = self.client()
        participant.join()
        seq = participant.state['seq']
        command = secrets.token_hex(16)
        intent = participant.state['intentions'][0]['id']
        participant.act('choose', seq=seq, command=command, intent=intent, experience='some')
        participant.act('choose', seq=seq, command=command, intent=intent, experience='some')
        self.assertEqual(participant.state['phase'], 'main_draft')
        participant.act('start')
        participant.act('save', content=DRAFT)
        participant.act('save', edit_version=0, content={**DRAFT, 'title': '旧版覆盖'}, status=409)
        self.assertEqual(participant.get()['work'], DRAFT)

    def test_revision_and_mode_isolation(self):
        participant = self.client()
        info = participant.join()
        before = self.study.settings('pilot')['active']
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': False})
        changed = {**before['protocol'], 'title': '新的预试方案'}
        self.study.change_settings('pilot', {'op': 'revision', 'protocol': changed})
        after = self.study.settings('pilot')['active']
        self.assertNotEqual(before['id'], after['id'])
        self.assertNotEqual(participant.get()['title'], changed['title'])
        self.assertEqual(self.research.overview('pilot', None)['enrolled'], 0)
        self.assertEqual(self.research.overview('pilot', before['id'])['enrolled'], 1)
        self.assertEqual(self.auth_admin().get('/research/api/formal/person/' + info['id']).status_code, 404)

    def test_protocol_draft_persists_without_publishing(self):
        admin = self.auth_admin()
        path = '/research/api/pilot/settings'
        before = admin.get(path).get_json()
        protocol = {**before['active']['protocol'], 'title': '新版预试', 'contact': '', 'cap': None}
        draft_request = {'op': 'save_draft', 'protocol': protocol, 'editor_version': before['editor_version']}
        response = admin.post(path, headers=HEADERS, json=draft_request)
        self.assertEqual(response.status_code, 200)
        saved = response.get_json()
        self.assertEqual(saved['saved_draft'], protocol)
        self.assertEqual(saved['active'], before['active'])
        self.assertTrue(saved['recruiting'])
        self.assertIsNone(admin.get('/research/api/formal/settings').get_json()['saved_draft'])
        self.assertEqual(admin.post(path, headers=HEADERS, json=draft_request).status_code, 409)
        self.assertEqual(self.study.landing('pilot')['title'], before['active']['protocol']['title'])

        reopened = Store(self.path)
        self.addCleanup(reopened.db.close)
        self.assertEqual(Study(reopened, clock=self.clock).settings('pilot')['saved_draft'], protocol)
        self.assertEqual(admin.post(path, headers=HEADERS,
                                    json={'op': 'recruiting', 'value': False}).status_code, 200)
        publish = {'op': 'revision', 'protocol': protocol, 'editor_version': saved['editor_version']}
        self.assertEqual(admin.post(path, headers=HEADERS, json=publish).status_code, 400)
        self.assertEqual(admin.get(path).get_json()['saved_draft'], protocol)

        protocol.update(contact='研究联系人 test@example.com', cap=40)
        saved = admin.post(path, headers=HEADERS,
                           json={**draft_request, 'editor_version': saved['editor_version']}).get_json()
        publish['editor_version'] = saved['editor_version']
        result = admin.post(path, headers=HEADERS, json=publish)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.get_json()['saved_draft'])
        self.assertEqual(self.study.landing('pilot')['title'], '新版预试')

    def test_image_contract_two_rounds_and_selected_packet(self):
        requests = []
        def provider(request):
            requests.append(request)
            return [ImageCandidate(PNG, 'image/png'), ImageCandidate(PNG, 'image/png')]
        self.study = Study(self.store, clock=self.clock, image_name='qa:image', provider=provider)
        self.addCleanup(self.study.executor.shutdown)
        self.study.change_settings('pilot', {'op': 'revision', 'protocol': self.study.settings('pilot')['active']['protocol']})
        self.study.change_settings('pilot', {'op': 'recruiting', 'value': True})
        self.init_apps()
        participant = self.client()
        participant.join()
        participant.draft()
        participant.act('submit', content=DRAFT)
        self.assertEqual(participant.state['phase'], 'main_image')

        for index in range(2):
            participant.act('image_generate', refinement='使用手工纹理')
            for _ in range(100):
                if len(participant.get()['image']['rounds']) == index + 1:
                    break
                time.sleep(.01)
            self.assertEqual(len(participant.state['image']['rounds']), index + 1)
        participant.act('image_generate', status=400)
        image = participant.state['image']['rounds'][1]['files'][0]
        response = participant.client.get(participant.base + 'image/' + image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG)
        response.close()
        participant.act('image_select', choice=image)
        self.assertEqual(participant.state['phase'], 'main_post')
        self.assertEqual([request.refinement for request in requests], ['', '使用手工纹理'])
        stream = self.research.packet('pilot', None, 'h4')
        with stream, zipfile.ZipFile(stream) as archive:
            files = [name for name in archive.namelist() if name.startswith('artifacts/')]
            self.assertEqual(len(files), 1)
            self.assertEqual(archive.read(files[0]), PNG)
            self.assertNotIn('P-', files[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
