"""
warna_endpoint.py - REVISED VERSION
====================================
Perubahan dari versi sebelumnya:
1. insert_sensor_db() → UPDATE row terakhir (bukan INSERT baru)
2. Fix kolom pompa_normal → pompa_nutrisi
3. Fix PH_NORMAL_MAX 9.0 → 10.5
4. Tambah vol_nutrisi ke UPDATE DB
5. Fix get_waktu_pompa_terakhir() cek kolom pompa_nutrisi
6. Fix fase_sebelumnya logic
7. Fix fallback baca DB kalau hasil_warna.json hilang
8. Hapus persentase_warna dari semua query
9. Koneksi DB pakai environment variable Railway
"""

import os
import io
import json
import threading
import subprocess
import sys
import numpy as np
import cv2
import pymysql
import pymysql.cursors
from flask import Flask, request, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)

# =============================================
# KONFIGURASI POMPA NUTRISI
# =============================================
TESTING_MODE         = True
INTERVAL_POMPA_DETIK = 3 * 60          # 3 menit (testing)
# INTERVAL_POMPA_DETIK = 3 * 24 * 3600 # 3 hari (produksi)

V_MEDIA_LITER = 45.0

KONSTANTA_NUTRISI = {
    "Fase 1: Pembibitan" : 0.2,
    "Fase 2: Pertumbuhan": 0.4,
    "Fase 3: Optimal"    : 0.3,
    "Fase 4: Panen"      : 0.1,
}

URUTAN_FASE = [
    "Fase 1: Pembibitan",
    "Fase 2: Pertumbuhan",
    "Fase 3: Optimal",
    "Fase 4: Panen",
]

# ── FIX: threshold pH sesuai spesifikasi sistem ─────────────────
PH_NORMAL_MIN = 8.5
PH_NORMAL_MAX = 10.5   # sebelumnya 9.0 → SALAH

# =============================================
# KONEKSI DATABASE (env variable Railway)
# =============================================
def get_db():
    return pymysql.connect(
        host    = os.environ.get("MYSQLHOST",     "localhost"),
        port    = int(os.environ.get("MYSQLPORT", "3306")),
        user    = os.environ.get("MYSQLUSER",     "root"),
        password= os.environ.get("MYSQLPASSWORD", ""),
        database= os.environ.get("MYSQLDATABASE", "railway"),
        charset = "utf8mb4",
        cursorclass = pymysql.cursors.DictCursor,
        connect_timeout = 10,
    )

# =============================================
# HELPER DATABASE
# =============================================
def get_ph_terbaru():
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT pH FROM mikroalga_sensor "
                "WHERE pH IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        return float(row["pH"]) if row and row["pH"] is not None else None
    except Exception as e:
        print(f"[DB] Gagal ambil pH: {e}")
        return None

def get_waktu_pompa_nutrisi_terakhir():
    """
    FIX: sebelumnya cek pompa_normal = 'ON' (salah kolom).
    Sekarang cek pompa_nutrisi = 'ON' (benar).
    """
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT waktu FROM mikroalga_sensor "
                "WHERE pompa_nutrisi = 'ON' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        return row["waktu"] if row and row["waktu"] else None
    except Exception as e:
        print(f"[DB] Gagal ambil waktu pompa nutrisi terakhir: {e}")
        return None

def get_fase_db_terakhir():
    """
    Ambil fase warna terakhir yang terdeteksi dari DB.
    Dipakai sebagai fase_sebelumnya untuk deteksi berikutnya.
    """
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT warna FROM mikroalga_sensor "
                "WHERE warna IS NOT NULL "
                "AND warna != '' "
                "AND warna != 'tidak terdeteksi' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        return row["warna"] if row else None
    except Exception as e:
        print(f"[DB] Gagal ambil fase terakhir: {e}")
        return None

def get_id_sensor_terakhir():
    """
    Ambil id row terakhir dari mikroalga_sensor.
    Dipakai untuk UPDATE (bukan INSERT baru).
    """
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT MAX(id) as max_id FROM mikroalga_sensor")
            row = cur.fetchone()
        db.close()
        return row["max_id"] if row and row["max_id"] else None
    except Exception as e:
        print(f"[DB] Gagal ambil id terakhir: {e}")
        return None

# =============================================
# LOGIKA POMPA NUTRISI
# =============================================
def hitung_volume_nutrisi(fase_label):
    k = KONSTANTA_NUTRISI.get(fase_label, 0.0)
    return round(k * V_MEDIA_LITER * 1000, 2)   # hasil dalam mL

def cek_fase_mundur(fase_sekarang, fase_sebelumnya):
    if not fase_sekarang or not fase_sebelumnya:
        return False
    if fase_sekarang not in URUTAN_FASE or fase_sebelumnya not in URUTAN_FASE:
        return False
    return URUTAN_FASE.index(fase_sekarang) < URUTAN_FASE.index(fase_sebelumnya)

def cek_pompa_nutrisi(fase_sekarang, fase_sebelumnya_val):
    if not fase_sekarang or fase_sekarang == "tidak terdeteksi":
        return False, "fase tidak terdeteksi", 0.0

    volume = hitung_volume_nutrisi(fase_sekarang)
    ph     = get_ph_terbaru()

    fase_mundur = cek_fase_mundur(fase_sekarang, fase_sebelumnya_val)

    ph_darurat = False
    if ph is not None:
        ph_darurat = (ph < PH_NORMAL_MIN or ph > PH_NORMAL_MAX)

    waktu_terakhir  = get_waktu_pompa_nutrisi_terakhir()
    sudah_waktunya  = False
    if waktu_terakhir is None:
        sudah_waktunya = True
    else:
        selisih = (datetime.now() - waktu_terakhir).total_seconds()
        sudah_waktunya = selisih >= INTERVAL_POMPA_DETIK

    # Prioritas kondisi
    if fase_mundur and ph_darurat:
        return True, f"DARURAT: fase mundur ({fase_sebelumnya_val}→{fase_sekarang}) + pH={ph}", volume
    if fase_mundur:
        return True, f"DARURAT: fase mundur ({fase_sebelumnya_val}→{fase_sekarang})", volume
    if ph_darurat:
        alasan_ph = f"pH rendah ({ph})" if ph < PH_NORMAL_MIN else f"pH tinggi ({ph})"
        return True, f"DARURAT: {alasan_ph}", volume
    if sudah_waktunya:
        mode = "testing 3 menit" if TESTING_MODE else "produksi 3 hari"
        return True, f"TERJADWAL ({mode})", volume

    return False, "belum waktunya", 0.0

def update_status_pompa_db(pompa_on: bool):
    """Update tabel status_pompa untuk pompa_nutrisi."""
    try:
        db  = get_db()
        val = "ON" if pompa_on else "OFF"
        with db.cursor() as cur:
            cur.execute(
                "UPDATE status_pompa SET pompa_nutrisi = %s WHERE id = 1",
                (val,)
            )
        db.commit()
        db.close()
        print(f"[POMPA] status_pompa.pompa_nutrisi = {val}")
    except Exception as e:
        print(f"[DB] Gagal update status_pompa: {e}")

# =============================================
# UPDATE SENSOR DB (FIX: UPDATE bukan INSERT)
# =============================================
def update_sensor_db(label, status, skor, fase_sebelumnya_val):
    try:
        pompa_on, alasan, volume_ml = cek_pompa_nutrisi(label, fase_sebelumnya_val)
        update_status_pompa_db(pompa_on)

        pompa_nutrisi_val = "ON" if pompa_on else "OFF"

        max_id = get_id_sensor_terakhir()
        if not max_id:
            print("[DB] Tidak ada row untuk di-UPDATE, skip.")
            return False, "tidak ada row", 0.0

        db = get_db()
        with db.cursor() as cur:

            # ── TAHAP 1: Update semua row kosong dengan warna saja ──
            cur.execute(
                """
                UPDATE mikroalga_sensor
                SET
                    warna        = %s,
                    status_warna = %s,
                    pompa_nutrisi = 'OFF'
                WHERE (warna = 'tidak terdeteksi' OR warna IS NULL)
                AND id <= %s
                """,
                (label, status, max_id)
            )

            # ── TAHAP 2: Update row terakhir lengkap dengan pompa ──
            cur.execute(
                """
                UPDATE mikroalga_sensor
                SET
                    warna           = %s,
                    status_warna    = %s,
                    pompa_nutrisi   = %s,
                    vol_nutrisi     = %s,
                    fase_sebelumnya = %s
                WHERE id = %s
                """,
                (
                    label,
                    status,
                    pompa_nutrisi_val,
                    volume_ml,
                    fase_sebelumnya_val,
                    max_id,
                )
            )

        db.commit()
        db.close()

        print(f"[DB] UPDATE semua row kosong → warna={label}")
        print(f"[DB] UPDATE id={max_id} | pompa_nutrisi={pompa_nutrisi_val} | vol={volume_ml}mL | alasan={alasan}")
        return pompa_on, alasan, volume_ml

    except Exception as e:
        print(f"[DB] Gagal UPDATE sensor: {e}")
        return False, "db error", 0.0

# =============================================
# STATE GLOBAL
# =============================================
_lock       = threading.Lock()
_hasil_warna = {
    "warna"       : "tidak terdeteksi",
    "status_warna": "-",
    "skor"        : 0.0,
    "device_id"   : "-",
    "timestamp"   : None,
    "menit_lalu"  : None,
    "bbox"        : None,
}

# =============================================
# PROFIL HSV FALLBACK
# =============================================
PROFIL_FALLBACK = {
    "fase1": {
        "label" : "Fase 1: Pembibitan",
        "lower" : [25, 30, 80],
        "upper" : [45, 120, 200],
        "hist_h": [], "hist_s": [],
    },
    "fase2": {
        "label" : "Fase 2: Pertumbuhan",
        "lower" : [40, 60, 60],
        "upper" : [75, 180, 180],
        "hist_h": [], "hist_s": [],
    },
    "fase3": {
        "label" : "Fase 3: Optimal",
        "lower" : [75, 80, 40],
        "upper" : [100, 220, 160],
        "hist_h": [], "hist_s": [],
    },
    "fase4": {
        "label" : "Fase 4: Panen",
        "lower" : [90, 100, 20],
        "upper" : [120, 255, 100],
        "hist_h": [], "hist_s": [],
    },
}

BOBOT_HSV  = 0.55
BOBOT_HIST = 0.45
SKOR_MIN   = 0.35

# =============================================
# GOOGLE DRIVE
# =============================================
GDRIVE_PARENT_FOLDER_ID = "17ISXne7N15wOEdZwwdW_lGtJWGUHiTbc"
_gdrive_folder_cache    = {}
_gdrive_lock            = threading.Lock()

def _get_gdrive_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_json:
        raise ValueError("Env variable GOOGLE_TOKEN_JSON tidak ditemukan")
    token_data = json.loads(token_json)
    creds = Credentials(
        token         = token_data.get("token"),
        refresh_token = token_data.get("refresh_token"),
        token_uri     = token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id     = token_data.get("client_id"),
        client_secret = token_data.get("client_secret"),
        scopes        = token_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)

def _get_atau_buat_folder(service, nama_folder, parent_id):
    query  = (f"name='{nama_folder}' and '{parent_id}' in parents "
              f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    hasil  = service.files().list(q=query, fields="files(id, name)").execute()
    files  = hasil.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": nama_folder,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]}
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def upload_ke_gdrive(jpeg_bytes: bytes, device_id: str, timestamp: str):
    try:
        from googleapiclient.http import MediaIoBaseUpload
        service = _get_gdrive_service()
        try:
            ts = datetime.fromisoformat(timestamp)
        except Exception:
            ts = datetime.now()
        nama_folder = ts.strftime("%d-%m-%Y")
        nama_file   = ts.strftime("%H%M%S") + f"_{device_id}.jpg"
        with _gdrive_lock:
            if nama_folder not in _gdrive_folder_cache:
                folder_id = _get_atau_buat_folder(service, nama_folder, GDRIVE_PARENT_FOLDER_ID)
                _gdrive_folder_cache[nama_folder] = folder_id
            else:
                folder_id = _gdrive_folder_cache[nama_folder]
        metadata = {"name": nama_file, "parents": [folder_id]}
        media    = MediaIoBaseUpload(io.BytesIO(jpeg_bytes), mimetype="image/jpeg", resumable=False)
        uploaded = service.files().create(body=metadata, media_body=media, fields="id, name").execute()
        print(f"[GDRIVE] Upload sukses: {nama_folder}/{nama_file} → {uploaded['id']}")
    except Exception as e:
        print(f"[GDRIVE ERROR] {e}")

# =============================================
# LOAD PROFIL HSV
# =============================================
def load_profil():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profil_hsv.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        print(f"[PROFIL] Dimuat: {list(data.keys())}")
        return data
    print("[PROFIL] Pakai profil fallback.")
    return PROFIL_FALLBACK

profil_data = load_profil()

# =============================================
# FUNGSI DETEKSI WARNA (OpenCV)
# =============================================
def buat_mask_kaca(hsv_img):
    S    = hsv_img[:, :, 1]
    V    = hsv_img[:, :, 2]
    mask = ((S > 30) & (V > 25) & (V < 245)).astype(np.uint8) * 255
    k    = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,   k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_DILATE, k)
    return mask

def skor_hsv(frame_hsv, mask_kaca, lower, upper):
    total = cv2.countNonZero(mask_kaca)
    if total == 0:
        return 0.0
    mask_hsv = cv2.inRange(frame_hsv,
                           np.array(lower, dtype=np.uint8),
                           np.array(upper, dtype=np.uint8))
    mask_hsv = cv2.bitwise_and(mask_hsv, mask_kaca)
    k        = np.ones((5, 5), np.uint8)
    mask_hsv = cv2.morphologyEx(mask_hsv, cv2.MORPH_OPEN,   k)
    mask_hsv = cv2.morphologyEx(mask_hsv, cv2.MORPH_DILATE, k)
    return cv2.countNonZero(mask_hsv) / total

def skor_histogram(frame_hsv, mask_kaca, hist_h_ref, hist_s_ref):
    if not hist_h_ref or not hist_s_ref:
        return 0.0
    valid_px = frame_hsv[mask_kaca == 255]
    if len(valid_px) < 100:
        return 0.0
    h_arr    = valid_px[:, 0]
    s_arr    = valid_px[:, 1]
    hist_h_f = cv2.calcHist([h_arr], [0], None, [180], [0, 180])
    hist_s_f = cv2.calcHist([s_arr], [0], None, [256], [0, 256])
    cv2.normalize(hist_h_f, hist_h_f)
    cv2.normalize(hist_s_f, hist_s_f)
    ref_h = np.array(hist_h_ref, dtype=np.float32).reshape(-1, 1)
    ref_s = np.array(hist_s_ref, dtype=np.float32).reshape(-1, 1)
    cv2.normalize(ref_h, ref_h)
    cv2.normalize(ref_s, ref_s)
    dist_h = cv2.compareHist(hist_h_f, ref_h, cv2.HISTCMP_BHATTACHARYYA)
    dist_s = cv2.compareHist(hist_s_f, ref_s, cv2.HISTCMP_BHATTACHARYYA)
    return ((1 - dist_h) + (1 - dist_s)) / 2.0

def hitung_bbox(frame_hsv, mask_kaca, lower, upper):
    mask_fase = cv2.inRange(frame_hsv,
                            np.array(lower, dtype=np.uint8),
                            np.array(upper, dtype=np.uint8))
    mask_fase = cv2.bitwise_and(mask_fase, mask_kaca)
    kernel    = np.ones((7, 7), np.uint8)
    mask_fase = cv2.morphologyEx(mask_fase, cv2.MORPH_OPEN,   kernel)
    mask_fase = cv2.morphologyEx(mask_fase, cv2.MORPH_DILATE, kernel)
    contours, _ = cv2.findContours(mask_fase, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest   = max(contours, key=cv2.contourArea)
    area      = cv2.contourArea(largest)
    h_frame, w_frame = frame_hsv.shape[:2]
    if area < (w_frame * h_frame * 0.01):
        return None
    x, y, w, h = cv2.boundingRect(largest)
    return {
        "x": round(x / w_frame, 4),
        "y": round(y / h_frame, 4),
        "w": round(w / w_frame, 4),
        "h": round(h / h_frame, 4),
    }

def deteksi_warna(jpeg_bytes: bytes):
    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame     = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if frame is None:
        return "tidak terdeteksi", "-", 0.0, None
    hsv       = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask_kaca = buat_mask_kaca(hsv)
    hasil     = []
    for fase_key, profil in profil_data.items():
        s_hsv  = skor_hsv(hsv, mask_kaca, profil["lower"], profil["upper"])
        s_hist = skor_histogram(hsv, mask_kaca,
                                profil.get("hist_h", []),
                                profil.get("hist_s", []))
        skor   = BOBOT_HSV * s_hsv + BOBOT_HIST * s_hist
        hasil.append((skor, profil["label"], fase_key))
    hasil.sort(reverse=True)
    if hasil and hasil[0][0] >= SKOR_MIN:
        skor_terbaik, label, fase_key_terbaik = hasil[0]
        STATUS_MAP = {
            "Fase 1: Pembibitan" : "pembibitan",
            "Fase 2: Pertumbuhan": "pertumbuhan",
            "Fase 3: Optimal"    : "optimal",
            "Fase 4: Panen"      : "siap panen",
        }
        status        = STATUS_MAP.get(label, "-")
        profil_terbaik = profil_data[fase_key_terbaik]
        bbox          = hitung_bbox(hsv, mask_kaca,
                                    profil_terbaik["lower"],
                                    profil_terbaik["upper"])
        return label, status, round(skor_terbaik, 3), bbox
    return "tidak terdeteksi", "-", 0.0, None

# =============================================
# SIMPAN / BACA HASIL (JSON cache lokal)
# =============================================
HASIL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hasil_warna.json")

def simpan_hasil(data: dict):
    try:
        with open(HASIL_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[JSON] Gagal simpan hasil_warna.json: {e}")

def baca_hasil() -> dict:
    """
    FIX: fallback ke DB kalau file JSON tidak ada (Railway restart).
    """
    # Coba baca dari file lokal dulu
    if os.path.exists(HASIL_PATH):
        try:
            with open(HASIL_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass

    # Fallback: baca dari DB
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT warna, status_warna, waktu "
                "FROM mikroalga_sensor "
                "WHERE warna IS NOT NULL AND warna != 'tidak terdeteksi' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        if row:
            return {
                "warna"       : row["warna"],
                "status_warna": row["status_warna"] or "-",
                "skor"        : 0.0,
                "device_id"   : "-",
                "timestamp"   : row["waktu"].isoformat() if row["waktu"] else None,
                "menit_lalu"  : None,
                "bbox"        : None,
            }
    except Exception as e:
        print(f"[DB] Fallback baca hasil gagal: {e}")

    return _hasil_warna.copy()

# =============================================
# ROUTES
# =============================================
@app.route("/upload_foto", methods=["POST"])
def upload_foto():
    try:
        device_id = request.form.get("device_id", "unknown")

        if "foto" not in request.files:
            return jsonify({"ok": False, "error": "Tidak ada field 'foto'"}), 400

        jpeg_bytes = request.files["foto"].read()
        if len(jpeg_bytes) < 100:
            return jsonify({"ok": False, "error": "File terlalu kecil"}), 400

        print(f"[UPLOAD] {device_id} — {len(jpeg_bytes)} bytes")

        # Ambil fase DB terakhir SEBELUM deteksi → ini akan jadi fase_sebelumnya
        fase_sebelumnya_val = get_fase_db_terakhir()

        # Deteksi warna + bbox
        label, status, skor, bbox = deteksi_warna(jpeg_bytes)

        timestamp_now = datetime.now().isoformat()

        hasil = {
            "warna"       : label,
            "status_warna": status,
            "skor"        : skor,
            "device_id"   : device_id,
            "timestamp"   : timestamp_now,
            "bbox"        : bbox,
        }

        simpan_hasil(hasil)
        with _lock:
            _hasil_warna.update(hasil)

        print(f"[DETEKSI] {label} | {status} | skor={skor} | fase_sebelumnya={fase_sebelumnya_val}")

        # UPDATE row terakhir di DB
        pompa_on, alasan, volume_ml = update_sensor_db(
            label, status, skor, fase_sebelumnya_val
        )

        hasil["pompa_nutrisi"] = "ON" if pompa_on else "OFF"
        hasil["pompa_alasan"]  = alasan
        hasil["volume_ml"]     = volume_ml
        simpan_hasil(hasil)

        # Upload foto ke Google Drive (background thread)
        threading.Thread(
            target=upload_ke_gdrive,
            args=(jpeg_bytes, device_id, timestamp_now),
            daemon=True,
        ).start()

        return jsonify({
            "ok"          : True,
            "warna"       : label,
            "status_warna": status,
            "skor"        : skor,
            "bbox"        : bbox,
            "pompa_nutrisi": "ON" if pompa_on else "OFF",
            "volume_ml"   : volume_ml,
            "alasan"      : alasan,
        })

    except Exception as e:
        print(f"[ERROR /upload_foto] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/status_pompa_nutrisi", methods=["GET"])
def status_pompa_nutrisi():
    try:
        data      = baca_hasil()
        fase      = data.get("warna", "tidak terdeteksi")
        fase_sblm = get_fase_db_terakhir()
        pompa_on, alasan, volume_ml = cek_pompa_nutrisi(fase, fase_sblm)
        return jsonify({
            "pompa_nutrisi": "ON" if pompa_on else "OFF",
            "volume_ml"    : volume_ml,
            "alasan"       : alasan,
            "fase"         : fase,
            "interval_mode": "testing" if TESTING_MODE else "produksi",
        })
    except Exception as e:
        print(f"[ERROR /status_pompa_nutrisi] {e}")
        return jsonify({"pompa_nutrisi": "OFF", "error": str(e)}), 500


@app.route("/hasil_warna", methods=["GET"])
def hasil_warna():
    data = baca_hasil()
    if data.get("timestamp"):
        try:
            ts            = datetime.fromisoformat(data["timestamp"])
            selisih       = (datetime.now() - ts).total_seconds()
            data["menit_lalu"] = int(selisih / 60)
        except Exception:
            data["menit_lalu"] = None
    return jsonify(data)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status"        : "ok",
        "timestamp"     : datetime.now().isoformat(),
        "testing_mode"  : TESTING_MODE,
        "interval_detik": INTERVAL_POMPA_DETIK,
    })

# =============================================
# MAIN — Flask + Bot Telegram
# =============================================
def run_bot():
    import time
    print("[BOT] Menunggu 20 detik sebelum start...")
    time.sleep(20)
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_telegram.py")
    while True:
        print("[BOT] Memulai bot_telegram.py...")
        process = subprocess.Popen(
            [sys.executable, bot_path],
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        process.wait()
        print("[BOT] bot_telegram.py berhenti! Restart dalam 30 detik...")
        time.sleep(30)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 50)
    print("Warna Endpoint REVISED — Mikroalga Spirulina")
    print("=" * 50)
    print(f"[FLASK] Port  : {port}")
    print(f"[MODE]  {'TESTING (3 menit)' if TESTING_MODE else 'PRODUKSI (3 hari)'}")
    print(f"[pH]    Normal: {PH_NORMAL_MIN} – {PH_NORMAL_MAX}")

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
