import os
import uuid
import subprocess
import re
import pytz
import threading as _threading
import time as _time
from collections import deque
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for,
    flash, request, abort, jsonify
)
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models import db, User, Audio, Schedule, AudioSettings

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///alarm.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', 'static/uploads/audios')
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_CONTENT_LENGTH', 10485760))

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '')
ALLOWED_EXTENSIONS = {'mp3'}

# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'
login_manager.login_message = 'Silakan login untuk mengakses halaman ini.'

# ---------------------------------------------------------------------------
# Timezone — WIB (Asia/Jakarta, UTC+7)
# ---------------------------------------------------------------------------
WIB = pytz.timezone('Asia/Jakarta')

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(daemon=True, timezone=WIB)
scheduler.start()


# ---------------------------------------------------------------------------
# Audio event log — ring buffer (newest first, max 200 entries)
# ---------------------------------------------------------------------------
_audio_log_lock  = _threading.Lock()
_audio_log_deque: deque = deque(maxlen=200)


def _log_audio(level: str, event: str, detail: str = ''):
    """
    level: 'ok' | 'warn' | 'error' | 'info'
    event: short keyword, e.g. 'ALARM_FIRE', 'PLAYBACK_OK', 'PLAYBACK_FAIL', 'REPAIR'
    detail: free-form string
    """
    entry = {
        'ts':     datetime.now(WIB).strftime('%H:%M:%S'),
        'level':  level,
        'event':  event,
        'detail': detail,
    }
    with _audio_log_lock:
        _audio_log_deque.appendleft(entry)
    # Also push to Flask logger for systemd journal
    msg = f'[AUDIO-LOG] {event}: {detail}'
    if level == 'error':
        app.logger.error(msg)
    elif level == 'warn':
        app.logger.warning(msg)
    else:
        app.logger.info(msg)


def get_audio_log(limit: int = 100) -> list:
    with _audio_log_lock:
        return list(_audio_log_deque)[:limit]


# ---------------------------------------------------------------------------
# ALSA / Audio helpers
# ---------------------------------------------------------------------------
def get_alsa_devices():
    devices = []
    card_names = {}
    try:
        with open('/proc/asound/cards', 'r') as f:
            for line in f:
                m = re.match(r'^\s*(\d+)\s+\[(\S+)\s*\].*?-\s*(.+)$', line)
                if m:
                    card_names[m.group(1)] = m.group(3).strip()
    except Exception:
        pass
    usb_card_name = card_names.get('1', 'USB Audio Device')
    devices.append({'id': 'default', 'name': f'USB Audio Device — {usb_card_name}',
                    'card': '1', 'device': '0', 'note': 'Recommended'})
    for card_num, card_name in card_names.items():
        devices.append({'id': f'hw:{card_num},0',
                        'name': f'{card_name} (Direct hw:{card_num},0)',
                        'card': card_num, 'device': '0', 'note': 'Direct'})
    return devices


def get_current_volume() -> int:
    """Read hardware volume via amixer. Returns 0-100, or -1 if unreadable."""
    card = _check_alsa_card()
    ok, vol, _ = _amixer_sget(card)
    return vol if ok else -1


def set_hardware_volume(percent: int):
    """Set USB audio volume via amixer. Uses raw value for accuracy."""
    percent = max(0, min(100, percent))
    raw = round(percent / 100 * 37)
    card = _check_alsa_card()
    # Try card short name first, then index, then fallback to index 1
    candidates = []
    if card.get('short_name'):
        candidates.append(['amixer', '-c', card['short_name'], 'sset', 'Speaker', str(raw)])
    if card.get('index'):
        candidates.append(['amixer', '-c', card['index'], 'sset', 'Speaker', str(raw)])
    candidates.append(['amixer', '-c', '1', 'sset', 'Speaker', str(raw)])
    for cmd in candidates:
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            if result.returncode == 0:
                return
        except Exception:
            pass
    app.logger.error('amixer volume set failed: all candidates exhausted')


def get_settings():
    s = db.session.get(AudioSettings, 1)
    if not s:
        s = AudioSettings(id=1)
        db.session.add(s)
        db.session.commit()
    return s


# ---------------------------------------------------------------------------
# Audio hardware health monitor
# ---------------------------------------------------------------------------
_audio_health = {
    'ok':         False,
    'card':       '',      # e.g. "USB Audio Device"
    'card_index': '',      # e.g. "1"
    'device_node':'',      # e.g. "pcmC1D0p"
    'usb_id':     '',      # e.g. "0d8c:0014"
    'last_check': '',
    'last_repair':'',
    'error':      '',
}
_health_lock = _threading.Lock()


def _check_usb_present() -> bool:
    """Return True if USB audio device is visible to lsusb."""
    try:
        out = subprocess.check_output(['lsusb'], stderr=subprocess.DEVNULL, text=True)
        return bool(re.search(r'0d8c', out))
    except Exception:
        return False


def _check_alsa_card() -> dict:
    """Return dict with card info if ALSA sees the USB audio card."""
    result = {'found': False, 'index': '', 'name': '', 'short_name': ''}
    try:
        with open('/proc/asound/cards', 'r') as f:
            for line in f:
                # e.g. " 1 [Device         ]: USB-Audio - USB Audio Device"
                m = re.match(r'^\s*(\d+)\s+\[(\S+)\s*\].*?-\s*(.+)$', line)
                if m:
                    result = {
                        'found':      True,
                        'index':      m.group(1),
                        'short_name': m.group(2).strip(),   # e.g. "Device"
                        'name':       m.group(3).strip(),   # e.g. "USB Audio Device"
                    }
    except Exception:
        pass
    return result


def _amixer_sget(card: dict) -> tuple[bool, int, str]:
    """
    Try to read Speaker volume via amixer.
    Returns (ok: bool, volume: int, error_str: str).
    Uses card short_name preferentially (most reliable across ALSA configs).
    """
    candidates = []
    if card.get('short_name'):
        candidates.append(['amixer', '-c', card['short_name'], 'sget', 'Speaker'])
    if card.get('index'):
        candidates.append(['amixer', '-c', card['index'], 'sget', 'Speaker'])
        candidates.append(['amixer', '-D', f'hw:{card["index"]}', 'sget', 'Speaker'])

    last_err = ''
    for cmd in candidates:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.PIPE, text=True, timeout=3)
            m = re.search(r'\[(\d+)%\]', out)
            if m:
                return True, int(m.group(1)), ''
            last_err = f'no-[xx%]-match cmd={" ".join(cmd[2:])}'
        except subprocess.CalledProcessError as e:
            last_err = f'rc={e.returncode} stderr={e.stderr[:60] if e.stderr else ""}'
        except Exception as e:
            last_err = str(e)[:60]
    return False, 0, last_err


def _check_pcm_accessible(card_index: str) -> bool:
    """Return True if the PCM playback device node exists and is writable."""
    pcm = f'/dev/snd/pcmC{card_index}D0p'
    if os.path.exists(pcm) and os.access(pcm, os.W_OK):
        return True
    # Also try /proc/asound/cardN/pcm0p as fallback
    proc_pcm = f'/proc/asound/card{card_index}/pcm0p'
    return os.path.isdir(proc_pcm)


def _repair_audio() -> str:
    """
    Attempt to restore audio.
    Only reloads kernel module if ALSA card is NOT detected.
    Returns a status string.
    """
    steps = []
    card = _check_alsa_card()

    # Only reload module if card is completely gone — reload causes brief disconnect
    if not card['found']:
        try:
            subprocess.run(['modprobe', '-r', 'snd-usb-audio'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            _time.sleep(2)
            subprocess.run(['modprobe', 'snd-usb-audio'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            _time.sleep(5)   # give device time to enumerate
            steps.append('module-reloaded')
        except Exception as e:
            steps.append(f'module-error:{e}')
    else:
        steps.append('module-ok')

    # Fix /dev/snd permissions
    for node in ['/dev/snd/pcmC1D0p', '/dev/snd/pcmC1D0c',
                 '/dev/snd/controlC1', '/dev/snd/seq', '/dev/snd/timer']:
        try:
            if os.path.exists(node):
                subprocess.run(['chmod', 'o+rw', node],
                               capture_output=True, timeout=2)
        except Exception:
            pass
    steps.append('perms-fixed')

    # Verify/write /etc/asound.conf
    try:
        asound_ok = False
        if os.path.exists('/etc/asound.conf'):
            with open('/etc/asound.conf', 'r') as f:
                content = f.read()
                asound_ok = ('hw:Device' in content or 'card Device' in content)
        if not asound_ok:
            with open('/etc/asound.conf', 'w') as f:
                f.write(
                    "# Web Alarm — auto-repaired\n"
                    "# Use card short name 'Device' (stable across reconnects)\n"
                    "pcm.!default {\n    type plug\n    slave { pcm \"hw:Device,0\" }\n}\n"
                    "ctl.!default {\n    type hw\n    card Device\n}\n"
                )
            steps.append('asound-written')
        else:
            steps.append('asound-ok')
    except Exception:
        steps.append('asound-skip')

    # Disable JACK/pulse ALSA configs
    for conf in ['/etc/alsa/conf.d/50-jack.conf',
                 '/etc/alsa/conf.d/50-pulseaudio.conf',
                 '/etc/alsa/conf.d/99-pulse.conf']:
        try:
            if os.path.exists(conf):
                os.rename(conf, conf + '.disabled')
                steps.append(f'disabled:{os.path.basename(conf)}')
        except Exception:
            pass

    return ','.join(steps)


def run_audio_health_check():
    """
    Full health check + auto-repair. Updates _audio_health dict.
    Safe to call from any thread.
    amixer is used for volume only — failures here do NOT block playback.
    """
    now = datetime.now(WIB).strftime('%H:%M:%S')
    usb_ok = _check_usb_present()
    card   = _check_alsa_card()
    pcm_ok = _check_pcm_accessible(card['index']) if card['found'] else False

    amixer_ok = False
    vol = 0
    amixer_err = ''

    if card['found']:
        amixer_ok, vol, amixer_err = _amixer_sget(card)
        if amixer_err:
            app.logger.warning(f'amixer error (non-fatal): {amixer_err}')
            # amixer failure is non-fatal: if USB+card+pcm are ok, audio can still play
            if usb_ok and pcm_ok:
                amixer_ok = True   # mark ok so playback is not blocked
                # Use DB volume instead of hardcoded 80
                try:
                    with app.app_context():
                        s = get_settings()
                        vol = s.volume
                except Exception:
                    vol = vol or 80  # last resort fallback only

    overall_ok = usb_ok and card['found'] and pcm_ok and amixer_ok
    repair_msg = ''

    if not overall_ok:
        err_detail = f'usb={usb_ok} card={card["found"]} pcm={pcm_ok} amixer={amixer_ok} err={amixer_err}'
        app.logger.warning(f'Audio health FAIL: {err_detail} — attempting repair')
        _log_audio('warn', 'HEALTH_FAIL', err_detail)
        repair_msg = _repair_audio()
        _log_audio('info', 'REPAIR_START', repair_msg)
        _time.sleep(6)

        # Re-check after repair
        card   = _check_alsa_card()
        pcm_ok = _check_pcm_accessible(card['index']) if card['found'] else False
        if card['found']:
            amixer_ok, new_vol, _ = _amixer_sget(card)
            if amixer_ok:
                vol = new_vol
        overall_ok = usb_ok and card['found'] and pcm_ok and amixer_ok
        repair_result = f'{repair_msg} → ok={overall_ok}'
        app.logger.info(f'Audio repair result: {repair_result}')
        _log_audio('ok' if overall_ok else 'error', 'REPAIR_DONE', repair_result)

        # Reload scheduler jobs if repaired successfully
        if overall_ok:
            try:
                with app.app_context():
                    for sched in Schedule.query.filter_by(is_active=True).all():
                        add_scheduler_job(sched)
                app.logger.info('Scheduler jobs reloaded after audio repair')
                _log_audio('ok', 'SCHEDULER_RELOAD', 'Jobs reloaded after repair')
            except Exception as e:
                app.logger.error(f'Scheduler reload error: {e}')
                _log_audio('error', 'SCHEDULER_RELOAD', str(e))
    else:
        _log_audio('ok', 'HEALTH_OK',
                   f'card={card.get("name","")} vol={vol}% pcm={pcm_ok}')

    with _health_lock:
        _audio_health.update({
            'ok':          overall_ok,
            'card':        card.get('name', ''),
            'card_index':  card.get('index', ''),
            'device_node': f'pcmC{card.get("index","")}D0p' if card.get('index') else '',
            'usb_id':      '0d8c:0014' if usb_ok else '',
            'last_check':  now,
            'last_repair': repair_msg,
            'error':       '' if overall_ok else
                           f'usb={usb_ok} card={card["found"]} pcm={pcm_ok} amixer={amixer_ok}',
            'volume':      vol,
            'amixer_warn': amixer_err if amixer_err else '',
        })

    return overall_ok


def _audio_monitor_loop():
    """Background thread: check audio health every 30 seconds."""
    _time.sleep(8)   # give Flask time to fully start
    while True:
        try:
            run_audio_health_check()
        except Exception as e:
            app.logger.error(f'Audio monitor error: {e}')
        _time.sleep(30)


def get_audio_health() -> dict:
    with _health_lock:
        return dict(_audio_health)


# ---------------------------------------------------------------------------
# Audio playback — alarm state + test process tracking
# ---------------------------------------------------------------------------
_test_process: subprocess.Popen | None = None
_test_process_lock = _threading.Lock()

_alarm_state: dict = {
    'playing': False, 'label': '', 'time': '',
    'started_at': '', 'process': None,
}
_alarm_lock = _threading.Lock()


def _alarm_is_playing() -> bool:
    with _alarm_lock:
        proc = _alarm_state['process']
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        _alarm_state['playing'] = False
        _alarm_state['process'] = None
        return False


def _verify_alarm_playback(proc: subprocess.Popen, label: str,
                           filepath: str, volume: int, time_str: str):
    """
    Called in a background thread 2 seconds after alarm starts.
    Checks if mpg123 is still running. If it died instantly (bad output device),
    logs the failure, runs health repair, and retries playback once.
    """
    _time.sleep(2)
    if proc.poll() is not None:
        # Process already exited — output probably failed
        _log_audio('error', 'PLAYBACK_FAIL',
                   f'mpg123 exited immediately (rc={proc.returncode}) '
                   f'label="{label}" → auto-repair & retry')
        # Run full health repair
        try:
            with app.app_context():
                run_audio_health_check()
        except Exception as e:
            _log_audio('error', 'REPAIR_ERROR', str(e))
            return

        _time.sleep(2)
        # Retry playback
        try:
            set_hardware_volume(volume)
            retry_proc = subprocess.Popen(
                ['mpg123', '-q', filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            _time.sleep(2)
            if retry_proc.poll() is None:
                _log_audio('ok', 'PLAYBACK_RETRY_OK',
                           f'pid={retry_proc.pid} label="{label}" — retry succeeded')
                with _alarm_lock:
                    _alarm_state.update({
                        'playing': True, 'label': label, 'time': time_str,
                        'started_at': datetime.now(WIB).strftime('%Y-%m-%dT%H:%M:%S'),
                        'process': retry_proc,
                    })
            else:
                _log_audio('error', 'PLAYBACK_RETRY_FAIL',
                           f'label="{label}" rc={retry_proc.returncode} — FATAL, audio output dead')
        except Exception as e:
            _log_audio('error', 'PLAYBACK_RETRY_ERROR', str(e))
    else:
        _log_audio('ok', 'PLAYBACK_OK',
                   f'pid={proc.pid} label="{label}" — mpg123 still running ✓')


def _start_alarm(filepath: str, volume: int, label: str, time_str: str):
    global _alarm_state
    with _alarm_lock:
        old = _alarm_state.get('process')
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass

        # Auto-repair audio if not healthy before playing alarm
        h = get_audio_health()
        if not h.get('ok'):
            app.logger.warning('Audio unhealthy before alarm — running repair')
            _log_audio('warn', 'ALARM_PRECHECK_FAIL',
                       f'label="{label}" time={time_str} → running repair first')
            run_audio_health_check()

        set_hardware_volume(volume)
        _log_audio('info', 'ALARM_FIRE',
                   f'label="{label}" time={time_str} file={os.path.basename(filepath)} vol={volume}%')
        try:
            proc = subprocess.Popen(
                ['mpg123', '-q', filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except Exception as e:
            app.logger.error(f'Alarm play error: {e}')
            _log_audio('error', 'ALARM_PLAY_ERROR',
                       f'label="{label}" file={os.path.basename(filepath)} err={e}')
            return
        _alarm_state.update({
            'playing': True, 'label': label, 'time': time_str,
            'started_at': datetime.now(WIB).strftime('%Y-%m-%dT%H:%M:%S'),
            'process': proc,
        })
        _log_audio('ok', 'ALARM_STARTED',
                   f'pid={proc.pid} label="{label}" file={os.path.basename(filepath)}')

    # Verify playback is actually running after a short delay (non-blocking)
    _threading.Thread(
        target=_verify_alarm_playback,
        args=(proc, label, filepath, volume, time_str),
        daemon=True
    ).start()


def stop_alarm():
    global _alarm_state
    with _alarm_lock:
        proc = _alarm_state.get('process')
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        label = _alarm_state.get('label', '')
        _alarm_state.update({'playing': False, 'label': '', 'time': '', 'process': None})
    _log_audio('info', 'ALARM_STOPPED', f'label="{label}"')


def get_alarm_state() -> dict:
    playing = _alarm_is_playing()
    with _alarm_lock:
        return {
            'playing': playing, 'label': _alarm_state['label'],
            'time': _alarm_state['time'], 'started_at': _alarm_state['started_at'],
        }


def _is_test_playing() -> bool:
    global _test_process
    with _test_process_lock:
        if _test_process is None:
            return False
        if _test_process.poll() is None:
            return True
        _test_process = None
        return False


def stop_test_audio():
    global _test_process
    with _test_process_lock:
        if _test_process is not None:
            try:
                _test_process.terminate()
                _test_process.wait(timeout=2)
            except Exception:
                try:
                    _test_process.kill()
                except Exception:
                    pass
            _test_process = None


def play_test_audio(filepath, volume=80) -> bool:
    global _test_process
    with _test_process_lock:
        if _test_process is not None and _test_process.poll() is None:
            return False
        try:
            set_hardware_volume(volume)
            _test_process = subprocess.Popen(
                ['mpg123', '-q', filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            _log_audio('info', 'TEST_PLAY',
                       f'pid={_test_process.pid} file={os.path.basename(filepath)} vol={volume}%')
            return True
        except Exception as e:
            app.logger.error(f'Error playing test audio: {e}')
            _log_audio('error', 'TEST_PLAY_ERROR', str(e))
            _test_process = None
            return False


def play_audio_with_settings(filepath, schedule_label='', schedule_time=''):
    with app.app_context():
        s = get_settings()
        _start_alarm(filepath, volume=s.volume,
                     label=schedule_label, time_str=schedule_time)


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------
def get_job_id(schedule_id):
    return f'schedule_{schedule_id}'


def add_scheduler_job(schedule):
    job_id = get_job_id(schedule.id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if not schedule.is_active:
        return
    filepath = os.path.join(app.root_path, schedule.audio.filepath)
    days_str = ','.join(str(d) for d in schedule.days_list())
    label = schedule.label or schedule.audio.original_name
    scheduler.add_job(
        func=play_audio_with_settings,
        trigger=CronTrigger(
            day_of_week=days_str, hour=schedule.time_hour,
            minute=schedule.time_minute, second=0, timezone=WIB
        ),
        args=[filepath, label, schedule.time_display()],
        id=job_id, replace_existing=True
    )


def remove_scheduler_job(schedule_id):
    job_id = get_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ---------------------------------------------------------------------------
# Auth helpers & decorators
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    """Allow only admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


def member_required(f):
    """Allow any active authenticated user (admin or user)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        if not current_user.is_active:
            logout_user()
            flash('Akun Anda telah dinonaktifkan.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def is_admin():
    return current_user.is_authenticated and current_user.role == 'admin'


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


app.jinja_env.globals['is_admin'] = is_admin

# ---------------------------------------------------------------------------
# DB init + seed admin + load schedules
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Ensure AudioSettings singleton
    if not db.session.get(AudioSettings, 1):
        db.session.add(AudioSettings(id=1))
        db.session.commit()

    # Seed: make sure the ADMIN_EMAIL user has role='admin'
    if ADMIN_EMAIL:
        admin_user = User.query.filter_by(email=ADMIN_EMAIL).first()
        if admin_user and admin_user.role != 'admin':
            admin_user.role = 'admin'
            db.session.commit()

    # Migrate: add role/is_active columns to existing users table if missing
    # (SQLAlchemy doesn't auto-migrate, so we add defaults for existing rows)
    try:
        from sqlalchemy import text
        db.session.execute(text(
            "UPDATE users SET role='user' WHERE role IS NULL"
        ))
        db.session.execute(text(
            "UPDATE users SET is_active=1 WHERE is_active IS NULL"
        ))
        if ADMIN_EMAIL:
            db.session.execute(text(
                f"UPDATE users SET role='admin' WHERE email='{ADMIN_EMAIL}'"
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def load_active_schedules():
    with app.app_context():
        for schedule in Schedule.query.filter_by(is_active=True).all():
            add_scheduler_job(schedule)


load_active_schedules()

# Run initial health check in main thread at startup
try:
    _time.sleep(3)   # allow USB device to enumerate before first check
    with app.app_context():
        run_audio_health_check()
except Exception:
    pass

# Start audio health monitor background thread
_monitor_thread = _threading.Thread(
    target=_audio_monitor_loop, daemon=True, name='audio-monitor'
)
_monitor_thread.start()


# ---------------------------------------------------------------------------
# Routes — Authentication
# ---------------------------------------------------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not name or not email or not password:
            flash('Semua kolom wajib diisi.', 'danger')
        elif len(password) < 6:
            flash('Password minimal 6 karakter.', 'danger')
        elif password != confirm:
            flash('Password tidak cocok.', 'danger')
        elif User.query.filter_by(email=email).first():
            flash('Email sudah terdaftar.', 'danger')
        else:
            role = 'admin' if email == ADMIN_EMAIL else 'user'
            user = User(name=name, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Akun berhasil dibuat. Silakan login.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash('Email atau password salah.', 'danger')
        elif not user.is_active:
            flash('Akun Anda telah dinonaktifkan. Hubungi admin.', 'danger')
        else:
            login_user(user, remember=remember)
            flash(f'Selamat datang, {user.name}!', 'success')
            return redirect(request.args.get('next') or url_for('index'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Anda telah logout.', 'info')
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route('/')
@login_required
def index():
    schedules = Schedule.query.order_by(Schedule.time_hour, Schedule.time_minute).all()
    total = Schedule.query.count()
    active = Schedule.query.filter_by(is_active=True).count()
    audio_count = Audio.query.count()
    user_count = User.query.count()
    return render_template('index.html', schedules=schedules,
                           total=total, active_count=active,
                           audio_count=audio_count, user_count=user_count)


# ---------------------------------------------------------------------------
# Routes — Audio Management (admin + user)
# ---------------------------------------------------------------------------
@app.route('/audios')
@login_required
def audios():
    audio_list = Audio.query.order_by(Audio.original_name).all()
    return render_template('audios.html', audios=audio_list)


@app.route('/audios/upload', methods=['POST'])
@login_required
@member_required
def upload_audio():
    if 'file' not in request.files:
        flash('Tidak ada file yang dikirim.', 'danger')
        return redirect(url_for('audios'))
    file = request.files['file']
    if file.filename == '':
        flash('Tidak ada file dipilih.', 'danger')
        return redirect(url_for('audios'))
    if not allowed_file(file.filename):
        flash('Hanya file .mp3 yang diizinkan.', 'danger')
        return redirect(url_for('audios'))
    original_name = secure_filename(file.filename)
    unique_name = f'{uuid.uuid4().hex}.mp3'
    upload_folder = app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, unique_name)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'Gagal menyimpan file: {e}', 'danger')
        return redirect(url_for('audios'))
    audio = Audio(filename=unique_name, original_name=original_name,
                  filepath=filepath, uploaded_by=current_user.id)
    db.session.add(audio)
    db.session.commit()
    flash(f'"{original_name}" berhasil diupload.', 'success')
    return redirect(url_for('audios'))


@app.route('/audios/delete/<int:audio_id>', methods=['POST'])
@login_required
def delete_audio(audio_id):
    audio = db.session.get(Audio, audio_id)
    if not audio:
        abort(404)
    # Admin can delete anyone's audio; user can only delete their own
    if not is_admin() and audio.uploaded_by != current_user.id:
        abort(403)
    if Schedule.query.filter_by(audio_id=audio_id).count() > 0:
        flash('Tidak bisa dihapus: audio dipakai oleh jadwal aktif.', 'danger')
        return redirect(url_for('audios'))
    if os.path.exists(audio.filepath):
        try:
            os.remove(audio.filepath)
        except OSError as e:
            flash(f'Gagal hapus file: {e}', 'danger')
            return redirect(url_for('audios'))
    db.session.delete(audio)
    db.session.commit()
    flash(f'"{audio.original_name}" dihapus.', 'success')
    return redirect(url_for('audios'))


# ---------------------------------------------------------------------------
# Routes — Schedule Management (admin + user)
# ---------------------------------------------------------------------------
@app.route('/schedules/new', methods=['GET', 'POST'])
@login_required
@member_required
def new_schedule():
    audio_list = Audio.query.order_by(Audio.original_name).all()
    if request.method == 'POST':
        audio_id = request.form.get('audio_id', type=int)
        time_str = request.form.get('time', '')
        days = request.form.getlist('days')
        label = request.form.get('label', '').strip()
        errors = []
        if not audio_id: errors.append('Pilih file audio.')
        if not time_str: errors.append('Tentukan waktu alarm.')
        if not days:    errors.append('Pilih minimal satu hari.')
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('schedule_form.html', audios=audio_list, schedule=None)
        try:
            hour, minute = map(int, time_str.split(':'))
        except ValueError:
            flash('Format waktu tidak valid.', 'danger')
            return render_template('schedule_form.html', audios=audio_list, schedule=None)
        schedule = Schedule(
            audio_id=audio_id, time_hour=hour, time_minute=minute,
            days_of_week=','.join(sorted(set(days))),
            label=label or None, is_active=True,
            created_by=current_user.id
        )
        db.session.add(schedule)
        db.session.commit()
        add_scheduler_job(schedule)
        flash('Jadwal berhasil dibuat.', 'success')
        return redirect(url_for('index'))
    return render_template('schedule_form.html', audios=audio_list, schedule=None)


@app.route('/schedules/edit/<int:schedule_id>', methods=['GET', 'POST'])
@login_required
@member_required
def edit_schedule(schedule_id):
    schedule = db.session.get(Schedule, schedule_id)
    if not schedule:
        abort(404)
    # Admin can edit any; user can only edit their own
    if not is_admin() and schedule.created_by != current_user.id:
        abort(403)
    audio_list = Audio.query.order_by(Audio.original_name).all()
    if request.method == 'POST':
        audio_id = request.form.get('audio_id', type=int)
        time_str = request.form.get('time', '')
        days = request.form.getlist('days')
        label = request.form.get('label', '').strip()
        errors = []
        if not audio_id: errors.append('Pilih file audio.')
        if not time_str: errors.append('Tentukan waktu alarm.')
        if not days:    errors.append('Pilih minimal satu hari.')
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('schedule_form.html', audios=audio_list, schedule=schedule)
        try:
            hour, minute = map(int, time_str.split(':'))
        except ValueError:
            flash('Format waktu tidak valid.', 'danger')
            return render_template('schedule_form.html', audios=audio_list, schedule=schedule)
        schedule.audio_id = audio_id
        schedule.time_hour = hour
        schedule.time_minute = minute
        schedule.days_of_week = ','.join(sorted(set(days)))
        schedule.label = label or None
        db.session.commit()
        add_scheduler_job(schedule)
        flash('Jadwal berhasil diperbarui.', 'success')
        return redirect(url_for('index'))
    return render_template('schedule_form.html', audios=audio_list, schedule=schedule)


@app.route('/schedules/delete/<int:schedule_id>', methods=['POST'])
@login_required
@member_required
def delete_schedule(schedule_id):
    schedule = db.session.get(Schedule, schedule_id)
    if not schedule:
        abort(404)
    if not is_admin() and schedule.created_by != current_user.id:
        abort(403)
    remove_scheduler_job(schedule_id)
    db.session.delete(schedule)
    db.session.commit()
    flash('Jadwal dihapus.', 'success')
    return redirect(url_for('index'))


@app.route('/schedules/toggle/<int:schedule_id>', methods=['POST'])
@login_required
@admin_required
def toggle_schedule(schedule_id):
    schedule = db.session.get(Schedule, schedule_id)
    if not schedule:
        abort(404)
    schedule.is_active = not schedule.is_active
    db.session.commit()
    if schedule.is_active:
        add_scheduler_job(schedule)
        flash('Jadwal diaktifkan.', 'success')
    else:
        remove_scheduler_job(schedule_id)
        flash('Jadwal dinonaktifkan.', 'warning')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Routes — User Management (admin only)
# ---------------------------------------------------------------------------
@app.route('/users')
@login_required
@admin_required
def users():
    user_list = User.query.order_by(User.created_at.desc()).all()
    return render_template('users.html', users=user_list, config_admin_email=ADMIN_EMAIL)


@app.route('/users/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_user():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        if not name or not email or not password:
            flash('Semua kolom wajib diisi.', 'danger')
        elif len(password) < 6:
            flash('Password minimal 6 karakter.', 'danger')
        elif User.query.filter_by(email=email).first():
            flash('Email sudah terdaftar.', 'danger')
        elif role not in ('admin', 'user'):
            flash('Role tidak valid.', 'danger')
        else:
            user = User(name=name, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f'User "{name}" berhasil dibuat.', 'success')
            return redirect(url_for('users'))
    return render_template('user_form.html', user=None)


@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', 'user')
        password = request.form.get('password', '').strip()
        if not name or not email:
            flash('Nama dan email wajib diisi.', 'danger')
        elif role not in ('admin', 'user'):
            flash('Role tidak valid.', 'danger')
        elif User.query.filter(User.email == email, User.id != user_id).first():
            flash('Email sudah digunakan oleh user lain.', 'danger')
        else:
            user.name = name
            user.email = email
            user.role = role
            if password:
                if len(password) < 6:
                    flash('Password minimal 6 karakter.', 'danger')
                    return render_template('user_form.html', user=user)
                user.set_password(password)
            db.session.commit()
            flash(f'User "{name}" berhasil diperbarui.', 'success')
            return redirect(url_for('users'))
    return render_template('user_form.html', user=user)


@app.route('/users/toggle/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.email == ADMIN_EMAIL:
        flash('Tidak bisa menonaktifkan akun admin utama.', 'danger')
        return redirect(url_for('users'))
    user.is_active = not user.is_active
    db.session.commit()
    status = 'diaktifkan' if user.is_active else 'dinonaktifkan'
    flash(f'User "{user.name}" {status}.', 'success' if user.is_active else 'warning')
    return redirect(url_for('users'))


@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.email == ADMIN_EMAIL:
        flash('Tidak bisa menghapus akun admin utama.', 'danger')
        return redirect(url_for('users'))
    if user.id == current_user.id:
        flash('Tidak bisa menghapus akun sendiri.', 'danger')
        return redirect(url_for('users'))
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{user.name}" dihapus.', 'success')
    return redirect(url_for('users'))


# ---------------------------------------------------------------------------
# Routes — Audio Settings (admin only)
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def audio_settings():
    settings = get_settings()
    devices = get_alsa_devices()
    hw_volume = get_current_volume()
    # Only sync DB ← hardware when amixer can actually read (hw_volume >= 0)
    # hw_volume == -1 means amixer failed; keep the DB value as the source of truth
    if hw_volume >= 0 and settings.volume != hw_volume:
        settings.volume = hw_volume
        db.session.commit()
    if request.method == 'POST':
        device_id = request.form.get('output_device', 'default')
        device_label = request.form.get('device_label', 'Default Output')
        volume = request.form.get('volume', settings.volume, type=int)
        volume = max(0, min(100, volume))
        settings.output_device = device_id
        settings.device_label = device_label
        settings.volume = volume
        db.session.commit()
        set_hardware_volume(volume)
        flash('Pengaturan audio disimpan.', 'success')
        return redirect(url_for('audio_settings'))
    return render_template('settings.html', settings=settings, devices=devices)


@app.route('/settings/volume', methods=['POST'])
@login_required
@admin_required
def set_volume_realtime():
    data = request.get_json(silent=True) or {}
    volume = max(0, min(100, int(data.get('volume', 80))))
    set_hardware_volume(volume)
    settings = get_settings()
    settings.volume = volume
    db.session.commit()
    # Return the requested volume directly (amixer may need a tick to settle)
    return jsonify({'ok': True, 'volume': volume})


@app.route('/settings/test', methods=['POST'])
@login_required
@admin_required
def test_audio():
    if _is_test_playing():
        return jsonify({'ok': False, 'busy': True,
                        'msg': 'Sedang memutar. Stop dulu sebelum test baru.'})
    data = request.get_json(silent=True) or {}
    volume = max(0, min(100, int(data.get('volume', 80))))
    audio = Audio.query.first()
    if not audio:
        return jsonify({'ok': False, 'busy': False, 'msg': 'Belum ada file audio.'})
    filepath = os.path.join(app.root_path, audio.filepath)
    if not os.path.exists(filepath):
        return jsonify({'ok': False, 'busy': False, 'msg': 'File audio tidak ditemukan.'})
    started = play_test_audio(filepath, volume=volume)
    if started:
        return jsonify({'ok': True, 'playing': True,
                        'msg': f'Memutar "{audio.original_name}" — {volume}%'})
    return jsonify({'ok': False, 'busy': True, 'msg': 'Sedang memutar audio.'})


@app.route('/settings/stop', methods=['POST'])
@login_required
@admin_required
def stop_test():
    if _is_test_playing():
        stop_test_audio()
        return jsonify({'ok': True, 'playing': False, 'msg': 'Playback dihentikan.'})
    return jsonify({'ok': True, 'playing': False, 'msg': 'Tidak ada yang sedang diputar.'})


@app.route('/settings/status', methods=['GET'])
@login_required
@admin_required
def test_status():
    return jsonify({'playing': _is_test_playing()})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route('/api/alarm/state')
def api_alarm_state():
    return jsonify(get_alarm_state())


@app.route('/api/alarm/stop', methods=['POST'])
@login_required
def api_alarm_stop():
    stop_alarm()
    return jsonify({'ok': True, 'playing': False})


@app.route('/api/alarm/test_fire', methods=['POST'])
@login_required
@admin_required
def api_alarm_test_fire():
    audio = Audio.query.first()
    if not audio:
        return jsonify({'ok': False, 'msg': 'No audio'})
    filepath = os.path.join(app.root_path, audio.filepath)
    _start_alarm(filepath, volume=get_settings().volume,
                 label=audio.original_name,
                 time_str=datetime.now(WIB).strftime('%H:%M'))
    return jsonify({'ok': True, 'msg': 'Alarm fired'})


@app.route('/api/time')
def api_time():
    now_wib = datetime.now(WIB)
    next_alarms = []
    for j in scheduler.get_jobs():
        if j.next_run_time:
            next_alarms.append(j.next_run_time.astimezone(WIB).strftime('%H:%M'))
    return jsonify({
        'time': now_wib.strftime('%H:%M:%S'),
        'date': now_wib.strftime('%A, %d %B %Y'),
        'timezone': 'WIB (UTC+7)',
        'next_alarms': next_alarms
    })


@app.route('/api/devices')
@login_required
@admin_required
def api_devices():
    return jsonify(get_alsa_devices())


@app.route('/api/audio/health')
def api_audio_health():
    """Returns current audio hardware health — polled by UI."""
    return jsonify(get_audio_health())


@app.route('/api/audio/repair', methods=['POST'])
@login_required
@admin_required
def api_audio_repair():
    """Manually trigger audio repair + health check."""
    ok = run_audio_health_check()
    return jsonify({'ok': ok, 'health': get_audio_health()})


@app.route('/api/audio/log')
@login_required
def api_audio_log():
    """Return the most recent audio event log entries (max 100)."""
    limit = min(int(request.args.get('limit', 100)), 200)
    return jsonify(get_audio_log(limit))


@app.route('/api/audio/test-fire', methods=['POST'])
@login_required
@admin_required
def api_audio_test_fire():
    """Fire a quick alarm test (uses first available audio) to verify real output."""
    audio = Audio.query.first()
    if not audio:
        _log_audio('error', 'TEST_FIRE_FAIL', 'No audio files in library')
        return jsonify({'ok': False, 'msg': 'Tidak ada file audio.'})
    filepath = os.path.join(app.root_path, audio.filepath)
    if not os.path.exists(filepath):
        _log_audio('error', 'TEST_FIRE_FAIL', f'File missing: {audio.filepath}')
        return jsonify({'ok': False, 'msg': 'File audio tidak ditemukan di disk.'})
    h = get_audio_health()
    if not h.get('ok'):
        _log_audio('warn', 'TEST_FIRE_REPAIR', 'Audio not OK, repairing before test fire')
        run_audio_health_check()
    _start_alarm(filepath, volume=get_settings().volume,
                 label=f'[TEST] {audio.original_name}',
                 time_str=datetime.now(WIB).strftime('%H:%M'))
    return jsonify({'ok': True, 'msg': f'Test alarm dibunyikan: {audio.original_name}'})


@app.route('/audio-monitor')
@login_required
@admin_required
def audio_monitor():
    """Realtime audio output monitoring page."""
    return render_template('audio_monitor.html')


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(413)
def too_large(e):
    flash('File terlalu besar. Maksimal 10 MB.', 'danger')
    return redirect(url_for('audios'))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
