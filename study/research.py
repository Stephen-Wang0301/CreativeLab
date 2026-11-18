import collections
import csv
import io
import json
import math
import re
import statistics
import tempfile
import zipfile

from .config import GROUPS
from .engine import PHASE_NAMES, StudyError, find
from .storage import digest, dumps, timestamp


QUALITY = '1 = 很低；4 = 中等；7 = 很高。原创性和实用性分别判断。'
FIT = '1 = 几乎没有体现；4 = 有一定体现但并不突出；7 = 是作品的核心。'
TASKS = {
    'h1': ('H1 · 建议特征采纳', '按顺序逐项判断终稿是否体现预定义特征。1 = 体现，0 = 未体现。'
           '未列出的特征列留空。公共简报通用要求不算特有特征。'),
    'h2': ('H2 · 实质性接回', '对照简报、本人原稿、AI 草稿和文字定稿。本人原稿特有元素重新出现并得到发展记 1，'
           '其余记 0。排除简报规定和 AI 草稿共有元素。备注可列出观察到的元素。'),
    'h3': ('H3 · 文字意图契合度', '以封存意图为参照独立评分。' + FIT),
    'h4': ('H4 · 图像意图契合度', '以封存意图为参照独立评分。' + FIT),
    'quality_main': ('主任务 · 文字创意质量', '对照公共简报独立评分。' + QUALITY),
    'quality_timing': ('时机任务 · 终稿创意质量', '对照公共简报独立评分。' + QUALITY),
    'quality_image': ('主任务 · 图像创意质量', '对照公共简报独立评分。' + QUALITY),
    'breadth': ('时机任务 · 构思类别跨度', '比较两条构思所采用的设计方向。同一类别记 1；不同类别记 2。'
                '可在备注写出采用的类别标签。'),
}
FEATURE_COLS = [f'feature_{i}' for i in range(1, 9)]


def csv_value(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list, tuple)):
        value = dumps(value)
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def csv_bytes(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({name: csv_value(row.get(name)) for name in fields})
    return stream.getvalue().encode('utf-8-sig')


def score_fields(task):
    if task == 'h1':
        return FEATURE_COLS
    if task.startswith('quality_'):
        return ['originality', 'usefulness']
    return ['score']


def draft_text(draft):
    return '\n'.join(filter(None, [draft.get('title'), draft.get('concept'),
                                   ' / '.join(filter(None, draft.get('elements', [])))]))


def blind_id(row, task):
    return task.upper() + '-' + digest(row['blind'] + '|' + task)[:24].upper()


def file_archive():
    return tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode='w+b')


class Research:
    def __init__(self, study):
        self.study = study
        self.store = study.store

    def select(self, mode, revision=None):
        db = self.store.db
        active = self.study.mode_status(db, mode)['revision']
        if revision is None:
            revision = active
        else:
            try:
                revision = int(revision)
            except (TypeError, ValueError) as error:
                raise StudyError('方案版本无效。') from error
            if not db.execute('SELECT 1 FROM revisions WHERE mode=? AND id=?', (mode, revision)).fetchone():
                raise StudyError('找不到所选方案版本。', 404)
        rows = list(db.execute('SELECT * FROM participants WHERE mode=? AND revision=? ORDER BY created,id',
                               (mode, revision))) if revision is not None else []
        return revision, rows

    @staticmethod
    def summary(row, state):
        return {'id': row['id'], 'revision': row['revision'], 'phase': state['phase'],
                'phase_name': PHASE_NAMES[state['phase']], 'group': state['main'].get('group'),
                'main_brief': state['main']['brief'], 'timing_brief': state['timing'].get('brief'),
                'timing_suggestion': state['timing'].get('suggestion'), 'timing_entry': state['timing'].get('entry'),
                'created': timestamp(row['created']), 'updated': timestamp(row['updated']),
                'missing': len(state['missing']), 'synced': row['serial'] == row['mirrored'],
                'main_complete': state['main'].get('final_complete', False),
                'timing_complete': state['timing'].get('final_complete', False),
                'image_status': state['image']['status']}

    def overview(self, mode, revision):
        with self.store.lock:
            revision, rows = self.select(mode, revision)
            participants = [self.summary(row, json.loads(row['data'])) for row in rows]
            groups = [
                {'group': key, 'label': GROUPS[key]['label'],
                 'assigned': sum(p['group'] == key for p in participants),
                 'text': sum(p['group'] == key and p['main_complete'] for p in participants),
                 'image': sum(p['group'] == key and p['image_status'] == 'selected' for p in participants),
                 'early': sum(p['group'] == key and p['timing_entry'] == 'early' for p in participants),
                 'late': sum(p['group'] == key and p['timing_entry'] == 'late' for p in participants),
                 'timing_final': sum(p['group'] == key and p['timing_complete'] for p in participants)}
                for key in GROUPS
            ]
        return {'revision': revision, 'participants': participants, 'groups': groups,
                'enrolled': len(participants), 'assigned': sum(g['assigned'] for g in groups),
                'completed': sum(p['phase'] == 'done' and p['timing_complete'] and p['main_complete'] for p in participants),
                'withdrawn': sum(p['phase'] == 'withdrawn' for p in participants),
                'phases': dict(collections.Counter(p['phase_name'] for p in participants)),
                'images': dict(collections.Counter(p['image_status'] for p in participants)),
                'unsynced': [p['id'] for p in participants if not p['synced']]}

    def participant(self, mode, pid):
        row = self.store.db.execute('SELECT * FROM participants WHERE mode=? AND id=?', (mode, pid)).fetchone()
        if not row:
            raise StudyError('找不到此参与者。', 404)
        return row

    def detail(self, mode, pid):
        with self.store.lock:
            return self.store.export(self.store.db, self.participant(mode, pid))

    def item(self, row, protocol, task):
        state = json.loads(row['data'])
        if state['phase'] == 'withdrawn':
            return None
        main, timing = state['main'], state['timing']
        main_brief = find(protocol['main_briefs'], main['brief'])
        timing_brief = find(protocol['timing_briefs'], timing['brief']) if 'brief' in timing else None
        has_main, has_timing = main.get('final_complete'), timing.get('final_complete')
        image = state['image'].get('selected')
        data = {'artifact_id': blind_id(row, task)}
        if task == 'h1' and has_timing:
            suggestion = find(timing_brief['suggestions'], timing['suggestion'])
            data.update(brief=timing_brief['brief'], final=draft_text(timing['final']),
                        features=suggestion['features'])
        elif task == 'h2' and has_main:
            data.update(brief=main_brief['brief'], original=draft_text(main['original']),
                        ai_draft=draft_text(main['ai']), final=draft_text(main['final']))
        elif task == 'h3' and has_main:
            intent = find(main_brief['intentions'], main['intent'])
            data.update(intention=intent['label'] + '\n' + intent['detail'], final=draft_text(main['final']))
        elif task == 'h4' and image:
            intent = find(main_brief['intentions'], main['intent'])
            data.update(intention=intent['label'] + '\n' + intent['detail'], image=image)
        elif task == 'quality_main' and has_main:
            data.update(brief=main_brief['brief'], final=draft_text(main['final']))
        elif task == 'quality_timing' and has_timing:
            data.update(brief=timing_brief['brief'], final=draft_text(timing['final']))
        elif task == 'quality_image' and image:
            data.update(brief=main_brief['brief'], image=image)
        elif task == 'breadth' and timing.get('ideas_complete'):
            data.update(brief=timing_brief['brief'], **timing['ideas'])
        else:
            return None
        return data

    def packet_items(self, mode, revision, task):
        if task not in TASKS:
            raise StudyError('评分任务无效。', 404)
        revision, rows = self.select(mode, revision)
        if revision is None:
            return []
        protocol = self.store.revision(self.store.db, revision)['protocol']
        items = []
        for row in rows:
            item = self.item(row, protocol, task)
            if item:
                if 'image' in item and not (self.store.participant_path(row) / 'images' / item['image']).is_file():
                    continue
                items.append((row, item))
        return sorted(items, key=lambda pair: pair[1]['artifact_id'])

    def packet(self, mode, revision, task):
        with self.store.lock:
            items = self.packet_items(mode, revision, task)
        if not items:
            raise StudyError('该任务目前没有可供评分的材料。', 404)
        artifact_rows = []
        stream = file_archive()
        try:
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                for row, item in items:
                    data = dict(item)
                    if 'image' in data:
                        file = self.store.participant_path(row) / 'images' / data['image']
                        safe_name = 'artifacts/' + data['artifact_id'] + file.suffix
                        archive.write(file, safe_name, compress_type=zipfile.ZIP_STORED)
                        data['image'] = safe_name
                    if 'features' in data:
                        data['features'] = '\n'.join(f'{i}　{feature}' for i, feature in enumerate(data['features'], 1))
                    artifact_rows.append(data)
                archive.writestr('artifacts.csv', csv_bytes(list(artifact_rows[0]), artifact_rows))
                archive.writestr('ratings.csv', csv_bytes(['artifact_id', *score_fields(task), 'notes'], artifact_rows))
                archive.writestr('评分说明.txt', '\n'.join([
                    TASKS[task][0], '', TASKS[task][1], '',
                    '请独立评审。artifacts.csv 是阅读材料，ratings.csv 是填写模板。',
                    '保留 artifact_id 和表头，用 CSV UTF-8 格式保存。未评分的行可留空，备注可选。',
                ]))
        except BaseException:
            stream.close()
            raise
        stream.seek(0)
        return stream

    def import_ratings(self, mode, revision, body):
        task, rater, content = body.get('task'), body.get('rater'), body.get('csv')
        if not isinstance(task, str) or task not in TASKS:
            raise StudyError('请选择评分任务。')
        if not isinstance(rater, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', rater):
            raise StudyError('评审编号须为 1–32 位字母、数字、短横或下划线。')
        if not isinstance(content, str) or len(content) > 2_000_000:
            raise StudyError('请选择 2 MB 以内的评分 CSV。')
        changed = set()
        with self.store.transaction() as db:
            items = self.packet_items(mode, revision, task)
            eligible = {item['artifact_id']: (row['id'], item) for row, item in items}
            reader = csv.DictReader(io.StringIO(content.removeprefix('\ufeff')))
            fields = score_fields(task)
            if not reader.fieldnames or not {'artifact_id', *fields}.issubset(reader.fieldnames):
                raise StudyError('CSV 表头与所选评分任务不匹配，请使用对应模板。')
            seen = set()
            for index, entry in enumerate(reader, 2):
                key = (entry['artifact_id'] or '').strip()
                raw = [(entry[name] or '').strip() for name in fields]
                if not any(raw):
                    continue
                if key not in eligible or key in seen or None in entry:
                    raise StudyError(f'第 {index} 行作品编号重复、无效或列数不符。')
                seen.add(key)
                pid, item = eligible[key]
                notes = (entry.get('notes') or '').strip()
                if len(notes) > 2000:
                    raise StudyError(f'第 {index} 行备注超过 2000 字。')
                try:
                    if task == 'h1':
                        count = len(item['features'])
                        if any(raw[count:]) or not all(x in ('0', '1') for x in raw[:count]):
                            raise ValueError()
                        score = {'features': [int(x) for x in raw[:count]]}
                    else:
                        allowed = (0, 1) if task == 'h2' else (1, 2) if task == 'breadth' else range(1, 8)
                        score = {name: int(value) for name, value in zip(fields, raw)}
                        if any(value not in allowed for value in score.values()):
                            raise ValueError()
                except ValueError as error:
                    raise StudyError(f'第 {index} 行评分缺失或超出任务量表范围。') from error
                now = self.study.clock()
                db.execute('''INSERT INTO ratings VALUES (?,?,?,?,?,?)
                              ON CONFLICT(participant,task,rater) DO UPDATE SET
                              time=excluded.time,score=excluded.score,notes=excluded.notes''',
                           (pid, task, rater, now, dumps(score), notes))
                db.execute('UPDATE participants SET serial=serial+1 WHERE id=?', (pid,))
                changed.add(pid)
            if not changed:
                raise StudyError('CSV 中没有可导入的有效评分。')
        for pid in changed:
            self.store.sync(pid)
        return {'imported': len(changed)}

    def score_records(self, mode, revision):
        revision, rows = self.select(mode, revision)
        protocol = self.store.revision(self.store.db, revision)['protocol'] if revision is not None else None
        current = self.store.db.execute(
            '''SELECT ratings.* FROM ratings JOIN participants ON participants.id=ratings.participant
               WHERE participants.mode=? AND participants.revision=? ORDER BY task,rater''', (mode, revision))
        scores = collections.defaultdict(list)
        for row in current:
            scores[row['participant'], row['task']].append(json.loads(row['score']))
        records = []
        for row in rows:
            state = json.loads(row['data'])
            main, timing = state['main'], state['timing']
            record = dict(id=row['id'], revision=revision, group=main.get('group'), fit=main.get('fit'),
                          friction=main.get('friction'), main_brief=main['brief'], main_intent=main.get('intent'),
                          ai_draft=main.get('ai_id'), experience=main.get('experience'), entry=timing.get('entry'),
                          timing_brief=timing.get('brief'), timing_suggestion=timing.get('suggestion'),
                          timing_intent=timing.get('intent'), phase=state['phase'], image_status=state['image']['status'],
                          withdrawn=state['phase'] == 'withdrawn', main_complete=bool(main.get('final_complete')),
                          timing_complete=bool(timing.get('final_complete')), draft_method=main.get('original_method'),
                          main_final_method=main.get('final_method'), ideas_method=timing.get('ideas_method'),
                          timing_final_method=timing.get('final_method'))
            for task in TASKS:
                eligible = self.item(row, protocol, task) is not None
                observed = scores[row['id'], task] if eligible else []
                record[task + '_eligible'] = eligible
                record[task + '_raters'] = len(observed)
                record[task] = None
                if len(observed) < 3:
                    continue
                if task == 'h1':
                    record[task] = statistics.mean(statistics.mean(s['features']) for s in observed)
                elif task == 'h2':
                    average = statistics.mean(s['score'] for s in observed)
                    record[task] = int(average > .5) if average != .5 else None
                elif task.startswith('quality_'):
                    for name in ('originality', 'usefulness'):
                        record[task + '_' + name] = statistics.mean(s[name] for s in observed)
                    record[task] = statistics.mean((record[task + '_originality'], record[task + '_usefulness']))
                else:
                    record[task] = statistics.mean(s['score'] for s in observed)
            records.append(record)
        return records

    def analysis(self, mode, revision):
        with self.store.lock:
            records = self.score_records(mode, revision)
        coverage = [
            {'task': task, 'label': label, 'eligible': sum(record[task + '_eligible'] for record in records),
             'rated': sum(record[task] is not None for record in records)}
            for task, (label, _) in TASKS.items()
        ]

        def contrast(task, field, specification):
            cells = []
            for key, weight in specification:
                values = [r[task] for r in records if r[field] == key and r[task] is not None]
                cells.append({'cell': key, 'weight': weight, 'n': len(values),
                              'mean': statistics.mean(values) if values else None,
                              'variance': statistics.variance(values) if len(values) > 1 else None})
            estimate = None if any(cell['n'] == 0 for cell in cells) else sum(cell['weight'] * cell['mean'] for cell in cells)
            interval = None
            if estimate is not None and all(cell['variance'] is not None for cell in cells):
                margin = 1.96 * math.sqrt(sum(cell['weight'] ** 2 * cell['variance'] / cell['n'] for cell in cells))
                interval = [estimate - margin, estimate + margin]
            return {'task': task, 'cells': cells, 'estimate': estimate, 'interval': interval}

        return {'coverage': coverage, 'contrasts': [
            contrast('h1', 'entry', [('early', 1), ('late', -1)]),
            contrast('h2', 'group', [(3, 1), (4, -1)]),
            contrast('h3', 'group', [(3, 1), (4, -1), (1, -1), (2, 1)]),
            contrast('h4', 'group', [(3, 1), (4, -1)]),
        ]}

    def analysis_csv(self, mode, revision):
        with self.store.lock:
            records = self.score_records(mode, revision)
        if not records:
            raise StudyError('所选方案版本尚无参与者。', 404)
        fields = list(dict.fromkeys(key for record in records for key in record))
        return csv_bytes(fields, records)

    def raw_zip(self, mode, revision=None, pid=None):
        with self.store.lock:
            db = self.store.db
            if pid:
                rows = [self.participant(mode, pid)]
                revision = rows[0]['revision']
            else:
                revision, rows = self.select(mode, revision)
            if not rows:
                raise StudyError('所选方案版本尚无参与者。', 404)
            bundles = [(row, self.store.export(db, row)) for row in rows]
            protocol = self.store.revision(db, revision)
        stream = file_archive()
        try:
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('protocol.json', dumps(protocol, pretty=True))
                summary = []
                ratings = []
                for row, bundle in bundles:
                    summary.append(self.summary(row, bundle['responses']))
                    for name, value in bundle.items():
                        if name in ('events', 'versions'):
                            data = ''.join(dumps(item) + '\n' for item in value)
                            archive.writestr(f'{row["id"]}/{name}.jsonl', data)
                        else:
                            archive.writestr(f'{row["id"]}/{name}.json', dumps(value, pretty=True))
                    for rating in bundle['ratings']:
                        ratings.append(dict(id=row['id'], **rating))
                    for record in bundle['responses']['image']['rounds']:
                        for filename in record['files']:
                            file = self.store.participant_path(row) / 'images' / filename
                            if file.is_file():
                                archive.write(file, f'{row["id"]}/images/{filename}',
                                              compress_type=zipfile.ZIP_STORED)
                archive.writestr('participants.csv', csv_bytes(list(summary[0]), summary))
                archive.writestr('ratings_long.csv', csv_bytes(['id', 'task', 'rater', 'at', 'score', 'notes'], ratings))
        except BaseException:
            stream.close()
            raise
        stream.seek(0)
        return stream
