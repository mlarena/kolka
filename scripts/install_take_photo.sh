#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Установка kolka_take_photo (oneshot + timer)
# Расписание: нечётные часы (01:00, 03:00, ..., 23:00)
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
TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}.timer"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Установка: ${SERVICE_NAME} (oneshot + timer)"
echo "  Расписание: 01:00, 03:00, 05:00, ..., 23:00"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Остановка старого сервиса если есть ───────────────────────────────────
echo "[1/8] Остановка старого сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl stop "${SERVICE_NAME}.timer" 2>/dev/null || true

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
cp "${SCANNER_DIR}/requirements.txt"            "${SERVICE_DIR}/requirements.txt"

# ── 4. Создание виртуального окружения ──────────────────────────────────────
echo "[4/8] Создание виртуального окружения..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install --upgrade -r "${SERVICE_DIR}/requirements.txt"

# ── 5. Создание systemd-сервиса (oneshot) ────────────────────────────────────
echo "[5/8] Создание systemd сервиса (oneshot)..."
cat > "${SERVICE_FILE}" << 'UNIT'
[Unit]
Description=Kolka Take Photo — снимки с фотоловушек
After=network.target bluetooth.target postgresql.service
Wants=postgresql.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/kolka_service_take_photo
ExecStart=/opt/kolka_service_take_photo/venv/bin/python /opt/kolka_service_take_photo/kolka_take_photo.py

# Таймаут (30 мин — камеры могут быть медленными, BLE + Wi-Fi)
TimeoutStartSec=1800

# Graceful shutdown
KillSignal=SIGTERM
SendSIGKILL=no

# Лимиты памяти
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

# ── 6. Создание systemd timer (нечётные часы) ───────────────────────────────
echo "[6/8] Создание systemd timer (нечётные часы)..."
cat > "${TIMER_FILE}" << 'TIMER'
[Unit]
Description=Таймер снимков с фотоловушек (нечётные часы)

[Timer]
OnCalendar=*-*-* 01,03,05,07,09,11,13,15,17,19,21,23:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
TIMER

# ── 7. Настройка ротации логов ──────────────────────────────────────────────
echo "[7/8] Настройка ротации логов..."
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

# ── 8. Активация ────────────────────────────────────────────────────────────
echo "[8/8] Активация..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.timer"

# ── 9. Запуск таймера ──────────────────────────────────────────────────────
echo "[9/9] Запуск таймера..."
systemctl start "${SERVICE_NAME}.timer"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ${SERVICE_NAME} установлен (oneshot + timer)"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Расписание: 01:00, 03:00, 05:00, 07:00, 09:00, 11:00,"
echo "            13:00, 15:00, 17:00, 19:00, 21:00, 23:00"
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
echo "  Файлы:       tail -f ${LOG_DIR}/take_photo_log_*.log"
echo ""
echo "Остановка:"
echo "  sudo systemctl stop ${SERVICE_NAME}.timer"
echo "  sudo systemctl disable ${SERVICE_NAME}.timer"
echo ""
