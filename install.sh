#!/usr/bin/env bash
# ============================================
#  Lab Manager — установка (Debian 12, root)
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
    err "Запустите от root:  sudo ./install.sh"
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="lab-manager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${PROJECT_DIR}/.env"
VENV_DIR="${PROJECT_DIR}/venv"
APPS_DIR="/root/apps"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     Lab Manager — Установка          ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""

# --- 1. Системные зависимости ---
info "Проверяю системные зависимости..."

PACKAGES_TO_INSTALL=()
for pkg in python3 python3-venv python3-pip git; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        PACKAGES_TO_INSTALL+=("$pkg")
    fi
done

if [ ${#PACKAGES_TO_INSTALL[@]} -gt 0 ]; then
    info "Устанавливаю: ${PACKAGES_TO_INSTALL[*]}"
    apt-get update -qq
    apt-get install -y -qq "${PACKAGES_TO_INSTALL[@]}"
    ok "Системные пакеты установлены."
else
    ok "Системные зависимости уже установлены."
fi

# --- 2. Python venv ---
# Проверяем валидность существующего venv (файлы в bin/ должны быть исполняемыми)
VENV_VALID=0
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python3" ] && [ -x "$VENV_DIR/bin/pip" ]; then
    VENV_VALID=1
fi

if [ "$VENV_VALID" -eq 0 ]; then
    if [ -d "$VENV_DIR" ]; then
        warn "venv существует, но повреждён (нет exec-прав). Пересоздаю..."
        rm -rf "$VENV_DIR"
    else
        info "Создаю виртуальное окружение..."
    fi
    python3 -m venv "$VENV_DIR"
    ok "venv создан."
else
    ok "venv уже существует и валиден."
fi

# На всякий случай проставляем exec-бит для всех файлов в bin/
chmod +x "$VENV_DIR"/bin/* 2>/dev/null || true

info "Устанавливаю pip-зависимости..."
"$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python3" -m pip install --quiet -r "${PROJECT_DIR}/requirements.txt"
ok "Python-зависимости установлены."

# --- 3. Файл .env ---
if [ -f "$ENV_FILE" ]; then
    warn "Файл .env уже существует — пропускаю настройку."
    warn "Для пересоздания: удалите .env и запустите снова."
else
    echo ""
    info "Настройка пароля администратора."
    echo ""

    while true; do
        read -s -p "  Введите пароль администратора: " ADMIN_PASS
        echo ""
        read -s -p "  Повторите пароль: " ADMIN_PASS2
        echo ""

        if [ -z "$ADMIN_PASS" ]; then
            echo -e "  ${RED}Пароль не может быть пустым.${NC}"
            continue
        fi
        if [ "$ADMIN_PASS" != "$ADMIN_PASS2" ]; then
            echo -e "  ${RED}Пароли не совпадают.${NC}"
            continue
        fi
        break
    done

    PASS_HASH=$("$VENV_DIR/bin/python3" -c "
from werkzeug.security import generate_password_hash
import sys
print(generate_password_hash(sys.argv[1]))
" "$ADMIN_PASS")

    SECRET_KEY=$("$VENV_DIR/bin/python3" -c "import secrets; print(secrets.token_hex(32))")

    echo ""
    read -p "  Порт менеджера [80]: " MANAGER_PORT
    MANAGER_PORT="${MANAGER_PORT:-80}"

    cat > "$ENV_FILE" <<EOF
ADMIN_PASSWORD=${PASS_HASH}
SECRET_KEY=${SECRET_KEY}
MANAGER_PORT=${MANAGER_PORT}
APPS_DIR=${APPS_DIR}
EOF

    chmod 600 "$ENV_FILE"
    ok "Файл .env создан (права 600)."
fi

MANAGER_PORT=$(grep -oP '(?<=MANAGER_PORT=).*' "$ENV_FILE" 2>/dev/null || echo "80")

# --- 4. Директория приложений и БД ---
mkdir -p "$APPS_DIR"
ok "Директория ${APPS_DIR} готова."

mkdir -p "${PROJECT_DIR}/data"
chmod 700 "${PROJECT_DIR}/data"
ok "Директория для БД (${PROJECT_DIR}/data) готова."

# --- 5. Systemd service ---
info "Создаю systemd service..."

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Lab Manager
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python3 ${PROJECT_DIR}/manager.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=10
SendSIGKILL=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl start "${SERVICE_NAME}.service"

ok "Сервис ${SERVICE_NAME} запущен и добавлен в автозагрузку."

# --- 6. Итог ---
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     Установка завершена!             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
LOCAL_IP="${LOCAL_IP:-localhost}"

echo -e "  Панель управления: ${CYAN}http://${LOCAL_IP}:${MANAGER_PORT}${NC}"
echo -e "  Папка приложений:  ${CYAN}${APPS_DIR}${NC}"
echo ""
echo "  Управление:"
echo "    systemctl status  ${SERVICE_NAME}"
echo "    systemctl restart ${SERVICE_NAME}"
echo "    systemctl stop    ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
