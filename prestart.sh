#!/bin/bash
# ============================================================
# Web Alarm — Pre-start script (dijalankan sebagai root)
# Handle semua persiapan audio sebelum Flask start
# ============================================================
set -e
LOG_PREFIX="[web-alarm prestart]"
log() { echo "$LOG_PREFIX $*"; }

log "====== START $(date) ======"

# ── 1. Disable ALSA plugin configs yang crash tanpa X11/JACK session ──
ALSA_CONF_DIR="/etc/alsa/conf.d"
DISABLE_CONFS="50-jack.conf 50-pulseaudio.conf 99-pulse.conf 99-pulseaudio-default.conf"
for conf in $DISABLE_CONFS; do
    if [ -f "$ALSA_CONF_DIR/$conf" ]; then
        mv "$ALSA_CONF_DIR/$conf" "$ALSA_CONF_DIR/${conf}.disabled"
        log "Disabled $conf"
    fi
done

# Bersihkan JACK shared memory (stale lock dari session sebelumnya)
rm -rf /dev/shm/jack_db-* /dev/shm/jack-* 2>/dev/null || true
log "Cleared stale JACK shm"

# ── 2. Load kernel modules audio ────────────────────────────
for mod in snd snd-pcm snd-usb-audio; do
    modprobe "$mod" 2>/dev/null || true
done
log "Kernel modules loaded"

# ── 3. Tunggu USB audio device siap ─────────────────────────
log "Waiting for USB audio device..."
MAX_WAIT=45
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if [ -f /proc/asound/cards ] && grep -qi "usb\|c-media\|device" /proc/asound/cards 2>/dev/null; then
        log "USB audio found after ${WAITED}s"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done
if [ $WAITED -ge $MAX_WAIT ]; then
    log "WARNING: USB audio not found after ${MAX_WAIT}s — continuing anyway"
fi

# ── 4. Fix /dev/snd permissions (world-rw) ──────────────────
# Dilakukan tiap start karena udev bisa reset saat device reconnect
if [ -d /dev/snd ]; then
    chmod o+rw /dev/snd/pcmC* /dev/snd/controlC* /dev/snd/seq /dev/snd/timer 2>/dev/null || true
    log "/dev/snd permissions set to world-rw"
else
    log "WARNING: /dev/snd not found"
fi

# ── 5. Pastikan /etc/asound.conf ada dan benar ───────────────
cat > /etc/asound.conf << 'ASOUND'
# Web Alarm — ALSA config
# Route default output to USB Audio card (card 1)
pcm.!default {
    type plug
    slave { pcm "hw:1,0" }
}
ctl.!default {
    type hw
    card 1
}
ASOUND
log "/etc/asound.conf written"

# ── 6. Set hardware volume ───────────────────────────────────
CARD_NUM=$(grep -oP '^\s*\K\d+' /proc/asound/cards 2>/dev/null | head -1)
if [ -n "$CARD_NUM" ]; then
    for attempt in 1 2 3 4 5; do
        if amixer -c "$CARD_NUM" sset 'Speaker' 80% 2>/dev/null; then
            log "Volume set to 80% on card $CARD_NUM (attempt $attempt)"
            break
        fi
        [ $attempt -lt 5 ] && sleep 2
    done
else
    log "WARNING: No sound card found, skipping volume"
fi

# ── 7. Pastikan direktori upload ada ────────────────────────
mkdir -p /home/adminlh/alarm-apps/static/uploads/audios
chown -R adminlh:adminlh /home/adminlh/alarm-apps/static 2>/dev/null || true
log "Upload directory ready"

# ── 8. Bersihkan zombie mpg123 dari boot sebelumnya ─────────
pkill -9 mpg123 2>/dev/null || true
log "Cleared any stale mpg123 processes"

log "====== DONE — Starting Flask ======"
