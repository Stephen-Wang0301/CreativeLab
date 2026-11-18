"""One personal invitation per attempt; readable labels are not credentials."""

import hashlib
import hmac
import re
import secrets

from .errors import StudyError
from .catalog import release_info
from .storage import timestamp

PERSONAL_TOKEN = re.compile(r'^I([0-9a-f]{24})_([0-9a-f]{64})$')


class Invitations:
    def __init__(self, study):
        self.study = study
        self.store = study.store

    def token(self, row):
        message = f"personal-invite|{row['id']}|{row['mode']}|{row['revision']}|{row['participant_no']}"
        signature = hmac.new(self.store.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return 'I' + row['id'] + '_' + signature

    def resolve(self, db, mode, value):
        match = PERSONAL_TOKEN.fullmatch(value or '') if isinstance(value, str) else None
        if not match:
            return None
        row = db.execute('SELECT * FROM invitations WHERE id=? AND mode=?', (match[1], mode)).fetchone()
        if not row or row['disabled'] or not hmac.compare_digest(value, self.token(row)):
            raise StudyError('个人邀请无效或已停用，请联系研究人员。', 403)
        return row

    def describe(self, db, row, include_token=True):
        version = self.store.revision(db, row['revision'])
        result = {'id': row['id'], 'label': row['label'], 'participant_code': row['participant_code'],
                  'version_code': version['version_code'], 'revision': row['revision'],
                  'created': timestamp(row['created']), 'used': bool(row['participant']),
                  'participant': row['participant'], 'disabled': bool(row['disabled'])}
        result.update(release_info(version))
        if include_token:
            result['token'] = self.token(row)
        return result

    def create(self, mode, revision=None, participant=None, db=None):
        if db is None:
            with self.store.transaction() as transaction:
                return self.create(mode, revision, participant, transaction)
        record = self.study.mode_status(db, mode)
        if participant is not None:
            person = db.execute('SELECT * FROM participants WHERE mode=? AND id=?', (mode, participant)).fetchone()
            if not person:
                raise StudyError('找不到此参与者。', 404)
            existing = db.execute('SELECT * FROM invitations WHERE participant_code=?', (person['participant_code'],)).fetchone()
            if existing:
                return self.describe(db, existing)
            revision, number, code = person['revision'], person['participant_no'], person['participant_code']
        else:
            revision = record['revision'] if revision is None else revision
            if not revision or revision != record['revision']:
                raise StudyError('请为当前已发布版本生成个人邀请。', 409)
            number, code = self.store.allocate_participant(db, revision)
        ident = secrets.token_hex(12)
        label = code  # Only the participant alias is displayed; the signed token authenticates.
        db.execute('''INSERT INTO invitations
            (id,mode,revision,participant_no,participant_code,label,created,participant,disabled)
            VALUES (?,?,?,?,?,?,?,?,0)''',
            (ident, mode, revision, number, code, label, self.study.clock(), participant))
        return self.describe(db, db.execute('SELECT * FROM invitations WHERE id=?', (ident,)).fetchone())

    def list(self, mode, revision, offset=0):
        with self.store.lock:
            self.study.mode_status(self.store.db, mode)
            return [self.describe(self.store.db, row) for row in self.store.db.execute(
                '''SELECT * FROM invitations WHERE mode=? AND revision=? AND participant IS NULL
                   AND disabled=0 ORDER BY created DESC,id DESC LIMIT 11 OFFSET ?''',
                (mode, revision, offset))]

    def claim(self, mode, value):
        """A personal link may authenticate its existing attempt, never create a second one."""
        with self.store.transaction() as db:
            invitation = self.resolve(db, mode, value)
            if not invitation or not invitation['participant']:
                return None
            pid = invitation['participant']
            token = secrets.token_urlsafe(32)
            db.execute('UPDATE participants SET token=?,serial=serial+1 WHERE id=?',
                       (hashlib.sha256(token.encode()).hexdigest(), pid))
            self.store.event(db, pid, self.study.clock(), 'access', 'personal_link_opened', invitation=invitation['id'])
        self.store.sync(pid)
        return token
