"""
Фоновая периодическая проверка обновлений Host Manager и приложений.

Раз в UPDATE_CHECK_HOURS часов (по умолчанию 6) делает `git fetch` для самой
панели и для каждого git-приложения, сохраняя результат в памяти. UI читает
кэш мгновенно — без сетевых задержек. UPDATE_CHECK_HOURS=0 отключает автопроверку.
"""
import os
import time
import threading
import datetime
import logging

log = logging.getLogger(__name__)

_state = {
    "self": None,        # {update_available, behind, commit, error}
    "apps": {},          # name -> {is_git, commit, branch, update_available, error}
    "checked_at": None,  # ISO-строка времени последней проверки
    "running": False,    # идёт ли проверка прямо сейчас
}
_lock = threading.Lock()
_thread = None


def _interval_seconds():
    try:
        hours = float(os.environ.get("UPDATE_CHECK_HOURS", "6"))
    except ValueError:
        hours = 6
    return int(hours * 3600)


def run_check_all():
    """Однократная синхронная проверка: Host Manager + все git-приложения."""
    from . import self_update
    from .config import Config
    from .services import check_app_updates

    with _lock:
        if _state["running"]:
            return
        _state["running"] = True

    try:
        # --- Host Manager ---
        self_result = {"update_available": False, "behind": 0,
                       "commit": None, "error": None}
        try:
            ver = self_update.get_version_info()
            self_result["commit"] = ver.get("commit")
            if self_update.is_git_repo():
                upd = self_update.check_for_updates()
                if upd.get("error"):
                    self_result["error"] = upd["error"]
                else:
                    behind = upd.get("behind", 0)
                    self_result["behind"] = behind
                    self_result["update_available"] = behind > 0
            else:
                self_result["error"] = "установлен не из git-репозитория"
        except Exception as e:
            self_result["error"] = str(e)[:200]

        # --- Приложения ---
        apps = {}
        if os.path.isdir(Config.APPS_DIR):
            for item in sorted(os.listdir(Config.APPS_DIR)):
                app_path = os.path.join(Config.APPS_DIR, item)
                if not os.path.isdir(app_path):
                    continue
                if not os.path.isdir(os.path.join(app_path, ".git")):
                    continue
                try:
                    apps[item] = check_app_updates(item)
                except Exception as e:
                    apps[item] = {"is_git": True, "commit": None, "branch": None,
                                  "update_available": False, "error": str(e)[:200]}

        with _lock:
            _state["self"] = self_result
            _state["apps"] = apps
            _state["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        log.info("[update-checker] проверка завершена")
    finally:
        with _lock:
            _state["running"] = False


def get_state():
    """Снимок состояния проверки (копия — безопасно отдавать в шаблон)."""
    with _lock:
        return {
            "self": dict(_state["self"]) if _state["self"] else None,
            "apps": {k: dict(v) for k, v in _state["apps"].items()},
            "checked_at": _state["checked_at"],
            "running": _state["running"],
        }


def mark_self_up_to_date(commit):
    """Сбрасывает кэш состояния панели после успешного самообновления —
    чтобы баннер «доступно обновление» исчезал сразу, не дожидаясь
    очередной фоновой проверки (раз в UPDATE_CHECK_HOURS)."""
    with _lock:
        _state["self"] = {
            "update_available": False,
            "behind": 0,
            "commit": commit,
            "error": None,
        }
        _state["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")


def mark_app_up_to_date(name, commit):
    """То же для приложения после успешного обновления."""
    with _lock:
        _state["apps"][name] = {
            "is_git": True,
            "commit": (commit[:7] if commit else None),
            "branch": _state["apps"].get(name, {}).get("branch"),
            "update_available": False,
            "commits": [],
            "error": None,
        }
        _state["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")


def get_app_update(name):
    """Кэшированный статус обновлений конкретного приложения или None."""
    with _lock:
        entry = _state["apps"].get(name)
        return dict(entry) if entry else None


def trigger_check_now():
    """Запускает проверку в фоновом потоке (для кнопки в UI)."""
    threading.Thread(
        target=run_check_all, name="update-check-manual", daemon=True
    ).start()


def _loop():
    time.sleep(30)  # не нагружаем момент старта панели
    while True:
        try:
            run_check_all()
        except Exception as e:
            log.warning(f"[update-checker] ошибка проверки: {e}")
        time.sleep(_interval_seconds())


def init(flask_app):
    """Запуск фоновой автопроверки. UPDATE_CHECK_HOURS=0 — отключить."""
    global _thread
    interval = _interval_seconds()
    if interval <= 0:
        log.info("[update-checker] автопроверка отключена (UPDATE_CHECK_HOURS=0)")
        return
    _thread = threading.Thread(target=_loop, name="update-checker", daemon=True)
    _thread.start()
    print(f"[update-checker] автопроверка обновлений включена "
          f"(каждые {interval // 3600} ч)")
