"""
TCP-форвардер с авторизацией для «прозрачного» проброса внутренних приложений
на внешние порты Host Manager.

Идея:
    Внешний клиент → 0.0.0.0:<external_port> (слушает Host Manager)
    Host Manager    → проверяет cookie labmgr_session и права доступа
    OK             → сырой TCP-splice на 127.0.0.1:<internal_port>
    Не авторизован → HTTP 302 на /login?next=<исходный URL>

Преимущества перед path-based proxy (/proxy/name/):
    - Приложение «живёт» на корне /, не нужны X-Script-Name, <base href>, относительные URL.
    - Работают любые протоколы поверх TCP (WebSocket, SSE, chunked, бинарные).
    - Cookies приложения не конфликтуют с cookies панели (разные порты, но одна кука авторизации
      панели — labmgr_session — видна браузером на всех портах одного хоста по RFC 6265).
"""
import os
import time
import socket
import threading
import select
import logging
from urllib.parse import quote

from . import db
from .services import get_assigned_port

log = logging.getLogger(__name__)

BUFFER_SIZE = 16 * 1024
HEADER_LIMIT = 32 * 1024        # максимум 32 KiB на HTTP-заголовки
ACCEPT_TIMEOUT = 1.0            # чтобы accept-loop мог корректно останавливаться
UPSTREAM_CONNECT_TIMEOUT = 5.0  # подключение к внутреннему приложению
IDLE_TIMEOUT = 600              # 10 минут на простаивающее соединение (WS/SSE живут долго)


def get_bind_ip():
    """
    Интерфейс, на котором слушают форвардеры.

    Источник (по приоритету): настройка из БД (задаётся в UI) → переменная
    окружения FORWARD_BIND_IP → 0.0.0.0 (все интерфейсы).

    Конкретный IP позволяет внешнему порту совпадать с внутренним портом
    приложения: приложение слушает 127.0.0.1:PORT, форвардер — <IP>:PORT;
    адреса разные, конфликта нет (в отличие от 0.0.0.0, который перекрывает
    127.0.0.1).
    """
    val = None
    try:
        val = db.get_setting('forward_bind_ip')
    except Exception:
        val = None
    if val is None:
        val = os.environ.get('FORWARD_BIND_IP', '')
    val = (val or '').strip()
    return val or '0.0.0.0'


def can_bind(ip):
    """Проверяет, что на адресе можно открыть слушающий сокет
    (т.е. это адрес локального интерфейса сервера)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((ip, 0))
        s.close()
        return True
    except OSError:
        return False


class PortForwarder:
    """Один форвардер = один внешний порт, слушающий 0.0.0.0."""

    def __init__(self, flask_app, name, external_port):
        self.flask_app = flask_app
        self.name = name
        self.external_port = int(external_port)
        self._server_sock = None
        self._thread = None
        self._stop = threading.Event()
        # Счётчики трафика
        self._stats_lock = threading.Lock()
        self.bytes_in = 0    # клиент → upstream
        self.bytes_out = 0   # upstream → клиент
        self.total_conns = 0
        self.active_conns = 0
        self.started_at = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        bind_ip = get_bind_ip()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_ip, self.external_port))
        sock.listen(128)
        sock.settimeout(ACCEPT_TIMEOUT)

        self._server_sock = sock
        self._stop.clear()
        if self.started_at is None:
            self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"fwd-{self.name}-{self.external_port}",
            daemon=True,
        )
        self._thread.start()
        log.info(f"[forwarder] {self.name}: слушаю {bind_ip}:{self.external_port}")

    def stop(self):
        self._stop.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        log.info(f"[forwarder] {self.name}: остановлен")

    # ------------------------------------------------------------------ loop

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.settimeout(None)
            with self._stats_lock:
                self.total_conns += 1
                self.active_conns += 1
            threading.Thread(
                target=self._handle_connection,
                args=(client, addr),
                daemon=True,
            ).start()

    # --------------------------------------------------------------- handler

    def _handle_connection(self, client, addr):
        upstream = None
        try:
            # 1. Читаем HTTP-заголовки, пока не встретим \r\n\r\n
            client.settimeout(10)
            buf = b''
            while b'\r\n\r\n' not in buf:
                try:
                    chunk = client.recv(BUFFER_SIZE)
                except socket.timeout:
                    return
                if not chunk:
                    return
                buf += chunk
                if len(buf) > HEADER_LIMIT:
                    self._send_simple(client, 400, "Headers too large")
                    return
            client.settimeout(None)

            header_end = buf.index(b'\r\n\r\n') + 4
            header_text = buf[:header_end].decode('iso-8859-1', errors='replace')
            lines = header_text.split('\r\n')

            request_line = lines[0] if lines else ''
            headers = {}
            for line in lines[1:]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.strip().lower()] = v.strip()

            # 2. Проверяем авторизацию через cookie labmgr_session
            cookie_header = headers.get('cookie', '')
            user_info = self._get_authorized_user(cookie_header)

            if not user_info:
                self._send_login_redirect(client, request_line, headers)
                return

            # 3. Подключаемся к внутреннему приложению
            internal_port = get_assigned_port(self.name)
            if not internal_port:
                self._send_simple(client, 502, f"Приложение {self.name}: не назначен внутренний порт")
                return

            try:
                upstream = socket.create_connection(
                    ('127.0.0.1', int(internal_port)),
                    timeout=UPSTREAM_CONNECT_TIMEOUT,
                )
                upstream.settimeout(None)
            except Exception as e:
                self._send_simple(client, 502, f"Приложение недоступно: {e}")
                return

            # 4. Инжектируем заголовки идентификации и отправляем upstream
            buf = self._inject_identity_headers(buf, header_end, user_info)
            upstream.sendall(buf)

            # 5. Двунаправленный splice до закрытия
            self._splice(client, upstream)

        except Exception as e:
            log.warning(f"[forwarder] {self.name}: ошибка обработки {addr}: {e}")
        finally:
            for s in (client, upstream):
                if s is None:
                    continue
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            with self._stats_lock:
                if self.active_conns > 0:
                    self.active_conns -= 1

    # -------------------------------------------------------- authorization

    def _get_authorized_user(self, cookie_header):
        """Валидирует labmgr_session и возвращает dict пользователя или None."""
        if not cookie_header:
            return None
        try:
            with self.flask_app.test_request_context(
                path='/', headers={'Cookie': cookie_header}
            ):
                from flask import session as flask_session
                user_id = flask_session.get('user_id')
                if not user_id:
                    return None
                user = db.get_user_by_id(user_id)
                if not user:
                    return None
                if not db.user_can_access_app(user, self.name):
                    return None
                return user
        except Exception as e:
            log.debug(f"[forwarder] session check failed: {e}")
            return None

    def _inject_identity_headers(self, buf, header_end, user):
        """Вставляет X-Remote-User и X-Remote-Name перед \\r\\n\\r\\n."""
        username = user.get('username', '')
        full_name = user.get('full_name', '')
        extra = f"X-Remote-User: {username}\r\nX-Remote-Name: {full_name}\r\n"
        # Вставляем перед завершающим \r\n\r\n
        insert_pos = header_end - 2  # перед последним \r\n
        return buf[:insert_pos] + extra.encode('utf-8') + buf[insert_pos:]

    # -------------------------------------------------------------- helpers

    def _send_login_redirect(self, client, request_line, headers):
        """Отправляет 302 на страницу логина панели с next=<исходный URL>."""
        try:
            method, path, _ = request_line.split(' ', 2)
        except ValueError:
            path = '/'

        host_header = headers.get('host', '')
        # hostname без порта
        panel_host = host_header.split(':')[0] if host_header else ''
        panel_port = int(os.environ.get('PANEL_PORT', '80'))
        panel_base = f"http://{panel_host}"
        if panel_port != 80:
            panel_base += f":{panel_port}"

        # Исходный URL для next
        original = f"http://{host_header}{path}" if host_header else path
        login_url = f"{panel_base}/login?next={quote(original, safe='')}"

        body = (
            b"<html><body>Redirecting to login...<br>"
            b"<a href=\"" + login_url.encode('utf-8') + b"\">login</a></body></html>"
        )
        resp = (
            f"HTTP/1.1 302 Found\r\n"
            f"Location: {login_url}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode('iso-8859-1') + body
        try:
            client.sendall(resp)
        except Exception:
            pass

    def _send_simple(self, client, status, message):
        reason = {
            400: 'Bad Request', 403: 'Forbidden',
            404: 'Not Found', 502: 'Bad Gateway',
        }.get(status, 'Error')
        body = f"<h1>{status} {reason}</h1><p>{message}</p>".encode('utf-8')
        resp = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode('iso-8859-1') + body
        try:
            client.sendall(resp)
        except Exception:
            pass

    def _splice(self, a, b):
        """Двунаправленная передача байтов между сокетами до закрытия.

        Оба сокета остаются в blocking-режиме. select() определяет,
        кто готов к чтению; recv() на «готовом» сокете не блокирует;
        sendall() на blocking-сокете корректно дожидается отправки
        всех байтов (на non-blocking он бросает BlockingIOError).
        """
        a.settimeout(None)
        b.settimeout(None)
        socks = [a, b]
        while True:
            try:
                r, _, x = select.select(socks, [], socks, IDLE_TIMEOUT)
            except (OSError, ValueError):
                return
            if x or (not r):
                return
            for s in r:
                try:
                    data = s.recv(BUFFER_SIZE)
                except Exception:
                    return
                if not data:
                    return
                # Считаем трафик: a — клиент, b — upstream
                n = len(data)
                with self._stats_lock:
                    if s is a:
                        self.bytes_in += n
                    else:
                        self.bytes_out += n
                other = b if s is a else a
                try:
                    other.sendall(data)
                except Exception:
                    return


# ============================================================= реестр

_forwarders = {}              # name -> PortForwarder
_lock = threading.Lock()
_flask_app = None


def init(flask_app):
    """Вызывается при старте Host Manager. Поднимает все сохранённые форвардеры."""
    global _flask_app
    _flask_app = flask_app
    try:
        ports = db.list_external_ports()
    except Exception as e:
        log.warning(f"[forwarder] не удалось прочитать external_ports из БД: {e}")
        return

    for name, port in ports:
        try:
            _start_unlocked(name, int(port))
            print(f"[forwarder] восстановлен: {name} → 0.0.0.0:{port}")
        except Exception as e:
            print(f"[forwarder] ОШИБКА при запуске {name} на порту {port}: {e}")


def _start_unlocked(name, external_port):
    # Остановить старый, если был
    old = _forwarders.pop(name, None)
    if old:
        old.stop()

    fwd = PortForwarder(_flask_app, name, external_port)
    fwd.start()
    _forwarders[name] = fwd
    return fwd


def start_forwarder(name, external_port):
    """Запустить/перезапустить форвардер для приложения. Бросает OSError при занятом порте."""
    with _lock:
        return _start_unlocked(name, external_port)


def stop_forwarder(name):
    with _lock:
        fwd = _forwarders.pop(name, None)
        if fwd:
            fwd.stop()


def get_stats(name):
    """Статистика трафика для форвардера приложения. None если не запущен."""
    fwd = _forwarders.get(name)
    if not fwd:
        return None
    with fwd._stats_lock:
        return {
            "name": fwd.name,
            "external_port": fwd.external_port,
            "bytes_in": fwd.bytes_in,
            "bytes_out": fwd.bytes_out,
            "total_conns": fwd.total_conns,
            "active_conns": fwd.active_conns,
            "started_at": fwd.started_at,
            "uptime_sec": (int(time.time() - fwd.started_at)
                           if fwd.started_at else 0),
        }


def rebind_all():
    """
    Перезапускает все активные форвардеры на текущем bind-IP.
    Вызывается после смены интерфейса в настройках.
    Возвращает список (name, error) для тех, кого не удалось поднять.
    """
    errors = []
    with _lock:
        for name, fwd in list(_forwarders.items()):
            port = fwd.external_port
            try:
                _start_unlocked(name, port)
            except Exception as e:
                _forwarders.pop(name, None)
                errors.append((name, str(e)))
    return errors


def get_forwarder_status(name):
    """Возвращает (port, running) или (None, False)."""
    fwd = _forwarders.get(name)
    if fwd:
        return fwd.external_port, (fwd._thread is not None and fwd._thread.is_alive())
    return None, False
