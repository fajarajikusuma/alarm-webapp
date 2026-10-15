import os
import uuid
import subprocess
import re
import pytz
import threading as _threading
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
    """Read hardware volume via amixer. Returns 0-100."""
    try:
        out = subprocess.check_output(
            ['amixer', '-c', '1', 'sget', 'Speaker'],
            stderr=subprocess.DEVNULL, text=True
        )
        m = re.search(r'\[(\d+)%\]', out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 80


def set_hardware_volume(percent: int):
    """Set USB audio volume via amixer. Uses raw value for accuracy."""
    percent = max(0, min(100, percent))
    try:
        # Use raw value (0-37 for this C-Media chip) for precise control
        # This avoids rounding issues with percentage-based amixer
        raw = round(percent / 100 * 37)
        subprocess.run(
            ['amixer', '-c', '1', 'sset', 'Speaker', str(raw)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2
        )
    except Exception as e:
        app.logger.error(f'amixer volume set failed: {e}')


def get_settings():
    s = db.session.get(AudioSettings, 1)
    if not s:
        s = AudioSettings(id=1)
        db.session.add(s)
        db.session.commit()
    return s


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


def _start_alarm(filepath: str, volume: int, label: str, time_str: str):
    global _alarm_state
    with _alarm_lock:
        old = _alarm_state.get('process')
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass
        set_hardware_volume(volume)
        try:
            proc = subprocess.Popen(
                ['mpg123', '-q', filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except Exception as e:
            app.logger.error(f'Alarm play error: {e}')
            return
        _alarm_state.update({
            'playing': True, 'label': label, 'time': time_str,
            'started_at': datetime.now(WIB).strftime('%Y-%m-%dT%H:%M:%S'),
            'process': proc,
        })


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
        _alarm_state.update({'playing': False, 'label': '', 'time': '', 'process': None})


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
            return True
        except Exception as e:
            app.logger.error(f'Error playing test audio: {e}')
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
    if settings.volume != hw_volume:
        settings.volume = hw_volume
        db.session.commit()
    if request.method == 'POST':
        device_id = request.form.get('output_device', 'default')
        device_label = request.form.get('device_label', 'Default Output')
        volume = request.form.get('volume', 80, type=int)
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
    _start_alarm(filepath, volume=80,
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
