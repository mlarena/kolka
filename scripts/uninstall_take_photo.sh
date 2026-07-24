#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Удаление сервиса kolka_take_photo
#
# Использование:
#   sudo bash uninstall_take_photo.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="kolka_take_photo"
SERVICE_DIR="/opt/kolka_service_take_photo"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

echo "═══════════════════════════════════════════════════════════"
echo "  Удаление сервиса: ${SERVICE_NAME}"
echo "═══════════════════════════════════════════════════════════"

# ── 1. Остановка и отключение ───────────────────────────────────────────────
echo "[1/4] Остановка сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

# ── 2. Удаление systemd-файла ───────────────────────────────────────────────
echo "[2/4] Удаление systemd-конфига..."
rm -f "${SERVICE_FILE}"
systemctl daemon-reload

# ── 3. Удаление logrotate ───────────────────────────────────────────────────
echo "[3/4] Удаление logrotate-конфига..."
rm -f "${LOGROTATE_FILE}"

# ── 4. Удаление рабочей директории ──────────────────────────────────────────
echo "[4/4] Удаление директории ${SERVICE_DIR}..."
rm -rf "${SERVICE_DIR}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Сервис ${SERVICE_NAME} удалён"
echo "═══════════════════════════════════════════════════════════"
echo ""
