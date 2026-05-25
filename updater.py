#!/usr/bin/env python3
"""
Standalone updater для Host Manager.

Запускается отдельным транзиентным systemd-юнитом
(`systemd-run --collect --unit=host-manager-updater`) — то есть **в собственном
процессе, независимом от панели**. Это устраняет всю прежнюю боль:
панель убивается чисто, апдейтер сам её поднимает и проверяет.

Шаги: git pull → pip install → systemctl restart → healthcheck → при
неудаче автооткат (git reset + pip + restart). Прогресс пишется в
`data/.update_status.json`, чтобы страница прогресса в UI могла его
поллить и показывать пользователю.

ВАЖНО: используется только стандартная библиотека, никаких импортов
из пакета `app` (тот может быть сломан текущим обновлением).
"""
import os
import sys
import json
import time
import datetime
import subprocess
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATUS_FILE = os.path.join(DATA_DIR, ".update_status.json")
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python3")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")


def _detect_service_name():
    for n in ("host-manager.service", "lab-manager.service"):
        if os.path.exists(os.path.join("/etc/systemd/system", n)):
            return n
    return "host-manager.service"


SERVICE = _detect_service_name()
PANEL_PORT = int(os.environ.get("MANAGER_PORT", "80"))
PANEL_URL = f"http://127.0.0.1:{PANEL_PORT}/login"

# Возможные фазы (понимает прогресс-страница):
#   starting / pulling / installing / restarting / healthcheck / rollback
#   done (ok=True)  /  failed (ok=False)


def write_status(phase, message="", ok=None, extra=None):
    data = {
        "phase": phase,
        "message": message,
        "ok": ok,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        data.update(extra)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_FILE)  # атомарная запись
    except Exception:
        pass


def _git_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault(
        "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    return env


def git(args, timeout=120):
    return subprocess.run(
        ["git", "-C", BASE_DIR] + args,
        capture_output=True, text=True, timeout=timeout, env=_git_env(),
    )


def pip_install():
    if not (os.path.exists(REQUIREMENTS) and os.path.exists(VENV_PYTHON)):
        return True, ""
    res = subprocess.run(
        [VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check",
         "--quiet", "-r", REQUIREMENTS],
        capture_output=True, text=True, timeout=600,
    )
    if res.returncode != 0:
        return False, (res.stderr.strip() or res.stdout.strip())[:400]
    return True, ""


def healthy(total=60):
    """Опрос /login до total секунд. Нужно 3 успешных ответа подряд."""
    deadline = time.time() + total
    consec = 0
    while time.time() < deadline:
        ok = False
        try:
            with urllib.request.urlopen(PANEL_URL, timeout=3) as r:
                ok = (r.status == 200)
        except Exception:
            ok = False
        consec = consec + 1 if ok else 0
        if consec >= 3:
            return True
        time.sleep(2)
    return False


def restart_panel():
    try:
        subprocess.run(["systemctl", "restart", SERVICE],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def main():
    write_status("starting", "Запуск апдейтера")

    rb = git(["rev-parse", "HEAD"]).stdout.strip()
    write_status("starting", f"Текущая версия: {rb[:7] if rb else '?'}")

    # 1. git pull
    write_status("pulling", "git pull --ff-only")
    pull = git(["pull", "--ff-only"])
    if pull.returncode != 0:
        err = (pull.stderr.strip() or pull.stdout.strip())
        write_status("failed", f"git pull: {err}", ok=False)
        return

    new = git(["rev-parse", "HEAD"]).stdout.strip()
    if new == rb:
        write_status("done", "Обновлений нет — установлена последняя версия",
                     ok=True, extra={"commit": new[:7] if new else None})
        return

    # 2. pip install
    write_status("installing", "Установка зависимостей (pip)")
    ok, msg = pip_install()
    if not ok:
        git(["reset", "--hard", rb])
        write_status("failed",
                     f"pip install упал: {msg}. Код откачен к {rb[:7]}.",
                     ok=False)
        return

    # 3. Перезапуск панели
    write_status("restarting", "Перезапуск сервиса панели",
                 extra={"new_commit": new[:7]})
    restart_panel()

    # 4. Healthcheck
    write_status("healthcheck", "Проверка работоспособности (до 60 сек)",
                 extra={"new_commit": new[:7]})
    if healthy():
        write_status("done", f"Обновлено до {new[:7]} — панель работает",
                     ok=True, extra={"commit": new[:7]})
        return

    # 5. Автооткат
    write_status("rollback", "Панель не отвечает — выполняю автооткат",
                 extra={"failed_commit": new[:7]})
    git(["reset", "--hard", rb])
    pip_install()
    restart_panel()
    if healthy(total=30):
        write_status("failed",
                     f"Обновление до {new[:7]} сломало панель — откатили к {rb[:7]}",
                     ok=False,
                     extra={"failed_commit": new[:7], "rolled_back_to": rb[:7]})
    else:
        write_status("failed",
                     f"Обновление до {new[:7]} сломало панель, и откат не помог — "
                     f"требуется ручное вмешательство по SSH",
                     ok=False,
                     extra={"failed_commit": new[:7], "rolled_back_to": rb[:7]})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_status("failed", f"Критическая ошибка апдейтера: {e}", ok=False)
        sys.exit(1)
