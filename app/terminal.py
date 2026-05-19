"""
Веб-терминал tmux: WebSocket ↔ PTY.

На каждое соединение поднимается псевдотерминал, в котором выполняется
`tmux attach-session`. Сессия tmux живёт на сервере независимо: закрытие
вкладки лишь отсоединяет (detach), процессы внутри продолжают работать.

Транспорт — flask-sock (работает поверх Werkzeug dev-server с threaded=True).
Если flask-sock не установлен, модуль импортируется без ошибок, а живой
терминал просто недоступен (управление сессиями по HTTP при этом работает).
"""
import os
import pty
import json
import time
import select
import struct
import fcntl
import signal
import termios
import logging
import threading
import subprocess

from .auth import current_user
from . import tmux_manager

log = logging.getLogger(__name__)

try:
    from flask_sock import Sock
    _sock = Sock()
    _HAVE_SOCK = True
except ImportError:
    _sock = None
    _HAVE_SOCK = False


def is_terminal_available():
    """Доступен ли живой веб-терминал (установлен ли flask-sock)."""
    return _HAVE_SOCK


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
    except Exception:
        pass


def _terminal_handler(ws, name):
    """Обработчик WebSocket-соединения для одной tmux-сессии."""
    # Авторизация: только администратор
    user = current_user()
    if not user or not user.get('is_admin'):
        ws.close()
        return
    if not tmux_manager.valid_name(name) or not tmux_manager.session_exists(name):
        ws.close()
        return

    # Ждём первое сообщение с размером терминала (клиент шлёт его сразу при
    # открытии), чтобы tmux стартовал уже в нужном размере, а не в 80x24.
    init_cols, init_rows = 120, 32
    pending_input = []
    deadline = time.time() + 4
    while time.time() < deadline:
        try:
            msg = ws.receive(timeout=1)
        except Exception:
            return
        if msg is None:
            continue
        try:
            obj = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if obj.get('t') == 'r':
            try:
                init_cols = max(1, min(500, int(obj.get('c', 120))))
                init_rows = max(1, min(300, int(obj.get('r', 32))))
            except (TypeError, ValueError):
                pass
            break
        if obj.get('t') == 'i':
            pending_input.append(obj.get('d', ''))

    # Гарантируем, что окно сессии подстраивается под размер клиента
    try:
        subprocess.run(['tmux', 'set-option', '-t', '=' + name,
                        'window-size', 'latest'],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    # Поднимаем PTY и запускаем в нём tmux attach
    pid, fd = pty.fork()
    if pid == 0:
        # Дочерний процесс: становится tmux-клиентом
        env = dict(os.environ)
        env['TERM'] = 'xterm-256color'
        try:
            # -d: отсоединяем прочих клиентов, чтобы окно tmux подстроилось
            #     именно под размер этого веб-терминала, а не под чужой клиент.
            os.execvpe('tmux',
                       ['tmux', 'attach-session', '-d', '-t', '=' + name], env)
        except Exception:
            pass
        os._exit(1)

    # Сразу выставляем размер PTY — tmux подхватит его при старте или по SIGWINCH
    _set_winsize(fd, init_rows, init_cols)
    for d in pending_input:
        try:
            os.write(fd, d.encode('utf-8', 'replace'))
        except Exception:
            pass

    stop = threading.Event()

    def pty_to_ws():
        """Поток: вывод PTY → клиент."""
        while not stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if fd in r:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    ws.send(data)
            except Exception:
                break
        stop.set()
        try:
            ws.close()
        except Exception:
            pass

    reader = threading.Thread(target=pty_to_ws, name=f"term-{name}", daemon=True)
    reader.start()

    try:
        while not stop.is_set():
            msg = ws.receive(timeout=1)
            if msg is None:
                continue
            try:
                obj = json.loads(msg)
            except (ValueError, TypeError):
                continue
            kind = obj.get('t')
            if kind == 'i':                       # ввод с клавиатуры
                os.write(fd, obj.get('d', '').encode('utf-8', 'replace'))
            elif kind == 'r':                     # изменение размера окна
                try:
                    cols = max(1, min(500, int(obj.get('c', 80))))
                    rows = max(1, min(300, int(obj.get('r', 24))))
                    _set_winsize(fd, rows, cols)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    finally:
        stop.set()
        # Завершаем tmux-клиента (сама tmux-сессия при этом сохраняется)
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass


# Регистрируем WebSocket-маршрут, если flask-sock доступен
if _HAVE_SOCK:
    _sock.route('/tmux/<name>/ws')(_terminal_handler)


def init(app):
    """Подключает WebSocket-терминал к приложению."""
    if _HAVE_SOCK:
        _sock.init_app(app)
        log.info("[terminal] веб-терминал tmux включён")
    else:
        log.warning("[terminal] flask-sock не установлен — "
                    "живой веб-терминал tmux недоступен")
