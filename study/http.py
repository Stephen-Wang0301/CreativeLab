import hmac
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory
from itsdangerous import BadSignature, TimestampSigner
from werkzeug.exceptions import HTTPException

from .engine import MODES, PID_FORMAT, TOKEN_FORMAT, StudyError
from .research import Research, TASKS

WEB = os.path.join(os.path.dirname(__file__), 'web')
IMAGE_FILE = re.compile(r'^r[12]-[12]-[a-f0-9]{24}\.(png|jpg|webp)$')
ADMIN_COOKIE = 'creative_research'
ADMIN_PATH = '/research/'
ADMIN_SESSION_SECONDS = 8 * 60 * 60


def same_origin_json():
    origin = request.headers.get('Origin')
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError as error:
            raise StudyError('请求来源无效。', 403) from error
        if (parsed.scheme not in ('https', 'http') or parsed.netloc.lower() != request.host.lower()
                or parsed.path or parsed.query or parsed.fragment):
            raise StudyError('请求来源与当前站点不一致。', 403)
    if request.headers.get('X-Study-Request') != '1' or not request.is_json:
        raise StudyError('请求格式无效。', 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise StudyError('提交数据不是有效的 JSON。')
    return body


def create_apps(study, network):
    public = Flask('creative_participant', static_folder=None)
    admin = Flask('creative_research', static_folder=None)
    research = Research(study)
    public.config.update(MAX_CONTENT_LENGTH=32 * 1024)
    admin.config.update(MAX_CONTENT_LENGTH=4 * 1024 * 1024)
    admin.config['TRUSTED_HOSTS'] = ['localhost', '127.0.0.1']
    admin_host = urlsplit(network.get('admin_url') or '').hostname
    if admin_host:
        admin.config['TRUSTED_HOSTS'].append(admin_host)
    signer = TimestampSigner(study.store.secret, salt='creative-research-session')

    def admin_signature():
        return hmac.new(study.store.secret.encode(),
                        ('research|' + study.store.admin_key).encode(), 'sha256').hexdigest()

    @admin.before_request
    def protect_admin():
        if request.remote_addr != '127.0.0.1':
            raise StudyError('研究后台仅在实验电脑上开放。', 403)
        if admin_host and request.host.split(':')[0].lower() == admin_host and not request.is_secure:
            raise StudyError('请通过 HTTPS 登录研究后台。', 403)
        if request.path.startswith('/research/api/') and request.path != '/research/api/login':
            supplied = request.cookies.get(ADMIN_COOKIE, '')
            if admin_host:
                try:
                    supplied = signer.unsign(supplied, max_age=ADMIN_SESSION_SECONDS).decode('ascii')
                except (BadSignature, UnicodeError):
                    supplied = ''
            if not re.fullmatch(r'[0-9a-f]{64}', supplied) or not hmac.compare_digest(supplied, admin_signature()):
                raise StudyError('请先登录研究后台。', 401)

    def make_app_headers(response):
        response.headers.update({
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'X-Frame-Options': 'DENY',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "img-src 'self'; object-src 'none'; base-uri 'none'; "
                                       "frame-ancestors 'none'; form-action 'self'",
        })
        return response

    def study_error(error):
        return jsonify(error=str(error), code=error.code), error.status

    def http_error(error):
        messages = {400: '请求有误。', 404: '找不到此页面。', 405: '请求方法不可用。',
                    413: '提交内容超出大小限制。', 500: '服务暂时无法处理此操作，请稍后重试。'}
        return jsonify(error=messages.get(error.code, '请求无法完成。')), error.code

    for app in (public, admin):
        app.after_request(make_app_headers)
        app.register_error_handler(StudyError, study_error)
        app.register_error_handler(HTTPException, http_error)

    @public.before_request
    def public_scope():
        if network.get('access') == 'pilot_tunnel' and request.view_args and request.view_args.get('mode') == 'formal':
            raise StudyError('临时公网入口用于预试。', 404)

    @public.get('/')
    def public_home():
        return redirect('/p/pilot/', code=302)

    @public.get('/healthz')
    def public_health():
        return jsonify(service='creative-study', instance=network.get('_probe_id'), https=request.is_secure)

    @public.get('/p/<mode>/')
    def participant_home(mode):
        if mode not in MODES:
            raise StudyError('实验入口不存在。', 404)
        return send_from_directory(WEB, 'participant.html')

    @public.get('/assets/<name>')
    def public_assets(name):
        if name not in ('common.js', 'participant.js', 'style.css'):
            raise StudyError('找不到此资源。', 404)
        return send_from_directory(WEB, name)

    @public.get('/p/<mode>/api/landing')
    def landing(mode):
        return jsonify(study.landing(mode))

    def participant_token(mode):
        return request.cookies.get('creative_' + mode, '')

    def session_cookie(response, mode, token):
        response.set_cookie('creative_' + mode, token, httponly=True,
                            secure=request.is_secure, samesite='Lax', max_age=60*60*24*30, path=f'/p/{mode}/')
        return response

    @public.post('/p/<mode>/api/join')
    def join(mode):
        body = same_origin_json()
        existing_token = participant_token(mode)
        if existing_token:
            try:
                existing = study.view(mode, existing_token)
            except StudyError as error:
                if error.status != 401:
                    raise
            else:
                return jsonify(view=existing)
        pid, code, token = study.join(mode, body)
        response = jsonify(view=study.view(mode, token), resume={'id': pid, 'code': code})
        return session_cookie(response, mode, token)

    @public.post('/p/<mode>/api/resume')
    def resume(mode):
        token = study.resume(mode, same_origin_json())
        return session_cookie(jsonify(view=study.view(mode, token)), mode, token)

    @public.get('/p/<mode>/api/state')
    def state(mode):
        return jsonify(study.view(mode, participant_token(mode)))

    @public.post('/p/<mode>/api/action')
    def action(mode):
        return jsonify(study.action(mode, participant_token(mode), same_origin_json()))

    @public.get('/p/<mode>/api/image/<filename>')
    def participant_image(mode, filename):
        if not IMAGE_FILE.fullmatch(filename):
            raise StudyError('找不到此图片。', 404)
        token = participant_token(mode)
        with study.store.lock:
            row = study.authenticate(study.store.db, mode, token)
            state = json.loads(row['data'])
            if state['phase'] != 'main_image' or filename not in [
                    item for candidate in state['image']['rounds'] for item in candidate['files']]:
                raise StudyError('找不到此图片。', 404)
            folder = study.store.participant_path(row) / 'images'
        return send_from_directory(folder, filename)

    @admin.get('/')
    def admin_home():
        return redirect(ADMIN_PATH, code=302)

    @admin.get('/research/')
    def admin_page():
        return send_from_directory(WEB, 'research.html')

    @admin.get('/research/assets/<name>')
    def admin_assets(name):
        if name not in ('common.js', 'research.js', 'style.css'):
            raise StudyError('找不到此资源。', 404)
        return send_from_directory(WEB, name)

    @admin.post('/research/api/login')
    def login():
        key = same_origin_json().get('key')
        if not isinstance(key, str) or not TOKEN_FORMAT.fullmatch(key) or not hmac.compare_digest(key, study.store.admin_key):
            raise StudyError('后台访问密钥有误。', 403)
        response = jsonify(ok=True)
        cookie = signer.sign(admin_signature()).decode('ascii') if admin_host else admin_signature()
        response.set_cookie(ADMIN_COOKIE, cookie, httponly=True, secure=request.is_secure,
                            samesite='Strict', path=ADMIN_PATH,
                            max_age=ADMIN_SESSION_SECONDS if admin_host else None)
        return response

    @admin.post('/research/api/logout')
    def logout():
        same_origin_json()
        response = jsonify(ok=True)
        response.delete_cookie(ADMIN_COOKIE, path=ADMIN_PATH)
        return response

    @admin.get('/research/api/health')
    def health():
        visible = dict(network)
        visible.pop('_probe_id', None)
        return jsonify(network=visible, data_path=str(study.store.root),
                       tasks={key: {'label': value[0], 'instructions': value[1]} for key, value in TASKS.items()})

    @admin.get('/research/api/<mode>/settings')
    def settings(mode):
        return jsonify(study.settings(mode))

    @admin.post('/research/api/<mode>/settings')
    def set_settings(mode):
        return jsonify(study.change_settings(mode, same_origin_json()))

    @admin.get('/research/api/<mode>/overview')
    def overview(mode):
        return jsonify(research.overview(mode, request.args.get('revision')))

    @admin.get('/research/api/<mode>/analysis')
    def analysis(mode):
        return jsonify(research.analysis(mode, request.args.get('revision')))

    @admin.get('/research/api/<mode>/analysis.csv')
    def analysis_csv(mode):
        data = research.analysis_csv(mode, request.args.get('revision'))
        return Response(data, mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="{mode}-analysis.csv"'})

    @admin.get('/research/api/<mode>/person/<pid>')
    def person(mode, pid):
        return jsonify(research.detail(mode, pid))

    @admin.get('/research/api/<mode>/person/<pid>/image/<filename>')
    def person_image(mode, pid, filename):
        if not IMAGE_FILE.fullmatch(filename):
            raise StudyError('找不到此图片。', 404)
        with study.store.lock:
            row = research.participant(mode, pid)
            folder = study.store.participant_path(row) / 'images'
        return send_from_directory(folder, filename)

    @admin.post('/research/api/folder')
    def open_folder():
        body = same_origin_json()
        mode, pid = body.get('mode'), body.get('id')
        path = study.store.root
        if pid:
            if not isinstance(pid, str) or not PID_FORMAT.fullmatch(pid):
                raise StudyError('参与者编号无效。')
            with study.store.lock:
                row = research.participant(mode, pid)
                path = study.store.participant_path(row)
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            elif os.uname().sysname == 'Darwin':
                subprocess.Popen(['open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise StudyError(f'请在文件管理器中打开：{path}', 409) from error
        return jsonify(path=str(path))

    @admin.post('/research/api/sync')
    def sync_data():
        same_origin_json()
        study.store.sync_pending()
        with study.store.lock:
            return jsonify(errors=list(study.store.mirror_errors))

    def zip_response(stream, filename):
        response = send_file(stream, mimetype='application/zip', download_name=filename, as_attachment=True)
        response.call_on_close(stream.close)
        return response

    @admin.get('/research/api/<mode>/raw.zip')
    def raw_zip(mode):
        pid = request.args.get('id')
        stream = research.raw_zip(mode, request.args.get('revision'), pid)
        return zip_response(stream, (pid if pid else mode + '-raw') + '.zip')

    @admin.get('/research/api/<mode>/packet/<task>')
    def packet(mode, task):
        stream = research.packet(mode, request.args.get('revision'), task)
        return zip_response(stream, task + '-blind-packet.zip')

    @admin.post('/research/api/<mode>/ratings')
    def ratings(mode):
        body = same_origin_json()
        return jsonify(research.import_ratings(mode, request.args.get('revision'), body))

    if admin_host:
        participant_wsgi = public.wsgi_app

        def route_public_request(environ, start_response):
            path = environ.get('PATH_INFO', '')
            if path == ADMIN_PATH.rstrip('/') or path.startswith(ADMIN_PATH):
                return admin(environ, start_response)
            return participant_wsgi(environ, start_response)

        public.wsgi_app = route_public_request
    return public, admin
