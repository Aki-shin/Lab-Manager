"""Декораторы авторизации и доступ к текущему пользователю."""
import functools
from flask import session, redirect, url_for, request, abort
from . import db


def current_user():
    """Возвращает dict текущего пользователя или None."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    user = db.get_user_by_id(user_id)
    if not user:
        # Пользователь удалён — чистим сессию
        session.clear()
        return None
    return user


def login_required(f):
    """Требует авторизации (любой пользователь)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for('main.login', next=request.url))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Требует прав администратора."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for('main.login', next=request.url))
        if not user.get('is_admin'):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def require_app_access(app_name):
    """Проверка прав на конкретное приложение (для /proxy)."""
    user = current_user()
    if not user:
        return None, redirect(url_for('main.login', next=request.url))
    if not db.user_can_access_app(user, app_name):
        abort(403)
    return user, None
