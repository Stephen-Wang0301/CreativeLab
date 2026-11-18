"""Create a Windows project directory without replacing another project."""

import hashlib
import os
from pathlib import Path
import shutil
import stat
from contextlib import ExitStack
from tempfile import TemporaryDirectory

from .storage import atomic_write

DEVELOPMENT = Path(r'D:\Development')
PROJECT = DEVELOPMENT / 'Projects' / 'python_project' / 'PhDWorking' / '004CreativeLab'
OLD_DATA = Path(
    r'D:\SystemData\Desktop\03实验设计推进\数字创意人智协作_实验系统_Windows免配置版'
    r'\CreativeStudy\data'
)
MARKER = '.creative-study-004'
INSTALL_FILES = (
    'run.py', 'README.md', 'requirements.txt', 'Start_Local.bat', 'Start_Quick_Demo.bat',
    'Start_Internet_Pilot.bat', 'Check_Startup.bat', 'Install_004.bat',
)
INSTALL_DIRS = ('study', 'runtime', 'tests', 'deployment')
TREE_SKIP = {
    '.cache', '.conda', '.git', '.hg', '.idea', '.mypy_cache', '.pytest_cache', '.runtime',
    '.svn', '.venv', '__pycache__', 'build', 'dist', 'envs', 'node_modules', 'site-packages', 'venv',
}
TREE_LIMIT = 2500


def structure_report(root, destination):
    """List directory names locally, excluding environment internals and directory links."""
    lines = [str(root), '目录清单仅保存在本机。括号标记跳过的目录。', '']

    def walk(folder, depth):
        if depth >= 9:
            lines.append('  ' * depth + '（到达扫描深度上限）')
            return
        try:
            with os.scandir(folder) as entries:
                directories = sorted((entry for entry in entries
                                      if entry.is_dir(follow_symlinks=False) or entry.is_symlink()),
                                     key=lambda entry: entry.name.casefold())
        except OSError:
            lines.append('  ' * depth + '（此目录无法读取）')
            return
        for entry in directories:
            if len(lines) >= TREE_LIMIT:
                return
            name = entry.name
            try:
                attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
            except OSError:
                lines.append('  ' * depth + name + '/（无法读取）')
                continue
            linked = entry.is_symlink() or bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
            skip = name.casefold() in TREE_SKIP or linked or Path(entry.path) == destination
            lines.append('  ' * depth + name + '/' + ('（未展开）' if skip else ''))
            if not skip:
                walk(entry.path, depth + 1)

    walk(root, 1)
    if len(lines) >= TREE_LIMIT:
        lines.append('（目录数量达到上限）')
    return '\n'.join(lines) + '\n'


def copy_data(source, target, lock):
    if not source:
        return
    with ExitStack() as locks:
        locks.enter_context(lock(source))
        quick = source / 'quick-demo'
        if quick.is_dir():
            locks.enter_context(lock(quick))
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('.server.lock'))


def fingerprint(source):
    return hashlib.sha256((source / 'run.py').read_bytes()).hexdigest()


def install(source, target, development, old_data, lock):
    source = source.resolve()
    original_target = target.absolute()
    target = original_target.resolve()
    if target.name != PROJECT.name or target != original_target:
        raise RuntimeError(f'目标路径不是预期的实际目录：{original_target}。请核对该路径。')
    if old_data and target.is_relative_to(old_data.resolve()):
        raise RuntimeError('数据源不能是目标项目的上级目录。')
    if source == target:
        if old_data and not (target / 'data').exists():
            with TemporaryDirectory(prefix='.creative-data-', dir=target) as temporary:
                staged_data = Path(temporary) / 'data'
                copy_data(old_data, staged_data, lock)
                staged_data.rename(target / 'data')
        atomic_write(target / MARKER, fingerprint(source) + '\n')
        status = '已在 004CreativeLab 目录内'
    elif target.exists():
        marker = target / MARKER
        if (not marker.is_file() or not (target / 'run.py').is_file()
                or marker.read_text(encoding='ascii').strip() != fingerprint(source)
                or fingerprint(target) != fingerprint(source)):
            raise RuntimeError(f'目标已存在且属于其他项目或版本：{target}。请核对该目录后再部署。')
        status = '已找到此前部署的 004CreativeLab'
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.creative004-', dir=target.parent) as temporary:
            stage = Path(temporary) / PROJECT.name
            stage.mkdir()
            for name in INSTALL_FILES:
                shutil.copy2(source / name, stage / name)
            for name in INSTALL_DIRS:
                shutil.copytree(source / name, stage / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            copy_data(old_data, stage / 'data', lock)
            atomic_write(stage / MARKER, fingerprint(source) + '\n')
            stage.rename(target)
        status = '已部署 004CreativeLab'
    report = target / 'logs' / 'development-tree.txt'
    atomic_write(report, structure_report(development, target))
    return target, status, report
