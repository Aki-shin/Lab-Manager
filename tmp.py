import os
import socket
import subprocess
import functools
import requests  # pip install requests
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, Response

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_home_lab'

# --- КОНФИГУРАЦИЯ ---
USER_HOME = os.path.expanduser("~")
APPS_DIR = os.path.join(USER_HOME, "apps")
SYSTEMD_USER_DIR = os.path.join(USER_HOME, ".config/systemd/user")
MANAGER_PORT = 80
# Пароль для входа (в реальном проекте лучше хранить хеш или в env)
ADMIN_PASSWORD = "SuperSecret52!" 

if not os.path.exists(SYSTEMD_USER_DIR):
    os.makedirs(SYSTEMD_USER_DIR)

# --- АВТОРИЗАЦИЯ ---

def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- ШАБЛОНЫ ---

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Вход</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light d-flex align-items-center justify-content-center" style="height: 100vh;">
    <div class="card shadow p-4" style="width: 350px;">
        <h3 class="text-center mb-3">🛡 Вход</h3>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}<div class="alert alert-danger">{{ messages[0][1] }}</div>{% endif %}
        {% endwith %}
        <form method="post">
            <div class="mb-3">
                <input type="password" name="password" class="form-control" placeholder="Пароль" required autofocus>
            </div>
            <button type="submit" class="btn btn-primary w-100">Войти</button>
        </form>
    </div>
</body>
</html>
"""

# (Ваш BASE_LAYOUT остался почти тем же, добавлена кнопка Выход)
BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>{% block title %}App Manager{% endblock %}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
.terminal { background: #1e1e1e; color: #0f0; padding: 15px; border-radius: 5px; font-family: monospace; max-height: 400px; overflow-y: auto; white-space: pre-wrap; font-size: 0.9em; }
</style>
</head>
<body class="bg-light">
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
<div class="container">
<a class="navbar-brand" href="/">🛠 App Manager</a>
<div class="collapse navbar-collapse">
<ul class="navbar-nav me-auto">
<li class="nav-item"><a class="nav-link" href="/">Список приложений</a></li>
<li class="nav-item"><a class="nav-link" href="/help">📖 Инструкция</a></li>
</ul>
<ul class="navbar-nav">
    <li class="nav-item"><a class="nav-link text-danger" href="/logout">Выход</a></li>
</ul>
</div>
</div>
</nav>

<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}
{% for category, message in messages %}
<div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
{{ message }}
<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
</div>
{% endfor %}
{% endif %}
{% endwith %}

{% block content %}{% endblock %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# Изменена ссылка "Открыть" - теперь она ведет на прокси
PAGE_APP_DETAIL = """
{% extends "base" %}
{% block title %}{{ app.name }} - Детали{% endblock %}
{% block content %}
<div class="row">
<div class="col-md-4">
<div class="card shadow-sm mb-4">
<div class="card-body">
<h3 class="card-title">{{ app.name }}</h3>
<p class="text-muted small">{{ app.path }}</p>
<hr>
{% if not app.has_service %}
<div class="alert alert-secondary">Сервис еще не создан.</div>
<form action="{{ url_for('create_service', name=app.name) }}" method="post">
<div class="mb-3">
<label for="portInput" class="form-label">Назначить порт</label>
<input type="number" class="form-control" id="portInput" name="custom_port" placeholder="Авто">
</div>
<button class="btn btn-primary w-100">⚙️ Создать (Localhost Only)</button>
</form>

{% else %}
<div class="mb-3">
Status: 
{% if app.active_state == 'active' %}
<span class="badge bg-success">Running</span>
{% else %}
<span class="badge bg-secondary">{{ app.active_state }}</span>
{% endif %}
</div>

{% if app.assigned_port and app.active_state == 'active' %}
<div class="d-grid gap-2 mb-3">
<a href="/proxy/{{ app.name }}/" target="_blank" class="btn btn-success">
🚀 Открыть приложение
</a>
</div>
{% endif %}

<div class="d-grid gap-2">
{% if app.active_state == 'active' %}
<a href="{{ url_for('service_action', name=app.name, action='restart') }}" class="btn btn-warning">🔄 Перезагрузить</a>
<a href="{{ url_for('service_action', name=app.name, action='stop') }}" class="btn btn-danger">⏹ Остановить</a>
{% else %}
<a href="{{ url_for('service_action', name=app.name, action='start') }}" class="btn btn-success">▶ Запустить</a>
{% endif %}
</div>
<hr>
<a href="{{ url_for('delete_service', name=app.name) }}" class="btn btn-sm btn-outline-danger w-100" onclick="return confirm('Удалить?')">🗑 Удалить сервис</a>
{% endif %}
</div>
</div>
</div>

<div class="col-md-8">
<div class="card shadow-sm">
<div class="card-header bg-dark text-white">📋 Логи</div>
<div class="card-body bg-dark p-0">
<div class="terminal">{% if logs %}{{ logs }}{% else %}Нет логов.{% endif %}</div>
</div>
</div>
</div>
</div>
{% endblock %}
"""

PAGE_HELP = """
{% extends "base" %}
{% block title %}Инструкция{% endblock %}
{% block content %}
<div class="card shadow-sm">
<div class="card-body">
<h2 class="mb-4">📖 Как писать приложения для прокси</h2>
<div class="alert alert-warning">
<strong>Важно:</strong> Приложения теперь работают в закрытом режиме. Они должны слушать <b>127.0.0.1</b>.
</div>
<h4>Требования к коду (Flask)</h4>
<p>Диспетчер передает переменные <code>PORT</code> и <code>HOST</code>. Используйте их.</p>
<pre class="bg-dark text-white p-3 rounded">
import os
from flask import Flask, url_for

app = Flask(__name__)

# Чтобы url_for генерировал правильные ссылки через прокси
# (Flask автоматически подхватит заголовки X-Script-Name, если включить ProxyFix,
# но часто достаточно просто использовать относительные пути).

@app.route("/")
def index():
    return "Привет! <a href='./page2'>Вторая страница</a>"

@app.route("/page2")
def page2():
    return "Это страница 2. <a href='./'>Назад</a>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1") # Теперь по умолчанию localhost!
    app.run(host=host, port=port)
</pre>
</div>
</div>
{% endblock %}
"""

PAGE_DASHBOARD = """
{% extends "base" %}
{% block title %}Список приложений{% endblock %}
{% block content %}
<div class="card shadow-sm">
    <div class="card-header bg-white">
        <h4 class="mb-0">Ваши приложения</h4>
    </div>
    <div class="card-body p-0">
        <table class="table table-hover align-middle mb-0">
            <thead class="table-light">
                <tr>
                    <th>Имя</th>
                    <th>Статус</th>
                    <th>Порт</th>
                    <th class="text-end">Действия</th>
                </tr>
            </thead>
            <tbody>
            {% for app in apps %}
                <tr style="cursor: pointer;" onclick="window.location='/app/{{ app.name }}'">
                    <td class="fw-bold">{{ app.name }}</td>
                    <td>
                        {% if app.has_service %}
                            {% if app.active_state == 'active' %}
                                <span class="badge bg-success">Active</span>
                            {% elif app.active_state == 'failed' %}
                                <span class="badge bg-danger">Failed</span>
                            {% else %}
                                <span class="badge bg-secondary">Stopped</span>
                            {% endif %}
                        {% else %}
                            <span class="badge bg-warning text-dark">Не установлен</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if app.assigned_port %}
                            <span class="badge bg-info text-dark">:{{ app.assigned_port }}</span>
                        {% else %}
                            <span class="text-muted">-</span>
                        {% endif %}
                    </td>
                    <td class="text-end">
                        <a href="/app/{{ app.name }}" class="btn btn-sm btn-outline-primary">Управление →</a>
                    </td>
                </tr>
            {% else %}
                <tr><td colspan="4" class="text-center py-4">Папка <code>~/apps</code> пуста</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endblock %}
"""

# Остальные шаблоны (dashboard) используем как были, добавив их в словарь
templates = {
    "login": LOGIN_PAGE,
    "base": BASE_LAYOUT,
    "dashboard": PAGE_DASHBOARD, # Возьмите из вашего исходного кода
    "detail": PAGE_APP_DETAIL,
    "help": PAGE_HELP
}

# --- ПОДМЕНА ШАБЛОНОВ ---
# (Для работы этого примера нужно добавить PAGE_DASHBOARD из вашего старого кода в словарь templates)
from jinja2 import DictLoader
app.jinja_loader = DictLoader(templates)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def run_systemctl(args):
    cmd = ["systemctl", "--user"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return ""

def get_assigned_port(service_name):
    service_file = os.path.join(SYSTEMD_USER_DIR, service_name)
    if os.path.exists(service_file):
        with open(service_file, 'r') as f:
            for line in f:
                if "Environment=PORT=" in line:
                    return line.split('=')[-1].strip()
    return None

def is_port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', int(port))) != 0 # Проверяем на localhost

def find_free_port(start=5001):
    port = start
    while port < 6000:
        if is_port_free(port):
            return port
        port += 1
    return None

def get_app_status(name):
    service_name = f"{name}.service"
    has_service = os.path.exists(os.path.join(SYSTEMD_USER_DIR, service_name))
    active_state = "N/A"
    port = None
    if has_service:
        active_state = run_systemctl(["is-active", service_name])
        port = get_assigned_port(service_name)
    return {
        "name": name,
        "path": os.path.join(APPS_DIR, name),
        "has_service": has_service,
        "active_state": active_state,
        "assigned_port": port
    }

# --- МАРШРУТЫ АВТОРИЗАЦИИ ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['user'] = 'admin'
            flash('Добро пожаловать!', 'success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            flash('Неверный пароль', 'danger')
    return render_page('login')

@app.route('/logout')
def logout():
    session.pop('user', None)
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))

# --- ОСНОВНЫЕ МАРШРУТЫ ---

@app.route('/')
@login_required
def index():
    apps = []
    if os.path.exists(APPS_DIR):
        for item in sorted(os.listdir(APPS_DIR)):
            path = os.path.join(APPS_DIR, item)
            if os.path.isdir(path):
                apps.append(get_app_status(item))
    return render_page("dashboard", apps=apps)

@app.route('/help')
@login_required
def help_page():
    return render_page("help")

@app.route('/app/<name>')
@login_required
def app_detail(name):
    app_data = get_app_status(name)
    logs = ""
    if app_data['has_service']:
        cmd = ["journalctl", "--user", "-u", f"{name}.service", "-n", "50", "--no-pager"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        logs = res.stdout
    return render_page("detail", app=app_data, logs=logs)

# --- PROXY CORE ---
# Перехватываем путь /proxy/<имя_приложения>/<всё_остальное>
@app.route('/proxy/<name>/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/proxy/<name>/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def proxy(name, path):
    # 1. Узнаем порт приложения
    app_data = get_app_status(name)
    if not app_data['assigned_port'] or app_data['active_state'] != 'active':
        return "Application is not running", 502

    target_url = f"http://127.0.0.1:{app_data['assigned_port']}/{path}"

    # 2. Собираем заголовки (исключаем хост, чтобы не путать backend)
    headers = {key: value for (key, value) in request.headers if key != 'Host'}
    
    # 3. Добавляем заголовки для корректной работы Flask/Django за прокси
    # Это позволяет приложению понять, что оно находится по пути /proxy/appname/
    headers['X-Script-Name'] = f"/proxy/{name}"
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Proto'] = request.scheme

    try:
        # 4. Выполняем запрос к локальному приложению
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False, # Сами обработаем редиректы
            params=request.args
        )

        # 5. Обрабатываем ответ
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers_back = [(name, value) for (name, value) in resp.headers.items()
                        if name.lower() not in excluded_headers]

        # Если приложение делает редирект, нам нужно подправить Location
        # Например, backend редиректит на /login, а нам нужно /proxy/appname/login
        if 'Location' in resp.headers:
             # Логика упрощена, но для относительных редиректов должно работать
             # Лучше полагаться на X-Script-Name внутри приложения
             pass 

        return Response(resp.content, resp.status_code, headers_back)

    except Exception as e:
        return f"Proxy Error: {e}", 500


# --- УПРАВЛЕНИЕ СЕРВИСАМИ ---

@app.route('/create/<name>', methods=['POST'])
@login_required
def create_service(name):
    app_path = os.path.join(APPS_DIR, name)
    # ... (логика выбора порта осталась прежней) ...
    # Упростил для примера
    port = find_free_port()
    if not port:
        flash("Нет свободных портов", "danger")
        return redirect(url_for('app_detail', name=name))

    # Поиск python (как было у вас)
    python_exec = "/usr/bin/python3"
    venv_python = os.path.join(app_path, "venv/bin/python")
    if os.path.exists(venv_python):
        python_exec = venv_python
    
    exec_cmd = f"{python_exec} {app_path}/main.py" # Упрощено

    # ГЕНЕРАЦИЯ СЕРВИСА
    # ВАЖНО: Добавлено HOST=127.0.0.1
    service_content = f"""[Unit]
Description={name} (Port {port})
After=network.target

[Service]
Type=simple
WorkingDirectory={app_path}
Environment=PORT={port}
Environment=HOST=127.0.0.1
Environment=PYTHONUNBUFFERED=1
ExecStart={exec_cmd}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    try:
        with open(os.path.join(SYSTEMD_USER_DIR, f"{name}.service"), "w") as f:
            f.write(service_content)
        run_systemctl(["daemon-reload"])
        run_systemctl(["enable", f"{name}.service"])
        run_systemctl(["start", f"{name}.service"])
        flash(f"Сервис создан. Доступ только через Manager.", "success")
    except Exception as e:
        flash(f"Ошибка: {e}", "danger")
    
    return redirect(url_for('app_detail', name=name))

@app.route('/action/<name>/<action>')
@login_required
def service_action(name, action):
    run_systemctl([action, f"{name}.service"])
    return redirect(url_for('app_detail', name=name))

@app.route('/delete/<name>')
@login_required
def delete_service(name):
    service = f"{name}.service"
    run_systemctl(["stop", service])
    run_systemctl(["disable", service])
    path = os.path.join(SYSTEMD_USER_DIR, service)
    if os.path.exists(path): os.remove(path)
    run_systemctl(["daemon-reload"])
    flash(f"Сервис {name} удален", "warning")
    return redirect(url_for('app_detail', name=name))

def render_page(template_name, **context):
    context['manager_port'] = MANAGER_PORT
    return render_template_string(templates[template_name], **context)

if __name__ == '__main__':
    # Сам менеджер слушаем на 0.0.0.0, чтобы зайти в него
    app.run(host='0.0.0.0', port=MANAGER_PORT)