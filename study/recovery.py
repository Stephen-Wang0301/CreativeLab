"""Explicit researcher recovery without discarding prior experimental evidence."""

import copy
import math

from .errors import StudyError
from .storage import timestamp

DRAFTS = {'main_draft', 'main_edit', 'timing_ideas', 'timing_final'}
TIMED = DRAFTS | {'timing_suggestion'}
NAMES = {'main_brief': '任务一 · 阅读简报', 'main_draft': '任务一 · 独立构思',
         'main_edit': '任务一 · 编辑定稿', 'main_image': '任务一 · 图像制作',
         'main_post': '任务一 · 回顾意图', 'timing_intro': '任务二 · 阅读简报',
         'timing_suggestion': '任务二 · 阅读建议', 'timing_ideas': '任务二 · 构思两案',
         'timing_final': '任务二 · 完成提案'}


class Recovery:
    def __init__(self, study):
        self.study = study
        self.store = study.store

    def plan(self, row, state):
        phase = state['phase']
        if phase in {'done', 'withdrawn', 'restarted'}:
            return {'allowed': False, 'reason': '已完成或主动退出的记录保持封存；再次测试请生成新的个人邀请。'}
        if state.get('pause'):
            return {'allowed': False, 'reason': '参与者已自行暂停；发送继续链接即可继续，计时和分组保持不变。'}
        if state['image'].get('job'):
            return {'allowed': False, 'reason': '图片生成尚未结束，请稍后再恢复。'}
        if phase == 'incomplete':
            # The engine terminates incomplete attempts when their independent original is missing.
            phase = 'main_draft' if state['main'].get('intent') else 'main_brief'
        if phase not in NAMES:
            return {'allowed': False, 'reason': '无法可靠判断恢复阶段，请先核对完整记录。'}
        version = self.store.revision(self.store.db, row['revision'])
        work = copy.deepcopy(state.get('recovery_work', state['work'])) if state['phase'] == phase else {}
        if phase in DRAFTS and not work:
            work = copy.deepcopy({'main_draft': state['main'].get('original', {}),
                                  'main_edit': state['main'].get('final', state['main'].get('ai', {})),
                                  'timing_ideas': state['timing'].get('ideas', {}),
                                  'timing_final': state['timing'].get('final', {})}.get(phase, {}))
        duration = version['protocol']['durations_seconds'].get(phase)
        remaining = None
        if phase in TIMED:
            remaining = (max(1, math.ceil(state['deadline'] - self.study.clock()))
                if state['phase'] == phase and state.get('deadline') and state['deadline'] > self.study.clock()
                else duration)
        return {'allowed': True, 'phase': phase, 'phase_name': NAMES[phase],
                'version_code': version['version_code'], 'expected_serial': row['serial'],
                'seconds_on_start': remaining, 'has_saved_work': bool(work), '_work': work,
                'reason': '保留已完成阶段、分组和已保存内容，回到中断阶段；开始后按显示的时长继续计时。'}

    def preview(self, row, state):
        return {key: value for key, value in self.plan(row, state).items() if not key.startswith('_')}

    def restore(self, mode, pid, serial, reason):
        if type(serial) is not int:
            raise StudyError('请刷新参与者详情后再恢复。', 409)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise StudyError('请填写本次人工恢复的原因。')
        import json
        now = self.study.clock()
        with self.store.transaction() as db:
            self.study.mode_status(db, mode)
            row = db.execute('SELECT * FROM participants WHERE mode=? AND id=?', (mode, pid)).fetchone()
            if not row:
                raise StudyError('找不到此参与者。', 404)
            if row['serial'] != serial:
                raise StudyError('参与者进度已更新，请刷新详情并重新核对恢复位置。', 409)
            state = json.loads(row['data'])
            plan = self.plan(row, state)
            if not plan['allowed']:
                raise StudyError(plan['reason'], 409)
            previous = state['phase']
            snapshot = self.store.version(db, pid, now, previous, 'admin_recovery_snapshot', copy.deepcopy(state))
            self.study.transition(db, pid, state, plan['phase'], now)
            if plan['phase'] in DRAFTS:
                state['recovery_work'] = plan['_work']
            if plan['seconds_on_start'] is not None:
                state['recovery_seconds'] = plan['seconds_on_start']
                state['recovery_timer_seconds'] = plan['seconds_on_start']
            state['recovery_count'] = state.get('recovery_count', 0) + 1
            state['main']['restore_step'] = 0
            state.setdefault('recoveries', []).append({'at': timestamp(now), 'from': previous,
                'to': plan['phase'], 'reason': reason.strip(), 'snapshot': snapshot})
            self.store.event(db, pid, now, plan['phase'], 'admin_recovered', previous=previous,
                             reason=reason.strip(), snapshot=snapshot, seconds_on_start=plan['seconds_on_start'])
            invitation = self.study.access.create(mode, participant=pid, db=db)
            state['invitation_label'] = invitation['label']
            self.store.save(db, pid, now, state)
        self.store.sync(pid)
        return {'ok': True, 'invitation': invitation, 'recovery': {key: value for key,value in plan.items() if not key.startswith('_')}}
