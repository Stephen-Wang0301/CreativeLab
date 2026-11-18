import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .catalog import participant_code, version_info, release_info


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
        self.catalog_errors = {}
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
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, revision INTEGER NOT NULL REFERENCES revisions(id),
                participant_no INTEGER NOT NULL, participant_code TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL, created REAL NOT NULL,
                participant TEXT UNIQUE REFERENCES participants(id), disabled INTEGER NOT NULL DEFAULT 0,
                UNIQUE(revision,participant_no)
            );
        """)
        self.migrate_catalog()
        self.preserve_legacy_images()
        self.sync_pending()

    def migrate_catalog(self):
        """Add metadata without replacing IDs, frozen materials, sessions or answers."""
        with self.transaction() as db:
            for table, name, kind in (('revisions', 'version_no', 'INTEGER'),
                                      ('participants', 'participant_no', 'INTEGER'),
                                      ('participants', 'participant_code', 'TEXT')):
                if name not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
            db.execute('''CREATE TABLE IF NOT EXISTS participant_sequences (
                       revision INTEGER PRIMARY KEY REFERENCES revisions(id), last_no INTEGER NOT NULL)''')
            for mode in ('pilot', 'formal'):
                last = db.execute('SELECT coalesce(max(version_no),0) FROM revisions WHERE mode=?', (mode,)).fetchone()[0]
                for row in list(db.execute('SELECT id FROM revisions WHERE mode=? AND version_no IS NULL ORDER BY id', (mode,))):
                    last += 1
                    db.execute('UPDATE revisions SET version_no=? WHERE id=?', (last, row['id']))
            for revision in list(db.execute('SELECT * FROM revisions ORDER BY id')):
                last = db.execute('SELECT coalesce(max(participant_no),0) FROM participants WHERE revision=?',
                                  (revision['id'],)).fetchone()[0]
                for row in list(db.execute('SELECT id,mode FROM participants WHERE revision=? AND participant_code IS NULL ORDER BY created,id',
                                           (revision['id'],))):
                    if row['mode'] != revision['mode']:
                        raise RuntimeError('Existing participant and version modes do not match.')
                    last += 1
                    code = participant_code(row['mode'], revision['version_no'], last, self.root.name == 'quick-demo')
                    db.execute('UPDATE participants SET participant_no=?,participant_code=?,mirrored=-1 WHERE id=?',
                               (last, code, row['id']))
                db.execute('''INSERT INTO participant_sequences(revision,last_no) VALUES (?,?)
                              ON CONFLICT(revision) DO UPDATE SET last_no=max(last_no,excluded.last_no)''',
                           (revision['id'], last))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS revision_number ON revisions(mode,version_no)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS participant_code_unique ON participants(participant_code)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS participant_number ON participants(revision,participant_no)')
            db.execute('''CREATE TRIGGER IF NOT EXISTS immutable_revision
                          BEFORE UPDATE OF mode,version_no,protocol,image_provider,created ON revisions
                          WHEN OLD.version_no IS NOT NULL AND
                          (NEW.mode IS NOT OLD.mode OR NEW.version_no IS NOT OLD.version_no OR
                           NEW.protocol IS NOT OLD.protocol OR NEW.image_provider IS NOT OLD.image_provider OR NEW.created IS NOT OLD.created)
                          BEGIN SELECT RAISE(ABORT, 'Published experiment versions are immutable'); END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS preserve_revision BEFORE DELETE ON revisions
                          BEGIN SELECT RAISE(ABORT, 'Experiment versions must be retained'); END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS immutable_participant_batch
                          BEFORE UPDATE OF mode,revision,participant_code,participant_no ON participants
                          WHEN OLD.participant_code IS NOT NULL AND
                          (NEW.mode IS NOT OLD.mode OR NEW.revision IS NOT OLD.revision OR
                           NEW.participant_code IS NOT OLD.participant_code OR NEW.participant_no IS NOT OLD.participant_no)
                          BEGIN SELECT RAISE(ABORT, 'Participant batch identity is immutable'); END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS preserve_participant BEFORE DELETE ON participants
                          BEGIN SELECT RAISE(ABORT, 'Participant records must be retained'); END''')

    def allocate_participant(self, db, revision):
        record = self.revision(db, revision)
        db.execute('INSERT OR IGNORE INTO participant_sequences VALUES (?,0)', (revision,))
        db.execute('UPDATE participant_sequences SET last_no=last_no+1 WHERE revision=?', (revision,))
        number = db.execute('SELECT last_no FROM participant_sequences WHERE revision=?', (revision,)).fetchone()[0]
        return number, participant_code(record['mode'], record['version_no'], number, self.root.name == 'quick-demo')

    def preserve_legacy_images(self):
        """Copy images to the new archive; keep original folders as migration evidence."""
        for row in self.db.execute('SELECT * FROM participants'):
            progress = self.participant_path(row) / 'progress.json'
            try:
                metadata = json.loads(progress.read_text(encoding='utf-8'))
                stale = metadata.get('serial') != row['serial'] or metadata.get('participant_code') != row['participant_code']
            except (OSError, ValueError):
                stale = True
            if stale:
                self.db.execute('UPDATE participants SET mirrored=-1 WHERE id=?', (row['id'],))
            original = self.root / row['mode'] / row['id'] / 'images'
            if not original.is_dir():
                continue
            destination = self.participant_path(row) / 'images'
            destination.mkdir(parents=True, exist_ok=True)
            for image in original.iterdir():
                if not image.is_file() or image.is_symlink():
                    continue
                target = destination / image.name
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(image.read_bytes()).digest():
                        raise RuntimeError('An existing image archive differs; neither copy was replaced.')
                else:
                    shutil.copy2(image, target)

    @staticmethod
    def immutable_file(path, content):
        """Never replace a published snapshot with different content."""
        text = dumps(content, pretty=True) + '\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) != content:
                raise OSError('A published version snapshot differs from the database: ' + str(path))
            return
        fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if json.loads(path.read_text(encoding='utf-8')) != content:
                    raise OSError('A published version snapshot differs from the database: ' + str(path))
        finally:
            temporary.unlink(missing_ok=True)

    def sync_catalog(self):
        with self.lock:
            for row in self.db.execute('SELECT * FROM revisions ORDER BY id'):
                key = str(row['id'])
                try:
                    revision = self.revision(self.db, row['id'])
                    folder = self.root / revision['data_directory']
                    self.immutable_file(folder / 'protocol.json', revision)
                    self.immutable_file(folder / 'batch.json', {
                        key: revision[key] for key in ('id', 'mode', 'stage', 'stage_name', 'version_no',
                                                       'version_code', 'batch_id', 'created', 'data_directory')})
                    release = release_info(revision)
                    self.immutable_file(folder / 'releases' / (release['release_code'] + '.json'),
                                        {'revision': row['id'], **release,
                                         'protocol_snapshot': '../protocol.json'})
                    self.catalog_errors.pop(key, None)
                except (OSError, ValueError) as error:
                    self.catalog_errors[key] = str(error)
                    logging.error('版本归档未完成：%s', error)

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
                'protocol': json.loads(row['protocol']), 'image_provider': row['image_provider'],
                **version_info(row['mode'], row['version_no'])}

    def participant_path(self, row):
        revision = self.revision(self.db, row['revision'])
        return self.root / revision['data_directory'] / 'participants' / row['participant_code']

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
            'identity': {'id': row['id'], 'revision': row['revision'], 'participant_code': row['participant_code'],
                         'participant_no': row['participant_no'], 'mode': row['mode'],
                         **version_info(row['mode'], revision['version_no'])},
            'progress': {'id': row['id'], 'mode': row['mode'], 'revision': row['revision'],
                         'participant_code': row['participant_code'], 'version_code': revision['version_code'],
                         'batch_id': revision['batch_id'],
                         'recovery_count': state.get('recovery_count', 0),
                         'release_code': state.get('last_release', revision['version_code'] + '.0'),
                         'enrolled_release': state.get('enrolled_release', revision['version_code'] + '.0'),
                         'paused': bool(state.get('pause')), 'pause_count': state.get('pause_count', 0),
                         'pause_seconds': state.get('pause_seconds', 0),
                         'restart_to': state.get('restart_to'), 'restart_from': state.get('restart_from'),
                         'invitation_label': state.get('invitation_label'),
                         'serial': row['serial'], 'created_at': timestamp(row['created']),
                         'updated_at': timestamp(row['updated']), 'phase': state['phase'],
                         'deadline_at': timestamp(state['deadline']),
                         'missing': state['missing'], 'withdrawn': state['phase'] == 'withdrawn',
                         'image_status': state['image']['status']},
            'responses': state,
            'assignments': {'id': row['id'], 'participant_code': row['participant_code'],
                            'blind_base': row['blind'], 'revision': row['revision'],
                            'version_code': revision['version_code'], 'batch_id': revision['batch_id'],
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
                self.immutable_file(folder / 'identity.json', bundle['identity'])
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
        self.sync_catalog()
        with self.lock:
            pids = [row['id'] for row in self.db.execute('SELECT id FROM participants WHERE serial<>mirrored')]
        for pid in pids:
            self.sync(pid)
