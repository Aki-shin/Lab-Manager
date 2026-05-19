"""Управление tmux-сессиями на хосте."""
import re
import shutil
import datetime
import subprocess

# Имя сессии: tmux не любит точки и двоеточия — ограничиваемся безопасным набором
_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


def is_available():
    """Установлен ли tmux."""
    return shutil.which('tmux') is not None


def valid_name(name):
    return bool(name) and bool(_NAME_RE.match(name))


def _tmux(args, timeout=10):
    return subprocess.run(
        ['tmux'] + args, capture_output=True, text=True, timeout=timeout
    )


def list_sessions():
    """Список tmux-сессий. Пустой список, если tmux-сервер не запущен."""
    if not is_available():
        return []
    fmt = ('#{session_name}\x1f#{session_windows}\x1f'
           '#{session_created}\x1f#{session_attached}')
    try:
        res = _tmux(['list-sessions', '-F', fmt])
    except Exception:
        return []
    if res.returncode != 0:
        return []  # «no server running» — сессий нет
    sessions = []
    for line in res.stdout.strip().split('\n'):
        if '\x1f' not in line:
            continue
        parts = line.split('\x1f')
        if len(parts) < 4:
            continue
        name, windows, created, attached = parts[:4]
        created_str = created
        try:
            created_str = datetime.datetime.fromtimestamp(
                int(created)).strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError):
            pass
        sessions.append({
            'name': name,
            'windows': windows,
            'created': created,
            'created_str': created_str,
            'attached': attached not in ('', '0'),
        })
    return sessions


def session_exists(name):
    if not is_available() or not valid_name(name):
        return False
    try:
        res = _tmux(['has-session', '-t', '=' + name])
        return res.returncode == 0
    except Exception:
        return False


def _new_session_cmd(name):
    """
    Команда создания сессии.

    tmux-сервер запускается в отдельном systemd-scope, чтобы он НЕ попал
    в cgroup сервиса host-manager. Иначе при перезапуске/обновлении панели
    systemd убивает весь её cgroup — вместе с tmux-сервером и всеми
    сессиями. Scope живёт независимо, поэтому сессии переживают рестарт.
    """
    base = ['tmux', 'new-session', '-d', '-s', name]
    if shutil.which('systemd-run'):
        return ['systemd-run', '--scope', '--quiet', '--collect'] + base
    return base


def create_session(name):
    """Создаёт detached tmux-сессию. Возвращает (ok, message)."""
    if not is_available():
        return False, 'tmux не установлен на сервере'
    if not valid_name(name):
        return False, 'Недопустимое имя (A-Z, a-z, 0-9, _, -, до 32 символов)'
    if session_exists(name):
        return False, f'Сессия «{name}» уже существует'
    try:
        res = subprocess.run(_new_session_cmd(name),
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            # Запасной путь: создать без systemd-run
            res = _tmux(['new-session', '-d', '-s', name])
    except Exception as e:
        return False, f'Ошибка: {e}'
    if res.returncode != 0:
        return False, res.stderr.strip() or 'не удалось создать сессию'
    return True, f'Сессия «{name}» создана'


def kill_session(name):
    """Завершает tmux-сессию. Возвращает (ok, message)."""
    if not is_available():
        return False, 'tmux не установлен'
    if not valid_name(name):
        return False, 'Недопустимое имя сессии'
    if not session_exists(name):
        return False, f'Сессия «{name}» не найдена'
    try:
        res = _tmux(['kill-session', '-t', '=' + name])
    except Exception as e:
        return False, f'Ошибка: {e}'
    if res.returncode != 0:
        return False, res.stderr.strip() or 'не удалось завершить сессию'
    return True, f'Сессия «{name}» завершена'


def rename_session(old, new):
    """Переименовывает tmux-сессию. Возвращает (ok, message)."""
    if not is_available():
        return False, 'tmux не установлен'
    if not valid_name(old) or not session_exists(old):
        return False, f'Сессия «{old}» не найдена'
    if not valid_name(new):
        return False, 'Недопустимое новое имя'
    if session_exists(new):
        return False, f'Сессия «{new}» уже существует'
    try:
        res = _tmux(['rename-session', '-t', '=' + old, new])
    except Exception as e:
        return False, f'Ошибка: {e}'
    if res.returncode != 0:
        return False, res.stderr.strip() or 'не удалось переименовать'
    return True, f'Сессия переименована: {old} → {new}'
