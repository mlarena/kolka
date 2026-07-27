#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Удаление kolka_download
#
# Использование:
#   sudo bash uninstall_download.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="kolka_download"
SERVICE_DIR="/opt/kolka_service_download"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}.timer"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Удаление: ${SERVICE_NAME}"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Остановка и отключение ───────────────────────────────────────────────
echo "[1/4] Остановка..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl stop "${SERVICE_NAME}.timer" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.timer" 2>/dev/null || true
rm -f /var/lib/systemd/timers/stamp-${SERVICE_NAME}.timer 2>/dev/null || true

# ── 2. Удаление systemd-файлов ──────────────────────────────────────────────
echo "[2/4] Удаление systemd конфигов..."
rm -f "${SERVICE_FILE}"
rm -f "${TIMER_FILE}"
systemctl daemon-reload

# ── 3. Удаление logrotate ───────────────────────────────────────────────────
echo "[3/4] Удаление logrotate..."
rm -f "${LOGROTATE_FILE}"

# ── 4. Удаление рабочей директории ──────────────────────────────────────────
echo "[4/4] Удаление ${SERVICE_DIR}..."
rm -rf "${SERVICE_DIR}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ${SERVICE_NAME} удалён"
echo "═══════════════════════════════════════════════════════════"
echo ""
