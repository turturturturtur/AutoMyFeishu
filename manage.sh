#!/usr/bin/env bash
# manage.sh — AutoMyFeishu service management scaffold
# Usage: sudo bash manage.sh {install|start|stop|restart|status|log|uninstall}
set -euo pipefail

SERVICE_NAME="claude-feishu-flow"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="${PROJECT_DIR}/deploy/${SERVICE_NAME}.service"
LOG_FILE="${PROJECT_DIR}/logs/service.log"

_require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] This command must be run as root. Use: sudo bash manage.sh $1"
    exit 1
  fi
}

case "${1:-}" in

  # ── install ──────────────────────────────────────────────────────────────────
  install)
    _require_root "install"

    if [ ! -f "$TEMPLATE" ]; then
      echo "[ERROR] Service template not found: $TEMPLATE"
      echo "        Make sure you are running this script from the project root."
      exit 1
    fi

    # Ensure logs directory exists
    mkdir -p "${PROJECT_DIR}/logs"

    # Substitute placeholder and write to systemd directory
    sed "s|<PROJECT_DIR>|${PROJECT_DIR}|g" "$TEMPLATE" > "$UNIT_FILE"
    echo "[OK] Unit file written to ${UNIT_FILE}"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    echo "[OK] Service enabled (autostart on boot)"
    echo ""
    echo "Run the following to start it now:"
    echo "  sudo bash manage.sh start"
    ;;

  # ── start ─────────────────────────────────────────────────────────────────────
  start)
    systemctl start "$SERVICE_NAME"
    echo "[OK] ${SERVICE_NAME} started"
    ;;

  # ── stop ──────────────────────────────────────────────────────────────────────
  stop)
    systemctl stop "$SERVICE_NAME"
    echo "[OK] ${SERVICE_NAME} stopped"
    ;;

  # ── restart ───────────────────────────────────────────────────────────────────
  restart)
    systemctl restart "$SERVICE_NAME"
    echo "[OK] ${SERVICE_NAME} restarted"
    ;;

  # ── status ────────────────────────────────────────────────────────────────────
  status)
    systemctl status "$SERVICE_NAME"
    ;;

  # ── log ───────────────────────────────────────────────────────────────────────
  log)
    if [ -f "$LOG_FILE" ]; then
      echo "[INFO] Tailing ${LOG_FILE} (Ctrl-C to exit) ..."
      tail -f "$LOG_FILE"
    else
      echo "[INFO] Log file not found, falling back to journalctl ..."
      journalctl -u "$SERVICE_NAME" -f
    fi
    ;;

  # ── uninstall ─────────────────────────────────────────────────────────────────
  uninstall)
    _require_root "uninstall"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    echo "[OK] ${SERVICE_NAME} service removed"
    ;;

  # ── help / default ────────────────────────────────────────────────────────────
  *)
    echo "AutoMyFeishu Service Manager"
    echo ""
    echo "Usage:"
    echo "  sudo bash manage.sh install    — install & enable autostart (requires root)"
    echo "  sudo bash manage.sh start      — start the service"
    echo "  sudo bash manage.sh stop       — stop the service"
    echo "  sudo bash manage.sh restart    — restart the service"
    echo "       bash manage.sh status     — show current status"
    echo "       bash manage.sh log        — tail live logs (Ctrl-C to exit)"
    echo "  sudo bash manage.sh uninstall  — remove the service (requires root)"
    exit 1
    ;;

esac
