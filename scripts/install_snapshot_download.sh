#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Установка kolka_snapshot_download (снимок + загрузка по timer)
# Расписание: каждый час с 08:00 до 15:00
#
# Использование:
#   sudo bash install_snapshot_download.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="kolka_snapshot_download"
SERVICE_DIR="/opt/kolka_service_snapshot_download"
SCANNER_DIR="/scanner"
VENV_DIR="${SERVICE_DIR}/venv"
LOG_DIR="${SERVICE_DIR}/logs"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}.timer"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Установка: ${SERVICE_NAME} (oneshot + timer)"
echo "  Расписание: 08:00, 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Остановка старого сервиса если есть ───────────────────────────────────
echo "[1/9] Остановка старого сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl stop "${SERVICE_NAME}.timer" 2>/dev/null || true

# Удаление старого stamp-файла от предыдущих установок
rm -f /var/lib/systemd/timers/stamp-${SERVICE_NAME}.timer 2>/dev/null || true

# ── 2. Создание рабочей директории ──────────────────────────────────────────
echo "[2/9] Создание директории ${SERVICE_DIR}..."
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# ── 3. Копирование файлов ───────────────────────────────────────────────────
echo "[3/9] Копирование файлов..."
cp "${SCANNER_DIR}/kolka_snapshot_and_download.py" "${SERVICE_DIR}/kolka_snapshot_and_download.py"
cp "${SCANNER_DIR}/models.py"                      "${SERVICE_DIR}/models.py"
cp "${SCANNER_DIR}/config_loader.py"               "${SERVICE_DIR}/config_loader.py"
cp "${SCANNER_DIR}/appsettings.json"               "${SERVICE_DIR}/appsettings.json"
cp "${SCANNER_DIR}/requirements.txt"               "${SERVICE_DIR}/requirements.txt"
cp "${SCANNER_DIR}/compress_images.py"             "${SERVICE_DIR}/compress_images.py"

# ── 4. Создание виртуального окружения ──────────────────────────────────────
echo "[4/9] Создание виртуального окружения..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install --upgrade -r "${SERVICE_DIR}/requirements.txt"

# ── 5. Создание systemd service (oneshot) ───────────────────────────────────
echo "[5/9] Создание systemd service (oneshot)..."
cat > "${SERVICE_FILE}" << 'UNIT'
[Unit]
Description=Kolka Snapshot Download — снимок и загрузка фото
After=network.target bluetooth.target postgresql.service
Wants=postgresql.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/kolka_service_snapshot_download
ExecStart=/opt/kolka_service_snapshot_download/venv/bin/python /opt/kolka_service_snapshot_download/kolka_snapshot_and_download.py

# Таймаут (30 мин)
TimeoutStartSec=1800

# Логирование в systemd journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-snapshot-download

# Окружение
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UNIT

# ── 6. Создание systemd timer (08:00–15:00 каждый час) ─────────────────────
echo "[6/9] Создание systemd timer (08:00–15:00)..."
cat > "${TIMER_FILE}" << 'TIMER'
[Unit]
Description=Таймер снимка и загрузки (каждый час)

[Timer]
OnCalendar=*-*-* 00,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23:00
RandomizedDelaySec=60
Persistent=false

[Install]
WantedBy=timers.target
TIMER

# ── 7. Настройка ротации логов ──────────────────────────────────────────────
echo "[7/9] Настройка ротации логов..."
cat > "${LOGROTATE_FILE}" << 'LOGROTATE'
/opt/kolka_service_snapshot_download/logs/*.log {
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

# ── 8. Активация ────────────────────────────────────────────────────────────
echo "[8/9] Активация..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.timer"

# ── 9. Запуск таймера ──────────────────────────────────────────────────────
echo "[9/9] Запуск таймера..."
systemctl start "${SERVICE_NAME}.timer"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ${SERVICE_NAME} установлен (oneshot + timer, 08–15 часов)"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Расписание: 08:00, 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00"
echo ""
echo "Таймер:"
echo "  Статус:      sudo systemctl status ${SERVICE_NAME}.timer"
echo "  Список:      sudo systemctl list-timers ${SERVICE_NAME}.timer"
echo ""
echo "Ручной запуск:"
echo "  Запуск:      sudo systemctl start ${SERVICE_NAME}"
echo "  Статус:      sudo systemctl status ${SERVICE_NAME}"
echo ""
echo "Логи:"
echo "  journalctl:  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Файлы:       tail -f ${LOG_DIR}/snapshot_download_log_*.log"
echo ""
echo "Остановка:"
echo "  sudo systemctl stop ${SERVICE_NAME}.timer"
echo "  sudo systemctl disable ${SERVICE_NAME}.timer"
echo ""
