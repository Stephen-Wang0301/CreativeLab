"""Separate working edits, server tests, and user-approved Git milestones."""

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

from version_policy import allowed, check_contents, digest, git, private_values, source_files

BASE = Path(__file__).resolve().parents[1]
LOCAL = BASE / '.runtime'


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def repository():
    actual = git(BASE, 'rev-parse', '--show-toplevel').stdout.decode().strip()
    if Path(actual).resolve() != BASE:
        raise RuntimeError('Use an independent Git repository in this project directory.')


def staged():
    files = []
    for entry in git(BASE, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, sha, stage = metadata.split()
        if mode not in (b'100644', b'100755') or stage != b'0':
            raise RuntimeError('The index contains a symlink, submodule or unresolved conflict.')
        files.append((name.decode('utf-8'), git(BASE, 'cat-file', 'blob', sha.decode()).stdout))
    return files


def committed(ref):
    revision = git(BASE, 'rev-parse', '--verify', ref + '^{commit}').stdout.decode().strip()
    files = []
    for entry in git(BASE, 'ls-tree', '-r', '-z', revision).stdout.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, sha = metadata.split()
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise RuntimeError('The revision contains a symlink or submodule.')
        files.append((name.decode('utf-8'), git(BASE, 'cat-file', 'blob', sha.decode()).stdout))
    return revision, files


def working():
    files = []
    for name in source_files(BASE):
        path = BASE / name
        if not path.resolve().is_relative_to(BASE) or any(p.is_symlink() for p in [path, *path.parents]):
            raise RuntimeError('Source links are not supported: ' + name)
        files.append((name, path.read_bytes().replace(b'\r\n', b'\n')))
    return files


def verify(files):
    errors = check_contents(files, private_values(BASE))
    if errors:
        output({'blocked': [{'file': name, 'reason': reason} for name, reason in errors]})
        raise RuntimeError('Private-source check failed. No matching credential was displayed.')
    return files


def check(args):
    repository()
    files = staged() if args.staged else working()
    verify(files)
    count = 0
    if args.history:
        seen = set()
        for revision in git(BASE, 'rev-list', '--all').stdout.decode().splitlines():
            _, history = committed(revision)
            fresh = [(n, c) for n, c in history if (n, digest(c)) not in seen]
            verify(fresh)
            seen.update((n, digest(c)) for n, c in fresh)
            count += 1
    output({'source_files_checked': len(files), 'history_commits_checked': count,
            'private_files': 'excluded; root README.md stays private'})


def configuration():
    path = BASE / 'private/server.json'
    if not path.is_file():
        raise RuntimeError('Copy deployment/server.example.json to private/server.json and configure it.')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not re.fullmatch(r'[A-Za-z0-9._-]+', config['host']) or not re.fullmatch(r'[A-Za-z0-9_-]+', config['user']):
        raise ValueError('Invalid SSH host or user.')
    if not 1 <= int(config['port']) <= 65535 or not config['root'].startswith('/home/'):
        raise ValueError('Invalid SSH port or project root.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', config['service']):
        raise ValueError('Invalid service name.')
    for key in ('identity', 'known_hosts'):
        config[key] = str((BASE / config[key]).resolve())
        if not Path(config[key]).is_file():
            raise RuntimeError('Missing private SSH configuration: ' + key)
    return config


def connection(config, scp=False):
    return ['scp' if scp else 'ssh', '-P' if scp else '-p', str(config['port']),
            '-i', config['identity'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + config['known_hosts']]


def remote(config, command):
    result = subprocess.run([*connection(config), '-T', config['user'] + '@' + config['host'], command],
                            capture_output=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('SSH command failed:\n' + result.stderr[-2000:] + result.stdout[-2000:])
    return result.stdout


def transfer(config, source, destination, download=False):
    endpoint = config['user'] + '@' + config['host'] + ':'
    args = [endpoint + source, str(destination)] if download else [str(source), endpoint + destination]
    subprocess.run([*connection(config, True), *args], check=True)


def helper(config, action, argument=None):
    # Helpers live outside the running code, so a failed release cannot replace the recovery tool.
    control = config['root'] + '/.runtime/control'
    remote(config, 'mkdir -p ' + shlex.quote(control) + ' && chmod 700 ' + shlex.quote(control))
    for name in ('server_release.py', 'version_policy.py'):
        transfer(config, BASE / 'deployment' / name, control + '/' + name)
    command = [config['root'] + '/.venv/bin/python', control + '/server_release.py',
               '--root', config['root'], '--service', config['service'], action]
    if argument:
        command.append(argument)
    return json.loads(remote(config, shlex.join(command)))


def baseline_path(config):
    tag = digest((config['host'] + ':' + str(config['port']) + config['root']).encode())[:12]
    return LOCAL / ('server-code-' + tag + '.json')


def remember(config, state):
    LOCAL.mkdir(exist_ok=True)
    baseline_path(config).write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')


def deployment(args):
    config = configuration()
    state = helper(config, 'status')
    baseline = baseline_path(config)
    if not baseline.exists():
        raise RuntimeError('Inspect the server first: python deployment/manage.py server-status --accept-baseline')
    previous = json.loads(baseline.read_text(encoding='utf-8'))
    if state['files'] != previous['files']:
        raise RuntimeError('Server code changed since the last inspection. Fetch and review server code before deploying.')
    if args.ref:
        revision, files = committed(args.ref)
    else:
        revision = None
        files = working()
    verify(files)
    hashes = {name: digest(content) for name, content in files}
    content_hash = digest(json.dumps(hashes, sort_keys=True).encode())
    ident = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + content_hash[:10]
    manifest = {'id': ident, 'git_commit': revision, 'kind': 'milestone' if revision else 'working-test',
                'files': hashes, 'expected_server_files': state['files']}
    LOCAL.mkdir(exist_ok=True)
    archive = LOCAL / (ident + '.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('release.json', json.dumps(manifest, ensure_ascii=False))
        for name, content in files:
            bundle.writestr('source/' + name, content)
    incoming = config['root'] + '/.runtime/' + archive.name
    transfer(config, archive, incoming)
    result = helper(config, 'apply', incoming)
    fresh = helper(config, 'status')
    remember(config, fresh)
    output(result)
    output({'verification': 'server code hashes match release' if fresh['files'] == hashes else 'HASH MISMATCH'})
    if fresh['files'] != hashes:
        raise RuntimeError('The deployed code differs from the release manifest.')
    verify_public(config)


def verify_public(config):
    request = urllib.request.Request(config['site'].rstrip('/') + '/healthz',
                                     headers={'User-Agent': 'CreativeStudyHealth/1.0'})
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open(request, timeout=30) as response:
        data = json.load(response)
    if data.get('service') != 'creative-study' or not data.get('https'):
        raise RuntimeError('Public HTTPS health check failed.')
    print('Public HTTPS health check passed.')


def server_status(args):
    config = configuration()
    state = helper(config, 'status')
    if args.accept_baseline:
        remember(config, state)
    summary = {key: value for key, value in state.items() if key not in {'files', 'release'}}
    summary['release'] = ({key: state['release'].get(key) for key in ('id', 'kind', 'git_commit', 'status')}
                          if state['release'] else None)
    output(summary)
    print('Server source files: ' + str(len(state['files'])))


def download_backup(config):
    command = shlex.join([config['root'] + '/.venv/bin/python', config['root'] + '/deployment/backup.py'])
    result = json.loads(remote(config, command))
    path = PurePosixPath(result['archive'])
    if str(path.parent) != config['root'] + '/backups' or not re.fullmatch(r'creative-study-[0-9-]+\.tar\.gz', path.name):
        raise RuntimeError('Unexpected backup path.')
    destination = BASE / 'backups' / path.name
    destination.parent.mkdir(exist_ok=True)
    transfer(config, result['archive'], destination, download=True)
    if digest(destination.read_bytes()) != result['sha256']:
        raise RuntimeError('Backup checksum mismatch.')
    output({'verified_backup': str(destination), 'data': result['data']})


def fetch_code(args):
    config = configuration()
    result = helper(config, 'export')
    name = Path(result['archive']).name
    if not re.fullmatch(r'code-[0-9TZ]+\.zip', name) or result['archive'] != config['root'] + '/.runtime/' + name:
        raise RuntimeError('Unexpected server source archive.')
    destination = LOCAL / name
    transfer(config, result['archive'], destination, download=True)
    if digest(destination.read_bytes()) != result['sha256']:
        raise RuntimeError('Server code checksum mismatch.')
    folder = LOCAL / name.removesuffix('.zip')
    with zipfile.ZipFile(destination) as bundle:
        for member in bundle.infolist():
            if not allowed(member.filename):
                raise RuntimeError('Unexpected path in server code archive.')
        bundle.extractall(folder)
    print('Server source downloaded for comparison: ' + str(folder))
    print('Local source and server data were not replaced. Review differences before accepting a new baseline.')


def github_api(path, data=None):
    # GCM retains the credential. The token stays in memory and is never logged or copied to the server.
    credential = git(BASE, 'credential', 'fill', input=b'protocol=https\nhost=github.com\n\n')
    fields = dict(line.split('=', 1) for line in credential.stdout.decode().splitlines() if '=' in line)
    if not fields.get('password'):
        raise RuntimeError('Sign in to GitHub using Git Credential Manager first.')
    proxy = git(BASE, 'config', '--get', 'http.proxy', check=False).stdout.decode().strip()
    handler = urllib.request.ProxyHandler({'https': proxy} if proxy else {})
    client = urllib.request.build_opener(handler)
    request = urllib.request.Request('https://api.github.com' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + fields['password'], 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'CreativeLab-Version-Tools', 'Content-Type': 'application/json'})
    with client.open(request, timeout=30) as response:
        return json.load(response)


def save(args):
    verify(working())
    print(git(BASE, 'status', '--short').stdout.decode('utf-8'))
    if not args.confirm and input('Type SAVE to keep this stage as a Git commit: ').strip() != 'SAVE':
        print('No commit created.')
        return
    git(BASE, 'add', '--all')
    verify(staged())
    result = git(BASE, 'commit', '-m', args.message)
    print(result.stdout.decode('utf-8'))
    print('Saved locally. Publish to GitHub separately when requested.')


def publish(args):
    check(argparse.Namespace(staged=True, history=True))
    if not args.confirm and input('Type PUBLISH to push confirmed commits to GitHub: ').strip() != 'PUBLISH':
        return
    settings = BASE / 'private/server.json'
    repo = args.repository or (json.loads(settings.read_text(encoding='utf-8-sig')).get('github_repository')
                               if settings.is_file() else None)
    if not repo:
        raise RuntimeError('Specify --repository owner/name; GitHub publishing does not require a server connection.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid GitHub repository name.')
    try:
        existing = github_api('/repos/' + repo)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        account = github_api('/user')
        owner, name = repo.split('/')
        if owner != account['login']:
            raise RuntimeError('Only creation under the signed-in personal account is supported.')
        existing = github_api('/user/repos', {'name': name, 'private': False,
            'description': 'CreativeLab: human-AI collaboration experiment platform', 'auto_init': False})
    expected = 'https://github.com/' + repo + '.git'
    origin = git(BASE, 'remote', 'get-url', 'origin', check=False)
    if origin.returncode:
        git(BASE, 'remote', 'add', 'origin', expected)
    elif origin.stdout.decode().strip() != expected:
        raise RuntimeError('The existing origin differs from the configured repository.')
    subprocess.run(['git', '-C', str(BASE), 'push', '-u', 'origin', 'main'], check=True)
    print('Published: ' + existing['html_url'])


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    review = commands.add_parser('check')
    review.add_argument('--staged', action='store_true')
    review.add_argument('--history', action='store_true')
    milestone = commands.add_parser('save')
    milestone.add_argument('-m', '--message', required=True)
    milestone.add_argument('--confirm', action='store_true', help='Use only after explicit user approval.')
    upload = commands.add_parser('publish')
    upload.add_argument('--repository', help='GitHub owner/name; independent of server SSH configuration.')
    upload.add_argument('--confirm', action='store_true', help='Use only after explicit user approval.')
    deploy = commands.add_parser('deploy')
    choice = deploy.add_mutually_exclusive_group(required=True)
    choice.add_argument('--working-tree', action='store_true')
    choice.add_argument('--ref')
    status = commands.add_parser('server-status')
    status.add_argument('--accept-baseline', action='store_true')
    commands.add_parser('fetch-code')
    commands.add_parser('backup')
    rollback = commands.add_parser('rollback')
    rollback.add_argument('release_id')
    args = parser.parse_args()
    repository()
    if args.command == 'status':
        print(git(BASE, 'status', '--short', '--branch').stdout.decode('utf-8'))
        print(git(BASE, 'log', '-8', '--oneline', check=False).stdout.decode('utf-8'))
    elif args.command == 'check':
        check(args)
    elif args.command == 'save':
        save(args)
    elif args.command == 'publish':
        publish(args)
    elif args.command == 'deploy':
        deployment(args)
    elif args.command == 'server-status':
        server_status(args)
    elif args.command == 'fetch-code':
        fetch_code(args)
    elif args.command == 'backup':
        download_backup(configuration())
    elif args.command == 'rollback':
        config = configuration()
        output(helper(config, 'rollback', args.release_id))
        remember(config, helper(config, 'status'))
        verify_public(config)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
