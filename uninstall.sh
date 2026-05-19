#!/usr/bin/env bash
# ============================================
#  Host Manager — удаление (Debian 12, root)
# ============================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# Жёсткая остановка systemd-юнита: stop -> ждём -> kill main pid -> освобождение порта
hard_stop_unit() {
    local unit="$1"

    # Получаем данные ДО остановки
    local main_pid
    main_pid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null || echo 0)"
    local port
    port="$(systemctl show -p Environment --value "$unit" 2>/dev/null \
            | tr ' ' '\n' | grep '^PORT=' | cut -d= -f2 || true)"

    # 1. Graceful stop
    systemctl stop "$unit" 2>/dev/null || true

    # 2. Ждём до 5 секунд
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if [ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ]; then
            break
        fi
        sleep 0.5
    done

    # 3. Принудительно убиваем дерево по MainPID, если процесс жив
    if [ -n "${main_pid:-}" ] && [ "$main_pid" != "0" ] && kill -0 "$main_pid" 2>/dev/null; then
        warn "Процесс $main_pid ещё жив — принудительно SIGKILL дерева"
        pkill -KILL -P "$main_pid" 2>/dev/null || true
        kill -KILL "$main_pid" 2>/dev/null || true
    fi

    # 4. Safety net: освобождаем порт, если его кто-то держит
    if [ -n "${port:-}" ] && command -v ss >/dev/null 2>&1; then
        local pids_on_port
        pids_on_port="$(ss -ltnp "sport = :${port}" 2>/dev/null \
                        | grep -oP 'pid=\K[0-9]+' | sort -u || true)"
        if [ -n "$pids_on_port" ]; then
            warn "Порт ${port} занят PID(s): $pids_on_port — убиваю"
            for p in $pids_on_port; do
                kill -KILL "$p" 2>/dev/null || true
            done
        fi
    fi

    systemctl reset-failed "$unit" 2>/dev/null || true
}

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root:  sudo ./uninstall.sh"
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="host-manager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${PROJECT_DIR}/.env"
VENV_DIR="${PROJECT_DIR}/venv"
APPS_DIR=$(grep -oP '(?<=APPS_DIR=).*' "$ENV_FILE" 2>/dev/null || echo "/root/apps")
SERVICE_PREFIX="labapp-"

echo ""
echo -e "${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${RED}║     Host Manager — Удаление          ║${NC}"
echo -e "${RED}╚══════════════════════════════════════╝${NC}"
echo ""

# --- 1. Остановка Host Manager ---
# Чистим и текущее имя сервиса, и старое (lab-manager) при наличии
PANEL_FOUND=0
for svc in "$SERVICE_NAME" "lab-manager"; do
    svc_file="/etc/systemd/system/${svc}.service"
    if [ -f "$svc_file" ]; then
        PANEL_FOUND=1
        info "Останавливаю сервис ${svc}..."
        hard_stop_unit "${svc}.service"
        systemctl disable "${svc}.service" 2>/dev/null || true
        rm -f "$svc_file"
        systemctl daemon-reload
        ok "Сервис ${svc} остановлен и удалён."
    fi
done
if [ "$PANEL_FOUND" -eq 0 ]; then
    warn "Сервис панели не найден — пропускаю."
fi

# --- 2. Управляемые приложения ---
MANAGED_SERVICES=()
for svc_file in /etc/systemd/system/${SERVICE_PREFIX}*.service; do
    [ -f "$svc_file" ] || continue
    svc_name="$(basename "$svc_file")"
    app_name="${svc_name#${SERVICE_PREFIX}}"
    app_name="${app_name%.service}"
    MANAGED_SERVICES+=("$app_name")
done

if [ ${#MANAGED_SERVICES[@]} -gt 0 ]; then
    echo ""
    warn "Найдены сервисы управляемых приложений:"
    for svc in "${MANAGED_SERVICES[@]}"; do
        echo "    - ${svc}"
    done
    echo ""
    read -p "  Удалить их systemd-сервисы? (код в ${APPS_DIR} останется) [y/N]: " REMOVE_APP_SERVICES
    if [[ "${REMOVE_APP_SERVICES,,}" == "y" ]]; then
        for svc in "${MANAGED_SERVICES[@]}"; do
            unit="${SERVICE_PREFIX}${svc}.service"
            info "  → ${svc}"
            hard_stop_unit "$unit"
            systemctl disable "$unit" 2>/dev/null || true
            rm -f "/etc/systemd/system/${unit}"
        done
        systemctl daemon-reload
        ok "Сервисы приложений удалены (процессы добиты)."
    else
        ok "Сервисы приложений оставлены."
    fi
fi

# --- 3. Удаление venv ---
if [ -d "$VENV_DIR" ]; then
    echo ""
    read -p "  Удалить виртуальное окружение (venv)? [Y/n]: " REMOVE_VENV
    REMOVE_VENV="${REMOVE_VENV:-y}"
    if [[ "${REMOVE_VENV,,}" == "y" ]]; then
        rm -rf "$VENV_DIR"
        ok "venv удалён."
    else
        ok "venv оставлен."
    fi
fi

# --- 4. Удаление .env ---
if [ -f "$ENV_FILE" ]; then
    echo ""
    read -p "  Удалить .env (хеш пароля и секреты)? [Y/n]: " REMOVE_ENV
    REMOVE_ENV="${REMOVE_ENV:-y}"
    if [[ "${REMOVE_ENV,,}" == "y" ]]; then
        rm -f "$ENV_FILE"
        ok ".env удалён."
    else
        ok ".env оставлен."
    fi
fi

# --- 5. Папка приложений ---
if [ -d "$APPS_DIR" ]; then
    APP_COUNT=$(find "$APPS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    if [ "$APP_COUNT" -gt 0 ]; then
        warn "Папка ${APPS_DIR} содержит ${APP_COUNT} приложений — не трогаю."
        echo "    Удалите вручную: rm -rf ${APPS_DIR}"
    fi
fi

# --- 6. Файлы проекта ---
echo ""
read -p "  Удалить файлы проекта (${PROJECT_DIR})? [y/N]: " REMOVE_PROJECT
if [[ "${REMOVE_PROJECT,,}" == "y" ]]; then
    cd /root
    rm -rf "$PROJECT_DIR"
    ok "Файлы проекта удалены."
else
    ok "Файлы проекта оставлены."
fi

# --- Итог ---
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     Удаление завершено.              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
