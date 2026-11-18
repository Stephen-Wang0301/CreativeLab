"""Public source allowlist and credential checks shared by local tooling."""

import hashlib
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess


ROOT_FILES = {'.gitignore', '.gitattributes', 'AGENTS.md', 'run.py', 'requirements.txt'}
DEPLOY_DOCS = {'version-control.md', 'server.example.json'}
BAD_PARTS = {'data', 'private', '.runtime', '.git', 'backups', 'logs', 'runtime', '__pycache__'}
BAD_SUFFIXES = {'.key', '.pem', '.token', '.sqlite', '.sqlite3', '.db', '.pyc', '.pyd'}


def allowed(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or '\\' in name or ':' in name or '..' in path.parts
            or any(part in BAD_PARTS for part in path.parts)
            or path.suffix.lower() in BAD_SUFFIXES or path.name.startswith('.env')):
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES or path.suffix == '.bat'
    parent, suffix = path.parts[0], path.suffix
    if parent == 'study':
        return suffix in {'.py', '.js', '.css', '.html', '.json'}
    if parent == 'tests':
        return len(path.parts) == 2 and suffix == '.py'
    if parent == 'deployment':
        return len(path.parts) == 2 and (suffix == '.py' or path.name in DEPLOY_DOCS
                                        or path.name == 'Project_Tools.ps1')
    if parent == '.github':
        return suffix in {'.md', '.yml', '.yaml'}
    return parent == '.githooks' and len(path.parts) == 2 and path.name in {'pre-commit', 'pre-push'}


def git(root, *args, check=True, input=None):
    return subprocess.run(['git', '-C', str(root), *args], input=input, capture_output=True,
                          check=check)


def source_files(root):
    result = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    files = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    return [name for name in files if (root / name).is_file()]


def private_values(root):
    """Read local private values for exact-match checks; never display them."""
    values = set()
    for folder in (root / 'data/_system', root / 'data/quick-demo/_system', root / 'private',
                   root / '.runtime/migration-final/private',
                   root / '.runtime/server-migration-20261118-141801/private'):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.suffix in {'.key', '.token'} and path.is_file():
                value = path.read_bytes().strip()
                if len(value) >= 12:
                    values.add(value)
        database = folder / 'study.sqlite3'
        if database.is_file():
            with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
                for row in db.execute('SELECT invite FROM modes'):
                    if row[0] and len(row[0]) >= 12:
                        values.add(row[0].encode())
    return values


PATTERNS = [
    re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    re.compile(rb'\beyJ[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'),
    re.compile(rb'#invite=[A-Za-z0-9_-]{12,}'),
]


def check_contents(files, values=()):
    """Return filenames and reasons, never the matching credential."""
    errors = []
    for name, content in files:
        if not allowed(name):
            errors.append((name, 'path is outside the public source allowlist'))
        elif any(value in content for value in values) or any(p.search(content) for p in PATTERNS):
            errors.append((name, 'possible private credential or invitation'))
    return errors


def digest(content):
    return hashlib.sha256(content).hexdigest()
