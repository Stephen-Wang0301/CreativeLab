"""Start the experiment server and the local researcher console."""

import argparse
from contextlib import closing, contextmanager
from ipaddress import IPv4Address
import logging
import os
from pathlib import Path
import platform
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile


BASE = Path(__file__).resolve().parent
VENDORED = BASE / 'runtime' / 'packages'


def dependencies():
    # Embedded Python's ._pth mode can omit the directory containing run.py.
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    if not (VENDORED / 'flask' / '__init__.py').is_file() or not (VENDORED / 'waitress' / '__init__.py').is_file():
        raise RuntimeError('运行依赖缺失。请完整解压 004 项目包。')
    if str(VENDORED) not in sys.path:
        sys.path.insert(0, str(VENDORED))


@contextmanager
def exclusive_root(root):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / '.server.lock', 'a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f'此数据目录已有服务在运行：{root}') from error
        yield


def arguments():
    parser = argparse.ArgumentParser(description='人智协作实验服务器')
    parser.add_argument('--data', type=Path, default=BASE / 'data', help='本地数据目录')
    parser.add_argument('--port', type=int, default=8765, help='受试者端口，默认 8765')
    parser.add_argument('--admin-port', type=int, default=8766, help='研究后台端口，默认 8766')
    parser.add_argument('--public-admin', action='store_true',
                        help='在固定 HTTPS 网址的 /research/ 开放密钥登录后台')
    access = parser.add_mutually_exclusive_group()
    access.add_argument('--public', action='store_true', help='通过 cloudflared 开放临时互联网链接')
    access.add_argument('--external-url', help='已配置 HTTPS 代理或隧道的固定源地址')
    access.add_argument('--local-only', action='store_true', help='仅允许在本机访问')
    parser.add_argument('--cloudflared', help='cloudflared.exe 路径，默认从 PATH 查找')
    parser.add_argument('--tunnel-token-file', type=Path, help='配合 --external-url 自动启动命名隧道的本机令牌文件')
    parser.add_argument('--tunnel-protocol', choices=('auto', 'http2', 'quic'),
                        help='Cloudflare 连接协议，临时隧道默认 http2，命名隧道默认 auto')
    parser.add_argument('--public-probe-bind', type=IPv4Address,
                        help='用指定本机 IPv4 和加密 DNS 直连公网自检地址（需要 curl）')
    parser.add_argument('--image-provider', default=os.environ.get('STUDY_IMAGE_PROVIDER', ''),
                        help='可选图像插件，格式 模块:函数')
    parser.add_argument('--quick', action='store_true', help='在独立演练数据目录缩短计时')
    parser.add_argument('--no-browser', action='store_true', help='启动时不打开研究后台')
    setup = parser.add_mutually_exclusive_group()
    setup.add_argument('--check', action='store_true', help='检查解释器、依赖、数据目录与端口')
    setup.add_argument('--install-004', action='store_true', help='将项目部署到 D 盘 PhDWorking 下的 004')
    return parser.parse_args()


def configure_logging():
    logfile = BASE / 'logs' / 'startup.log'
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(logfile, encoding='utf-8'), logging.StreamHandler()])
    logging.info('Python %s · %s', platform.python_version(), sys.executable)


def check_startup(args):
    from study.launch import listener_host, validate_ports
    validate_ports(args)
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute("SELECT json_extract('{\"ready\":1}', '$.ready')").fetchone()
    logging.info('SQLite %s · %s', sqlite3.sqlite_version, ssl.OPENSSL_VERSION)
    args.data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=args.data) as stream:
        stream.write(b'creative-study')
        stream.flush()
    logging.info('数据目录可写  %s', args.data)
    for host, port in ((listener_host(args), args.port), ('127.0.0.1', args.admin_port)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
                check.bind((host, port))
        except OSError as error:
            raise RuntimeError(f'{host}:{port} 无法绑定。请停止占用该端口的服务，或指定其他端口。') from error
        else:
            logging.info('端口可绑定  %s:%s', host, port)


def install_004(args):
    if os.name != 'nt':
        raise RuntimeError('请在 Windows 本机运行 004 部署。')
    from study.deploy import DEVELOPMENT, OLD_DATA, PROJECT, install
    if not DEVELOPMENT.is_dir():
        raise RuntimeError(f'找不到开发目录 {DEVELOPMENT}。')
    source_data = BASE / 'data'
    candidates = []
    if args.data != source_data and args.data.is_dir():
        candidates.append(args.data)
    candidates.extend(path for path in (source_data, OLD_DATA) if path.is_dir())
    candidates = list(dict.fromkeys(path.resolve() for path in candidates))
    populated = [path for path in candidates if (path / '_system' / 'study.sqlite3').is_file()
                 or (path / 'quick-demo' / '_system' / 'study.sqlite3').is_file()]
    if len(populated) > 1 and BASE != PROJECT.resolve():
        print(f'发现两份实验数据。004 将使用随项目目录内的数据；旧桌面数据保留在 {OLD_DATA}。', flush=True)
    old_data = next(iter(populated or candidates), None)
    target, status, report = install(BASE, PROJECT, DEVELOPMENT, old_data, exclusive_root)
    print(f'{status}  {target}', flush=True)
    print(f'D 盘目录清单  {report}', flush=True)
    interpreter = sys.executable
    if Path(interpreter).resolve().is_relative_to(BASE / 'runtime'):
        interpreter = str(target / 'runtime' / 'win-amd64' / 'python.exe')
    command = [interpreter, '-u', str(target / 'run.py'), '--local-only', '--port', str(args.port),
               '--admin-port', str(args.admin_port)]
    if args.quick:
        command.append('--quick')
    if args.no_browser:
        command.append('--no-browser')
    check = subprocess.run([*command, '--check'], cwd=target)
    if check.returncode:
        print(f'项目已部署。启动检查报告见 {target / "logs" / "startup.log"}', flush=True)
        return check.returncode
    if args.quick:
        print('本机检查通过，正在启动快速演练。', flush=True)
        return subprocess.run(command, cwd=target).returncode
    print(f'本机检查通过。双击 {target / "Start_Quick_Demo.bat"} 开始试跑。', flush=True)
    return 0


def main():
    if sys.version_info < (3, 11):
        print('请使用 64 位 Python 3.12 或 3.13 启动。')
        return 2
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    args = arguments()
    if args.quick and not args.install_004:
        args.data = args.data / 'quick-demo'
    args.data = args.data.expanduser().resolve()
    try:
        configure_logging()
        dependencies()
        if args.install_004:
            return install_004(args)
        if args.check:
            check_startup(args)
            return 0
        from study.launch import serve
        with exclusive_root(args.data):
            serve(args, BASE)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logging.exception('启动失败：%s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
