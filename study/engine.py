import copy
import hmac
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from .config import GROUPS, create_initial, validate
from .images import ImageRequest, validate_candidates
from .storage import atomic_write, digest, dumps, timestamp


PHASE_NAMES = {
    'main_brief': '任务一 · 阅读简报', 'main_draft': '任务一 · 独立构思',
    'main_edit': '任务一 · 编辑定稿', 'main_image': '任务一 · 图像制作',
    'main_post': '任务一 · 回顾意图', 'timing_intro': '任务二 · 阅读简报',
    'timing_suggestion': '任务二 · 阅读建议', 'timing_ideas': '任务二 · 构思两案',
    'timing_final': '任务二 · 完成提案', 'done': '已完成',
    'withdrawn': '已退出', 'incomplete': '未完成',
}
TIMED = {'main_draft', 'main_edit', 'timing_suggestion', 'timing_ideas', 'timing_final'}
DRAFT_PHASES = {'main_draft', 'main_edit', 'timing_ideas', 'timing_final'}
TERMINAL = {'done', 'withdrawn', 'incomplete'}
MODES = {'pilot': '预试', 'formal': '正式'}
TOKEN_FORMAT = re.compile(r'^[A-Za-z0-9_-]{16,96}$')
PID_FORMAT = re.compile(r'^P-[A-F0-9]{16}$')


class StudyError(Exception):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.status = status
        self.code = code


def read_text(body, key, maximum):
    value = body.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise StudyError(f'文字须在 {maximum} 字以内。')
    return value.strip()


def normalize_content(phase, content):
    if not isinstance(content, dict):
        raise StudyError('提交内容格式有误。')
    if phase == 'timing_ideas':
        return {key: read_text(content, key, 1200) for key in ('idea1', 'idea2')}
    item = {'title': read_text(content, 'title', 100), 'concept': read_text(content, 'concept', 1800)}
    if phase in {'main_draft', 'main_edit'}:
        elements = content.get('elements', [])
        if not isinstance(elements, list) or len(elements) > 3:
            raise StudyError('请在三个输入框中填写关键元素。')
        item['elements'] = [read_text({'element': element}, 'element', 120) for element in elements]
    return item


def filled(phase, content):
    if phase == 'timing_ideas':
        return bool(content.get('idea1') and content.get('idea2'))
    if not content.get('title') or not content.get('concept'):
        return False
    return phase not in {'main_draft', 'main_edit'} or len(set(filter(None, content.get('elements', [])))) >= 2


def find(items, key):
    return next(item for item in items if item['id'] == key)


class Study:
    def __init__(self, store, quick=False, image_name='', provider=None, clock=time.time):
        self.store = store
        self.clock = clock
        self.image_name = image_name
        self.provider = provider
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='study-image') if provider else None
        with self.store.transaction() as db:
            for mode in MODES:
                existing = db.execute('SELECT * FROM modes WHERE id=?', (mode,)).fetchone()
                if existing:
                    if (existing['revision'] and existing['recruiting']
                            and self.store.revision(db, existing['revision'])['image_provider'] != self.image_name):
                        db.execute('UPDATE modes SET recruiting=0 WHERE id=?', (mode,))
                    continue
                revision = None
                if mode == 'pilot':
                    revision = self.add_revision(db, mode, create_initial(quick), clock())
                db.execute('INSERT INTO modes VALUES (?,?,?,?)',
                           (mode, revision, int(mode == 'pilot'), secrets.token_urlsafe(14)))
        self.recover_image_jobs()

    def add_revision(self, db, mode, config, now):
        return db.execute('INSERT INTO revisions(mode,created,protocol,image_provider) VALUES (?,?,?,?)',
                          (mode, now, dumps(config), self.image_name)).lastrowid

    def mode_status(self, db, mode):
        if mode not in MODES:
            raise StudyError('实验模式无效。', 404)
        return db.execute('SELECT * FROM modes WHERE id=?', (mode,)).fetchone()

    def settings(self, mode):
        with self.store.lock:
            db = self.store.db
            record = self.mode_status(db, mode)
            saved_draft = db.execute('SELECT token, protocol FROM protocol_drafts WHERE mode=?', (mode,)).fetchone()
            revisions = [
                {'id': item['id'], 'at': timestamp(item['created']),
                 'label': json.loads(item['protocol'])['label'], 'image_provider': item['image_provider']}
                for item in db.execute('SELECT * FROM revisions WHERE mode=? ORDER BY id DESC', (mode,))
            ]
            return {'mode': mode, 'recruiting': bool(record['recruiting']), 'invite': record['invite'],
                    'active': self.store.revision(db, record['revision']) if record['revision'] else None,
                    'saved_draft': json.loads(saved_draft['protocol']) if saved_draft else None,
                    'editor_version': saved_draft['token'] if saved_draft else str(record['revision'] or 0),
                    'draft': create_initial(), 'revisions': revisions, 'image_provider': self.image_name}

    def change_settings(self, mode, body):
        op = body.get('op')
        if op in ('save_draft', 'revision'):
            try:
                config = validate(body.get('protocol'), allow_incomplete=op == 'save_draft')
            except ValueError as error:
                raise StudyError(str(error)) from error
            if op == 'revision' and mode == 'formal':
                if not body.get('confirmed'):
                    raise StudyError('请确认已核对正式材料和知情同意说明。')
                if (config['consent'] == create_initial()['consent']
                        or config['contact'] == create_initial()['contact']):
                    raise StudyError('请填写本研究的正式知情说明和联系方式。')
        with self.store.transaction() as db:
            record = self.mode_status(db, mode)
            if op in ('save_draft', 'revision'):
                draft = db.execute('SELECT token FROM protocol_drafts WHERE mode=?', (mode,)).fetchone()
                expected = draft['token'] if draft else str(record['revision'] or 0)
                if (op == 'save_draft' or draft or 'editor_version' in body) and body.get('editor_version') != expected:
                    raise StudyError('这份方案已在其他页面更新。请刷新后重试。', 409)
            if op == 'save_draft':
                db.execute('''INSERT INTO protocol_drafts(mode, token, protocol) VALUES (?, ?, ?)
                              ON CONFLICT(mode) DO UPDATE SET token=excluded.token, protocol=excluded.protocol''',
                           (mode, secrets.token_hex(16), dumps(config)))
            elif op == 'revision':
                if record['recruiting']:
                    raise StudyError('请先暂停招募，再建立新方案版本。', 409)
                revision = self.add_revision(db, mode, config, self.clock())
                db.execute('UPDATE modes SET revision=? WHERE id=?', (revision, mode))
                db.execute('DELETE FROM protocol_drafts WHERE mode=?', (mode,))
            elif op == 'recruiting':
                recruiting = body.get('value')
                if type(recruiting) is not bool:
                    raise StudyError('招募状态无效。')
                if recruiting and not record['revision']:
                    raise StudyError('请先保存方案，再开放招募。')
                if recruiting and self.store.revision(db, record['revision'])['image_provider'] != self.image_name:
                    raise StudyError('当前图像插件与方案版本不一致，请先保存对应的新版本。', 409)
                db.execute('UPDATE modes SET recruiting=? WHERE id=?', (recruiting, mode))
            elif op == 'rotate_invite':
                db.execute('UPDATE modes SET invite=? WHERE id=?', (secrets.token_urlsafe(14), mode))
            else:
                raise StudyError('无法识别此操作。')
            return self.settings(mode)

    @staticmethod
    def draw(db, scope, choices):
        key = dumps(scope)
        block = db.execute('SELECT pending FROM blocks WHERE scope=?', (key,)).fetchone()
        remaining = json.loads(block['pending']) if block else []
        if not remaining:
            remaining = list(choices)
            secrets.SystemRandom().shuffle(remaining)
        result = remaining.pop()
        db.execute('INSERT INTO blocks VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET pending=excluded.pending',
                   (key, dumps(remaining)))
        return result

    def landing(self, mode):
        with self.store.lock:
            db = self.store.db
            record = self.mode_status(db, mode)
            if not record['revision']:
                return {'mode': mode, 'open': False, 'title': '人智协作与数字创意实验'}
            protocol = self.store.revision(db, record['revision'])['protocol']
            return {'mode': mode, 'open': bool(record['recruiting']), 'title': protocol['title'],
                    'consent': protocol['consent'], 'contact': protocol['contact'],
                    'adult_only': protocol['adult_only']}

    def join(self, mode, body):
        invite, nonce = body.get('invite'), body.get('nonce')
        if body.get('consent') is not True:
            raise StudyError('请确认知情同意。')
        if not isinstance(nonce, str) or not TOKEN_FORMAT.fullmatch(nonce):
            raise StudyError('报名请求无效，请刷新后重试。')
        nonce_hash = digest(mode + '|' + nonce)
        recovery = hmac.new(self.store.secret.encode(), ('resume|' + nonce_hash).encode(), 'sha256').hexdigest()[:24]
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.store.transaction() as db:
            record = self.mode_status(db, mode)
            if not isinstance(invite, str) or not TOKEN_FORMAT.fullmatch(invite) or not hmac.compare_digest(invite, record['invite']):
                raise StudyError('邀请口令有误，请使用研究人员发给你的链接。', 403)
            existing = db.execute('SELECT id FROM participants WHERE nonce=?', (nonce_hash,)).fetchone()
            if existing:
                pid = existing['id']
                db.execute('UPDATE participants SET token=? WHERE id=?', (digest(token), pid))
            else:
                if not record['recruiting'] or not record['revision']:
                    raise StudyError('当前未开放新报名。', 409)
                revision = record['revision']
                protocol = self.store.revision(db, revision)['protocol']
                if protocol['adult_only'] and body.get('adult') is not True:
                    raise StudyError('请确认已满足年龄条件。')
                count = db.execute('SELECT COUNT(*) FROM participants WHERE mode=?', (mode,)).fetchone()[0]
                if count >= protocol['cap']:
                    raise StudyError('当前招募名额已满。', 409)
                brief = self.draw(db, [revision, 'main_brief'], [x['id'] for x in protocol['main_briefs']])
                pid = 'P-' + secrets.token_hex(8).upper()
                state = {'phase': 'main_brief', 'seq': 1, 'entered': now, 'started': None,
                         'deadline': None, 'edit_version': 0, 'work': {},
                         'main': {'brief': brief}, 'timing': {}, 'missing': [],
                         'consented': timestamp(now),
                         'adult_declared': True if protocol['adult_only'] else None,
                         'image': {'status': 'pending', 'rounds': [], 'job': None}}
                db.execute('''INSERT INTO participants
                              (id,mode,revision,created,updated,token,resume,nonce,blind,data)
                              VALUES (?,?,?,?,?,?,?,?,?,?)''',
                           (pid, mode, revision, now, now, digest(token), digest(recovery),
                            nonce_hash, secrets.token_hex(16), dumps(state)))
                self.store.event(db, pid, now, 'main_brief', 'consented', revision=revision, brief=brief)
        self.store.sync(pid)
        return pid, recovery, token

    def resume(self, mode, body):
        pid = body.get('id')
        code = body.get('code')
        if not isinstance(pid, str) or not isinstance(code, str) or not PID_FORMAT.fullmatch(pid.upper().strip()) or not re.fullmatch(r'[a-fA-F0-9]{24}', code.strip()):
            raise StudyError('请填写正确的参与者编号和恢复码。', 403)
        pid, code = pid.upper().strip(), code.lower().strip()
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM participants WHERE id=? AND mode=?', (pid, mode)).fetchone()
            if not row or not hmac.compare_digest(row['resume'], digest(code)):
                raise StudyError('参与者编号或恢复码有误。', 403)
            db.execute('UPDATE participants SET token=?,serial=serial+1 WHERE id=?', (digest(token), pid))
            self.store.event(db, pid, self.clock(), json.loads(row['data'])['phase'], 'session_resumed')
        self.store.sync(pid)
        return token

    @staticmethod
    def authenticate(db, mode, token):
        if not token or not TOKEN_FORMAT.fullmatch(token):
            raise StudyError('请先报名或恢复进度。', 401)
        row = db.execute('SELECT * FROM participants WHERE mode=? AND token=?', (mode, digest(token))).fetchone()
        if not row:
            raise StudyError('当前浏览器会话已失效，请用恢复码继续。', 401)
        return row

    @staticmethod
    def public_brief(brief):
        return {key: brief[key] for key in ('title', 'brief')}

    def view(self, mode, token):
        with self.store.lock:
            row = self.authenticate(self.store.db, mode, token)
        self.expire(row['id'])
        with self.store.lock:
            db = self.store.db
            row = self.authenticate(db, mode, token)
            state = json.loads(row['data'])
            revision = self.store.revision(db, row['revision'])
            return self.public_state(row, state, revision['protocol'])

    def public_state(self, row, state, protocol):
        phase = state['phase']
        result = {'id': row['id'], 'phase': phase, 'seq': state['seq'], 'title': protocol['title'],
                  'phase_name': PHASE_NAMES[phase], 'contact': protocol['contact'],
                  'started': state['started'], 'deadline': state['deadline'],
                  'now': self.clock(), 'edit_version': state['edit_version'],
                  'duration': protocol['durations_seconds'].get(phase),
                  'mode': row['mode']}
        if phase in TERMINAL:
            return result
        main, timing = state['main'], state['timing']
        active = state['started'] is not None
        if phase.startswith('main_'):
            brief = find(protocol['main_briefs'], main['brief'])
            result['brief'] = self.public_brief(brief)
            if phase == 'main_brief':
                result['intentions'] = [{k: value[k] for k in ('id', 'label', 'detail')} for value in brief['intentions']]
            if phase in {'main_draft', 'main_post'} and 'intent' in main:
                result['chosen'] = find(brief['intentions'], main['intent'])['label']
            if phase == 'main_edit' and active:
                result.update(own=main['original'], ai=main['ai'], restore_step=main.get('restore_step', 0))
            if phase == 'main_image':
                result['final'] = main['final']
                result['image'] = {key: state['image'][key] for key in ('status', 'rounds')}
        if phase.startswith('timing_'):
            brief = find(protocol['timing_briefs'], timing['brief'])
            result['brief'] = self.public_brief(brief)
            if phase == 'timing_intro':
                result['intentions'] = brief['intentions']
            if active and phase in {'timing_suggestion', 'timing_final'}:
                result['suggestion'] = find(brief['suggestions'], timing['suggestion'])['text']
            if active and phase == 'timing_final':
                result['ideas'] = timing.get('ideas', {})
        if phase in DRAFT_PHASES and active:
            result['work'] = state['work']
        return result

    def transition(self, db, pid, state, phase, now):
        previous = state['phase']
        state.update(phase=phase, seq=state['seq']+1, entered=now, started=None, deadline=None,
                     edit_version=0, work={}, work_version=None)
        self.store.event(db, pid, now, phase, 'phase_entered', previous=previous)

    def action(self, mode, token, body):
        with self.store.lock:
            row = self.authenticate(self.store.db, mode, token)
        pid = row['id']
        self.expire(pid)
        key = body.get('command')
        if not isinstance(key, str) or not TOKEN_FORMAT.fullmatch(key):
            raise StudyError('操作编号无效。')
        job = None
        with self.store.transaction() as db:
            row = self.authenticate(db, mode, token)
            repeat = db.execute('SELECT 1 FROM commands WHERE participant=? AND command=?', (pid, key)).fetchone()
            if not repeat:
                state = json.loads(row['data'])
                if body.get('seq') != state['seq']:
                    raise StudyError('进度已更新，正在读取最新记录。', 409, 'stale_phase')
                revision = self.store.revision(db, row['revision'])
                now = self.clock()
                if state['deadline'] is not None and now >= state['deadline']:
                    raise StudyError('本阶段计时已结束。', 409, 'stale_phase')
                job = self.handle(db, row, state, revision, body, now)
                if body.get('op') != 'save':
                    db.execute('INSERT INTO commands VALUES (?,?)', (pid, key))
                self.store.save(db, pid, now, state)
        if body.get('op') != 'save':
            self.store.sync(pid)
        if job:
            self.executor.submit(self.generate_images, pid, job)
        return self.view(mode, token)

    def handle(self, db, row, state, revision, body, now):
        pid = row['id']
        phase, op = state['phase'], body.get('op')
        protocol = revision['protocol']
        main, timing = state['main'], state['timing']
        if not isinstance(op, str):
            raise StudyError('操作类型无效。')
        if phase in TERMINAL:
            raise StudyError('本次实验已经结束。', 409)
        if op == 'withdraw':
            state['withdrawal'] = {'at': timestamp(now), 'phase': phase,
                                   'reason': read_text(body, 'reason', 500), 'work': state['work']}
            self.store.event(db, pid, now, phase, 'withdrawal')
            self.transition(db, pid, state, 'withdrawn', now)
            return None
        if op == 'start' and phase in TIMED and state['started'] is None:
            state['started'] = now
            state['deadline'] = now + protocol['durations_seconds'][phase]
            if phase == 'main_edit':
                state['work'] = copy.deepcopy(main['ai'])
                state['work_version'] = self.store.version(db, pid, now, phase, 'ai_working_draft', state['work'])
                main['ai_version'] = state['work_version']
            self.store.event(db, pid, now, phase, 'phase_started', deadline_at=timestamp(state['deadline']))
            if phase == 'timing_suggestion':
                self.store.event(db, pid, now, phase, 'suggestion_exposed', suggestion=timing['suggestion'])
            return None
        if phase in TIMED and state['started'] is None:
            raise StudyError('请先开始本阶段。')
        if phase in DRAFT_PHASES and op in {'save', 'submit'}:
            content = normalize_content(phase, body.get('content'))
            if body.get('edit_version') != state['edit_version'] and content != state['work']:
                raise StudyError('这个页面的稿件版本已过期，请查看最新记录。', 409, 'stale_edit')
            if op == 'submit' and not filled(phase, content):
                raise StudyError('请填写全部必填项，主任务须有至少两个不同的关键元素。')
            if content != state['work']:
                state['work'] = content
                state['edit_version'] += 1
                state['work_version'] = self.store.version(
                    db, pid, now, phase, 'autosave' if op == 'save' else 'submit_edit', content,
                    parent=state.get('work_version'))
                self.store.event(db, pid, now, phase, 'draft_saved', edit_version=state['edit_version'])
            if op == 'submit':
                self.finish_timed(db, row, state, revision, now, False)
            return None
        if op == 'reference':
            reference = body.get('reference')
            allowed = {'main_edit': ('own', 'ai'), 'timing_final': ('suggestion', 'ideas')}
            if reference not in allowed.get(phase, ()):
                raise StudyError('此参考材料在当前阶段不可用。')
            self.store.event(db, pid, now, phase, 'reference_opened', reference=reference)
            return None
        if phase == 'main_brief' and op == 'choose':
            brief = find(protocol['main_briefs'], main['brief'])
            intent = body.get('intent')
            if intent not in [x['id'] for x in brief['intentions']]:
                raise StudyError('请选择一项优先意图。')
            experience = body.get('experience')
            if experience not in ('none', 'some', 'regular', 'skip'):
                raise StudyError('请选择设计经历，或选择不填写。')
            main.update(intent=intent, intent_label=find(brief['intentions'], intent)['label'],
                        experience=experience, intent_at=timestamp(now))
            self.store.event(db, pid, now, phase, 'intention_frozen', intent=intent, experience=experience)
            self.transition(db, pid, state, 'main_draft', now)
        elif phase == 'main_edit' and op.startswith('restore_'):
            self.restore(db, pid, state, body, now)
        elif phase == 'main_image':
            return self.image_action(db, row, state, revision, body, now)
        elif phase == 'main_post' and op == 'post':
            changed = body.get('changed')
            if changed not in ('yes', 'no', 'unsure'):
                raise StudyError('请选择是否改变了原始意图。')
            reason = read_text(body, 'reason', 1500)
            if not reason:
                raise StudyError('请简要填写原因。')
            main['post'] = {'changed': changed, 'reason': reason, 'at': timestamp(now)}
            options = [[brief['id'], suggestion['id']] for brief in protocol['timing_briefs']
                       for suggestion in brief['suggestions']]
            timing['brief'], timing['suggestion'] = self.draw(
                db, [row['revision'], 'timing_material', main['group']], options)
            self.store.event(db, pid, now, phase, 'post_submitted', changed=changed)
            self.transition(db, pid, state, 'timing_intro', now)
        elif phase == 'timing_intro' and op == 'choose':
            brief = find(protocol['timing_briefs'], timing['brief'])
            intent = body.get('intent')
            if intent not in [x['id'] for x in brief['intentions']]:
                raise StudyError('请选择一项优先意图。')
            timing.update(intent=intent, intent_label=find(brief['intentions'], intent)['label'],
                          intent_at=timestamp(now))
            timing['entry'] = self.draw(db, [row['revision'], 'timing', main['group'],
                                            timing['brief'], timing['suggestion']], ['early', 'late'])
            self.store.event(db, pid, now, phase, 'timing_randomized', **timing)
            self.transition(db, pid, state, 'timing_suggestion' if timing['entry'] == 'early' else 'timing_ideas', now)
        elif phase == 'timing_suggestion' and op == 'submit':
            raise StudyError('请等待建议阅读计时结束。', 409)
        else:
            raise StudyError('此操作在当前阶段不可用。', 409)
        return None

    def restore(self, db, pid, state, body, now):
        main = state['main']
        high = GROUPS[main['group']]['friction'] == 'high'
        op = body.get('op')
        step = main.get('restore_step', 0)
        if op == 'restore_open':
            self.store.event(db, pid, now, 'main_edit', 'restore_opened')
            if high:
                main['restore_step'] = 1
                return
        elif op == 'restore_select' and high and step == 1:
            main['restore_step'] = 2
            self.store.event(db, pid, now, 'main_edit', 'restore_version_selected')
            return
        elif op == 'restore_cancel' and high and step in {1, 2}:
            main['restore_step'] = 0
            self.store.event(db, pid, now, 'main_edit', 'restore_cancelled')
            return
        elif op != 'restore_confirm' or not high or step != 2:
            raise StudyError('请按页面提示选择原稿版本。', 409)
        if body.get('edit_version') != state['edit_version']:
            raise StudyError('请先保存或刷新当前稿件。', 409, 'stale_edit')
        before = self.store.version(db, pid, now, 'main_edit', 'before_restore', state['work'],
                                    parent=state.get('work_version'))
        state['work'] = copy.deepcopy(main['original'])
        after = self.store.version(db, pid, now, 'main_edit', 'restored_original', state['work'],
                                   parent=before, source=main['original_version'])
        state['work_version'] = after
        state['edit_version'] += 1
        main['restore_step'] = 0
        self.store.event(db, pid, now, 'main_edit', 'original_restored', before=before, after=after)

    def finish_timed(self, db, row, state, revision, now, expired):
        pid, phase = row['id'], state['phase']
        protocol = revision['protocol']
        main, timing = state['main'], state['timing']
        content = copy.deepcopy(state['work'])
        valid = phase == 'timing_suggestion' or filled(phase, content)
        if not valid:
            state['missing'].append({'phase': phase, 'reason': 'timeout_incomplete', 'at': timestamp(now)})
        frozen_at = state['deadline'] if expired else now
        method = 'timeout' if expired else 'submitted'
        version = None
        if phase in DRAFT_PHASES:
            version = self.store.version(
                db, pid, now, phase, 'frozen_timeout' if expired else 'frozen_submission', content,
                parent=state.get('work_version'))
        self.store.event(db, pid, now, phase, 'phase_timed_out' if expired else 'phase_submitted',
                         frozen_at=timestamp(frozen_at), complete=valid)
        if phase == 'main_draft':
            main.update(original=content, original_complete=valid, original_at=timestamp(frozen_at), original_version=version, original_method=method)
            if not valid:
                self.transition(db, pid, state, 'incomplete', now)
                return
            brief = find(protocol['main_briefs'], main['brief'])
            main['group'] = self.draw(db, [row['revision'], 'main', main['brief'], main['intent']], list(GROUPS))
            factor = GROUPS[main['group']]
            source = next(x for x in brief['intentions'] if (x['id'] == main['intent']) == (factor['fit'] == 'match'))
            main.update(fit=factor['fit'], friction=factor['friction'], ai_id=source['id'],
                        ai=source['draft'], assigned_at=timestamp(now))
            self.store.event(db, pid, now, phase, 'main_randomized', group=main['group'],
                             brief=main['brief'], intent=main['intent'], ai_id=main['ai_id'])
            self.transition(db, pid, state, 'main_edit', now)
        elif phase == 'main_edit':
            main.update(final=content, final_complete=valid, final_at=timestamp(frozen_at), final_version=version, final_method=method)
            if not revision['image_provider']:
                state['image']['status'] = 'not_configured'
            elif not valid:
                state['image']['status'] = 'no_complete_text'
            elif revision['image_provider'] != self.image_name:
                state['image']['status'] = 'provider_unavailable'
            else:
                state['image']['status'] = 'ready'
                self.transition(db, pid, state, 'main_image', now)
                return
            self.store.event(db, pid, now, phase, 'image_skipped', reason=state['image']['status'])
            self.transition(db, pid, state, 'main_post', now)
        elif phase == 'timing_suggestion':
            target = 'timing_ideas' if timing['entry'] == 'early' else 'timing_final'
            self.transition(db, pid, state, target, now)
        elif phase == 'timing_ideas':
            timing.update(ideas=content, ideas_complete=valid, ideas_at=timestamp(frozen_at), ideas_version=version, ideas_method=method)
            target = 'timing_suggestion' if timing['entry'] == 'late' else 'timing_final'
            self.transition(db, pid, state, target, now)
        elif phase == 'timing_final':
            timing.update(final=content, final_complete=valid, final_at=timestamp(frozen_at), final_version=version, final_method=method)
            self.transition(db, pid, state, 'done', now)

    def expire(self, pid):
        now = self.clock()
        with self.store.lock:
            row = self.store.db.execute('SELECT data FROM participants WHERE id=?', (pid,)).fetchone()
            deadline = json.loads(row['data'])['deadline'] if row else None
            if deadline is None or deadline > now:
                return
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone()
            state = json.loads(row['data'])
            if state['deadline'] is not None and state['deadline'] <= now:
                revision = self.store.revision(db, row['revision'])
                self.finish_timed(db, row, state, revision, now, True)
                self.store.save(db, pid, now, state)
        self.store.sync(pid)

    def tick(self):
        with self.store.lock:
            pids = [row['id'] for row in self.store.db.execute(
                "SELECT id FROM participants WHERE json_extract(data,'$.deadline')<=?", (self.clock(),))]
        for pid in pids:
            self.expire(pid)
        self.store.sync_pending()

    def image_action(self, db, row, state, revision, body, now):
        pid, op = row['id'], body.get('op')
        image = state['image']
        if image['job']:
            raise StudyError('正在生成图片，请稍候。', 409)
        if op == 'image_skip':
            image['status'] = 'skipped_by_participant'
            self.store.event(db, pid, now, 'main_image', 'image_skipped', reason=image['status'])
            self.transition(db, pid, state, 'main_post', now)
        elif op == 'image_select':
            choice = body.get('choice')
            if choice not in [item for round_ in image['rounds'] for item in round_['files']]:
                raise StudyError('请选择一张已生成的图片。')
            image.update(status='selected', selected=choice, selected_at=timestamp(now))
            self.store.event(db, pid, now, 'main_image', 'image_selected', file=choice)
            self.transition(db, pid, state, 'main_post', now)
        elif op == 'image_generate':
            if not self.provider or revision['image_provider'] != self.image_name:
                image['status'] = 'provider_unavailable'
                self.store.event(db, pid, now, 'main_image', 'image_skipped', reason=image['status'])
                self.transition(db, pid, state, 'main_post', now)
                return None
            if len(image['rounds']) >= 2:
                raise StudyError('两轮生成已经结束。')
            if sum(entry.get('round') == len(image['rounds']) + 1 for entry in image.get('failures', [])) >= 2:
                raise StudyError('本轮生成已失败两次。请选择已有图片，或跳过图像环节。', 409)
            if not image['rounds']:
                refinement = ''
            else:
                refinement = read_text(body, 'refinement', 500)
            job = {'id': secrets.token_hex(12), 'round': len(image['rounds'])+1, 'refinement': refinement, 'final_version': state['main']['final_version']}
            image.update(job=job, status='generating')
            self.store.event(db, pid, now, 'main_image', 'image_requested', **job)
            return job
        else:
            raise StudyError('请选择图像操作。')
        return None

    def generate_images(self, pid, job):
        try:
            with self.store.lock:
                db = self.store.db
                row = db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone()
                state = json.loads(row['data'])
                protocol = self.store.revision(db, row['revision'])['protocol']
            main = state['main']
            brief = find(protocol['main_briefs'], main['brief'])
            final = main['final']
            request = ImageRequest(brief['brief'], final['title'], final['concept'], tuple(final['elements']),
                                   job['refinement'], job['round'], job['id'])
            candidates = validate_candidates(self.provider(request))
            files = [f'r{job["round"]}-{index}-{job["id"]}.{ext}'
                     for index, (ext, _) in enumerate(candidates, 1)]
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone()
                state = json.loads(row['data'])
                image = state['image']
                if state['phase'] != 'main_image' or image['job'] != job:
                    return
                folder = self.store.participant_path(row) / 'images'
                for filename, (_, content) in zip(files, candidates):
                    atomic_write(folder / filename, content)
                now = self.clock()
                image['rounds'].append({'round': job['round'], 'refinement': job['refinement'], 'request_id': job['id'],
                                        'final_version': job['final_version'], 'files': files})
                image.update(job=None, status='available')
                self.store.event(db, pid, now, 'main_image', 'images_saved', files=files)
                self.store.save(db, pid, now, state)
        except Exception:
            logging.warning('参与者 %s 的图像请求未完成。', pid)
            self.fail_image(pid, job, 'provider_error')
        self.store.sync(pid)

    def fail_image(self, pid, job, reason):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone()
            state = json.loads(row['data'])
            image = state['image']
            if image['job'] != job:
                return
            now = self.clock()
            image.update(status='failed', job=None)
            image.setdefault('failures', []).append({'round': job['round'], 'reason': reason, 'at': timestamp(now)})
            self.store.event(db, pid, now, 'main_image', 'image_failed', reason=reason, job=job['id'])
            self.store.save(db, pid, now, state)
        self.store.sync(pid)

    def recover_image_jobs(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT id,data FROM participants WHERE json_type(data,'$.image.job')='object'").fetchall()
        for row in rows:
            self.fail_image(row['id'], json.loads(row['data'])['image']['job'], 'server_restarted')
