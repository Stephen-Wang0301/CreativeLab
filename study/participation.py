"""Server-authoritative pause and restart; never erase an earlier attempt."""

import copy
import hmac
import secrets

from .errors import StudyError
from .storage import timestamp

DRAFTS = {'main_draft', 'main_edit', 'timing_ideas', 'timing_final'}
PAGE_FIELDS = {'main_brief': {'intent', 'experience'}, 'timing_intro': {'intent'},
               'main_post': {'changed', 'reason'}, 'main_image': {'refinement'}}


class Participation:
    def __init__(self, study):
        self.study, self.store = study, study.store
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS restart_links (
                participant TEXT PRIMARY KEY REFERENCES participants(id),
                invitation TEXT UNIQUE NOT NULL REFERENCES invitations(id))''')

    def can_restart(self, db, row, revision):
        record = self.study.mode_status(db, row['mode'])
        if row['mode'] != 'pilot':
            raise StudyError('正式实验再次参与请联系研究人员获取新链接。', 409)
        if not record['recruiting'] or record['revision'] != row['revision']:
            raise StudyError('原版本已停止新报名，请联系研究人员领取当前版本的新链接。', 409)
        count = db.execute('SELECT count(*) FROM participants WHERE revision=?', (row['revision'],)).fetchone()[0]
        if count >= revision['protocol']['cap']:
            raise StudyError('当前版本报名名额已满，请联系研究人员。', 409)

    def handle(self, db, row, state, revision, body, now):
        op, phase, pid = body['op'], state['phase'], row['id']
        pause = state.get('pause')
        if op == 'pause':
            if pause:
                return
            if phase in DRAFTS and state['started'] is not None and 'content' in body:
                self.study.handle(db, row, state, revision, {**body, 'op': 'save'}, now)
            page = body.get('page_draft', {})
            if not isinstance(page, dict) or any(not isinstance(v, str) or len(v) > 1500 for v in page.values()):
                raise StudyError('页面草稿格式有误。')
            state['page_draft'] = {k: v for k, v in page.items() if k in PAGE_FIELDS.get(phase, set())}
            remaining = max(0, state['deadline'] - now) if state['deadline'] is not None else None
            state['pause'] = {'since': now, 'remaining': remaining, 'archived': False}
            state['deadline'] = None
            state['pause_count'] = state.get('pause_count', 0) + 1
            state['seq'] += 1
            self.store.event(db, pid, now, phase, 'paused', remaining_seconds=remaining)
            return
        if not pause:
            raise StudyError('请先暂停实验。', 409)
        if op == 'continue':
            duration = max(0, now - pause['since'])
            state['pause_seconds'] = state.get('pause_seconds', 0) + duration
            state['phase_pause_seconds'] = state.get('phase_pause_seconds', 0) + duration
            state.setdefault('pauses', []).append({'phase': phase, 'at': timestamp(pause['since']),
                                                  'seconds': duration, 'remaining': pause['remaining']})
            if pause['remaining'] is not None:
                state['deadline'] = now + pause['remaining']
            state.pop('pause')
            state.pop('restart_challenge', None)
            state['seq'] += 1
            self.store.event(db, pid, now, phase, 'pause_resumed', paused_seconds=duration,
                             remaining_seconds=pause['remaining'])
        elif op == 'archive_exit':
            pause['archived'] = True
            state.pop('restart_challenge', None)
            self.store.event(db, pid, now, phase, 'paused_exit')
            # Bind a continuation link without an administrative recovery or timer change.
            self.study.access.create(row['mode'], participant=pid, db=db)
        elif op == 'restart_prepare':
            self.can_restart(db, row, revision)
            if state['image'].get('job'):
                raise StudyError('图片仍在生成，请稍后再从头开始。', 409)
            state['restart_challenge'] = {'token': secrets.token_hex(16), 'ready_at': now + 10}
            self.store.event(db, pid, now, phase, 'restart_requested')
        elif op == 'restart_cancel':
            state.pop('restart_challenge', None)
        elif op == 'restart_confirm':
            challenge = state.get('restart_challenge')
            if not challenge or not hmac.compare_digest(str(body.get('challenge', '')), challenge['token']):
                raise StudyError('确认窗口已失效，请重新选择从头开始。', 409)
            if now < challenge['ready_at']:
                raise StudyError('请等待完整的 10 秒倒计时后确认。', 409)
            self.can_restart(db, row, revision)
            if state['image'].get('job'):
                raise StudyError('图片仍在生成，请稍后再从头开始。', 409)
            snapshot = self.store.version(db, pid, now, phase, 'restart_snapshot', copy.deepcopy(state))
            invitation = self.study.access.create(row['mode'], revision=row['revision'], db=db)
            db.execute('INSERT INTO restart_links VALUES (?,?)', (pid, invitation['id']))
            self.store.event(db, pid, now, phase, 'restarted', snapshot=snapshot,
                             next_participant_code=invitation['participant_code'])
            state['restart_to'] = invitation['participant_code']
            state['restart_at'] = timestamp(now)
            state['restart_snapshot'] = snapshot
            # Keep the complete answers and current work in this sealed attempt.
            state['pause_seconds'] = state.get('pause_seconds', 0) + max(0, now - pause['since'])
            state['phase'] = 'restarted'
            state['seq'] += 1
            state['deadline'] = None
            state.pop('pause')
            state.pop('restart_challenge', None)
        return None
