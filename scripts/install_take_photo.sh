#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Установка kolka_take_photo (снимки по расписанию, долгоживущий сервис)
#
# Использование:
#   sudo bash install_take_photo.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="kolka_take_photo"
SERVICE_DIR="/opt/kolka_service_take_photo"
SCANNER_DIR="/scanner"
VENV_DIR="${SERVICE_DIR}/venv"
LOG_DIR="${SERVICE_DIR}/logs"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Установка: ${SERVICE_NAME} (долгоживущий сервис)"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Остановка старого сервиса если есть ───────────────────────────────────
echo "[1/8] Остановка старого сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

# ── 2. Создание рабочей директории ──────────────────────────────────────────
echo "[2/8] Создание директории ${SERVICE_DIR}..."
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# ── 3. Копирование файлов ───────────────────────────────────────────────────
echo "[3/8] Копирование файлов..."
cp "${SCANNER_DIR}/kolka_take_photo_linux.py" "${SERVICE_DIR}/kolka_take_photo.py"
cp "${SCANNER_DIR}/models.py"                  "${SERVICE_DIR}/models.py"
cp "${SCANNER_DIR}/config_loader.py"           "${SERVICE_DIR}/config_loader.py"
cp "${SCANNER_DIR}/compress_images.py"         "${SERVICE_DIR}/compress_images.py"
cp "${SCANNER_DIR}/appsettings.json"           "${SERVICE_DIR}/appsettings.json"
cp "${SCANNER_DIR}/requirements_linux.txt"     "${SERVICE_DIR}/requirements.txt"

# ── 4. Создание виртуального окружения ──────────────────────────────────────
echo "[4/8] Создание виртуального окружения..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${SERVICE_DIR}/requirements.txt"

# ── 5. Создание systemd-сервиса ─────────────────────────────────────────────
echo "[5/8] Создание systemd сервиса..."
cat > "${SERVICE_FILE}" << 'UNIT'
[Unit]
Description=Kolka Take Photo — снимки по расписанию
After=network.target bluetooth.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/kolka_service_take_photo
ExecStart=/opt/kolka_service_take_photo/venv/bin/python /opt/kolka_service_take_photo/kolka_take_photo.py

# Перезапуск при ошибке (через 2 мин — даём BLE/Wi-Fi восстановиться)
Restart=on-failure
RestartSec=120

# Таймауты
TimeoutStartSec=60
TimeoutStopSec=60

# Graceful shutdown — SIGTERM для обработки в скрипте
KillSignal=SIGTERM
SendSIGKILL=no

# Лимиты памяти (для работы годами)
MemoryMax=512M
CPUQuota=50%

# Логирование в systemd journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-take-photo

# Окружение
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UNIT

# ── 6. Настройка ротации логов ──────────────────────────────────────────────
echo "[6/8] Настройка ротации логов..."
cat > "${LOGROTATE_FILE}" << 'LOGROTATE'
/opt/kolka_service_take_photo/logs/*.log {
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

# ── 7. Активация ────────────────────────────────────────────────────────────
echo "[7/8] Активация..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# ── 8. Запуск ───────────────────────────────────────────────────────────────
echo "[8/8] Запуск..."
systemctl start "${SERVICE_NAME}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ${SERVICE_NAME} установлен и запущен"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Управление:"
echo "  Статус:      sudo systemctl status ${SERVICE_NAME}"
echo "  Перезапуск:  sudo systemctl restart ${SERVICE_NAME}"
echo "  Остановка:   sudo systemctl stop ${SERVICE_NAME}"
echo ""
echo "Логи:"
echo "  journalctl:  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Файлы:       tail -f ${LOG_DIR}/take_photo_log_*.log"
echo ""
