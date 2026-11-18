"""Print a read-only inventory of the deployed Ubuntu site as JSON."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3


root = Path('/home/user/creative-study')
excluded = {'.venv', '__pycache__', 'backups', 'logs', 'bin'}
files = []
for current, directories, names in os.walk(root, followlinks=False):
    directories[:] = sorted(name for name in directories if name not in excluded)
    for name in sorted(names):
        path = Path(current) / name
        if path.is_symlink() or name.endswith('.pyc'):
            continue
        digest = None
        if not name.startswith('study.sqlite3') and name != '.server.lock':
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
        files.append({'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
                      'sha256': digest})

summaries = {}
invite = None
for relative in ('data', 'data/quick-demo'):
    database = root / relative / '_system/study.sqlite3'
    if not database.is_file():
        continue
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        summaries[relative] = {
            'participants': db.execute('SELECT count(*) FROM participants').fetchone()[0],
            'revisions': db.execute('SELECT count(*) FROM revisions').fetchone()[0],
            'drafts': db.execute('SELECT count(*) FROM protocol_drafts').fetchone()[0],
        }
        if relative == 'data':
            invite = db.execute("SELECT invite FROM modes WHERE id='pilot'").fetchone()[0]

print(json.dumps({'created_utc': datetime.now(timezone.utc).isoformat(), 'root': str(root),
                  'excluded_directories': sorted(excluded), 'files': files,
                  'data_summaries': summaries, 'pilot_invite': invite}, ensure_ascii=False))
