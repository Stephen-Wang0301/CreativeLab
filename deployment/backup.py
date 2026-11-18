"""Create a portable site backup with consistent SQLite snapshots."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from study.storage import Store


def snapshot_data(source, destination):
    databases = sorted(source.rglob('study.sqlite3'))
    if not databases:
        raise RuntimeError('The data directory has no study database.')
    for database in databases:
        target = destination / database.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as live, closing(sqlite3.connect(target)) as snapshot:
            live.backup(snapshot)
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('The database snapshot failed its integrity check.')
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
        '.server.lock', 'study.sqlite3', 'study.sqlite3-wal', 'study.sqlite3-shm'))
    counts = {}
    for database in databases:
        relative = database.relative_to(source)
        store = Store((destination / relative).parent.parent)
        try:
            store.db.execute('UPDATE participants SET mirrored=-1')
            store.sync_pending()
            if store.mirror_errors or store.catalog_errors:
                raise RuntimeError('Some participant files could not be regenerated from the snapshot.')
            counts[str(relative.parent.parent)] = {
                'participants': store.db.execute('SELECT count(*) FROM participants').fetchone()[0],
                'revisions': store.db.execute('SELECT count(*) FROM revisions').fetchone()[0],
                'batches': [
                    {**{k: store.revision(store.db, row['id'])[k] for k in
                        ('mode', 'version_code', 'batch_id', 'data_directory')},
                     'participants': row['n']}
                    for row in store.db.execute('''SELECT r.id,count(p.id) AS n FROM revisions r
                        LEFT JOIN participants p ON p.revision=r.id GROUP BY r.id ORDER BY r.mode,r.version_no''')],
            }
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            store.db.close()
    return counts


def create_backup(data, output, token_file):
    if output.is_relative_to(data):
        raise ValueError('Choose a backup directory outside the live data directory.')
    output.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc)
    archive = output / ('creative-study-' + created.strftime('%Y%m%d-%H%M%S-%f') + '.tar.gz')
    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=output) as folder:
        staging = Path(folder)
        counts = snapshot_data(data, staging / 'data')
        if token_file.is_file():
            (staging / 'private').mkdir()
            shutil.copy2(token_file, staging / 'private' / 'cloudflared.token')
        (staging / 'backup-manifest.json').write_text(json.dumps({
            'created_utc': created.isoformat(), 'data': counts,
            'tunnel_credential_included': token_file.is_file(),
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        def portable(info):
            path = Path(info.name)
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyd') or path.name in (
                    '.server.lock', 'study.sqlite3-wal', 'study.sqlite3-shm'):
                return None
            info.mode = 0o700 if info.isdir() else 0o600
            return info

        temporary = staging / 'backup.tar.gz'
        with tarfile.open(temporary, 'w:gz') as bundle:
            for name in ('run.py', 'requirements.txt', 'README.md', '.creative-study-004',
                         'study', 'runtime/packages', 'deployment'):
                if (BASE / name).exists():
                    bundle.add(BASE / name, arcname=name, filter=portable)
            for name in ('data', 'private', 'backup-manifest.json'):
                if (staging / name).exists():
                    bundle.add(staging / name, arcname=name, filter=portable)
        temporary.replace(archive)
    archive.chmod(0o600)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(
        digest + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'sha256': digest, 'data': counts}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=BASE / 'data')
    parser.add_argument('--output', type=Path, default=BASE / 'backups')
    parser.add_argument('--token-file', type=Path, default=BASE / 'private' / 'cloudflared.token')
    args = parser.parse_args()
    create_backup(args.data.expanduser().resolve(), args.output.expanduser().resolve(),
                  args.token_file.expanduser().resolve())
