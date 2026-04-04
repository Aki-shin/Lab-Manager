import os
import re
import functools
import requests
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, Response
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from .config import Config
from .services import (
    find_free_port, get_app_status, get_app_logs,
    create_app_service, control_service, delete_app_service,
    run_diagnostic_test
)


def is_safe_redirect_url(url):
    """Проверяет, что URL — безопасный относительный путь (без open redirect)."""
    if not url:
        return False
    return url.startswith('/') and not url.startswith('//')


bp = Blueprint('main', __name__)


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('main.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# --- Авторизация ---

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if Config.ADMIN_PASSWORD and check_password_hash(Config.ADMIN_PASSWORD, password):
            session['user'] = 'admin'
            flash('Добро пожаловать!', 'success')
            next_url = request.args.get('next')
            if not is_safe_redirect_url(next_url):
                next_url = url_for('main.index')
            return redirect(next_url)
        else:
            flash('Неверный пароль', 'danger')
    return render_template('login.html')


@bp.route('/logout')
def logout():
    session.pop('user', None)
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('main.login'))


# --- Основные страницы ---

@bp.route('/')
@login_required
def index():
    apps = []
    if os.path.exists(Config.APPS_DIR):
        for item in sorted(os.listdir(Config.APPS_DIR)):
            path = os.path.join(Config.APPS_DIR, item)
            if os.path.isdir(path):
                apps.append(get_app_status(item))
    return render_template("dashboard.html", apps=apps)


@bp.route('/help')
@login_required
def help_page():
    return render_template("help.html")


@bp.route('/app/<name>')
@login_required
def app_detail(name):
    safe_name = secure_filename(name)
    app_data = get_app_status(safe_name)

    logs = ""
    if app_data['has_service']:
        logs = get_app_logs(safe_name)

    return render_template("detail.html", app=app_data, logs=logs)


# --- Управление сервисами ---

@bp.route('/create/<name>', methods=['POST'])
@login_required
def create_service(name):
    safe_name = secure_filename(name)
    app_path = os.path.join(Config.APPS_DIR, safe_name)

    custom_port = request.form.get('custom_port')
    if custom_port:
        port = int(custom_port)
    else:
        port = find_free_port()

    if not port:
        flash("Нет свободных портов (диапазон 5001-6000 занят)", "danger")
        return redirect(url_for('main.app_detail', name=safe_name))

    entry_cmd = request.form.get('entry_cmd', '').strip()

    if entry_cmd:
        if not re.match(r'^[a-zA-Z0-9_./ -]+$', entry_cmd):
            flash("Недопустимые символы в команде запуска", "danger")
            return redirect(url_for('main.app_detail', name=safe_name))
    else:
        venv_python = os.path.join(app_path, "venv/bin/python")
        if os.path.exists(venv_python):
            python_exec = venv_python
        else:
            python_exec = "/usr/bin/python3"

        if os.path.exists(os.path.join(app_path, "app.py")):
            script = "app.py"
        elif os.path.exists(os.path.join(app_path, "wsgi.py")):
            script = "wsgi.py"
        else:
            script = "main.py"

        entry_cmd = f"{python_exec} {script}"

    try:
        create_app_service(safe_name, app_path, port, entry_cmd)
        flash(f"Сервис успешно создан на порту {port}", "success")
    except Exception as e:
        flash(f"Ошибка при создании сервиса: {e}", "danger")

    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/action/<name>/<action>', methods=['POST'])
@login_required
def service_action(name, action):
    safe_name = secure_filename(name)
    if action not in ['start', 'stop', 'restart']:
        flash("Недопустимое действие", "warning")
        return redirect(url_for('main.app_detail', name=safe_name))

    control_service(safe_name, action)
    flash(f"Команда {action} отправлена", "info")
    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/delete/<name>', methods=['POST'])
@login_required
def delete_service(name):
    safe_name = secure_filename(name)

    if delete_app_service(safe_name):
        flash(f"Сервис {safe_name} удален", "warning")
    else:
        flash("Файл сервиса не найден", "danger")

    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/diagnose/<name>')
@login_required
def diagnose_app(name):
    safe_name = secure_filename(name)
    report = run_diagnostic_test(safe_name)
    return render_template("diagnostic_result.html", name=safe_name, report=report)


# --- PROXY ---

@bp.route('/proxy/<name>/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@bp.route('/proxy/<name>/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@login_required
def proxy(name, path):
    safe_name = secure_filename(name)
    app_data = get_app_status(safe_name)

    if not app_data['assigned_port'] or app_data['active_state'] != 'active':
        return render_template("base.html", content=f"<h1>Ошибка 502</h1><p>Приложение {safe_name} не запущено.</p>"), 502

    target_url = f"http://127.0.0.1:{app_data['assigned_port']}/{path}"

    headers = {key: value for (key, value) in request.headers if key.lower() != 'host'}
    headers['X-Script-Name'] = f"/proxy/{safe_name}"
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.host

    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            params=request.args,
            stream=True
        )

        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers_back = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded_headers]

        if 'Location' in resp.headers:
            loc = resp.headers['Location']
            app_root = f"http://127.0.0.1:{app_data['assigned_port']}"

            if loc.startswith(app_root):
                loc = loc.replace(app_root, f"/proxy/{safe_name}")
            elif loc.startswith('/'):
                loc = f"/proxy/{safe_name}{loc}"

            headers_back = [(k, v) if k.lower() != 'location' else ('Location', loc) for k, v in headers_back]

        return Response(
            resp.iter_content(chunk_size=4096),
            status=resp.status_code,
            headers=headers_back
        )

    except requests.exceptions.ConnectionError:
        return "Ошибка подключения к приложению. Возможно, оно еще загружается.", 502
    except Exception as e:
        return f"Proxy Error: {e}", 500
