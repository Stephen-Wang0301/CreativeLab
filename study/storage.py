import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')


def dumps(value, pretty=False):
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data if isinstance(data, bytes) else data.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_or_create_secret(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, 'x', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(32))
            if os.name == 'posix':
                path.chmod(0o600)
        except FileExistsError:
            pass
    return path.read_text(encoding='ascii').strip()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.secret = read_or_create_secret(self.root / '_system' / 'server.key')
        self.admin_key = read_or_create_secret(self.root / '_system' / 'researcher.key')
        self.db = sqlite3.connect(self.root / '_system' / 'study.sqlite3', timeout=15,
                                  check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.mirror_errors = {}
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS modes (
                id TEXT PRIMARY KEY, revision INTEGER, recruiting INTEGER NOT NULL,
                invite TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, created REAL NOT NULL,
                protocol TEXT NOT NULL, image_provider TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS protocol_drafts (
                mode TEXT PRIMARY KEY REFERENCES modes(id), token TEXT NOT NULL, protocol TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, revision INTEGER NOT NULL REFERENCES revisions(id),
                created REAL NOT NULL, updated REAL NOT NULL, token TEXT NOT NULL UNIQUE,
                resume TEXT NOT NULL, nonce TEXT NOT NULL UNIQUE,
                blind TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
                serial INTEGER NOT NULL DEFAULT 1, mirrored INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS participant_mode ON participants(mode, revision);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, participant TEXT NOT NULL REFERENCES participants(id),
                time REAL NOT NULL, phase TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_participant ON events(participant, id);
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, participant TEXT NOT NULL REFERENCES participants(id),
                time REAL NOT NULL, phase TEXT NOT NULL, kind TEXT NOT NULL, contents TEXT NOT NULL,
                parent INTEGER REFERENCES versions(id), source INTEGER REFERENCES versions(id)
            );
            CREATE INDEX IF NOT EXISTS versions_participant ON versions(participant, id);
            CREATE TABLE IF NOT EXISTS blocks (
                scope TEXT PRIMARY KEY, pending TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                participant TEXT NOT NULL REFERENCES participants(id), command TEXT NOT NULL,
                PRIMARY KEY(participant, command)
            );
            CREATE TABLE IF NOT EXISTS ratings (
                participant TEXT NOT NULL REFERENCES participants(id), task TEXT NOT NULL,
                rater TEXT NOT NULL, time REAL NOT NULL, score TEXT NOT NULL, notes TEXT NOT NULL,
                PRIMARY KEY(participant, task, rater)
            );
        """)
        self.sync_pending()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    @staticmethod
    def event(db, pid, now, phase, kind, **details):
        db.execute('INSERT INTO events (participant,time,phase,kind,details) VALUES (?,?,?,?,?)',
                   (pid, now, phase, kind, dumps(details)))

    @staticmethod
    def version(db, pid, now, phase, kind, contents, parent=None, source=None):
        return db.execute(
            'INSERT INTO versions (participant,time,phase,kind,contents,parent,source) VALUES (?,?,?,?,?,?,?)',
            (pid, now, phase, kind, dumps(contents), parent, source)).lastrowid

    @staticmethod
    def save(db, pid, now, data):
        db.execute('UPDATE participants SET data=?, updated=?, serial=serial+1 WHERE id=?',
                   (dumps(data), now, pid))

    @staticmethod
    def revision(db, rev):
        row = db.execute('SELECT * FROM revisions WHERE id=?', (rev,)).fetchone()
        return {'id': rev, 'mode': row['mode'], 'created': timestamp(row['created']),
                'protocol': json.loads(row['protocol']), 'image_provider': row['image_provider']}

    def participant_path(self, row):
        return self.root / row['mode'] / row['id']

    def export(self, db, row):
        state = json.loads(row['data'])
        revision = self.revision(db, row['revision'])
        events = [
            {'id': item['id'], 'at': timestamp(item['time']), 'phase': item['phase'],
             'type': item['kind'], 'details': json.loads(item['details'])}
            for item in db.execute('SELECT * FROM events WHERE participant=? ORDER BY id', (row['id'],))
        ]
        versions = [
            {'id': item['id'], 'at': timestamp(item['time']), 'phase': item['phase'],
             'type': item['kind'], 'contents': json.loads(item['contents']),
             'parent': item['parent'], 'source': item['source']}
            for item in db.execute('SELECT * FROM versions WHERE participant=? ORDER BY id', (row['id'],))
        ]
        ratings = [
            {'task': item['task'], 'rater': item['rater'], 'at': timestamp(item['time']),
             'score': json.loads(item['score']), 'notes': item['notes']}
            for item in db.execute('SELECT * FROM ratings WHERE participant=? ORDER BY task,rater', (row['id'],))
        ]
        protocol = revision['protocol']
        main = next(x for x in protocol['main_briefs'] if x['id'] == state['main']['brief'])
        timing = next((x for x in protocol['timing_briefs'] if x['id'] == state['timing'].get('brief')), None)
        return {
            'progress': {'id': row['id'], 'mode': row['mode'], 'revision': row['revision'],
                         'serial': row['serial'], 'created_at': timestamp(row['created']),
                         'updated_at': timestamp(row['updated']), 'phase': state['phase'],
                         'deadline_at': timestamp(state['deadline']),
                         'missing': state['missing'], 'withdrawn': state['phase'] == 'withdrawn',
                         'image_status': state['image']['status']},
            'responses': state,
            'assignments': {'id': row['id'], 'blind_base': row['blind'], 'revision': row['revision'],
                            'main': state['main'], 'timing': state['timing'],
                            'main_material': main, 'timing_material': timing},
            'events': events, 'versions': versions, 'ratings': ratings,
        }

    def sync(self, pid):
        with self.lock:
            row = self.db.execute('SELECT * FROM participants WHERE id=? AND serial<>mirrored', (pid,)).fetchone()
            if not row:
                return
            try:
                bundle = self.export(self.db, row)
                folder = self.participant_path(row)
                for name in ('responses', 'assignments', 'ratings'):
                    atomic_write(folder / (name + '.json'), dumps(bundle[name], pretty=True) + '\n')
                for name in ('events', 'versions'):
                    atomic_write(folder / (name + '.jsonl'), ''.join(dumps(item) + '\n' for item in bundle[name]))
                atomic_write(folder / 'progress.json', dumps(bundle['progress'], pretty=True) + '\n')
                self.db.execute('UPDATE participants SET mirrored=serial WHERE id=?', (pid,))
                self.mirror_errors.pop(pid, None)
            except OSError as error:
                if pid not in self.mirror_errors:
                    logging.error('无法写入参与者目录 %s：%s', pid, error)
                self.mirror_errors[pid] = str(error)

    def sync_pending(self):
        with self.lock:
            pids = [row['id'] for row in self.db.execute('SELECT id FROM participants WHERE serial<>mirrored')]
        for pid in pids:
            self.sync(pid)
