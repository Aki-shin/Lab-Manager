#!/usr/bin/env python3
"""
Сторожевой скрипт самообновления Lab Manager.

Запускается отдельным транзиентным юнитом (`systemd-run`) через ~15 секунд
после применения обновления. Проверяет, поднялась ли панель; если нет —
откатывает код к предыдущей версии и сохраняет отчёт с журналом ошибки.

ВАЖНО: скрипт намеренно использует только стандартную библиотеку и НЕ
импортирует пакет `app` — обновление могло сломать код панели, и watchdog
обязан работать независимо от него.
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
PENDING_FILE = os.path.join(DATA_DIR, ".update_pending")
FAILED_FILE = os.path.join(DATA_DIR, ".update_failed")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python3")
SERVICE_NAME = "lab-manager.service"

# Параметры проверки здоровья
HEALTH_ATTEMPTS = 15        # сколько раз опрашивать (×3с ≈ 45с)
HEALTH_INTERVAL = 3         # секунд между опросами
HEALTH_NEEDED = 3           # подряд успешных ответов = панель здорова
HEALTH_TIMEOUT = 3          # таймаут одного HTTP-запроса


def log(msg):
    """Пишет в stdout — systemd-run перенаправит это в journal."""
    print(f"[update-watchdog] {msg}", flush=True)


def _git_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault(
        "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    return env


def _git(args, timeout=120):
    return subprocess.run(
        ["git", "-C", BASE_DIR] + args,
        capture_output=True, text=True, timeout=timeout, env=_git_env(),
    )


def journal_tail(lines=120):
    """Хвост журнала сервиса — содержит traceback упавшей панели."""
    try:
        res = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
        return res.stdout or res.stderr or "(журнал пуст)"
    except Exception as e:
        return f"(не удалось прочитать журнал: {e})"


def panel_is_healthy(port):
    """
    Опрашивает /login панели. Требует HEALTH_NEEDED успешных ответов подряд —
    это отсеивает crash-loop (панель кратко поднимается и снова падает).
    """
    consecutive = 0
    for attempt in range(1, HEALTH_ATTEMPTS + 1):
        ok = False
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/login", method="GET"
            )
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                ok = resp.status == 200
        except Exception:
            ok = False

        consecutive = consecutive + 1 if ok else 0
        log(f"проверка {attempt}/{HEALTH_ATTEMPTS}: "
            f"{'OK' if ok else 'нет ответа'} (подряд {consecutive})")
        if consecutive >= HEALTH_NEEDED:
            return True
        time.sleep(HEALTH_INTERVAL)
    return False


def pip_install():
    if not (os.path.exists(VENV_PYTHON) and os.path.exists(REQUIREMENTS)):
        return
    try:
        subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install",
             "--disable-pip-version-check", "--quiet", "-r", REQUIREMENTS],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as e:
        log(f"pip install при откате упал: {e}")


def restart_service():
    try:
        subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log(f"не удалось перезапустить сервис: {e}")


def write_failed_report(info, rolled_back_to, logs):
    report = {
        "failed_commit": info.get("new_commit"),
        "rolled_back_to": rolled_back_to,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "logs": logs[-8000:],
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(FAILED_FILE, "w") as f:
            json.dump(report, f)
    except Exception as e:
        log(f"не удалось записать отчёт: {e}")


def remove_pending():
    try:
        os.remove(PENDING_FILE)
    except Exception:
        pass


def main():
    if not os.path.exists(PENDING_FILE):
        log("маркер обновления отсутствует — нечего проверять")
        return

    try:
        with open(PENDING_FILE) as f:
            info = json.load(f)
    except Exception as e:
        log(f"маркер повреждён ({e}) — удаляю")
        remove_pending()
        return

    port = info.get("panel_port", 80)
    log(f"проверяю панель на 127.0.0.1:{port} после обновления "
        f"{info.get('new_commit', '?')[:7]}")

    if panel_is_healthy(port):
        log("панель работает штатно — обновление подтверждено")
        remove_pending()
        return

    # --- Панель не поднялась: откат ---
    log("панель НЕ отвечает — выполняю автооткат")
    logs = journal_tail()
    rollback = info.get("rollback_commit")
    rolled_back_to = None

    if rollback:
        res = _git(["reset", "--hard", rollback])
        if res.returncode == 0:
            rolled_back_to = rollback
            log(f"код откачен к {rollback[:7]}")
            pip_install()
        else:
            log(f"git reset не удался: {res.stderr.strip()}")
    else:
        log("точка отката неизвестна — откат кода невозможен")

    write_failed_report(info, rolled_back_to, logs)
    remove_pending()
    restart_service()
    log("откат завершён, сервис перезапущен")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"критическая ошибка watchdog: {e}")
        sys.exit(1)
