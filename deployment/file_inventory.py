"""Record local file roles and hashes against a read-only server inventory."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
GENERATED = {'deployment/file-inventory.csv', 'deployment/sync-baseline.json'}
FIRST_PARTY = {
    '.gitignore': ('限定可以进入 Git 的源代码路径，排除数据、私有说明和凭据', 'source'),
    '.gitattributes': ('固定文本换行规则，减少 Windows 与 Ubuntu 的无效差异', 'source'),
    'AGENTS.md': ('协作规则：由用户决定何时存版，代码部署不得覆盖线上数据', 'source'),
    '.github/README.md': ('GitHub 展示的公开项目说明，不含实际邀请或登录信息', 'document'),
    '.githooks/pre-commit': ('Git 提交前检查暂存文件和私密信息', 'source'),
    '.githooks/pre-push': ('Git 推送前检查暂存文件和已保存历史', 'source'),
    'Project_Tools.bat': ('打开 Windows 版本与服务器同步操作菜单', 'windows'),
    'deployment/Project_Tools.ps1': ('查看改动、测试部署、阶段保存、推送和下载备份的菜单', 'source'),
    'deployment/manage.py': ('独立执行代码部署、手动阶段提交、GitHub 发布、备份和回退', 'source'),
    'deployment/version_policy.py': ('公开源代码路径白名单、已知凭据及邀请口令检查', 'source'),
    'deployment/server_release.py': ('服务器代码预检、漂移检查、备份、替换、健康检查和回退', 'source'),
    'deployment/bootstrap.py': ('Git 克隆后安装依赖并启用检查钩子，不覆盖已有数据', 'source'),
    'deployment/version-control.md': ('版本、代码同步、数据备份与迁移操作说明', 'document'),
    'deployment/server.example.json': ('无真实连接信息的私有服务器配置示例', 'source'),
    'tests/verify_version_control.py': ('验证私有路径排除、凭据检查、归档保护、漂移拦截与失败回退', 'source'),
    'README.md': ('私有使用说明，首页包含网址、邀请链接和后台登录密钥', 'private-document'),
    'run.py': ('统一启动入口，解析参数、检查环境并锁定数据目录', 'source'),
    'requirements.txt': ('Python 依赖及其固定版本清单', 'source'),
    '.creative-study-004': ('Windows 安装器的历史部署识别标记', 'local-config'),
    'Start_Local.bat': ('Windows 启动器，查找 Python/Conda 后转交 run.py', 'windows'),
    'Start_Quick_Demo.bat': ('启动本机快速演练，使用 data/quick-demo', 'windows'),
    'Start_Internet_Pilot.bat': ('启动随机地址 Quick Tunnel 预试', 'windows'),
    'Check_Startup.bat': ('检查本机解释器、依赖、数据目录和端口', 'windows'),
    'Install_004.bat': ('Windows 安装入口，部署目标为 004CreativeLab', 'windows'),
    'study/__init__.py': ('study 业务包入口', 'source'),
    'study/config.py': ('校验方案、读取默认材料并设置快速演练时长', 'source'),
    'study/deploy.py': ('Windows 安装、旧数据复制、已有目录保护和目录清单生成', 'source'),
    'study/engine.py': ('实验状态机、计时、随机分组、作答及图像任务执行', 'source'),
    'study/http.py': ('受试者和后台 HTTP 路由、登录会话及请求保护', 'source'),
    'study/images.py': ('可接入图像模型的请求、候选图及插件接口', 'source'),
    'study/launch.py': ('启动 Waitress、监听端口、后台同步及公网检查线程', 'source'),
    'study/research.py': ('研究后台数据查询、盲评材料、评分导入及分析导出', 'source'),
    'study/storage.py': ('SQLite、运行密钥、原子写入及参与者文件镜像', 'source'),
    'study/tunnel.py': ('Cloudflare 连接器定位、校验、运行和公网 HTTPS 自检', 'source'),
    'study/materials/default.json': ('新数据目录的初始材料和参数；已发布方案保存在数据库中', 'source'),
    'study/web/common.js': ('网页 DOM、API、通知、复制和表格通用功能', 'source'),
    'study/web/participant.html': ('受试者网页入口和页面框架', 'source'),
    'study/web/participant.js': ('受试者任务界面、计时、自动保存和阶段操作', 'source'),
    'study/web/research.html': ('研究后台网页入口和导航框架', 'source'),
    'study/web/research.js': ('后台登录、方案草稿确认保存、招募、数据及盲评界面', 'source'),
    'study/web/style.css': ('受试者和研究后台的版式、颜色与响应式样式', 'source'),
    'tests/verify_flow.py': ('用临时数据验证实验流程、权限、草稿保存和公网后台登录', 'source'),
    'tests/verify_deploy.py': ('用临时目录验证安装、数据复制、目录保护和重复部署', 'source'),
    'deployment/backup.py': ('生成包含一致 SQLite 快照、代码及迁移凭据的整站备份', 'source'),
    'deployment/Download_Server_Backup.ps1': ('通过 SSH 创建服务器备份，下载后核对 SHA-256', 'source'),
    'deployment/creative-study.service': ('Ubuntu 实验程序的 systemd 启动、自启和重启配置', 'server-config'),
    'deployment/creative-study-tunnel.service': ('Ubuntu Cloudflare 隧道的 systemd 常驻配置', 'server-config'),
    'deployment/cloudflared.yml': ('Ubuntu 隧道本机配置，公开路由由 Cloudflare 管理', 'server-config'),
    'deployment/server-operations.md': ('服务器登录、后台、备份、迁出和回到 Windows 的操作说明', 'document'),
    'deployment/experiment-guide.md': ('实验流程、数据字段、盲评指标、图像插件及本机演练说明', 'document'),
    'deployment/file_inventory.py': ('刷新逐文件职责清单和两端相对路径、哈希基线', 'local-tool'),
    'deployment/server_inventory.py': ('在 Ubuntu 只读扫描业务文件哈希、数据库数量和当前邀请口令', 'local-tool'),
    'deployment/Refresh_Server_Inventory.ps1': ('经 SSH 取得最新服务器元数据快照，原子替换本机对照文件', 'local-tool'),
    'deployment/file-inventory.csv': ('所有本机文件和文件夹的用途、同步方式及服务器对应项', 'generated'),
    'deployment/sync-baseline.json': ('本次本机与服务器文件哈希的机器可读对照基线', 'generated'),
}
DIRECTORIES = {
    '.git': '本项目独立 Git 历史与索引；内部文件由 Git 管理，不逐项扫描',
    '.github': 'GitHub 公开项目说明', '.githooks': '本地提交和推送检查钩子',
    '.runtime/tools': '一次性 SSH 配置依赖；内部文件由 pip 管理，不逐项扫描',
    'study': '实验业务源代码', 'study/web': '受试者及后台网页资源',
    'study/materials': '新数据库使用的默认实验材料', 'tests': '临时数据验证脚本',
    'deployment': '服务器运行、整站备份、迁移和文件对齐工具',
    'runtime': '随项目保存的解释器、连接器和 Python 依赖',
    'runtime/packages': '固定版本的第三方 Python 运行依赖',
    'runtime/win-amd64': 'Windows x64 嵌入式 Python 3.14 及其原生库',
    'runtime/cloudflared': 'Windows 官方 Cloudflare 连接器',
    'data': '本机迁移前的数据副本', 'logs': '本机启动和隧道的历史日志',
    '.runtime': '本机网络配置、SSH 指纹、迁移快照及历史备份',
    'backups': '从服务器下载的整站备份默认保存目录',
    'private': '站点私有凭据目录',
}
DATA_FILES = {
    'study.sqlite3': '完整实验数据库，含方案、草稿、参与者、事件、版本与评分',
    'study.sqlite3-wal': 'SQLite 写前日志，可能包含尚未回写主文件的事务',
    'study.sqlite3-shm': 'SQLite WAL 共享内存索引',
    'researcher.key': '研究后台登录密钥', 'server.key': '签名和恢复验证使用的服务端密钥',
    '.server.lock': '服务运行时的数据目录排他锁文件',
    'progress.json': '参与者阶段、计时、完成状态和同步版本镜像',
    'responses.json': '参与者意图、稿件、回顾及图像状态镜像',
    'assignments.json': '参与者分组、区组变量和冻结材料镜像',
    'events.jsonl': '逐行保存的参与者操作事件镜像',
    'versions.jsonl': '逐行保存的稿件版本和恢复来源镜像',
    'ratings.json': '参与者作品的评审评分镜像',
}
DEPENDENCIES = {
    'flask': 'Web 应用与路由', 'waitress': 'WSGI HTTP 服务', 'werkzeug': 'HTTP 请求、响应及路由工具',
    'jinja2': '模板引擎', 'markupsafe': 'HTML 转义', 'itsdangerous': '会话签名与有效期校验',
    'click': '命令行参数工具', 'blinker': '事件信号', 'colorama': 'Windows 终端颜色兼容',
}
POLICIES = {
    'source': '按相对路径和 SHA-256 核对后同步',
    'server-config': '按文件核对，保留服务器用户、路径和平台参数',
    'private-document': '私有说明文件，包含登录信息，按需同步',
    'document': '文档按文件同步', 'windows': 'Windows 本机使用',
    'data': '通过整站备份和一致数据库快照迁移', 'secrets': '迁移时随完整数据和私有凭据保存',
    'local-config': '本机参数和安装标记，按当前环境维护',
    'dependency': '按固定版本和平台核对依赖', 'platform': '按操作系统和架构配置',
    'history': '保留为历史快照，按整包校验', 'cache': '运行时自动生成',
    'log': '诊断记录，按时间保存', 'generated': '扫描后重新生成',
    'local-tool': '本机文件对齐工具，按需同步',
}


def describe(relative, directory=False):
    path = Path(relative)
    parts, name = path.parts, path.name
    if relative in FIRST_PARTY:
        return FIRST_PARTY[relative]
    if relative == 'private/server.json':
        return '本机服务器连接参数、专用密钥路径及 GitHub 仓库名称，不含登录密码', 'secrets'
    if relative == '.runtime/deploy_ed25519':
        return '代码同步专用 SSH 私钥，仅当前 Windows 用户与 SYSTEM 可读', 'secrets'
    if relative == '.runtime/deploy_ed25519.pub':
        return '服务器已登记的代码同步专用 SSH 公钥', 'local-config'
    if '__pycache__' in parts or path.suffix == '.pyc':
        return 'Python 自动生成的编译缓存', 'cache'
    if directory and relative in DIRECTORIES:
        return DIRECTORIES[relative], 'generated' if relative == 'backups' else 'local-config'
    if relative.startswith('.runtime/server-migration-20261118-141801/'):
        original = relative.split('/', 2)[2]
        purpose, _ = describe(original, directory)
        purpose = {
            'README.md': '当时版本的项目使用说明',
            'study/deploy.py': '当时版本的 Windows 安装和数据复制代码，目标为旧 004',
        }.get(original, purpose)
        return '首次迁移的冻结副本；' + purpose, 'history'
    if parts[0] == '.runtime':
        known = {
            'creative-cloudflared.local.yml': 'Windows 物理网卡 IPv4 和官方边缘地址配置',
            'creative-cloudflared.yml': '启动器生成的空白 Cloudflare 默认配置',
            'remote-known_hosts': '已验证 SSH 服务器的 ED25519 主机公钥',
            'server-current-inventory.json': '本次只读取得的服务器文件元数据、哈希及数据数量',
            'server-migration-current.json': '首次迁移归档的路径、哈希和数据数量记录',
            'cloudflared-linux-amd64': '已校验的 Ubuntu x64 官方 Cloudflare 连接器',
            'browser-admin-update.tar.gz': '浏览器 HTTPS 后台登录功能的历史增量代码包',
            'creative-study-code-20261118.tar.gz': '初次部署时上传的纯代码和依赖包',
            'server-migration-20261118-141801.tar.gz': '初次迁移的代码、旧数据和运行密钥归档',
        }
        if name in known:
            category = 'local-config' if name.endswith('.yml') or name == 'remote-known_hosts' else 'history'
            if name == 'server-current-inventory.json':
                category = 'generated'
            if name == 'cloudflared-linux-amd64':
                category = 'platform'
            return known[name], category
        stages = {
            'backup-check': '备份脚本验证所用的历史快照',
            'migration-final': '最终切换时冻结的整站迁移包，含数据、密钥和隧道令牌',
            'server-backup-verified': '从服务器下载并在 Windows 恢复验证的整站备份',
            'server-migration-20261118-141801': '首次迁移的解包工作目录和旧数据副本',
        }
        purpose = stages.get(parts[1], '迁移或验证过程保存的本机文件')
        if name.endswith('.sha256'):
            purpose += '的 SHA-256 校验文件'
        return purpose, 'history'
    if parts[0] == 'data':
        if directory:
            if name == 'quick-demo':
                return '与普通预试独立的快速演练数据', 'data'
            if name == '_system':
                return '该套数据的完整 SQLite 数据库和运行密钥', 'data'
            if name in ('pilot', 'formal'):
                return ('预试' if name == 'pilot' else '正式实验') + '的参与者文件镜像', 'data'
            if name.startswith('P-'):
                return '该匿名参与者的作答、分组、事件、版本及评分文件', 'data'
            return '参与者图像或其他实验数据目录', 'data'
        purpose = DATA_FILES.get(name, '参与者图像或实验数据文件')
        return purpose, 'secrets' if name.endswith('.key') else 'data'
    if relative.startswith('runtime/packages/'):
        package = parts[2]
        if '.dist-info' in package:
            metadata = {
                'METADATA': '包名、版本和依赖元数据', 'RECORD': '安装文件及其校验记录',
                'WHEEL': 'Wheel 格式和平台兼容性说明', 'INSTALLER': '安装工具名称记录',
                'REQUESTED': '显式安装标记', 'LICENSE': '依赖包许可证',
                'LICENSE.txt': '依赖包许可证', 'LICENSE.rst': '依赖包许可证',
                'entry_points.txt': '命令行或插件入口声明', 'top_level.txt': '顶层模块名称',
            }
            return package + '；' + metadata.get(name, '发行包元数据或许可证'), 'dependency'
        purpose = DEPENDENCIES.get(package, '第三方运行依赖')
        if directory:
            return purpose + ('依赖包目录' if len(parts) == 3 else '的 ' + '/'.join(parts[3:]) + '模块目录'), 'dependency'
        module = '.'.join(Path('/'.join(parts[2:])).with_suffix('').parts)
        return purpose + '；' + (module + ' 模块' if path.suffix == '.py' else '包内资源 ' + name), 'dependency'
    if relative.startswith('runtime/win-amd64/'):
        known = {
            'python.exe': '带终端的 Python 解释器启动程序',
            'pythonw.exe': 'Windows 无终端窗口的 Python 启动程序',
            'python314.dll': 'CPython 3.14 核心运行库', 'python3.dll': 'Python 稳定 ABI 接口库',
            'python314.zip': '压缩保存的 Python 标准库', 'python314._pth': '嵌入式 Python 的模块搜索路径',
            'python.cat': '官方文件签名目录', 'LICENSE.txt': 'Python 及附带组件的许可证',
            'libcrypto-3.dll': 'OpenSSL 加密算法运行库', 'libssl-3.dll': 'OpenSSL TLS 运行库',
            'sqlite3.dll': 'SQLite 数据库原生运行库', 'libffi-8.dll': '外部函数调用运行库',
            'libtommath.dll': '大整数运算运行库',
        }
        purpose = known.get(name, 'Python 标准库的 ' + path.stem + ' Windows 原生扩展')
        if name.startswith('vcruntime'):
            purpose = 'Microsoft Visual C++ 运行库'
        return purpose, 'platform'
    if relative.startswith('runtime/cloudflared/'):
        return '官方 Windows x64 Cloudflare 隧道连接器', 'platform'
    if parts[0] == 'logs':
        return {
            'startup.log': '本机启动、异常和历史运行状态日志',
            'cloudflared.log': '本机隧道注册、预检及公网自检历史日志',
            'development-tree.txt': 'Windows 安装时生成的 D 盘开发目录清单',
        }.get(name, '本机诊断日志'), 'log'
    if parts[0] == 'backups':
        return '下载的整站恢复包或其校验文件', 'history'
    if parts[0] == 'private':
        return '该站点的 Cloudflare 隧道运行凭据', 'secrets'
    if directory:
        return '项目文件目录', 'source'
    return '用户保存的项目文件，后续同步时核对其用途', 'local-config'


def inventory(root, remote):
    remote_files = {item['path']: item for item in remote.get('files', [])}
    rows = []
    paths = []
    for current, directories, names in os.walk(root, followlinks=False):
        paths.extend(Path(current) / name for name in directories + names)
        directories[:] = [name for name in directories
                          if (Path(current) / name).relative_to(root).as_posix() not in {'.git', '.runtime/tools'}
                          and not (Path(current) / name).is_symlink()]
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix().lower()):
        relative = path.relative_to(root).as_posix()
        directory = path.is_dir()
        purpose, category = describe(relative, directory)
        digest = None
        size = None if directory else path.stat().st_size
        if relative in GENERATED:
            size = None
        elif not directory and category != 'cache' and not path.name.startswith('study.sqlite3') and path.name != '.server.lock':
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        counterpart = remote_files.get(relative)
        status = 'directory' if directory else 'local-only'
        if category in ('history', 'log', 'cache', 'platform', 'windows', 'local-config', 'generated'):
            status = 'managed-by-policy'
        elif counterpart:
            status = 'snapshot-required' if category in ('data', 'secrets') else (
                'same' if digest and digest == counterpart['sha256'] else 'different')
        rows.append({
            'path': relative, 'type': 'directory' if directory else 'file', 'purpose': purpose,
            'category': category, 'sync_policy': POLICIES[category], 'bytes': size, 'sha256': digest,
            'remote_path': remote.get('root', '/home/user/creative-study') + '/' + relative if counterpart else None,
            'remote_sha256': counterpart['sha256'] if counterpart else None, 'comparison': status,
        })
    return rows


def render_tree(rows, label):
    by_parent = {}
    for row in rows:
        by_parent.setdefault(str(Path(row['path']).parent).replace('\\', '/'), []).append(row)
    lines = [label + '/']

    def branch(parent, prefix):
        entries = sorted(by_parent.get(parent, []), key=lambda row: (row['type'] != 'directory', row['path'].lower()))
        for index, row in enumerate(entries):
            last = index == len(entries) - 1
            name = Path(row['path']).name + ('/' if row['type'] == 'directory' else '')
            lines.append(prefix + ('└── ' if last else '├── ') + name + '  # ' + row['purpose'])
            if row['type'] == 'directory':
                branch(row['path'], prefix + ('    ' if last else '│   '))

    branch('.', '')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote-snapshot', type=Path, default=BASE / '.runtime/server-current-inventory.json')
    args = parser.parse_args()
    remote = json.loads(args.remote_snapshot.read_text(encoding='utf-8-sig')) if args.remote_snapshot.is_file() else {}
    settings_path = BASE / 'private/server.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.is_file() else {}
    outputs = BASE / 'deployment'
    for name in ('file-inventory.csv', 'sync-baseline.json'):
        (outputs / name).touch(exist_ok=True)
    rows = inventory(BASE, remote)
    with (outputs / 'file-inventory.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    baseline = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'local_root': str(BASE),
        'remote_host': settings.get('host'), 'remote_port': settings.get('port'),
        'remote_root': remote.get('root', '/home/user/creative-study'),
        'remote_scanned_utc': remote.get('created_utc'),
        'remote_excluded_directories': remote.get('excluded_directories', []),
        'remote_data_summaries': remote.get('data_summaries', {}),
        'note': 'SQLite 按一致快照迁移；生成的清单自身及缓存不计算内容哈希；different 仅说明两端不同。',
        'files': [row for row in rows if row['type'] == 'file'],
        'remote_only_files': [item for item in remote.get('files', []) if not (BASE / item['path']).exists()],
    }
    (outputs / 'sync-baseline.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    counts = {status: sum(row['comparison'] == status and row['type'] == 'file' for row in rows)
              for status in ('same', 'different', 'local-only', 'snapshot-required', 'managed-by-policy')}
    print(json.dumps({'local_files': len(baseline['files']), 'comparison_counts': counts,
                      'remote_only_files': len(baseline['remote_only_files'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
