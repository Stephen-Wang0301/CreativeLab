import errno
import logging
import secrets
import socket
import threading
import webbrowser
from urllib.parse import urlsplit

from waitress import create_server

from .engine import Study
from .http import create_apps
from .images import load_provider
from .storage import Store
from .tunnel import PublicGateway


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.connect(('192.0.2.1', 9))
            return client.getsockname()[0]
    except OSError:
        return None


def external_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('固定网址必须是完整 HTTPS 源地址，例如 https://study.example.com。')
    if parsed.port == 0:
        raise ValueError('固定网址的端口无效。')
    return f'https://{parsed.netloc}'


def validate_ports(args):
    if not 1 <= args.port <= 65535 or not 1 <= args.admin_port <= 65535 or args.port == args.admin_port:
        raise ValueError('受试者与后台必须使用两个不同的有效端口。')


def listener_host(args):
    return '127.0.0.1' if args.public or args.external_url or args.local_only else '0.0.0.0'


def bind_server(app, host, port, threads, max_request_body_size, **options):
    try:
        return create_server(app, host=host, port=port, threads=threads,
                             max_request_body_size=max_request_body_size, **options)
    except OSError as error:
        if error.errno == errno.EADDRINUSE or getattr(error, 'winerror', None) == 10048:
            raise RuntimeError(f'{host}:{port} 已被占用。请关闭此前的实验窗口，或指定其他端口。') from error
        raise


def serve(args, base):
    validate_ports(args)
    fixed = external_origin(args.external_url) if args.external_url else None
    if args.public_admin and not fixed:
        raise ValueError('--public-admin 须与 --external-url 的固定 HTTPS 入口一起使用。')
    if args.tunnel_token_file and not fixed:
        raise ValueError('--tunnel-token-file 须与 --external-url 一起使用。')
    proxy_mode = bool(args.public or fixed)
    host = listener_host(args)
    lan_ip = lan_address() if host == '0.0.0.0' else None
    local_url = f'http://127.0.0.1:{args.port}'
    admin_url = fixed + '/research/' if args.public_admin else f'http://127.0.0.1:{args.admin_port}/research/'
    network = {'local_url': local_url, 'lan_url': f'http://{lan_ip}:{args.port}' if lan_ip else None,
               'public_url': None, 'tunnel_status': 'starting' if proxy_mode else 'off',
               'access': 'pilot_tunnel' if args.public else 'fixed' if fixed else 'local' if args.local_only else 'lan',
               'quick': args.quick, '_probe_id': secrets.token_hex(16),
               'admin_url': admin_url if args.public_admin else None}
    gateway = PublicGateway(args, base, network, local_url, fixed) if proxy_mode else None
    provider = load_provider(args.image_provider)
    store = Store(args.data)
    study = Study(store, quick=args.quick, image_name=args.image_provider, provider=provider)
    participant, admin = create_apps(study, network)
    public_options = dict(trusted_proxy='127.0.0.1', trusted_proxy_count=1,
                          trusted_proxy_headers={'x-forwarded-proto'}) if proxy_mode else {}
    public_server = bind_server(participant, host, args.port, threads=12,
                                max_request_body_size=32768, max_request_header_size=16384,
                                channel_timeout=90, **public_options)
    try:
        admin_server = bind_server(admin, '127.0.0.1', args.admin_port, threads=4,
                                   max_request_body_size=4 * 1024 * 1024)
    except BaseException:
        public_server.close()
        raise

    stop = threading.Event()

    def background():
        while not stop.wait(2):
            try:
                study.tick()
            except Exception:
                logging.exception('后台同步未完成，将自动重试。')

    print(f'研究后台  {admin_url}', flush=True)
    print(f'本机预览  {local_url}/p/pilot/', flush=True)
    if network['lan_url']:
        print(f'局域网    {network["lan_url"]}', flush=True)
    if fixed:
        print(f'固定网址  {fixed}', flush=True)
    print(f'数据目录  {store.root}', flush=True)
    logging.info('受试者监听 %s:%s，研究后台 %s，数据目录 %s', host, args.port, admin_url, store.root)
    if gateway:
        print('公网入口验证通过后，会在研究后台显示可分享的链接。', flush=True)
    print('采集期间请保持电脑联网、唤醒和本窗口运行。', flush=True)
    print(f'手动登录密钥文件  {store.root / "_system" / "researcher.key"}', flush=True)

    threads = [
        threading.Thread(target=admin_server.run, daemon=True, name='research-web'),
        threading.Thread(target=background, daemon=True, name='data-sync'),
    ]
    if gateway:
        threads.append(threading.Thread(target=gateway.run, daemon=True, name='public-gateway'))
    try:
        for thread in threads:
            thread.start()
        if not args.no_browser:
            threading.Thread(target=webbrowser.open, args=(admin_url + '#key=' + store.admin_key,),
                             daemon=True).start()
        public_server.run()
    finally:
        stop.set()
        if gateway:
            gateway.close()
        public_server.close()
        admin_server.close()
        if study.executor:
            study.executor.shutdown(wait=False, cancel_futures=True)
