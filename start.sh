#!/bin/bash
# ============================================================
# Web Alarm — Helper script
# Gunakan ini untuk start/stop/status/log/test/check service
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
  check)
    echo "=== Sound Cards ==="
    cat /proc/asound/cards 2>/dev/null || echo "No /proc/asound/cards"
    echo ""
    echo "=== ALSA Devices ==="
    aplay -l 2>/dev/null || echo "aplay error"
    echo ""
    echo "=== System Time & NTP Sync ==="
    timedatectl 2>/dev/null || date
    echo ""
    echo "=== Installed Audio Players ==="
    which mpv mpg123 ffplay 2>/dev/null || true
    echo ""
    echo "=== Service Status ==="
    sudo systemctl status web-alarm.service --no-pager -l
    ;;
  test-sound)
    echo "Testing direct audio output..."
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # Find any mp3 in static/uploads/audios
    TEST_FILE=$(find "$SCRIPT_DIR/static/uploads/audios" -type f -name "*.mp3" 2>/dev/null | head -1)
    if [ -n "$TEST_FILE" ]; then
      echo "Playing: $TEST_FILE"
      if which mpv >/dev/null 2>&1; then
        mpv --no-video --volume=80 "$TEST_FILE"
      else
        mpg123 "$TEST_FILE"
      fi
    else
      echo "No test MP3 file found in $SCRIPT_DIR/static/uploads/audios."
      echo "Generating test tone via speaker-test..."
      speaker-test -t sine -f 440 -l 1 2>/dev/null || echo "speaker-test not available."
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log|enable|disable|check|test-sound}"
    exit 1
    ;;
esac
