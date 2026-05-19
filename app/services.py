import os
import re
import time
import shutil
import socket
import zipfile
import tarfile
import subprocess
import signal
import urllib.request
import urllib.error
import psutil
from .config import Config


def _service_name(app_name):
    """Формирует имя systemd-сервиса с префиксом."""
    return f"{Config.SERVICE_PREFIX}{app_name}.service"


def _service_path(app_name):
    """Полный путь к .service файлу."""
    return os.path.join(Config.SYSTEMD_DIR, _service_name(app_name))


def run_systemctl(args):
    """Запуск системных systemctl команд."""
    cmd = ["systemctl"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return ""


def get_assigned_port(app_name):
    """Чтение порта из файла .service."""
    service_file = _service_path(app_name)
    if os.path.exists(service_file):
        with open(service_file, 'r') as f:
            for line in f:
                if "Environment=PORT=" in line:
                    return line.split('=')[-1].strip()
    return None


def is_port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', int(port))) != 0


def find_free_port(start=5001):
    port = start
    while port < 6000:
        if is_port_free(port):
            return port
        port += 1
    return None


def get_service_pid(name):
    """Возвращает MainPID сервиса или None."""
    svc = _service_name(name)
    out = run_systemctl(["show", svc, "--property=MainPID", "--value"])
    try:
        pid = int(out)
        return pid if pid > 0 else None
    except (ValueError, TypeError):
        return None


def get_process_resources(pid):
    """Возвращает CPU%, RAM (MB), uptime (сек) процесса и его потомков."""
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        all_procs = [proc] + children

        # CPU: суммируем по дереву процессов
        cpu = sum(p.cpu_percent(interval=None) for p in all_procs if p.is_running())

        # RAM: суммарный RSS в MB
        rss = sum(p.memory_info().rss for p in all_procs if p.is_running())
        ram_mb = round(rss / (1024 * 1024), 1)

        uptime = int(time.time() - proc.create_time())

        return {
            "cpu": round(cpu, 1),
            "ram_mb": ram_mb,
            "uptime": uptime,
            "uptime_str": _format_uptime(uptime)
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return None


def _format_uptime(seconds):
    """Форматирует uptime в человеко-читаемый вид."""
    if seconds < 60:
        return f"{seconds}с"
    if seconds < 3600:
        return f"{seconds // 60}м {seconds % 60}с"
    if seconds < 86400:
        return f"{seconds // 3600}ч {(seconds % 3600) // 60}м"
    return f"{seconds // 86400}д {(seconds % 86400) // 3600}ч"


def check_app_health(port, timeout=1):
    """HTTP-пинг приложения. Возвращает код ответа или None."""
    if not port:
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        # HTTP-ответ пришёл (даже 4xx/5xx) — значит приложение живо
        return e.code
    except Exception:
        return None


def get_app_status(name, with_metrics=False):
    """
    Сбор информации о приложении для дашборда.
    with_metrics=True — включает CPU/RAM/uptime/health (медленнее).
    """
    svc = _service_name(name)
    has_service = os.path.exists(_service_path(name))
    active_state = "N/A"
    port = None
    pid = None
    resources = None
    health = None

    if has_service:
        active_state = run_systemctl(["is-active", svc])
        port = get_assigned_port(name)

        if with_metrics and active_state == "active":
            pid = get_service_pid(name)
            resources = get_process_resources(pid)
            health = check_app_health(port)

    return {
        "name": name,
        "path": os.path.join(Config.APPS_DIR, name),
        "has_service": has_service,
        "active_state": active_state,
        "assigned_port": port,
        "pid": pid,
        "resources": resources,
        "health": health,
        "is_git": os.path.isdir(os.path.join(Config.APPS_DIR, name, ".git"))
    }


def get_system_stats():
    """Системные ресурсы для дашборда."""
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        boot_time = psutil.boot_time()
        uptime = int(time.time() - boot_time)

        return {
            "cpu": round(cpu, 1),
            "ram_percent": mem.percent,
            "ram_used_gb": round(mem.used / (1024**3), 1),
            "ram_total_gb": round(mem.total / (1024**3), 1),
            "disk_percent": disk.percent,
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "uptime_str": _format_uptime(uptime),
            "load_avg": [round(x, 2) for x in os.getloadavg()]
        }
    except Exception:
        return None


def get_app_logs(name, lines=50):
    """Получает последние N строк логов приложения."""
    svc = _service_name(name)
    cmd = ["journalctl", "-u", svc, "-n", str(lines), "--no-pager"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.stdout
    except Exception as e:
        return f"Error reading logs: {e}"


RESERVED_ENV_KEYS = {"PORT", "HOST", "PYTHONUNBUFFERED"}


def create_app_service(name, app_path, port, entry_cmd, extra_env=None, start=True):
    """Создаёт (и опционально запускает) systemd-сервис для приложения."""
    svc = _service_name(name)

    env_lines = [
        f"Environment=PORT={port}",
        "Environment=HOST=127.0.0.1",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    if extra_env:
        for k, v in extra_env.items():
            if k in RESERVED_ENV_KEYS or not k:
                continue
            # Экранируем спец-символы для systemd
            v_escaped = v.replace('\\', '\\\\').replace('"', '\\"')
            env_lines.append(f'Environment="{k}={v_escaped}"')

    env_block = "\n".join(env_lines)

    service_content = f"""[Unit]
Description={name} via Lab Manager (Port {port})
After=network.target

[Service]
Type=simple
WorkingDirectory={app_path}
{env_block}
ExecStart={entry_cmd}
Restart=always
RestartSec=5
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=10
SendSIGKILL=yes

[Install]
WantedBy=multi-user.target
"""
    with open(_service_path(name), "w") as f:
        f.write(service_content)

    run_systemctl(["daemon-reload"])
    run_systemctl(["enable", svc])
    if start:
        run_systemctl(["start", svc])


def update_app_service(name, port, entry_cmd, extra_env=None):
    """Обновляет .service файл (редактирование) и перезапускает."""
    app_path = os.path.join(Config.APPS_DIR, name)
    was_active = run_systemctl(["is-active", _service_name(name)]) == "active"
    create_app_service(name, app_path, port, entry_cmd, extra_env=extra_env, start=False)
    if was_active:
        run_systemctl(["restart", _service_name(name)])


def control_service(name, action):
    """start / stop / restart сервиса."""
    run_systemctl([action, _service_name(name)])


def _force_kill_tree(pid):
    """Принудительно убивает процесс и всех его потомков."""
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        procs = proc.children(recursive=True) + [proc]
    except psutil.NoSuchProcess:
        return
    except Exception:
        return

    # SIGTERM
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=3)
    # SIGKILL для всех, кто ещё жив
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def _kill_by_port(port):
    """Убивает процесс, занимающий указанный TCP-порт (safety net)."""
    if not port:
        return
    try:
        port = int(port)
    except (TypeError, ValueError):
        return
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN and conn.pid:
                _force_kill_tree(conn.pid)
    except (psutil.AccessDenied, Exception):
        pass


def delete_app_service(name):
    """Останавливает, отключает и удаляет .service файл.

    Гарантирует, что процесс приложения действительно убит:
    - systemctl stop (graceful)
    - ожидание перехода в inactive
    - принудительный kill дерева процессов по MainPID
    - освобождение порта (на случай «сиротского» процесса)
    """
    svc = _service_name(name)
    path = _service_path(name)

    if not os.path.exists(path):
        return False

    # Запоминаем данные ДО остановки — после stop MainPID обнулится
    port = get_assigned_port(name)
    pid = get_service_pid(name)

    # 1. Graceful stop
    run_systemctl(["stop", svc])

    # 2. Ждём до 5 секунд перехода в inactive
    for _ in range(10):
        if run_systemctl(["is-active", svc]) != "active":
            break
        time.sleep(0.5)

    # 3. Если процесс всё ещё жив — бьём по дереву
    if pid:
        try:
            if psutil.pid_exists(pid):
                _force_kill_tree(pid)
        except Exception:
            pass

    # 4. Safety net: убиваем всё, что ещё висит на порту
    _kill_by_port(port)

    # 5. Отключаем, сбрасываем failed-состояние
    run_systemctl(["disable", svc])
    run_systemctl(["reset-failed", svc])

    # 6. Удаляем unit-файл
    os.remove(path)
    run_systemctl(["daemon-reload"])
    return True


def parse_service_file(name):
    """
    Читает файл сервиса и извлекает:
    - ExecStart, WorkingDirectory, Environment
    Отдельно возвращает user_env (без служебных переменных).
    """
    service_path = _service_path(name)
    if not os.path.exists(service_path):
        return None

    info = {
        'cmd': '',
        'cwd': '',
        'env': {},
        'user_env': {},
        'port': None
    }

    with open(service_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('ExecStart='):
                info['cmd'] = line.split('=', 1)[1]
            elif line.startswith('WorkingDirectory='):
                info['cwd'] = line.split('=', 1)[1]
            elif line.startswith('Environment='):
                raw = line.split('=', 1)[1]
                # Поддержка формата Environment="K=V"
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]
                if '=' in raw:
                    k, v = raw.split('=', 1)
                    info['env'][k] = v
                    if k == 'PORT':
                        info['port'] = v
                    if k not in RESERVED_ENV_KEYS:
                        info['user_env'][k] = v

    return info


def run_diagnostic_test(name):
    """
    Запускает приложение на 5 секунд для проверки.
    Если сервис был запущен — перезапускает в конце.
    """
    svc = _service_name(name)
    was_active = run_systemctl(["is-active", svc]) == "active"
    run_systemctl(["stop", svc])

    config = parse_service_file(name)
    if not config or not config['cmd']:
        return "Ошибка: Не удалось прочитать конфигурацию сервиса."

    report = []
    report.append(f"Диагностика для: {name}")
    report.append(f"Директория: {config['cwd']}")
    report.append(f"Команда: {config['cmd']}")
    report.append("-" * 40)

    process = None
    try:
        process = subprocess.Popen(
            config['cmd'],
            shell=True,
            cwd=config['cwd'],
            env=config['env'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid
        )

        stdout, stderr = process.communicate(timeout=5)

        report.append("ОШИБКА: Приложение завершилось сразу после запуска.")
        report.append(f"Код возврата: {process.returncode}")
        if stdout:
            report.append(f"\n[STDOUT]:\n{stdout}")
        if stderr:
            report.append(f"\n[STDERR]:\n{stderr}")

    except subprocess.TimeoutExpired:
        report.append("УСПЕХ: Приложение запустилось и проработало 5 секунд.")
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            pass
        report.append("Тестовый процесс остановлен, порт освобожден.")

    except Exception as e:
        report.append(f"Системная ошибка диагностики: {e}")
        if process:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                pass

    if was_active:
        run_systemctl(["start", svc])
        report.append("\nСервис автоматически перезапущен.")

    return "\n".join(report)


# --- SSE streaming логов ---

def stream_app_logs(name):
    """
    Генератор для Server-Sent Events: непрерывно читает journalctl -f.
    Используется в /logs/<name>/stream эндпоинте.
    """
    svc = _service_name(name)
    # Сначала отдаём последние 50 строк, потом follow
    proc = subprocess.Popen(
        ["journalctl", "-u", svc, "-n", "50", "-f", "--no-pager", "-o", "short-iso"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            # SSE формат: data: <line>\n\n
            # Убираем \r и экранируем переводы строк
            clean = line.rstrip('\n').replace('\r', '')
            yield f"data: {clean}\n\n"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# --- Авто-настройка окружения приложения ---

def setup_app_environment(name):
    """
    Автоматически создаёт venv и ставит зависимости из requirements.txt,
    если это похоже на Python-приложение.
    Возвращает (ok, message).
    """
    app_path = os.path.join(Config.APPS_DIR, name)
    if not os.path.isdir(app_path):
        return False, f"Директория {app_path} не найдена"

    req_file = os.path.join(app_path, "requirements.txt")
    has_python = any(
        os.path.exists(os.path.join(app_path, f))
        for f in ("app.py", "main.py", "wsgi.py")
    )
    has_req = os.path.exists(req_file)

    if not has_req and not has_python:
        return True, "Python-файлов не обнаружено — venv не требуется"

    venv_dir = os.path.join(app_path, "venv")
    venv_python = os.path.join(venv_dir, "bin", "python")

    # 1. Создаём venv, если его нет или он сломан
    if not (os.path.exists(venv_python) and os.access(venv_python, os.X_OK)):
        # Если существует, но битый — сносим
        if os.path.isdir(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
        try:
            result = subprocess.run(
                ["python3", "-m", "venv", venv_dir],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return False, f"Ошибка создания venv: {result.stderr.strip()[:300]}"
        except subprocess.TimeoutExpired:
            return False, "Таймаут создания venv (>120с)"
        except Exception as e:
            return False, f"Ошибка создания venv: {e}"

    # 2. Обновляем pip и ставим зависимости
    if has_req:
        try:
            result = subprocess.run(
                [venv_python, "-m", "pip", "install",
                 "--disable-pip-version-check", "--quiet",
                 "-r", req_file],
                capture_output=True, text=True, timeout=600
            )
            if result.returncode != 0:
                err = (result.stderr.strip() or result.stdout.strip())[:400]
                return False, f"venv создан, но pip install упал: {err}"
            return True, "venv создан, зависимости установлены"
        except subprocess.TimeoutExpired:
            return False, "Таймаут установки зависимостей (>10 мин)"
        except Exception as e:
            return False, f"venv создан, но ошибка pip: {e}"

    return True, "venv создан (requirements.txt не найден)"


# --- Git интеграция ---

GIT_URL_RE = re.compile(r'^(https?://|git@)[\w.@:/\-~]+\.git$|^(https?://)[\w.@:/\-~]+$')


def is_valid_git_url(url):
    """Простая валидация git URL."""
    if not url:
        return False
    return bool(GIT_URL_RE.match(url.strip()))


def _git_env():
    """
    Окружение для git-операций: запрещаем любые интерактивные запросы.
    Без этого git зависает на запросе логина/пароля (приватный репозиторий)
    или подтверждения host key (SSH), т.к. в subprocess ввода нет.
    """
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'          # не спрашивать креды по HTTPS
    env['GCM_INTERACTIVE'] = 'never'           # git-credential-manager тоже молчит
    # Гарантируем PATH: при запуске под systemd окружение может быть урезано,
    # а без PATH subprocess не найдёт исполняемый файл git.
    env.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    if not env.get('PATH'):
        env['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    # SSH: не зависать на «authenticity of host» и на парольной фразе ключа
    env.setdefault(
        'GIT_SSH_COMMAND',
        'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
    )
    return env


def git_clone_app(git_url, name):
    """Клонирует репозиторий в APPS_DIR/name. Возвращает (ok, message)."""
    if not is_valid_git_url(git_url):
        return False, "Неверный формат git URL"

    target = os.path.join(Config.APPS_DIR, name)
    if os.path.exists(target):
        return False, f"Директория {target} уже существует"

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, target],
            capture_output=True, text=True, timeout=120, env=_git_env()
        )
        if result.returncode != 0:
            # Чистим за собой пустую/частичную директорию
            shutil.rmtree(target, ignore_errors=True)
            return False, f"git clone failed: {result.stderr.strip()}"
        return True, f"Репозиторий клонирован в {target}"
    except subprocess.TimeoutExpired:
        return False, "Таймаут клонирования (>120с)"
    except Exception as e:
        return False, f"Ошибка: {e}"


def git_pull_app(name):
    """git pull в существующем приложении. Возвращает (ok, message)."""
    target = os.path.join(Config.APPS_DIR, name)
    if not os.path.isdir(os.path.join(target, ".git")):
        return False, "Это не git-репозиторий"

    try:
        result = subprocess.run(
            ["git", "-C", target, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120, env=_git_env()
        )
        if result.returncode != 0:
            err = (result.stderr.strip() or result.stdout.strip())
            return False, f"git pull failed: {err}"
        return True, result.stdout.strip() or "Обновлено"
    except subprocess.TimeoutExpired:
        return False, "Таймаут обновления (>120с)"
    except Exception as e:
        return False, f"Ошибка: {e}"


# --- Обновление приложения с автооткатом ---

def _git_app_head(app_path):
    """Текущий commit приложения (полный hash) или None."""
    try:
        res = subprocess.run(
            ["git", "-C", app_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, env=_git_env()
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def _git_app_reset(app_path, commit):
    """Жёсткий откат рабочего дерева приложения на указанный commit."""
    try:
        res = subprocess.run(
            ["git", "-C", app_path, "reset", "--hard", commit],
            capture_output=True, text=True, timeout=30, env=_git_env()
        )
        return res.returncode == 0
    except Exception:
        return False


def _get_nrestarts(svc):
    """Сколько раз systemd автоматически перезапускал сервис (Restart=)."""
    out = run_systemctl(["show", svc, "--property=NRestarts", "--value"])
    try:
        return int(out)
    except (ValueError, TypeError):
        return None


def _wait_app_healthy(name, settle=20):
    """
    Проверяет, что приложение пережило обновление.

    «Здорово» = сервис стабильно `active` и не уходит в crash-loop.
    HTTP-доступность учитывается как бонус, но не требуется — приложение
    может вообще не быть веб-сервером. Crash-loop ловим по росту NRestarts:
    при ошибке запуска systemd (Restart=always) начинает перезапускать юнит.

    Возвращает (healthy: bool, reason: str|None).
    """
    svc = _service_name(name)
    restarts_start = _get_nrestarts(svc)
    deadline = time.time() + settle
    last_state = "?"

    while time.time() < deadline:
        time.sleep(2)
        last_state = run_systemctl(["is-active", svc])
        if last_state == "failed":
            return False, "сервис упал в состояние failed"
        nrestarts = _get_nrestarts(svc)
        if (restarts_start is not None and nrestarts is not None
                and nrestarts > restarts_start):
            return False, (f"приложение перезапускается циклически "
                           f"(crash-loop: {nrestarts - restarts_start} авто-рестартов)")

    if last_state != "active":
        return False, f"сервис не активен (состояние: {last_state})"

    # Бонус-диагностика: явная ошибка HTTP считается поломкой
    port = get_assigned_port(name)
    code = check_app_health(port) if port else None
    if code is not None and code >= 500:
        return False, f"приложение отвечает с ошибкой HTTP {code}"
    return True, None


def update_app_with_rollback(name):
    """
    Обновляет приложение из git с автооткатом при поломке.

    Шаги: точка отката → git pull → пересборка venv → перезапуск →
    проверка работоспособности. Если приложение не поднялось — откат кода,
    повторная пересборка venv, перезапуск.

    Возвращает (ok: bool, message: str, report: dict|None).
    report заполняется только при сбое и содержит детали для UI.
    """
    app_path = os.path.join(Config.APPS_DIR, name)
    if not os.path.isdir(os.path.join(app_path, ".git")):
        return False, "Это не git-репозиторий", None

    # 1. Точка отката
    old_commit = _git_app_head(app_path)
    if not old_commit:
        return False, "Не удалось определить текущий commit приложения", None

    # 2. git pull
    pull_ok, pull_msg = git_pull_app(name)
    if not pull_ok:
        return False, f"Обновление не выполнено: {pull_msg}", None

    new_commit = _git_app_head(app_path)
    if new_commit == old_commit:
        return True, "Обновлений нет — установлена последняя версия.", None

    # 3. venv и зависимости
    setup_ok, setup_msg = setup_app_environment(name)

    # 4. Перезапуск сервиса (если он есть)
    if not os.path.exists(_service_path(name)):
        return True, (f"Код обновлён до {new_commit[:7]} ({setup_msg}). "
                      f"Сервис не создан — проверка работоспособности пропущена."), None

    control_service(name, "restart")

    # 5. Проверка работоспособности
    healthy, reason = _wait_app_healthy(name)
    if healthy:
        return True, (f"Приложение обновлено до {new_commit[:7]} "
                      f"и работает штатно. {setup_msg}."), None

    # 6. Автооткат
    crash_logs = get_app_logs(name, lines=120)
    rb_ok = _git_app_reset(app_path, old_commit)
    setup_app_environment(name)
    control_service(name, "restart")

    report = {
        "failed_commit": new_commit,
        "rolled_back_to": old_commit if rb_ok else None,
        "reason": reason,
        "setup_msg": setup_msg,
        "logs": crash_logs,
    }
    if rb_ok:
        msg = (f"Обновление до {new_commit[:7]} сломало приложение "
               f"({reason}). Выполнен автооткат к {old_commit[:7]}.")
    else:
        msg = (f"Обновление до {new_commit[:7]} сломало приложение "
               f"({reason}), и автооткат не удался — требуется ручное вмешательство.")
    return False, msg, report


def check_app_updates(name):
    """
    Проверяет наличие обновлений приложения в git (git fetch + сравнение SHA).

    Сравнение по SHA, а не по `rev-list --count`, надёжно работает и на
    shallow-клонах (--depth 1), где полной истории между HEAD и origin нет.

    Возвращает dict: {is_git, commit, branch, update_available, error}.
    """
    app_path = os.path.join(Config.APPS_DIR, name)
    if not os.path.isdir(os.path.join(app_path, ".git")):
        return {"is_git": False, "commit": None, "branch": None,
                "update_available": False, "error": None}

    res = {"is_git": True, "commit": None, "branch": None,
           "update_available": False, "commits": [], "error": None}

    def _g(args, timeout=60):
        return subprocess.run(
            ["git", "-C", app_path] + args,
            capture_output=True, text=True, timeout=timeout, env=_git_env()
        )

    try:
        head = _git_app_head(app_path)
        res["commit"] = head[:7] if head else None

        br = _g(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = br.stdout.strip() if br.returncode == 0 else ""
        if not branch or branch == "HEAD":
            branch = "main"
        res["branch"] = branch

        fetch = _g(["fetch", "origin", branch], timeout=120)
        if fetch.returncode != 0:
            res["error"] = (fetch.stderr.strip() or "git fetch failed")[:200]
            return res

        remote = _g(["rev-parse", f"origin/{branch}"])
        if remote.returncode != 0:
            res["error"] = (remote.stderr.strip() or "rev-parse failed")[:200]
            return res

        remote_sha = remote.stdout.strip()
        if head and remote_sha and head != remote_sha:
            res["update_available"] = True
            # Список входящих коммитов (best-effort: на shallow-клоне может
            # быть неполным, но факт наличия обновления уже подтверждён по SHA)
            logres = _g(["log", "--format=%h\x1f%cI\x1f%s",
                         f"HEAD..origin/{branch}"])
            if logres.returncode == 0:
                for line in logres.stdout.strip().split("\n"):
                    if "\x1f" in line:
                        h, d, s = line.split("\x1f", 2)
                        res["commits"].append(
                            {"hash": h, "date": d, "subject": s})
    except subprocess.TimeoutExpired:
        res["error"] = "таймаут git"
    except Exception as e:
        res["error"] = str(e)[:200]
    return res


def get_app_git_info(name):
    """Версия приложения из git: commit / branch / date / subject."""
    app_path = os.path.join(Config.APPS_DIR, name)
    info = {"is_git": False, "commit": None, "commit_full": None,
            "branch": None, "date": None, "subject": None}
    if not os.path.isdir(os.path.join(app_path, ".git")):
        return info
    info["is_git"] = True

    def _g(args):
        return subprocess.run(
            ["git", "-C", app_path] + args,
            capture_output=True, text=True, timeout=15, env=_git_env()
        )
    try:
        br = _g(["rev-parse", "--abbrev-ref", "HEAD"])
        if br.returncode == 0:
            info["branch"] = br.stdout.strip()
        res = _g(["log", "-1", "--format=%h%n%H%n%cI%n%s"])
        if res.returncode == 0:
            parts = res.stdout.strip().split("\n", 3)
            if len(parts) == 4:
                (info["commit"], info["commit_full"],
                 info["date"], info["subject"]) = parts
    except Exception:
        pass
    return info


def _detect_default_branch(app_path):
    """Определяет ветку по умолчанию у origin после fetch."""
    def _g(args):
        return subprocess.run(
            ["git", "-C", app_path] + args,
            capture_output=True, text=True, timeout=30, env=_git_env()
        )
    # 1. origin/HEAD, если установлен
    res = _g(["symbolic-ref", "refs/remotes/origin/HEAD"])
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    # 2. Типовые имена
    for cand in ("main", "master"):
        if _g(["rev-parse", "--verify", f"origin/{cand}"]).returncode == 0:
            return cand
    # 3. Первая попавшаяся ветка origin
    res = _g(["branch", "-r"])
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("origin/") and "->" not in line:
                return line.split("/", 1)[1]
    return None


def attach_git_repo(name, git_url):
    """
    Привязывает существующее (не-git) приложение к git-репозиторию:
    git init → remote add → fetch → приведение кода к origin/<branch>.

    ВНИМАНИЕ: файлы приложения заменяются содержимым репозитория. Файлы вне
    репозитория (venv, рантайм-данные) сохраняются. Возвращает (ok, message).
    """
    if not is_valid_git_url(git_url):
        return False, "Неверный формат git URL"

    app_path = os.path.join(Config.APPS_DIR, name)
    if not os.path.isdir(app_path):
        return False, "Папка приложения не найдена"

    git_dir = os.path.join(app_path, ".git")
    if os.path.isdir(git_dir):
        return False, "Приложение уже привязано к git-репозиторию"

    def _g(args, timeout=120):
        return subprocess.run(
            ["git", "-C", app_path] + args,
            capture_output=True, text=True, timeout=timeout, env=_git_env()
        )

    try:
        init = _g(["init"])
        if init.returncode != 0:
            return False, f"git init failed: {init.stderr.strip()}"

        _g(["remote", "add", "origin", git_url])

        fetch = _g(["fetch", "--depth", "1", "origin"], timeout=120)
        if fetch.returncode != 0:
            shutil.rmtree(git_dir, ignore_errors=True)
            return False, f"git fetch failed: {fetch.stderr.strip()}"

        branch = _detect_default_branch(app_path)
        if not branch:
            shutil.rmtree(git_dir, ignore_errors=True)
            return False, "Не удалось определить ветку репозитория"

        # Приводим рабочее дерево к origin/<branch> (-f перезаписывает файлы)
        co = _g(["checkout", "-f", "-B", branch, f"origin/{branch}"])
        if co.returncode != 0:
            shutil.rmtree(git_dir, ignore_errors=True)
            return False, f"git checkout failed: {co.stderr.strip()}"

        # Гарантируем upstream — без него git pull при обновлении не сработает
        _g(["branch", f"--set-upstream-to=origin/{branch}", branch])

        return True, f"Репозиторий привязан, код синхронизирован с origin/{branch}"
    except subprocess.TimeoutExpired:
        shutil.rmtree(git_dir, ignore_errors=True)
        return False, "Таймаут операции git"
    except Exception as e:
        shutil.rmtree(git_dir, ignore_errors=True)
        return False, f"Ошибка: {e}"


# --- Загрузка архивов ---

SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')


def is_safe_app_name(name):
    """Валидация имени приложения (для папки)."""
    return bool(name) and bool(SAFE_NAME_RE.match(name)) and not name.startswith('.')


def extract_archive(archive_path, name):
    """
    Распаковывает zip/tar.gz в APPS_DIR/name.
    Защита от Zip Slip. Возвращает (ok, message).
    """
    if not is_safe_app_name(name):
        return False, "Недопустимое имя приложения"

    target = os.path.join(Config.APPS_DIR, name)
    if os.path.exists(target):
        return False, f"Директория {target} уже существует"

    os.makedirs(target, exist_ok=True)
    target_real = os.path.realpath(target)

    try:
        if archive_path.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as z:
                for member in z.namelist():
                    dest = os.path.realpath(os.path.join(target, member))
                    if not dest.startswith(target_real + os.sep) and dest != target_real:
                        raise ValueError(f"Zip Slip: {member}")
                z.extractall(target)
        elif archive_path.endswith(('.tar.gz', '.tgz', '.tar')):
            mode = 'r:gz' if archive_path.endswith(('.tar.gz', '.tgz')) else 'r:'
            with tarfile.open(archive_path, mode) as t:
                for member in t.getmembers():
                    dest = os.path.realpath(os.path.join(target, member.name))
                    if not dest.startswith(target_real + os.sep) and dest != target_real:
                        raise ValueError(f"Tar Slip: {member.name}")
                t.extractall(target)
        else:
            return False, "Поддерживаются только .zip, .tar.gz, .tgz"

        # Если архив содержит один корневой каталог — поднимаем содержимое
        _flatten_single_root(target)

        return True, f"Распаковано в {target}"
    except Exception as e:
        # Откатываем при ошибке
        shutil.rmtree(target, ignore_errors=True)
        return False, f"Ошибка распаковки: {e}"


def _flatten_single_root(target):
    """Если в target одна папка — поднять её содержимое на уровень выше."""
    entries = os.listdir(target)
    if len(entries) == 1:
        inner = os.path.join(target, entries[0])
        if os.path.isdir(inner):
            for item in os.listdir(inner):
                shutil.move(os.path.join(inner, item), os.path.join(target, item))
            os.rmdir(inner)
