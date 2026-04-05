import os
import re
import tempfile
import requests
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session,
    Response, stream_with_context, jsonify, abort
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from .config import Config
from . import db
from .auth import login_required, admin_required, current_user
from .services import (
    find_free_port, get_app_status, get_app_logs,
    create_app_service, update_app_service, control_service, delete_app_service,
    run_diagnostic_test, parse_service_file,
    get_system_stats, stream_app_logs,
    git_clone_app, git_pull_app, is_safe_app_name, extract_archive,
    setup_app_environment,
    RESERVED_ENV_KEYS
)


def is_safe_redirect_url(url):
    """Проверяет, что URL — безопасный относительный путь (без open redirect)."""
    if not url:
        return False
    return url.startswith('/') and not url.startswith('//')


bp = Blueprint('main', __name__)


# --- Авторизация ---

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        user = db.get_user_by_username(username) if username else None
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            flash(f'Добро пожаловать, {user["username"]}!', 'success')
            next_url = request.args.get('next')
            if not is_safe_redirect_url(next_url):
                next_url = url_for('main.index')
            return redirect(next_url)
        else:
            flash('Неверный логин или пароль', 'danger')
    return render_template('login.html')


@bp.route('/logout')
def logout():
    session.clear()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('main.login'))


# --- Основные страницы ---

@bp.route('/')
@login_required
def index():
    user = current_user()
    apps = []

    if user.get('is_admin'):
        # Администратор видит всё + системные метрики
        if os.path.exists(Config.APPS_DIR):
            for item in sorted(os.listdir(Config.APPS_DIR)):
                path = os.path.join(Config.APPS_DIR, item)
                if os.path.isdir(path):
                    apps.append(get_app_status(item, with_metrics=True))
        system = get_system_stats()
        return render_template("dashboard.html", apps=apps, system=system)

    # Обычный пользователь — только доступные приложения
    allowed = set(db.get_user_permissions(user['id']))
    if os.path.exists(Config.APPS_DIR):
        for item in sorted(os.listdir(Config.APPS_DIR)):
            if item not in allowed:
                continue
            path = os.path.join(Config.APPS_DIR, item)
            if os.path.isdir(path):
                apps.append(get_app_status(item, with_metrics=False))
    return render_template("my_apps.html", apps=apps)


@bp.route('/help')
@login_required
def help_page():
    return render_template("help.html")


@bp.route('/app/<name>')
@admin_required
def app_detail(name):
    safe_name = secure_filename(name)
    app_data = get_app_status(safe_name, with_metrics=True)

    logs = ""
    if app_data['has_service']:
        logs = get_app_logs(safe_name)

    return render_template("detail.html", app=app_data, logs=logs)


# --- Управление сервисами ---

def _parse_env_form(form):
    """Парсит env-переменные из формы (env_key_0, env_val_0, env_key_1, ...)."""
    env = {}
    for key in form:
        m = re.match(r'^env_key_(\d+)$', key)
        if not m:
            continue
        idx = m.group(1)
        k = form.get(f'env_key_{idx}', '').strip()
        v = form.get(f'env_val_{idx}', '').strip()
        if not k:
            continue
        if not re.match(r'^[A-Z_][A-Z0-9_]*$', k):
            raise ValueError(f"Недопустимое имя переменной: {k}")
        if k in RESERVED_ENV_KEYS:
            raise ValueError(f"Имя {k} зарезервировано")
        env[k] = v
    return env


def _resolve_entry_cmd(app_path, entry_cmd):
    """Валидирует или вычисляет команду запуска."""
    entry_cmd = (entry_cmd or '').strip()
    if entry_cmd:
        if not re.match(r'^[a-zA-Z0-9_./ -]+$', entry_cmd):
            raise ValueError("Недопустимые символы в команде запуска")
        return entry_cmd

    venv_python = os.path.join(app_path, "venv/bin/python")
    python_exec = venv_python if os.path.exists(venv_python) else "/usr/bin/python3"

    for candidate in ("app.py", "wsgi.py", "main.py"):
        if os.path.exists(os.path.join(app_path, candidate)):
            return f"{python_exec} {candidate}"
    return f"{python_exec} main.py"


@bp.route('/create/<name>', methods=['POST'])
@admin_required
def create_service(name):
    safe_name = secure_filename(name)
    app_path = os.path.join(Config.APPS_DIR, safe_name)

    custom_port = request.form.get('custom_port')
    port = int(custom_port) if custom_port else find_free_port()

    if not port:
        flash("Нет свободных портов (диапазон 5001-6000 занят)", "danger")
        return redirect(url_for('main.app_detail', name=safe_name))

    try:
        entry_cmd = _resolve_entry_cmd(app_path, request.form.get('entry_cmd', ''))
        extra_env = _parse_env_form(request.form)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for('main.app_detail', name=safe_name))

    try:
        create_app_service(safe_name, app_path, port, entry_cmd, extra_env=extra_env)
        flash(f"Сервис успешно создан на порту {port}", "success")
    except Exception as e:
        flash(f"Ошибка при создании сервиса: {e}", "danger")

    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/edit/<name>', methods=['GET', 'POST'])
@admin_required
def edit_service(name):
    safe_name = secure_filename(name)
    app_path = os.path.join(Config.APPS_DIR, safe_name)
    config = parse_service_file(safe_name)

    if not config:
        flash("Сервис не найден", "danger")
        return redirect(url_for('main.app_detail', name=safe_name))

    if request.method == 'POST':
        try:
            port = int(request.form.get('port') or config['port'] or 5001)
            entry_cmd = _resolve_entry_cmd(app_path, request.form.get('entry_cmd', ''))
            extra_env = _parse_env_form(request.form)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for('main.edit_service', name=safe_name))

        try:
            update_app_service(safe_name, port, entry_cmd, extra_env=extra_env)
            flash("Сервис обновлён", "success")
            return redirect(url_for('main.app_detail', name=safe_name))
        except Exception as e:
            flash(f"Ошибка обновления: {e}", "danger")

    return render_template("edit.html", name=safe_name, config=config)


@bp.route('/action/<name>/<action>', methods=['POST'])
@admin_required
def service_action(name, action):
    safe_name = secure_filename(name)
    if action not in ['start', 'stop', 'restart']:
        flash("Недопустимое действие", "warning")
        return redirect(url_for('main.app_detail', name=safe_name))

    control_service(safe_name, action)
    flash(f"Команда {action} отправлена", "info")
    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/delete/<name>', methods=['POST'])
@admin_required
def delete_service(name):
    safe_name = secure_filename(name)

    if delete_app_service(safe_name):
        # Удаляем связанные права доступа
        db.delete_app_permissions(safe_name)
        flash(f"Сервис {safe_name} удален", "warning")
    else:
        flash("Файл сервиса не найден", "danger")

    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/diagnose/<name>')
@admin_required
def diagnose_app(name):
    safe_name = secure_filename(name)
    report = run_diagnostic_test(safe_name)
    return render_template("diagnostic_result.html", name=safe_name, report=report)


# --- Загрузка приложений / Git ---

ALLOWED_ARCHIVE_EXT = ('.zip', '.tar.gz', '.tgz', '.tar')


@bp.route('/upload', methods=['POST'])
@admin_required
def upload_app():
    """Загрузка zip/tar.gz приложения."""
    name = (request.form.get('name') or '').strip()
    if not is_safe_app_name(name):
        flash("Недопустимое имя приложения (только a-z, 0-9, _, -, .)", "danger")
        return redirect(url_for('main.index'))

    file = request.files.get('archive')
    if not file or not file.filename:
        flash("Файл не выбран", "danger")
        return redirect(url_for('main.index'))

    fname = file.filename.lower()
    if not fname.endswith(ALLOWED_ARCHIVE_EXT):
        flash("Поддерживаются только .zip, .tar.gz, .tgz, .tar", "danger")
        return redirect(url_for('main.index'))

    if fname.endswith('.tar.gz'):
        suffix = '.tar.gz'
    else:
        suffix = os.path.splitext(fname)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        ok, msg = extract_archive(tmp_path, name)
        flash(msg, "success" if ok else "danger")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    if ok:
        # Авто-настройка окружения (venv + pip install)
        setup_ok, setup_msg = setup_app_environment(name)
        flash(f"Окружение: {setup_msg}", "info" if setup_ok else "warning")
        return redirect(url_for('main.app_detail', name=name))
    return redirect(url_for('main.index'))


@bp.route('/git/clone', methods=['POST'])
@admin_required
def git_clone():
    """Клонирование приложения из git-репозитория."""
    name = (request.form.get('name') or '').strip()
    git_url = (request.form.get('git_url') or '').strip()

    if not is_safe_app_name(name):
        flash("Недопустимое имя приложения", "danger")
        return redirect(url_for('main.index'))

    ok, msg = git_clone_app(git_url, name)
    flash(msg, "success" if ok else "danger")
    if ok:
        # Авто-настройка окружения (venv + pip install)
        setup_ok, setup_msg = setup_app_environment(name)
        flash(f"Окружение: {setup_msg}", "info" if setup_ok else "warning")
        return redirect(url_for('main.app_detail', name=name))
    return redirect(url_for('main.index'))


@bp.route('/git/pull/<name>', methods=['POST'])
@admin_required
def git_pull(name):
    """git pull для существующего приложения + обновление зависимостей."""
    safe_name = secure_filename(name)
    ok, msg = git_pull_app(safe_name)
    flash(msg, "success" if ok else "danger")
    if ok:
        setup_ok, setup_msg = setup_app_environment(safe_name)
        flash(f"Окружение: {setup_msg}", "info" if setup_ok else "warning")
    return redirect(url_for('main.app_detail', name=safe_name))


@bp.route('/setup/<name>', methods=['POST'])
@admin_required
def setup_env(name):
    """Ручная (пере)установка venv и зависимостей приложения."""
    safe_name = secure_filename(name)
    ok, msg = setup_app_environment(safe_name)
    flash(f"Окружение: {msg}", "success" if ok else "danger")
    return redirect(url_for('main.app_detail', name=safe_name))


# --- SSE-логи ---

@bp.route('/logs/<name>/stream')
@admin_required
def logs_stream(name):
    """Server-Sent Events: стрим логов в реальном времени."""
    safe_name = secure_filename(name)

    def generate():
        for chunk in stream_app_logs(safe_name):
            yield chunk

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


# --- API метрик ---

@bp.route('/api/metrics/<name>')
@admin_required
def api_app_metrics(name):
    """JSON с метриками приложения для обновления на странице."""
    safe_name = secure_filename(name)
    data = get_app_status(safe_name, with_metrics=True)
    return jsonify(data)


@bp.route('/api/system')
@admin_required
def api_system_metrics():
    """JSON системных метрик."""
    return jsonify(get_system_stats() or {})


# --- Управление пользователями (admin only) ---

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_.-]{3,32}$')


@bp.route('/users')
@admin_required
def users_list():
    users = db.list_users()
    return render_template('users.html', users=users)


@bp.route('/users/create', methods=['POST'])
@admin_required
def users_create():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    is_admin = bool(request.form.get('is_admin'))

    if not USERNAME_RE.match(username):
        flash('Недопустимое имя пользователя (3-32 символа: a-z, 0-9, _, -, .)', 'danger')
        return redirect(url_for('main.users_list'))
    if len(password) < 4:
        flash('Пароль должен быть не короче 4 символов', 'danger')
        return redirect(url_for('main.users_list'))
    if db.get_user_by_username(username):
        flash('Пользователь с таким именем уже существует', 'danger')
        return redirect(url_for('main.users_list'))

    db.create_user(username, generate_password_hash(password), is_admin=is_admin)
    flash(f'Пользователь {username} создан', 'success')
    return redirect(url_for('main.users_list'))


@bp.route('/users/<int:user_id>/edit', methods=['POST'])
@admin_required
def users_edit(user_id):
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)

    new_password = request.form.get('password') or ''
    is_admin_form = request.form.get('is_admin')
    # Чекбокс может отсутствовать — значит снимаем флаг
    new_is_admin = bool(is_admin_form)

    password_hash = None
    if new_password:
        if len(new_password) < 4:
            flash('Пароль должен быть не короче 4 символов', 'danger')
            return redirect(url_for('main.users_list'))
        password_hash = generate_password_hash(new_password)

    # Защита от снятия последнего админа
    if target['is_admin'] and not new_is_admin and db.count_admins() <= 1:
        flash('Нельзя снять права у последнего администратора', 'danger')
        return redirect(url_for('main.users_list'))

    db.update_user(user_id, password_hash=password_hash, is_admin=new_is_admin)
    flash('Пользователь обновлён', 'success')
    return redirect(url_for('main.users_list'))


@bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def users_delete(user_id):
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)

    me = current_user()
    if me and me['id'] == user_id:
        flash('Нельзя удалить самого себя', 'danger')
        return redirect(url_for('main.users_list'))

    if target['is_admin'] and db.count_admins() <= 1:
        flash('Нельзя удалить последнего администратора', 'danger')
        return redirect(url_for('main.users_list'))

    db.delete_user(user_id)
    flash(f'Пользователь {target["username"]} удалён', 'warning')
    return redirect(url_for('main.users_list'))


@bp.route('/users/<int:user_id>/permissions', methods=['GET', 'POST'])
@admin_required
def users_permissions(user_id):
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)

    # Список всех приложений
    all_apps = []
    if os.path.exists(Config.APPS_DIR):
        for item in sorted(os.listdir(Config.APPS_DIR)):
            if os.path.isdir(os.path.join(Config.APPS_DIR, item)):
                all_apps.append(item)

    if request.method == 'POST':
        selected = request.form.getlist('apps')
        # Фильтруем только реально существующие
        selected = [a for a in selected if a in all_apps]
        db.set_user_permissions(user_id, selected)
        flash('Права обновлены', 'success')
        return redirect(url_for('main.users_list'))

    current_perms = set(db.get_user_permissions(user_id))
    return render_template(
        'user_permissions.html',
        target=target, all_apps=all_apps, current_perms=current_perms
    )


# --- PROXY ---

@bp.route('/proxy/<name>/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@bp.route('/proxy/<name>/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@login_required
def proxy(name, path):
    safe_name = secure_filename(name)

    # Проверка прав доступа к приложению
    user = current_user()
    if not db.user_can_access_app(user, safe_name):
        abort(403)

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
