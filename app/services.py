import os
import socket
import subprocess
import shlex
import signal
from .config import Config

def run_systemctl(args):
    """Запуск systemctl --user команд"""
    cmd = ["systemctl", "--user"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return ""

def get_assigned_port(service_name):
    """Чтение порта из файла .service"""
    service_file = os.path.join(Config.SYSTEMD_USER_DIR, service_name)
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
    """Сбор информации о приложении для дашборда"""
    service_name = f"{name}.service"
    has_service = os.path.exists(os.path.join(Config.SYSTEMD_USER_DIR, service_name))
    active_state = "N/A"
    port = None
    
    if has_service:
        active_state = run_systemctl(["is-active", service_name])
        port = get_assigned_port(service_name)
        
    return {
        "name": name,
        "path": os.path.join(Config.APPS_DIR, name),
        "has_service": has_service,
        "active_state": active_state,
        "assigned_port": port
    }

def parse_service_file(name):
    """
    Читает файл сервиса и извлекает:
    - ExecStart (команда запуска)
    - WorkingDirectory (рабочая папка)
    - Environment (переменные окружения, например PORT)
    """
    service_path = os.path.join(Config.SYSTEMD_USER_DIR, f"{name}.service")
    if not os.path.exists(service_path):
        return None

    info = {
        'cmd': '',
        'cwd': '',
        'env': os.environ.copy() # Начинаем с текущего окружения
    }

    with open(service_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('ExecStart='):
                info['cmd'] = line.split('=', 1)[1]
            elif line.startswith('WorkingDirectory='):
                info['cwd'] = line.split('=', 1)[1]
            elif line.startswith('Environment='):
                # Формат: Environment=PORT=5001
                # Может быть несколько переменных в одной строке, но systemd обычно пишет по одной
                parts = line.split('=', 1)[1] # PORT=5001
                if '=' in parts:
                    k, v = parts.split('=', 1)
                    info['env'][k] = v
    
    return info

def run_diagnostic_test(name):
    """
    Запускает приложение на 5 секунд.
    Корректно убивает процесс после теста, чтобы не занимать порт.
    """
    # 1. Останавливаем сервис Systemd
    run_systemctl(["stop", f"{name}.service"])
    
    # 2. Получаем конфиг
    config = parse_service_file(name)
    if not config or not config['cmd']:
        return "Ошибка: Не удалось прочитать конфигурацию сервиса."

    report = []
    report.append(f"🔍 Диагностика для: {name}")
    report.append(f"📂 Директория: {config['cwd']}")
    report.append(f"🚀 Команда: {config['cmd']}")
    report.append("-" * 40)

    process = None
    try:
        # 3. Запускаем процесс через Popen
        # preexec_fn=os.setsid создает новую группу процессов.
        # Это позволит нам убить и shell, и python одним махом.
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

        # Ждем 5 секунд
        stdout, stderr = process.communicate(timeout=5)
        
        # Если мы здесь — процесс завершился САМ (упал) раньше 5 секунд
        report.append("❌ ПРИЛОЖЕНИЕ УПАЛО СРАЗУ ПОСЛЕ ЗАПУСКА.")
        report.append(f"Код возврата: {process.returncode}")
        
        if stdout: report.append(f"\n[STDOUT]:\n{stdout}")
        if stderr: report.append(f"\n[STDERR (ОШИБКИ)]:\n{stderr}")

    except subprocess.TimeoutExpired:
        # 4. УСПЕХ: Таймаут сработал = приложение висело и работало
        report.append("✅ УСПЕХ: Приложение успешно запустилось и проработало 5 секунд.")
        
        # Читаем то, что успело набежать в логи (без блокировки)
        # В Popen это сложнее, чем в run, но обычно stderr пуст или там логи запуска
        report.append("Оно не упало сразу. Вероятно, код рабочий.")

        # !!! ВАЖНО: УБИВАЕМ ПРОЦЕСС !!!
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except:
            pass
        
        report.append("\nℹ️ Тестовый процесс остановлен, порт освобожден.")

    except Exception as e:
        report.append(f"❌ Системная ошибка диагностики: {e}")
        # На всякий случай пытаемся убить
        if process:
            try: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except: pass

    return "\n".join(report)