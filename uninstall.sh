#!/usr/bin/env bash
# ============================================
#  Lab Manager — удаление (Debian 12, root)
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

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root:  sudo ./uninstall.sh"
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="lab-manager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${PROJECT_DIR}/.env"
VENV_DIR="${PROJECT_DIR}/venv"
APPS_DIR=$(grep -oP '(?<=APPS_DIR=).*' "$ENV_FILE" 2>/dev/null || echo "/root/apps")
SERVICE_PREFIX="labapp-"

echo ""
echo -e "${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${RED}║     Lab Manager — Удаление           ║${NC}"
echo -e "${RED}╚══════════════════════════════════════╝${NC}"
echo ""

# --- 1. Остановка Lab Manager ---
if [ -f "$SERVICE_FILE" ]; then
    info "Останавливаю сервис ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    ok "Сервис ${SERVICE_NAME} остановлен и удалён."
else
    warn "Сервис ${SERVICE_NAME} не найден — пропускаю."
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
            systemctl stop "${SERVICE_PREFIX}${svc}.service" 2>/dev/null || true
            systemctl disable "${SERVICE_PREFIX}${svc}.service" 2>/dev/null || true
            rm -f "/etc/systemd/system/${SERVICE_PREFIX}${svc}.service"
        done
        systemctl daemon-reload
        ok "Сервисы приложений удалены."
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
