from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    audios = db.relationship('Audio', backref='uploader', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.email}>'


class Audio(db.Model):
    __tablename__ = 'audios'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256), nullable=False)       # UUID-based stored name
    original_name = db.Column(db.String(256), nullable=False)  # Original upload name
    filepath = db.Column(db.String(512), nullable=False)       # Full relative path
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    schedules = db.relationship('Schedule', backref='audio', lazy=True)

    def __repr__(self):
        return f'<Audio {self.original_name}>'


class Schedule(db.Model):
    __tablename__ = 'schedules'

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120), nullable=True)           # Optional friendly name
    audio_id = db.Column(db.Integer, db.ForeignKey('audios.id'), nullable=False)
    time_hour = db.Column(db.Integer, nullable=False)
    time_minute = db.Column(db.Integer, nullable=False)
    # Comma-separated day numbers: 0=Mon, 1=Tue, ..., 6=Sun (APScheduler convention)
    days_of_week = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def days_list(self):
        """Return days as a list of integers."""
        return [int(d) for d in self.days_of_week.split(',') if d.strip()]

    def days_display(self):
        """Return human-readable day names."""
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        return ', '.join(day_names[d] for d in self.days_list())

    def time_display(self):
        """Return formatted time string HH:MM."""
        return f'{self.time_hour:02d}:{self.time_minute:02d}'

    def __repr__(self):
        return f'<Schedule id={self.id} {self.time_display()}>'


class AudioSettings(db.Model):
    """Global audio output settings (singleton row, id=1)."""
    __tablename__ = 'audio_settings'

    id = db.Column(db.Integer, primary_key=True)
    # ALSA device string e.g. 'default', 'hw:0,0', 'hw:1,0'
    output_device = db.Column(db.String(64), default='default', nullable=False)
    # Volume 0-100
    volume = db.Column(db.Integer, default=80, nullable=False)
    # Human-readable label for the device
    device_label = db.Column(db.String(128), default='Default Output', nullable=False)
