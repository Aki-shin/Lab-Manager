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


def delete_app_service(name):
    """Останавливает, отключает и удаляет .service файл."""
    svc = _service_name(name)
    run_systemctl(["stop", svc])
    run_systemctl(["disable", svc])

    path = _service_path(name)
    if os.path.exists(path):
        os.remove(path)
        run_systemctl(["daemon-reload"])
        return True
    return False


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


# --- Git интеграция ---

GIT_URL_RE = re.compile(r'^(https?://|git@)[\w.@:/\-~]+\.git$|^(https?://)[\w.@:/\-~]+$')


def is_valid_git_url(url):
    """Простая валидация git URL."""
    if not url:
        return False
    return bool(GIT_URL_RE.match(url.strip()))


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
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
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
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return False, f"git pull failed: {result.stderr.strip()}"
        return True, result.stdout.strip() or "Обновлено"
    except Exception as e:
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
