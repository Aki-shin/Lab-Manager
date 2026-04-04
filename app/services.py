import os
import socket
import subprocess
import signal
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


def get_app_status(name):
    """Сбор информации о приложении для дашборда."""
    svc = _service_name(name)
    has_service = os.path.exists(_service_path(name))
    active_state = "N/A"
    port = None

    if has_service:
        active_state = run_systemctl(["is-active", svc])
        port = get_assigned_port(name)

    return {
        "name": name,
        "path": os.path.join(Config.APPS_DIR, name),
        "has_service": has_service,
        "active_state": active_state,
        "assigned_port": port
    }


def get_app_logs(name, lines=50):
    """Получает последние N строк логов приложения."""
    svc = _service_name(name)
    cmd = ["journalctl", "-u", svc, "-n", str(lines), "--no-pager"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.stdout
    except Exception as e:
        return f"Error reading logs: {e}"


def create_app_service(name, app_path, port, entry_cmd):
    """Создаёт и запускает systemd-сервис для приложения."""
    svc = _service_name(name)
    service_content = f"""[Unit]
Description={name} via Lab Manager (Port {port})
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
WantedBy=multi-user.target
"""
    with open(_service_path(name), "w") as f:
        f.write(service_content)

    run_systemctl(["daemon-reload"])
    run_systemctl(["enable", svc])
    run_systemctl(["start", svc])


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
    """
    service_path = _service_path(name)
    if not os.path.exists(service_path):
        return None

    info = {
        'cmd': '',
        'cwd': '',
        'env': {}
    }

    with open(service_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('ExecStart='):
                info['cmd'] = line.split('=', 1)[1]
            elif line.startswith('WorkingDirectory='):
                info['cwd'] = line.split('=', 1)[1]
            elif line.startswith('Environment='):
                parts = line.split('=', 1)[1]
                if '=' in parts:
                    k, v = parts.split('=', 1)
                    info['env'][k] = v

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
