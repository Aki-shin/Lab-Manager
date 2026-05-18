"""
Самообновление Lab Manager через git-репозиторий.

Логика:
    1. get_version_info()   — текущий commit/дата/ветка
    2. check_for_updates()  — git fetch + сравнение с origin/<branch>
    3. do_update()          — сохранить точку отката → git pull --ff-only →
                              pip install → отложенный перезапуск сервиса
    4. do_rollback()        — git reset --hard на сохранённый commit → перезапуск

Перезапуск выполняется через `systemd-run --on-active`, чтобы HTTP-ответ
успел дойти до браузера прежде, чем systemd убьёт текущий процесс панели.
"""
import os
import subprocess

from .config import BASE_DIR
from .services import _git_env

SERVICE_NAME = "lab-manager.service"
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python3")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")
LAST_GOOD_FILE = os.path.join(BASE_DIR, "data", ".last_good_commit")

RESTART_DELAY = "3s"  # задержка перед перезапуском сервиса


def is_git_repo():
    """Установлен ли Lab Manager из git-репозитория."""
    return os.path.isdir(os.path.join(BASE_DIR, ".git"))


def _git(args, timeout=60):
    """Запуск git в директории проекта. Возвращает CompletedProcess."""
    return subprocess.run(
        ["git", "-C", BASE_DIR] + args,
        capture_output=True, text=True, timeout=timeout, env=_git_env()
    )


def _current_branch():
    try:
        res = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        return res.stdout.strip() or "main"
    except Exception:
        return "main"


def get_version_info():
    """Текущая версия: short hash, дата, сообщение, ветка."""
    info = {
        "is_git": is_git_repo(),
        "commit": None,
        "commit_full": None,
        "date": None,
        "subject": None,
        "branch": None,
    }
    if not info["is_git"]:
        return info
    try:
        info["branch"] = _current_branch()
        res = _git(["log", "-1", "--format=%h%n%H%n%cI%n%s"])
        if res.returncode == 0:
            parts = res.stdout.strip().split("\n", 3)
            if len(parts) == 4:
                info["commit"], info["commit_full"], info["date"], info["subject"] = parts
    except Exception:
        pass
    return info


def check_for_updates():
    """
    git fetch + сравнение HEAD с origin/<branch>.
    Возвращает dict: {ok, up_to_date, behind, commits, error}.
    """
    result = {"ok": False, "up_to_date": False, "behind": 0,
              "commits": [], "error": None}
    if not is_git_repo():
        result["error"] = "Lab Manager установлен не из git-репозитория"
        return result

    branch = _current_branch()
    try:
        fetch = _git(["fetch", "origin", branch], timeout=120)
        if fetch.returncode != 0:
            result["error"] = f"git fetch failed: {fetch.stderr.strip()}"
            return result

        ref = f"origin/{branch}"
        count = _git(["rev-list", "--count", f"HEAD..{ref}"])
        if count.returncode != 0:
            result["error"] = f"git rev-list failed: {count.stderr.strip()}"
            return result

        behind = int(count.stdout.strip() or "0")
        result["ok"] = True
        result["behind"] = behind
        result["up_to_date"] = behind == 0

        if behind > 0:
            log = _git(["log", "--format=%h\x1f%cI\x1f%s", f"HEAD..{ref}"])
            if log.returncode == 0:
                for line in log.stdout.strip().split("\n"):
                    if "\x1f" not in line:
                        continue
                    h, date, subject = line.split("\x1f", 2)
                    result["commits"].append(
                        {"hash": h, "date": date, "subject": subject})
    except subprocess.TimeoutExpired:
        result["error"] = "Таймаут git fetch (>120с)"
    except Exception as e:
        result["error"] = f"Ошибка: {e}"
    return result


def _save_rollback_point():
    """Сохраняет текущий commit в файл — точку отката."""
    try:
        res = _git(["rev-parse", "HEAD"])
        if res.returncode == 0:
            commit = res.stdout.strip()
            os.makedirs(os.path.dirname(LAST_GOOD_FILE), exist_ok=True)
            with open(LAST_GOOD_FILE, "w") as f:
                f.write(commit)
            return commit
    except Exception:
        pass
    return None


def get_rollback_commit():
    """Сохранённая точка отката (предыдущая версия) или None."""
    try:
        with open(LAST_GOOD_FILE, "r") as f:
            commit = f.read().strip()
            return commit or None
    except Exception:
        return None


def _pip_install():
    """Устанавливает зависимости из requirements.txt в venv."""
    if not os.path.exists(REQUIREMENTS):
        return True, "requirements.txt не найден — пропуск"
    if not os.path.exists(VENV_PYTHON):
        return False, f"venv не найден ({VENV_PYTHON})"
    try:
        res = subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install",
             "--disable-pip-version-check", "--quiet", "-r", REQUIREMENTS],
            capture_output=True, text=True, timeout=600
        )
        if res.returncode != 0:
            err = (res.stderr.strip() or res.stdout.strip())[:400]
            return False, f"pip install упал: {err}"
        return True, "зависимости обновлены"
    except subprocess.TimeoutExpired:
        return False, "таймаут pip install (>10 мин)"
    except Exception as e:
        return False, f"ошибка pip: {e}"


def schedule_restart():
    """
    Отложенный перезапуск сервиса через systemd-run.
    Команда возвращается мгновенно — HTTP-ответ успевает дойти до браузера.
    """
    try:
        subprocess.run(
            ["systemd-run", f"--on-active={RESTART_DELAY}",
             "systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=10
        )
        return True
    except Exception:
        return False


def do_update():
    """
    Полное обновление: точка отката → git pull --ff-only → pip → перезапуск.
    Возвращает (ok, message). При ok=True сервис перезапустится через ~3с.
    """
    if not is_git_repo():
        return False, "Lab Manager установлен не из git-репозитория"

    rollback = _save_rollback_point()

    try:
        pull = _git(["pull", "--ff-only"], timeout=120)
    except subprocess.TimeoutExpired:
        return False, "Таймаут git pull (>120с)"
    except Exception as e:
        return False, f"Ошибка git pull: {e}"

    if pull.returncode != 0:
        err = (pull.stderr.strip() or pull.stdout.strip())
        return False, f"git pull failed: {err}"

    pip_ok, pip_msg = _pip_install()
    if not pip_ok:
        # Код обновился, но зависимости не встали — откат во избежание «кирпича»
        if rollback:
            try:
                _git(["reset", "--hard", rollback])
            except Exception:
                pass
        return False, f"Обновление отменено ({pip_msg}). Выполнен откат кода."

    schedule_restart()
    return True, (f"Код обновлён, {pip_msg}. "
                  f"Панель перезапустится через ~3 секунды.")


def do_rollback():
    """Откат к сохранённой предыдущей версии + перезапуск."""
    if not is_git_repo():
        return False, "Lab Manager установлен не из git-репозитория"

    commit = get_rollback_commit()
    if not commit:
        return False, "Нет сохранённой точки отката"

    try:
        res = _git(["reset", "--hard", commit])
    except Exception as e:
        return False, f"Ошибка git reset: {e}"

    if res.returncode != 0:
        return False, f"git reset failed: {res.stderr.strip()}"

    _pip_install()
    schedule_restart()
    return True, (f"Выполнен откат к {commit[:7]}. "
                  f"Панель перезапустится через ~3 секунды.")
