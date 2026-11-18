"""Controlled Linux code deployment; live data and machine settings stay in place."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from version_policy import allowed, digest


def source(root):
    result = {}
    for current, directories, names in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root)
        if relative == Path('.'):
            directories[:] = [n for n in directories if n in {'study', 'tests', 'deployment', '.github', '.githooks'}]
        else:
            directories[:] = [n for n in directories if n != '__pycache__' and not (Path(current) / n).is_symlink()]
        for name in names:
            path = Path(current) / name
            relative_name = path.relative_to(root).as_posix()
            if allowed(relative_name):
                if path.is_symlink():
                    raise RuntimeError('A managed source path is a symlink: ' + relative_name)
                result[relative_name] = path.read_bytes()
    return result


def fingerprints(files):
    return {name: digest(content) for name, content in files.items()}


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def write_archive(destination, files):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    destination.chmod(0o600)


def read_archive(path, release=False):
    files, manifest, seen = {}, None, set()
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(m.file_size for m in members) > 20 * 1024 * 1024:
            raise RuntimeError('The code archive exceeds the source-only size limit.')
        for member in members:
            if member.filename in seen or member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Duplicate, directory or linked archive entry.')
            seen.add(member.filename)
            if release and member.filename == 'release.json':
                manifest = json.loads(archive.read(member))
                continue
            name = member.filename
            if release:
                if not name.startswith('source/'):
                    raise RuntimeError('Unexpected archive layout.')
                name = name[len('source/'):]
            if not allowed(name):
                raise RuntimeError('The archive contains a private or unexpected path: ' + name)
            files[name] = archive.read(member)
    if 'run.py' not in files or 'study/http.py' not in files:
        raise RuntimeError('The archive is missing the application source.')
    if release and (not manifest or manifest.get('files') != fingerprints(files)):
        raise RuntimeError('Release hashes do not match the archive.')
    return manifest, files


def service(name, action):
    subprocess.run(['sudo', '-n', 'systemctl', action, name], check=True, capture_output=True)


def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3) as response:
            result = json.load(response)
        if result.get('service') != 'creative-study':
            return False
        for url in ('http://127.0.0.1:8765/p/pilot/', 'http://127.0.0.1:8766/research/'):
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200:
                    return False
        return True
    except (OSError, ValueError):
        return False


def wait_healthy():
    for _ in range(25):
        if healthy():
            return
        time.sleep(1)
    raise RuntimeError('Application health check failed after restart.')


def data_summary(root):
    result = {}
    for name in ('data', 'data/quick-demo'):
        path = root / name / '_system/study.sqlite3'
        if path.is_file():
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                result[name] = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                for table in ('participants', 'revisions', 'protocol_drafts')}
            finally:
                db.close()
    return result


def state(root):
    current = root / '.runtime/current-release.json'
    return {'files': fingerprints(source(root)), 'healthy': healthy(), 'data': data_summary(root),
            'release': json.loads(current.read_text()) if current.exists() else None}


def safe_path(root, name):
    if not allowed(name):
        raise RuntimeError('Refusing to modify an unmanaged path.')
    path = root / name
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path, *path.parents]):
        raise RuntimeError('Refusing to follow a source path link.')
    return path


def replace_source(root, old, new):
    for name in sorted(set(old) - set(new)):
        safe_path(root, name).unlink(missing_ok=True)
    for name, content in sorted(new.items()):
        path = safe_path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(0o600)
        temporary.replace(path)


def preflight(root, files):
    with tempfile.TemporaryDirectory(prefix='preflight-', dir=root / '.runtime') as directory:
        stage = Path(directory)
        replace_source(stage, {}, files)
        (stage / 'runtime').symlink_to(root / 'runtime', target_is_directory=True)
        ports = []
        with socket.socket() as first, socket.socket() as second:
            for listener in (first, second):
                listener.bind(('127.0.0.1', 0))
                ports.append(str(listener.getsockname()[1]))
        result = subprocess.run([str(root / '.venv/bin/python'), str(stage / 'run.py'), '--check',
            '--local-only', '--data', str(stage / 'check-data'), '--port', ports[0], '--admin-port', ports[1]],
            capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('Staged startup check failed:\n' + result.stderr[-2000:])
        for name, content in files.items():
            if name.endswith('.py'):
                compile(content, name, 'exec')


def apply(root, service_name, files, manifest, expected):
    old = source(root)
    if expected != fingerprints(old):
        raise RuntimeError('Server code changed before deployment; nothing was replaced.')
    preflight(root, files)
    ident = manifest['id']
    if not re.fullmatch(r'[0-9TZ]+-[a-f0-9]{10}', ident):
        raise RuntimeError('Invalid release identifier.')
    folder = root / '.runtime/releases' / ident
    folder.mkdir(parents=True, exist_ok=False)
    write_archive(folder / 'before.zip', old)
    write_archive(folder / 'after.zip', files)
    # Online SQLite snapshots are taken before stopping the application.
    backup = subprocess.run([str(root / '.venv/bin/python'), str(root / 'deployment/backup.py')],
                            check=True, capture_output=True, text=True, timeout=120)
    backup_info = json.loads(backup.stdout)
    record = {**manifest, 'backup': backup_info['archive'], 'status': 'prepared'}
    (folder / 'release.json').write_text(json.dumps(record, indent=2) + '\n')
    if expected != fingerprints(source(root)):
        raise RuntimeError('Server source changed during the preflight; deployment cancelled.')
    previous_current = root / '.runtime/current-release.json'
    previous_record = previous_current.read_bytes() if previous_current.exists() else None
    service(service_name, 'stop')
    try:
        replace_source(root, old, files)
        if fingerprints(source(root)) != fingerprints(files):
            raise RuntimeError('The written code failed its checksum verification.')
        service(service_name, 'start')
        wait_healthy()
    except Exception as error:
        service(service_name, 'stop')
        replace_source(root, source(root), old)
        service(service_name, 'start')
        wait_healthy()
        record['status'] = 'failed; previous code restored'
        (folder / 'release.json').write_text(json.dumps(record, indent=2) + '\n')
        if previous_record is not None:
            previous_current.write_bytes(previous_record)
        raise RuntimeError('Deployment failed. Previous code is running; live data was not restored: ' + str(error)) from error
    record['status'] = 'active'
    (folder / 'release.json').write_text(json.dumps(record, indent=2) + '\n')
    previous_current.write_text(json.dumps(record, indent=2) + '\n')
    return {'release_id': ident, 'kind': manifest['kind'], 'git_commit': manifest.get('git_commit'),
            'healthy': True, 'data': data_summary(root), 'backup': record['backup'],
            'rollback_before_this_deploy': 'before-' + ident}


def main():
    import fcntl
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--service', default='creative-study')
    parser.add_argument('action', choices=('status', 'apply', 'rollback', 'export'))
    parser.add_argument('argument', nargs='?')
    args = parser.parse_args()
    root = args.root.resolve()
    if (root != args.root.absolute() or not str(root).startswith('/home/')
            or not (root / 'data/_system/study.sqlite3').is_file()
            or not re.fullmatch(r'[A-Za-z0-9_-]+', args.service)):
        raise RuntimeError('Unexpected project root or service.')
    local = root / '.runtime'
    local.mkdir(exist_ok=True)
    with (local / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'status':
            result = state(root)
        elif args.action == 'export':
            destination = local / ('code-' + stamp() + '.zip')
            write_archive(destination, source(root))
            result = {'archive': str(destination), 'sha256': digest(destination.read_bytes())}
        elif args.action == 'apply':
            bundle = Path(args.argument).resolve()
            if bundle.parent != local or bundle.suffix != '.zip':
                raise RuntimeError('Unexpected upload location.')
            manifest, files = read_archive(bundle, release=True)
            result = apply(root, args.service, files, manifest, manifest['expected_server_files'])
        else:
            target = args.argument or ''
            before = target.startswith('before-')
            ident = target.removeprefix('before-')
            if not re.fullmatch(r'[0-9TZ]+-[a-f0-9]{10}', ident):
                raise RuntimeError('Invalid rollback identifier.')
            _, files = read_archive(local / 'releases' / ident / ('before.zip' if before else 'after.zip'))
            manifest = {'id': stamp() + '-' + digest(json.dumps(fingerprints(files), sort_keys=True).encode())[:10],
                        'kind': 'code-rollback', 'target_release': target}
            result = apply(root, args.service, files, manifest, fingerprints(source(root)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
