#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Установка сервиса kolka_download (скачивание фото)
#
# Использование:
#   sudo bash install_download.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="kolka_download"
SERVICE_DIR="/opt/kolka_service_download"
SERVICE_USER="root"
SCANNER_DIR="/scanner"
VENV_DIR="${SERVICE_DIR}/venv"
LOG_DIR="${SERVICE_DIR}/logs"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Установка сервиса: ${SERVICE_NAME}"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Создание рабочей директории ──────────────────────────────────────────
echo "[1/7] Создание директории ${SERVICE_DIR}..."
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# ── 2. Копирование файлов ───────────────────────────────────────────────────
echo "[2/7] Копирование файлов..."
cp "${SCANNER_DIR}/kolka_download_linux.py"  "${SERVICE_DIR}/kolka_download.py"
cp "${SCANNER_DIR}/models.py"                "${SERVICE_DIR}/models.py"
cp "${SCANNER_DIR}/config_loader.py"         "${SERVICE_DIR}/config_loader.py"
cp "${SCANNER_DIR}/calibration.py"           "${SERVICE_DIR}/calibration.py"
cp "${SCANNER_DIR}/compress_images.py"       "${SERVICE_DIR}/compress_images.py"
cp "${SCANNER_DIR}/appsettings.json"         "${SERVICE_DIR}/appsettings.json"
cp "${SCANNER_DIR}/requirements_linux.txt"   "${SERVICE_DIR}/requirements.txt"

# ── 3. Создание виртуального окружения ──────────────────────────────────────
echo "[3/7] Создание виртуального окружения..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${SERVICE_DIR}/requirements.txt"

# ── 4. Создание systemd-сервиса ─────────────────────────────────────────────
echo "[4/7] Создание systemd-сервиса..."
cat > "${SERVICE_FILE}" << 'UNIT'
[Unit]
Description=Kolka Download (скачивание фото с фотоловушек)
After=network.target bluetooth.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/kolka_service_download
ExecStart=/opt/kolka_service_download/venv/bin/python /opt/kolka_service_download/kolka_download.py

# Перезапуск при ошибке
Restart=on-failure
RestartSec=60

# Логирование в systemd journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-download

# Ограничения
TimeoutStartSec=30
TimeoutStopSec=30

# Окружение
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

# ── 5. Настройка ротации логов ──────────────────────────────────────────────
echo "[5/7] Настройка ротации логов..."
cat > "${LOGROTATE_FILE}" << 'LOGROTATE'
/opt/kolka_service_download/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0644 root root
}
LOGROTATE

# ── 6. Активация сервиса ────────────────────────────────────────────────────
echo "[6/7] Активация сервиса..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "[7/7] Готово!"
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Сервис ${SERVICE_NAME} установлен"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Запуск:       sudo systemctl start ${SERVICE_NAME}"
echo "Остановка:    sudo systemctl stop ${SERVICE_NAME}"
echo "Статус:       sudo systemctl status ${SERVICE_NAME}"
echo "Логи (journald): sudo journalctl -u ${SERVICE_NAME} -f"
echo "Логи (файл):     tail -f ${LOG_DIR}/download_log_*.log"
echo ""
