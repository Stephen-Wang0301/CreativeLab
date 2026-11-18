"""Run an optional participant-only tunnel and verify its public HTTP endpoint."""

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .storage import atomic_write

CLOUDFLARED_VERSION = '2026.11.0'
CLOUDFLARED_SHA256 = 'd5c1d82031da6253dcdcff64c944cd0fe063016edeb2e4fd088075fc9bd52d0a'
QUICK_HOST = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com(?=[\s|/]|$)')
STARTUP_TIMEOUT = 45
RETRY_INTERVAL = 4
HEALTH_INTERVAL = 20


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def find_cloudflared(override, base):
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'指定的 cloudflared 文件不存在：{path}')
        return str(path)
    if os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64'):
        bundled = base / 'runtime' / 'cloudflared' / 'cloudflared.exe'
        if bundled.is_file():
            if checksum(bundled) != CLOUDFLARED_SHA256:
                raise RuntimeError('随包提供的 cloudflared 校验失败，请重新应用公网更新。')
            return str(bundled)
    local = base / ('cloudflared.exe' if os.name == 'nt' else 'cloudflared')
    if local.is_file():
        return str(local)
    on_path = shutil.which('cloudflared')
    if on_path:
        return on_path
    if os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64'):
        return windows_tunnel_download(base)
    raise FileNotFoundError('请安装与系统架构匹配的 cloudflared，或用 --cloudflared 指定其路径。')


def windows_tunnel_download(base):
    target = base / '.runtime' / f'cloudflared-{CLOUDFLARED_VERSION}-amd64.exe'
    if target.is_file() and checksum(target) == CLOUDFLARED_SHA256:
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = (f'https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}'
           '/cloudflared-windows-amd64.exe')
    logging.info('正在下载 Cloudflare 官方 Windows 连接器并校验 SHA-256。')
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.download', delete=False) as file:
        staging = Path(file.name)
        try:
            with urlopen(Request(url, headers={'User-Agent': 'CreativeStudy'}), timeout=15) as response:
                size = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    size += len(chunk)
                    if size > 120 * 1024 * 1024:
                        raise ValueError('下载文件超出预期大小。')
                    file.write(chunk)
            file.close()
            if checksum(staging) != CLOUDFLARED_SHA256:
                raise RuntimeError('Cloudflare 下载文件校验失败。')
            staging.replace(target)
            return str(target)
        except OSError as error:
            raise RuntimeError('自动下载 cloudflared 失败。可使用更新包中的连接器或从 Cloudflare 官网下载。') from error
        finally:
            file.close()
            staging.unlink(missing_ok=True)


def connector_command(binary, args, config, local_url, fixed):
    protocol = args.tunnel_protocol or ('http2' if args.public else 'auto')
    command = [binary, 'tunnel', '--no-autoupdate', '--loglevel', 'info',
               '--protocol', protocol, '--config', str(config)]
    if args.public:
        return [*command, '--url', local_url]
    if fixed and args.tunnel_token_file:
        return [*command, 'run', '--token-file', str(args.tunnel_token_file)]
    raise ValueError('启动命名隧道须提供网址和本机令牌文件。')


def probe_direct(request, bind_address):
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not curl:
        return None, '找不到 curl，无法从指定网卡执行公网自检。'
    command = [curl, '--disable', '--silent', '--show-error', '--max-time', '10', '--max-filesize', '2048',
               '--ipv4', '--interface', str(bind_address), '--noproxy', '*', '--proto', '=https',
               '--doh-url', 'https://cloudflare-dns.com/dns-query', '--write-out', '\n%{http_code}']
    for name, value in request.header_items():
        command.extend(('--header', f'{name}: {value}'))
    result = subprocess.run([*command, request.full_url], capture_output=True, timeout=12,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode == 60:
        return None, '公网 HTTPS 证书验证失败。请检查电脑时间或网络代理设置。'
    if result.returncode == 6:
        return None, '公网域名暂时无法解析。'
    if result.returncode:
        return None, f'公网自检暂时无法从 {bind_address} 连通。'
    body, _, code = result.stdout.rpartition(b'\n')
    status = int(code)
    if 300 <= status < 400:
        return None, '公网自检地址被重定向。请核对域名路由。'
    if status != 200:
        return None, f'公网自检收到 HTTP {status}。'
    return body, None


def probe_endpoint(url, instance, bind_address=None):
    health_url = url + '/healthz'
    request = Request(health_url, headers={'Accept': 'application/json', 'User-Agent': 'CreativeStudy-Health'})
    try:
        if bind_address is None:
            with urlopen(request, timeout=5) as response:
                if response.geturl() != health_url:
                    return '公网自检地址被重定向。请核对域名路由。'
                body = response.read(2048)
        else:
            body, error = probe_direct(request, bind_address)
            if error:
                return error
        data = json.loads(body)
    except HTTPError as error:
        return f'公网自检收到 HTTP {error.code}。'
    except (URLError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            return '公网 HTTPS 证书验证失败。请检查电脑时间或网络代理设置。'
        if isinstance(reason, socket.gaierror):
            return '公网域名暂时无法解析。'
        return '公网地址暂时无法连通。'
    if not isinstance(data, dict) or data.get('service') != 'creative-study' or data.get('instance') != instance:
        return '地址未指向当前实验进程，请检查路由是否指向受试者端口。'
    if data.get('https') is not True:
        return '代理未传递 HTTPS 协议标记，请核对 X-Forwarded-Proto。'
    return None


class PublicGateway:
    def __init__(self, args, base, network, local_url, fixed):
        self.args, self.base, self.network = args, base, network
        self.local_url, self.fixed = local_url, fixed
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.reader = None
        self.candidate = fixed
        self.log = logging.getLogger('creative.gateway')
        self.logfile = base / 'logs' / 'cloudflared.log'
        self.last_hint = ''

    def status(self, status, detail, public_url=None):
        if self.stop.is_set():
            return
        previous = self.network.get('tunnel_status'), self.network.get('tunnel_detail')
        self.network.update(tunnel_status=status, tunnel_detail=detail, public_url=public_url)
        if previous != (status, detail):
            self.log.info('%s · %s', status, detail)

    def read_output(self):
        with self.process.stdout as stream:
            for line in stream:
                if self.stop.is_set():
                    break
                clean = re.sub(r'(?i)(?:\beyJ[A-Za-z0-9._=-]{24,}|(?:--token\s+|TUNNEL_TOKEN=)[^\s]+)',
                               '[redacted]', line.strip())
                self.log.info('cloudflared · %s', clean)
                if self.args.public:
                    match = QUICK_HOST.search(line)
                    if match:
                        self.candidate = match[0]
                lower = line.lower()
                if '7844' in lower:
                    self.last_hint = '请检查当前网络到 Cloudflare 的出站 7844 端口。'
                if ('config file' in lower or 'configuration file' in lower) and 'quick tunnel' in lower:
                    self.last_hint = '连接器提示临时隧道配置冲突，请查看连接器日志。'

    def spawn(self):
        if self.args.tunnel_token_file:
            self.args.tunnel_token_file = self.args.tunnel_token_file.expanduser().resolve()
            if not self.args.tunnel_token_file.is_file():
                raise FileNotFoundError('找不到命名隧道令牌文件，请核对 --tunnel-token-file 路径。')
        binary = find_cloudflared(self.args.cloudflared, self.base)
        runtime = self.base / '.runtime'
        config = runtime / 'creative-cloudflared.local.yml'
        if not config.is_file():
            config = runtime / 'creative-cloudflared.yml'
            atomic_write(config, '{}\n')
        command = connector_command(binary, self.args, config, self.local_url, self.fixed)
        environment = os.environ.copy()
        environment.pop('TUNNEL_TOKEN', None)
        environment.pop('TUNNEL_TOKEN_FILE', None)
        self.log.info('连接器 %s · 本机受试者服务 %s', binary, self.local_url)
        with self.lock:
            if self.stop.is_set():
                return
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding='utf-8', errors='replace', env=environment)
            self.reader = threading.Thread(target=self.read_output, daemon=True, name='cloudflared-log')
            self.reader.start()

    def monitor(self):
        began = time.monotonic()
        failures = 0
        while not self.stop.is_set():
            if self.process and self.process.poll() is not None:
                if self.reader:
                    self.reader.join(timeout=1)
                tip = self.last_hint or '请查看 logs/cloudflared.log 最后的错误。'
                self.status('failed', f'连接器已退出（{self.process.returncode}）。{tip}')
                return
            candidate = self.candidate
            if not candidate:
                if time.monotonic() - began >= STARTUP_TIMEOUT:
                    self.status('unreachable', self.last_hint or '连接器尚未取得临时公网地址。请查看 logs/cloudflared.log。')
                self.stop.wait(1)
                continue
            error = probe_endpoint(candidate, self.network['_probe_id'], self.args.public_probe_bind)
            if not error:
                self.status('ready', '已通过公网 HTTPS 检查。', candidate)
                failures = 0
                self.stop.wait(HEALTH_INTERVAL)
                continue
            failures += 1
            if self.network.get('tunnel_status') != 'ready' or failures >= 2:
                stage = 'unreachable' if time.monotonic() - began >= STARTUP_TIMEOUT else 'verifying'
                self.status(stage, error)
            self.stop.wait(RETRY_INTERVAL)

    def run(self):
        handler = None
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.logfile, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.log.addHandler(handler)
            if self.args.public or self.args.tunnel_token_file:
                self.status('starting', '正在准备 Cloudflare 连接器。')
                self.spawn()
            self.monitor()
        except (OSError, ValueError, RuntimeError) as error:
            self.status('failed', str(error))
            self.log.exception('公网连接启动失败')
        finally:
            if handler:
                self.log.removeHandler(handler)
                handler.close()

    def close(self):
        with self.lock:
            self.stop.set()
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
