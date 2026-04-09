"""SQLite-хранилище пользователей и прав доступа."""
import os
import sqlite3
from contextlib import contextmanager
from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permissions (
    user_id INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    PRIMARY KEY (user_id, app_name),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS app_settings (
    app_name TEXT PRIMARY KEY,
    external_port INTEGER
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Добавляет колонки, которых нет в старых версиях БД."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if 'full_name' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")


def init_db():
    """Создаёт таблицы и bootstrap-админа при первом запуске."""
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    bootstrap_admin()


def bootstrap_admin():
    """Если в БД нет пользователей — создаём admin из ADMIN_PASSWORD env."""
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0 and Config.ADMIN_PASSWORD:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                ("admin", Config.ADMIN_PASSWORD)
            )
            print("[*] DB: создан начальный admin из ADMIN_PASSWORD")


# --- CRUD пользователей ---

def get_user_by_username(username):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, full_name, is_admin, created_at FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def create_user(username, password_hash, is_admin=False, full_name=''):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, full_name) VALUES (?, ?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0, full_name)
        )
        return cur.lastrowid


def update_user(user_id, username=None, password_hash=None, is_admin=None, full_name=None):
    fields, values = [], []
    if username is not None:
        fields.append("username = ?")
        values.append(username)
    if password_hash is not None:
        fields.append("password_hash = ?")
        values.append(password_hash)
    if is_admin is not None:
        fields.append("is_admin = ?")
        values.append(1 if is_admin else 0)
    if full_name is not None:
        fields.append("full_name = ?")
        values.append(full_name)
    if not fields:
        return
    values.append(user_id)
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)


def delete_user(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


# --- Права доступа к приложениям ---

def get_user_permissions(user_id):
    """Список имён приложений, доступных пользователю."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_name FROM permissions WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [r['app_name'] for r in rows]


def set_user_permissions(user_id, app_names):
    """Полностью заменяет список прав пользователя."""
    with get_db() as conn:
        conn.execute("DELETE FROM permissions WHERE user_id = ?", (user_id,))
        for app_name in app_names:
            if app_name:
                conn.execute(
                    "INSERT OR IGNORE INTO permissions (user_id, app_name) VALUES (?, ?)",
                    (user_id, app_name)
                )


def delete_app_permissions(app_name):
    """Удаляет все права, связанные с приложением (при удалении приложения)."""
    with get_db() as conn:
        conn.execute("DELETE FROM permissions WHERE app_name = ?", (app_name,))


# --- Настройки приложений (внешний порт) ---

def get_external_port(app_name):
    with get_db() as conn:
        row = conn.execute(
            "SELECT external_port FROM app_settings WHERE app_name = ?", (app_name,)
        ).fetchone()
        return row['external_port'] if row else None


def set_external_port(app_name, port):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO app_settings (app_name, external_port) VALUES (?, ?) "
            "ON CONFLICT(app_name) DO UPDATE SET external_port = excluded.external_port",
            (app_name, int(port))
        )


def clear_external_port(app_name):
    with get_db() as conn:
        conn.execute(
            "UPDATE app_settings SET external_port = NULL WHERE app_name = ?",
            (app_name,)
        )


def list_external_ports():
    """Список (app_name, port) всех приложений с настроенным внешним портом."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_name, external_port FROM app_settings WHERE external_port IS NOT NULL"
        ).fetchall()
        return [(r['app_name'], r['external_port']) for r in rows]


def delete_app_settings(app_name):
    """Полная очистка настроек приложения (при удалении сервиса)."""
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE app_name = ?", (app_name,))


def user_can_access_app(user, app_name):
    """Admin — всегда да; обычный пользователь — проверяем права."""
    if not user:
        return False
    if user.get('is_admin'):
        return True
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM permissions WHERE user_id = ? AND app_name = ?",
            (user['id'], app_name)
        ).fetchone()
        return row is not None
