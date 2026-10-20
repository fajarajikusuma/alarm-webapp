# Web Alarm System

Aplikasi alarm terjadwal berbasis web. Upload MP3, atur jadwal, dan alarm berbunyi otomatis melalui output audio server. Tersedia modal notifikasi di browser saat alarm berbunyi.

---

## Stack

| Komponen | Teknologi |
|---|---|
| Backend | Python 3.10+, Flask 3.x |
| Database | SQLite3 via Flask-SQLAlchemy |
| Auth | Flask-Login |
| Scheduler | APScheduler 3.x (BackgroundScheduler) |
| Audio | `mpg123` via subprocess |
| Frontend | Bootstrap 5.3, Jinja2 |
| Service | systemd |

---

## Hak Akses

| Role | Yang Bisa Dilakukan |
|---|---|
| **Admin** | Semua fitur: kelola user, upload/hapus semua audio, buat/edit/hapus/toggle semua jadwal, konfigurasi audio output |
| **User** | Upload audio sendiri, buat/edit/hapus jadwal sendiri, lihat semua data |

Admin ditentukan dari `ADMIN_EMAIL` di `.env`. Akun pertama yang register dengan email itu otomatis jadi Admin.

---

## A. Instalasi di Ubuntu Server (Bare Metal / VM biasa)

### 1. Syarat sistem

```bash
# Ubuntu 22.04 LTS (minimal)
# Python 3.10+
# USB Audio Device terhubung (opsional, untuk output ke amplifier)
sudo apt update
sudo apt install -y python3 python3-venv python3-pip mpv mpg123 alsa-utils git chrony
```

### 2. Clone / salin project

```bash
cd /home/$USER
git clone <repo-url> alarm-apps
# atau salin folder alarm-apps ke sini
cd alarm-apps
```

### 3. Virtual environment & dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`:
```
Flask==3.0.3
Flask-SQLAlchemy==3.1.1
Flask-Login==0.6.3
APScheduler==3.10.4
python-dotenv==1.0.1
Werkzeug==3.0.3
pytz
```

### 4. Konfigurasi `.env`

```bash
cp .env.example .env   # atau buat manual
nano .env
```

Isi `.env`:
```env
SECRET_KEY=ganti_dengan_string_acak_panjang
ADMIN_EMAIL=email_anda@domain.com
UPLOAD_FOLDER=static/uploads/audios
MAX_CONTENT_LENGTH=10485760
```

- `SECRET_KEY` — string acak apa saja, minimal 32 karakter
- `ADMIN_EMAIL` — email yang akan jadi Admin. Daftarkan email ini lewat halaman Register

### 5. Setup audio (ALSA)

```bash
# Cek apakah USB audio terdeteksi
aplay -l

# Jika tidak ada card, load module
sudo modprobe snd-usb-audio

# Agar auto-load saat boot
echo "snd-usb-audio" | sudo tee -a /etc/modules

# Arahkan ALSA default ke USB audio (ganti card index sesuai output aplay -l)
sudo nano /etc/asound.conf
```

Isi `/etc/asound.conf`:
```
pcm.!default {
    type plug
    slave { pcm "hw:1,0" }
}
ctl.!default {
    type hw
    card 1
}
```

> Ganti `1` dengan nomor card yang sesuai dari output `aplay -l`.

```bash
# Fix permission /dev/snd agar bisa diakses tanpa root
sudo chmod o+rw /dev/snd/*

# Agar permanen saat boot (buat udev rule)
sudo tee /etc/udev/rules.d/99-usb-audio.rules << 'EOF'
SUBSYSTEM=="sound", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="0014", MODE="0666"
EOF
sudo udevadm control --reload-rules
```

### 6. Jalankan manual (test)

```bash
cd /home/$USER/alarm-apps
source venv/bin/activate
python app.py
```

Buka browser: `http://<IP-SERVER>:5000`

---

## B. Setup di Proxmox (VM Ubuntu)

### Syarat tambahan: USB Passthrough

USB audio harus di-passthrough dari host Proxmox ke VM.

**Di Proxmox Web UI:**
1. Stop VM terlebih dahulu
2. Masuk ke VM → **Hardware** → **Add** → **USB Device**
3. Pilih **Use USB Vendor/Device ID**
4. Pilih perangkat audio (contoh: `C-Media Electronics — USB Audio Device`)
5. Klik **Add** → **Start** VM

**Atau via CLI di Proxmox host:**
```bash
# Cek ID USB audio di host
lsusb | grep -i audio

# Tambah ke konfigurasi VM (ganti 104 dengan VMID Anda)
qm set 104 -usb1 host=0d8c:0014
```

Setelah VM nyala, jalankan `aplay -l` di dalam VM — USB audio harus muncul.

### Lanjutkan instalasi

Ikuti langkah **A.1 – A.6** di dalam VM seperti biasa.

---

## C. Setup systemd Service (Auto-start)

Agar aplikasi otomatis jalan saat boot dan restart jika crash.

### 1. Buat prestart script

```bash
nano /home/adminlh/alarm-apps/prestart.sh
chmod +x /home/adminlh/alarm-apps/prestart.sh
```

Script ini dijalankan sebagai root sebelum Flask, bertugas load module audio dan fix permission.

### 2. Install service

```bash
sudo nano /etc/systemd/system/web-alarm.service
```

Isi file service:
```ini
[Unit]
Description=Web Alarm System (Flask + APScheduler)
After=network.target sound.target time-sync.target alsa-restore.service systemd-timesyncd.service
Wants=sound.target time-sync.target
StartLimitBurst=10
StartLimitIntervalSec=120s

[Service]
Type=simple
User=adminlh
Group=adminlh
WorkingDirectory=/home/adminlh/alarm-apps
ExecStartPre=+/bin/bash /home/adminlh/alarm-apps/prestart.sh
ExecStart=/home/adminlh/alarm-apps/venv/bin/python /home/adminlh/alarm-apps/app.py
Environment="PATH=/home/adminlh/alarm-apps/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="HOME=/home/adminlh"
Environment="USER=adminlh"
Environment="XDG_RUNTIME_DIR=/run/user/1000"
EnvironmentFile=/home/adminlh/alarm-apps/.env
Restart=on-failure
RestartSec=5s
SupplementaryGroups=audio
DeviceAllow=/dev/snd rw
KillMode=process
StandardOutput=journal
StandardError=journal
SyslogIdentifier=web-alarm

[Install]
WantedBy=multi-user.target
```

> Sesuaikan `User`, `Group`, `WorkingDirectory`, dan path dengan username Anda.

### 3. Aktifkan service

```bash
sudo systemctl daemon-reload
sudo systemctl enable web-alarm.service
sudo systemctl start web-alarm.service

# Cek status
sudo systemctl status web-alarm.service
```

### 4. Perintah service

```bash
sudo systemctl start   web-alarm    # jalankan
sudo systemctl stop    web-alarm    # hentikan
sudo systemctl restart web-alarm    # restart
sudo systemctl status  web-alarm    # cek status
sudo journalctl -u web-alarm -f     # lihat log realtime
```

---

## D. Penggunaan Aplikasi

### 1. Pertama kali login

1. Buka `http://<IP>:5000`
2. Klik **Daftar** — isi nama, email (gunakan `ADMIN_EMAIL` dari `.env`), password
3. Login → otomatis masuk sebagai **Admin**

### 2. Upload audio

- Menu **Audio Library** → pilih file `.mp3` → klik **Upload**
- Maksimal 10 MB per file
- Semua user (admin dan user biasa) bisa upload audio milik sendiri

### 3. Buat jadwal alarm

- Menu **Buat Jadwal**
- Isi: label (opsional), pilih audio, set waktu, centang hari
- Gunakan tombol preset: **Senin–Jumat**, **Weekend**, **Setiap Hari**
- Klik **Buat Jadwal**

> Waktu mengacu pada **WIB (UTC+7)**. Jam server tampil realtime di Dashboard.

### 4. Kelola jadwal (Admin)

- **Pause/Resume** — toggle aktif/nonaktif tanpa hapus jadwal
- **Edit** — ubah audio, waktu, atau hari
- **Hapus** — hapus permanen

### 5. Saat alarm berbunyi

- Modal notifikasi muncul otomatis di semua browser yang sedang buka aplikasi
- Polling setiap 2 detik — jika buka browser saat alarm sedang berbunyi, modal langsung muncul
- Klik **Hentikan Alarm** untuk stop

### 6. Audio Output (Admin)

- Menu **Audio Output**
- Pilih perangkat output (otomatis deteksi dari `/proc/asound/cards`)
- Atur volume dengan slider — langsung apply ke hardware tanpa simpan dulu
- Tombol **Test Bunyi** untuk verifikasi suara keluar

### 7. Kelola User (Admin)

- Menu **Kelola User**
- **Tambah User** — buat akun baru, pilih role Admin atau User
- **Edit** — ubah nama, email, role, atau reset password
- **Toggle** — nonaktifkan akun (tidak bisa login tapi data tetap ada)
- **Hapus** — hapus permanen (tidak bisa hapus akun sendiri atau admin utama)

### 8. Toggle tema

- Klik ikon bulan/matahari di navbar kanan atas
- Pilihan: **Dark Mode** (default) atau **Light Mode**
- Tersimpan di browser, tidak hilang saat refresh

---

## E. Troubleshooting

| Masalah | Solusi |
|---|---|
| `aplay -l` tidak menampilkan audio card | `sudo modprobe snd-usb-audio` |
| Alarm tidak bunyi | Cek `sudo journalctl -u web-alarm -n 50` |
| Volume tidak bisa di atas 80% | Gunakan slider di halaman Audio Output, bukan amixer manual |
| mpg123 crash / zombie process | `sudo pkill -9 mpg123` lalu restart service |
| Service gagal start setelah reboot | Cek `/etc/alsa/conf.d/50-jack.conf` — rename ke `.disabled` |
| USB audio tidak terdeteksi di VM | Pastikan USB passthrough sudah dikonfigurasi di Proxmox |
| Port 5000 sudah dipakai | Edit `app.py` baris terakhir: `app.run(port=5001)` |

### Cek audio manual

```bash
# Cek card tersedia
cat /proc/asound/cards

# Test bunyi langsung
mpg123 /path/ke/file.mp3

# Cek volume hardware
amixer -c 1 sget 'Speaker'

# Set volume 100%
amixer -c 1 sset 'Speaker' 37
```

---

## F. Struktur Direktori

```
alarm-apps/
├── app.py                  # Flask app, routes, APScheduler, audio logic
├── models.py               # SQLAlchemy models (User, Audio, Schedule, AudioSettings)
├── requirements.txt        # Python dependencies
├── .env                    # Konfigurasi environment (jangan di-commit ke git)
├── prestart.sh             # Script pre-start systemd (load audio, fix permission)
├── start.sh                # Helper: start/stop/status/log service
├── instance/
│   └── alarm.db            # SQLite database (auto-generated)
├── static/
│   └── uploads/
│       └── audios/         # File MP3 yang diupload
└── templates/
    ├── base.html           # Layout utama, navbar, theme toggle, alarm modal
    ├── index.html          # Dashboard
    ├── login.html          # Halaman login
    ├── register.html       # Halaman registrasi
    ├── audios.html         # Audio library
    ├── schedule_form.html  # Form buat/edit jadwal
    ├── settings.html       # Audio output settings
    ├── users.html          # Daftar user
    ├── user_form.html      # Form tambah/edit user
    ├── 403.html            # Halaman forbidden
    └── 404.html            # Halaman not found
```

---

## G. Keamanan (Produksi)

```bash
# Ganti SECRET_KEY dengan nilai acak yang kuat
python3 -c "import secrets; print(secrets.token_hex(32))"

# Gunakan WSGI server (Gunicorn) untuk produksi, bukan Flask dev server
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 app:app

# Pasang reverse proxy (Nginx) dengan HTTPS
# Batasi akses port 5000 hanya dari loopback jika pakai Nginx
```
