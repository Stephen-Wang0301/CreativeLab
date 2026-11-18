import copy
import json
import re
from pathlib import Path

MATERIAL = Path(__file__).parent / 'materials' / 'default.json'
STUDY_ID = re.compile(r'^[a-z][a-z0-9-]{0,39}$')
GROUPS = {
    1: {'fit': 'match', 'friction': 'low', 'label': '契合 × 一步恢复'},
    2: {'fit': 'match', 'friction': 'high', 'label': '契合 × 经历史恢复'},
    3: {'fit': 'mismatch', 'friction': 'low', 'label': '偏离 × 一步恢复'},
    4: {'fit': 'mismatch', 'friction': 'high', 'label': '偏离 × 经历史恢复'},
}
DURATION_FIELDS = {
    'main_brief': '阅读简报参考时长',
    'main_draft': '独立原稿时长',
    'main_edit': '编辑定稿时长',
    'timing_suggestion': '建议阅读时长',
    'timing_ideas': '撰写两条构思时长',
    'timing_final': '时机终稿时长',
}


def defaults():
    return json.loads(MATERIAL.read_text(encoding='utf-8'))


def validate(raw, *, allow_incomplete=False):
    """Normalize a protocol and enforce publication requirements unless editing a draft."""
    if not isinstance(raw, dict):
        raise ValueError('方案须为对象。')

    def string(obj, key, limit=2000, label=None):
        value = obj.get(key)
        if allow_incomplete and isinstance(value, str) and len(value.strip()) <= limit:
            return value
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f'{label or key}须填写 1–{limit} 个字符。')
        return value.strip()

    def integer(obj, key, low, high, label):
        value = obj.get(key)
        if allow_incomplete and (value is None or type(value) is int):
            return value
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{label}须为 {low}–{high} 的整数。')
        return value

    def array(obj, key, low, high, label):
        value = obj.get(key)
        if not isinstance(value, list) or not low <= len(value) <= high or not all(isinstance(x, dict) for x in value):
            raise ValueError(f'{label}须包含 {low}–{high} 项。')
        return value

    def identifier(obj):
        value = string(obj, 'id', 40, '材料 ID')
        if not allow_incomplete and not STUDY_ID.fullmatch(value):
            raise ValueError(f'材料 ID「{value}」仅可用小写英文字母、数字和连字符，须以字母开头。')
        return value

    def unique(entries, label):
        ids = [e['id'] for e in entries]
        if not allow_incomplete and len(set(ids)) != len(ids):
            raise ValueError(f'{label}的 ID 须唯一。')

    def brief(entry):
        return {'id': identifier(entry), 'title': string(entry, 'title', 100, '简报名称'),
                'brief': string(entry, 'brief', 2000, '简报正文')}

    def lines(entry, key, low, high, label):
        seq = entry.get(key)
        if allow_incomplete:
            low = 0
        if not isinstance(seq, list) or not low <= len(seq) <= high:
            raise ValueError(f'{label}须填写 {low}–{high} 项。')
        values = [string({'v': item}, 'v', 120, label) for item in seq]
        if not allow_incomplete and len(set(values)) != len(values):
            raise ValueError(f'{label}须各不相同。')
        return values

    adult_only = raw.get('adult_only', True)
    if type(adult_only) is not bool:
        raise ValueError('参与资格选项无效。')
    result = {
        'title': string(raw, 'title', 100, '实验名称'),
        'label': string(raw, 'label', 70, '方案版本说明'),
        'contact': string(raw, 'contact', 500, '联系说明'),
        'consent': string(raw, 'consent', 4000, '知情说明'),
        'adult_only': adult_only,
        'cap': integer(raw, 'cap', 1, 5000, '招募上限'),
        'durations_seconds': {},
        'main_briefs': [],
        'timing_briefs': [],
    }
    settings = raw.get('durations_seconds')
    if not isinstance(settings, dict):
        raise ValueError('请填写各阶段时长。')
    for stage, label in DURATION_FIELDS.items():
        result['durations_seconds'][stage] = integer(settings, stage, 1, 3600, label)
    for entry in array(raw, 'main_briefs', 1, 8, '主实验简报'):
        data = brief(entry)
        data['intentions'] = []
        for intent in array(entry, 'intentions', 2, 2, f'{data["title"]}的优先意图'):
            draft = intent.get('draft')
            if not isinstance(draft, dict):
                raise ValueError('每项优先意图须配一份 AI 草稿。')
            data['intentions'].append({
                'id': identifier(intent), 'label': string(intent, 'label', 100, '意图标签'),
                'detail': string(intent, 'detail', 400, '意图说明'),
                'draft': {
                    'title': string(draft, 'title', 100, 'AI 草稿标题'),
                    'concept': string(draft, 'concept', 1800, 'AI 草稿正文'),
                    'elements': lines(draft, 'elements', 2, 3, 'AI 草稿关键元素'),
                },
            })
        unique(data['intentions'], f'{data["title"]}的优先意图')
        if not allow_incomplete and data['intentions'][0]['draft'] == data['intentions'][1]['draft']:
            raise ValueError(f'{data["title"]}的两份 AI 草稿须体现不同方向。')
        result['main_briefs'].append(data)
    for entry in array(raw, 'timing_briefs', 2, 8, '时机实验简报'):
        data = brief(entry)
        data['intentions'] = [
            {'id': identifier(x), 'label': string(x, 'label', 100, '意图标签')}
            for x in array(entry, 'intentions', 2, 4, '时机实验优先意图')
        ]
        data['suggestions'] = [
            {'id': identifier(x), 'text': string(x, 'text', 1800, 'AI 建议'),
             'features': lines(x, 'features', 1, 8, '建议特有特征')}
            for x in array(entry, 'suggestions', 2, 2, '每份时机简报的 AI 建议')
        ]
        unique(data['intentions'], f'{data["title"]}的优先意图')
        unique(data['suggestions'], f'{data["title"]}的 AI 建议')
        if not allow_incomplete and data['suggestions'][0]['text'] == data['suggestions'][1]['text']:
            raise ValueError(f'{data["title"]}的两条建议须有不同内容。')
        result['timing_briefs'].append(data)
    unique(result['main_briefs'] + result['timing_briefs'], '全部任务简报')
    return result


def create_initial(quick=False):
    config = copy.deepcopy(defaults())
    if quick:
        config['label'] = '快速演练'
        config['durations_seconds'].update(main_brief=30, main_draft=120, main_edit=120,
                                 timing_suggestion=3, timing_ideas=120, timing_final=120)
    return validate(config)
