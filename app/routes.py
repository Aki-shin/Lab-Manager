import os
import functools
import requests
import shlex
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, Response
from werkzeug.utils import secure_filename
from .config import Config
from .services import run_systemctl, find_free_port, get_app_status, run_diagnostic_test

# Создаем Blueprint с именем 'main'
bp = Blueprint('main', __name__)

# --- Декоратор авторизации ---
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
        if password == Config.ADMIN_PASSWORD:
            session['user'] = 'admin'
            flash('Добро пожаловать!', 'success')
            next_url = request.args.get('next')
            # Если next_url пустой или небезопасный, идем на главную
            return redirect(next_url or url_for('main.index'))
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
        # Сканируем только директории
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
    # Безопасное имя файла, чтобы исключить ../
    safe_name = secure_filename(name)
    app_data = get_app_status(safe_name)
    
    logs = ""
    if app_data['has_service']:
        # Получаем последние 50 строк логов
        import subprocess
        cmd = ["journalctl", "--user", "-u", f"{safe_name}.service", "-n", "50", "--no-pager"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            logs = res.stdout
        except Exception as e:
            logs = f"Error reading logs: {e}"
            
    return render_template("detail.html", app=app_data, logs=logs)

# --- Управление сервисами ---

@bp.route('/create/<name>', methods=['POST'])
@login_required
def create_service(name):
    safe_name = secure_filename(name)
    app_path = os.path.join(Config.APPS_DIR, safe_name)
    
    # 1. Выбор порта
    custom_port = request.form.get('custom_port')
    if custom_port:
        port = int(custom_port)
    else:
        port = find_free_port()
    
    if not port:
        flash("Нет свободных портов (диапазон 5001-6000 занят)", "danger")
        return redirect(url_for('main.app_detail', name=safe_name))

    # 2. Определение команды запуска
    # Если пользователь ввел свою команду в форме (добавим поле в шаблон ниже)
    entry_cmd = request.form.get('entry_cmd', '').strip()
    
    if not entry_cmd:
        # Автоматическое определение
        venv_python = os.path.join(app_path, "venv/bin/python")
        if os.path.exists(venv_python):
            python_exec = venv_python
        else:
            python_exec = "/usr/bin/python3"
        
        # Ищем точку входа
        if os.path.exists(os.path.join(app_path, "app.py")):
            script = "app.py"
        elif os.path.exists(os.path.join(app_path, "wsgi.py")):
            script = "wsgi.py"
        else:
            script = "main.py"
            
        entry_cmd = f"{python_exec} {script}"

    # 3. Генерация Systemd Unit
    # Важно: HOST=127.0.0.1 заставляет приложение слушать только локально (безопасность)
    service_content = f"""[Unit]
Description={safe_name} via HomeLab Manager (Port {port})
After=network.target

[Service]
Type=simple
WorkingDirectory={app_path}
Environment=PORT={port}
Environment=HOST=127.0.0.1
Environment=PYTHONUNBUFFERED=1
ExecStart={entry_cmd}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    service_file_path = os.path.join(Config.SYSTEMD_USER_DIR, f"{safe_name}.service")

    try:
        with open(service_file_path, "w") as f:
            f.write(service_content)
        
        run_systemctl(["daemon-reload"])
        run_systemctl(["enable", f"{safe_name}.service"])
        run_systemctl(["start", f"{safe_name}.service"])
        
        flash(f"Сервис успешно создан на порту {port}", "success")
    except Exception as e:
        flash(f"Ошибка при создании сервиса: {e}", "danger")
    
    return redirect(url_for('main.app_detail', name=safe_name))

@bp.route('/action/<name>/<action>')
@login_required
def service_action(name, action):
    safe_name = secure_filename(name)
    if action not in ['start', 'stop', 'restart']:
        flash("Недопустимое действие", "warning")
        return redirect(url_for('main.app_detail', name=safe_name))
        
    run_systemctl([action, f"{safe_name}.service"])
    flash(f"Команда {action} отправлена", "info")
    return redirect(url_for('main.app_detail', name=safe_name))

@bp.route('/delete/<name>')
@login_required
def delete_service(name):
    safe_name = secure_filename(name)
    service_name = f"{safe_name}.service"
    
    # Останавливаем и отключаем
    run_systemctl(["stop", service_name])
    run_systemctl(["disable", service_name])
    
    # Удаляем файл
    file_path = os.path.join(Config.SYSTEMD_USER_DIR, service_name)
    if os.path.exists(file_path):
        os.remove(file_path)
        run_systemctl(["daemon-reload"])
        flash(f"Сервис {safe_name} удален", "warning")
    else:
        flash("Файл сервиса не найден", "danger")
        
    return redirect(url_for('main.app_detail', name=safe_name))

@bp.route('/diagnose/<name>')
@login_required
def diagnose_app(name):
    safe_name = secure_filename(name)
    
    # Запускаем тест
    report = run_diagnostic_test(safe_name)
    
    # Передаем отчет в шаблон (используем тот же detail.html или отдельный)
    # Для простоты, вернемся на detail и покажем отчет в flash или передадим как переменную
    return render_template("diagnostic_result.html", name=safe_name, report=report)

# --- PROXY CORE (Streaming) ---

@bp.route('/proxy/<name>/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@bp.route('/proxy/<name>/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@login_required
def proxy(name, path):
    safe_name = secure_filename(name)
    app_data = get_app_status(safe_name)
    
    # Проверка состояния
    if not app_data['assigned_port'] or app_data['active_state'] != 'active':
        return render_template("base.html", content=f"<h1>Ошибка 502</h1><p>Приложение {safe_name} не запущено.</p>"), 502

    target_url = f"http://127.0.0.1:{app_data['assigned_port']}/{path}"

    # Подготовка заголовков
    headers = {key: value for (key, value) in request.headers if key.lower() != 'host'}
    headers['X-Script-Name'] = f"/proxy/{safe_name}"
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.host

    try:
        # Выполняем запрос (stream=True для больших данных)
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False, # Сами обрабатываем редиректы
            params=request.args,
            stream=True 
        )

        # Фильтрация заголовков ответа
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers_back = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded_headers]

        # Обработка редиректов (Location Rewrite)
        if 'Location' in resp.headers:
            loc = resp.headers['Location']
            # Если редирект на корень приложения (http://127.0.0.1:5005/foo)
            app_root = f"http://127.0.0.1:{app_data['assigned_port']}"
            
            if loc.startswith(app_root):
                # Заменяем локальный адрес на адрес прокси
                loc = loc.replace(app_root, f"/proxy/{safe_name}")
            elif loc.startswith('/'):
                 # Если путь относительный (/login), добавляем префикс прокси
                loc = f"/proxy/{safe_name}{loc}"
            
            # Обновляем заголовок Location
            headers_back = [(k, v) if k.lower() != 'location' else ('Location', loc) for k, v in headers_back]

        # Возвращаем потоковый ответ
        return Response(
            resp.iter_content(chunk_size=4096),
            status=resp.status_code,
            headers=headers_back
        )

    except requests.exceptions.ConnectionError:
        return "Ошибка подключения к приложению. Возможно, оно еще загружается.", 502
    except Exception as e:
        return f"Proxy Error: {e}", 500