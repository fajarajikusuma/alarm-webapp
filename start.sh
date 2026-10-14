#!/bin/bash
# ============================================================
# Web Alarm — Helper script
# Gunakan ini untuk start/stop/status/log service
# ============================================================

ACTION=${1:-status}

case "$ACTION" in
  start)
    echo "Starting web-alarm service..."
    sudo systemctl start web-alarm.service
    sudo systemctl status web-alarm.service --no-pager -l
    ;;
  stop)
    echo "Stopping web-alarm service..."
    sudo systemctl stop web-alarm.service
    echo "Stopped."
    ;;
  restart)
    echo "Restarting web-alarm service..."
    sudo systemctl restart web-alarm.service
    sleep 2
    sudo systemctl status web-alarm.service --no-pager -l
    ;;
  status)
    sudo systemctl status web-alarm.service --no-pager -l
    ;;
  log)
    sudo journalctl -u web-alarm.service -f --no-pager
    ;;
  enable)
    sudo systemctl enable web-alarm.service
    echo "Service enabled (auto-start on boot)"
    ;;
  disable)
    sudo systemctl disable web-alarm.service
    echo "Service disabled (will not auto-start)"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log|enable|disable}"
    exit 1
    ;;
esac
