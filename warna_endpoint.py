"""
warna_endpoint.py - FINAL VERSION + Pompa Nutrisi + Bounding Box
====================================
Flask endpoint + Bot Telegram dalam satu process.
- Flask terima foto dari ESP32-CAM (POST /upload_foto)
- Bot Telegram jalan sebagai subprocess
- Hasil deteksi disimpan ke hasil_warna.json DAN MySQL
- Setiap foto diupload ke Google Drive (folder per tanggal DD-MM-YYYY)
- Logika pompa nutrisi: terjadwal + darurat
- Bounding box koordinat normalized disimpan ke hasil_warna.json
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
#  KONFIGURASI POMPA NUTRISI
# =============================================

TESTING_MODE = True

INTERVAL_POMPA_DETIK = 3 * 60          # 3 menit (testing)
# INTERVAL_POMPA_DETIK = 3 * 24 * 3600  # 3 hari (produksi)

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

PH_NORMAL_MIN = 8.5
PH_NORMAL_MAX = 9.0

# =============================================
#  KONEKSI DATABASE
# =============================================

def get_db():
    return pymysql.connect(
        host     = os.environ.get("MYSQLHOST",     "localhost"),
        port     = int(os.environ.get("MYSQLPORT", "3306")),
        user     = os.environ.get("MYSQLUSER",     "root"),
        password = os.environ.get("MYSQLPASSWORD", ""),
        database = os.environ.get("MYSQLDATABASE", "railway"),
        charset  = "utf8mb4",
        cursorclass = pymysql.cursors.DictCursor,
        connect_timeout = 10,
    )

# =============================================
#  LOGIKA POMPA NUTRISI
# =============================================

def get_ph_terbaru():
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT pH FROM mikroalga_sensor "
                "WHERE pH IS NOT NULL "
                "ORDER BY waktu DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        if row and row["pH"] is not None:
            return float(row["pH"])
        return None
    except Exception as e:
        print(f"[DB] Gagal ambil pH: {e}")
        return None


def get_waktu_pompa_terakhir():
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT waktu FROM mikroalga_sensor "
                "WHERE pompa_normal = 'ON' "
                "ORDER BY waktu DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        if row and row["waktu"]:
            return row["waktu"]
        return None
    except Exception as e:
        print(f"[DB] Gagal ambil waktu pompa terakhir: {e}")
        return None


def get_fase_sebelumnya():
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT warna, fase_sebelumnya FROM mikroalga_sensor "
                "WHERE warna IS NOT NULL AND warna != 'tidak terdeteksi' "
                "ORDER BY waktu DESC LIMIT 1"
            )
            row = cur.fetchone()
        db.close()
        if row:
            return row.get("fase_sebelumnya"), row.get("warna")
        return None, None
    except Exception as e:
        print(f"[DB] Gagal ambil fase sebelumnya: {e}")
        return None, None


def hitung_volume_nutrisi(fase_label):
    k = KONSTANTA_NUTRISI.get(fase_label, 0.0)
    return round(k * V_MEDIA_LITER, 2)


def cek_fase_mundur(fase_sekarang, fase_sebelumnya):
    if not fase_sekarang or not fase_sebelumnya:
        return False
    if fase_sekarang not in URUTAN_FASE or fase_sebelumnya not in URUTAN_FASE:
        return False
    return URUTAN_FASE.index(fase_sekarang) < URUTAN_FASE.index(fase_sebelumnya)


def cek_pompa_nutrisi(fase_sekarang):
    if not fase_sekarang or fase_sekarang == "tidak terdeteksi":
        return False, "fase tidak terdeteksi", 0.0

    volume = hitung_volume_nutrisi(fase_sekarang)
    ph     = get_ph_terbaru()

    fase_sebelumnya_db, _ = get_fase_sebelumnya()
    fase_mundur = cek_fase_mundur(fase_sekarang, fase_sebelumnya_db)

    ph_darurat = False
    if ph is not None:
        ph_darurat = (ph < PH_NORMAL_MIN or ph > PH_NORMAL_MAX)

    waktu_terakhir = get_waktu_pompa_terakhir()
    sudah_waktunya = False
    if waktu_terakhir is None:
        sudah_waktunya = True
    else:
        selisih = (datetime.now() - waktu_terakhir).total_seconds()
        sudah_waktunya = selisih >= INTERVAL_POMPA_DETIK

    if fase_mundur and ph_darurat:
        return True, f"DARURAT: fase mundur ({fase_sebelumnya_db}→{fase_sekarang}) + pH={ph}", volume
    if fase_mundur:
        return True, f"DARURAT: fase mundur ({fase_sebelumnya_db}→{fase_sekarang})", volume
    if ph_darurat:
        alasan_ph = f"pH rendah ({ph})" if ph < PH_NORMAL_MIN else f"pH tinggi ({ph})"
        return True, f"DARURAT: {alasan_ph}", volume
    if sudah_waktunya:
        mode = "testing 3 menit" if TESTING_MODE else "produksi 3 hari"
        return True, f"TERJADWAL ({mode})", volume

    return False, "belum waktunya", 0.0


def update_status_pompa_db(pompa_on: bool, volume_ml: float, alasan: str):
    try:
        db = get_db()
        status = "ON" if pompa_on else "OFF"
        with db.cursor() as cur:
            cur.execute(
                "UPDATE status_pompa SET pompa_normal = %s WHERE id = 1",
                (status,)
            )
        db.commit()
        db.close()
        print(f"[POMPA] Status DB diupdate: {status} | {alasan} | {volume_ml} mL")
    except Exception as e:
        print(f"[DB] Gagal update status pompa: {e}")

# =============================================
#  INSERT SENSOR KE DATABASE
# =============================================

def insert_sensor_db(label, status, skor, fase_sebelumnya_val):
    try:
        pompa_on, alasan, volume_ml = cek_pompa_nutrisi(label)
        update_status_pompa_db(pompa_on, volume_ml, alasan)
        pompa_normal_val = "ON" if pompa_on else "IDLE"

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mikroalga_sensor
                    (warna, status_warna, persentase_warna,
                     pompa_normal, fase_sebelumnya)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    label,
                    status,
                    round(skor * 100, 1),
                    pompa_normal_val,
                    fase_sebelumnya_val,
                )
            )
        db.commit()
        db.close()
        print(f"[DB] INSERT warna OK | pompa_normal={pompa_normal_val} | {alasan}")

        if pompa_on:
            print(f"[POMPA] AKTIF | Alasan: {alasan} | Volume: {volume_ml} mL")
        return pompa_on, alasan, volume_ml

    except Exception as e:
        print(f"[DB] Gagal INSERT sensor: {e}")
        return False, "db error", 0.0

# =============================================
#  STATE
# =============================================

_lock        = threading.Lock()
_hasil_warna = {
    "warna"        : "tidak terdeteksi",
    "status_warna" : "-",
    "skor"         : 0.0,
    "device_id"    : "-",
    "timestamp"    : None,
    "menit_lalu"   : None,
    "bbox"         : None,
}

# =============================================
#  PROFIL HSV FALLBACK
# =============================================

PROFIL_FALLBACK = {
    "fase1": {
        "label" : "Fase 1: Pembibitan",
        "lower" : [25, 30, 80],
        "upper" : [45, 120, 200],
        "hist_h": [],
        "hist_s": [],
    },
    "fase2": {
        "label" : "Fase 2: Pertumbuhan",
        "lower" : [40, 60, 60],
        "upper" : [75, 180, 180],
        "hist_h": [],
        "hist_s": [],
    },
    "fase3": {
        "label" : "Fase 3: Optimal",
        "lower" : [75, 80, 40],
        "upper" : [100, 220, 160],
        "hist_h": [],
        "hist_s": [],
    },
    "fase4": {
        "label" : "Fase 4: Panen",
        "lower" : [90, 100, 20],
        "upper" : [120, 255, 100],
        "hist_h": [],
        "hist_s": [],
    },
}

BOBOT_HSV  = 0.55
BOBOT_HIST = 0.45
SKOR_MIN   = 0.35

# =============================================
#  GOOGLE DRIVE CONFIG
# =============================================

GDRIVE_PARENT_FOLDER_ID = "17ISXne7N15wOEdZwwdW_lGtJWGUHiTbc"

_gdrive_folder_cache = {}
_gdrive_lock = threading.Lock()


def _get_gdrive_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_json:
        raise ValueError("Env variable GOOGLE_TOKEN_JSON tidak ditemukan")

    token_data = json.loads(token_json)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def _get_atau_buat_folder(service, nama_folder, parent_id):
    query = (
        f"name='{nama_folder}' "
        f"and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    hasil = service.files().list(q=query, fields="files(id, name)").execute()
    files = hasil.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": nama_folder,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    print(f"[GDRIVE] Folder baru dibuat: {nama_folder} → {folder['id']}")
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
        media = MediaIoBaseUpload(
            io.BytesIO(jpeg_bytes), mimetype="image/jpeg", resumable=False
        )
        uploaded = service.files().create(
            body=metadata, media_body=media, fields="id, name"
        ).execute()
        print(f"[GDRIVE] Upload sukses: {nama_folder}/{nama_file} → {uploaded['id']}")
    except Exception as e:
        print(f"[GDRIVE ERROR] {e}")

# =============================================
#  LOAD PROFIL
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
#  FUNGSI DETEKSI
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
    ref_h    = np.array(hist_h_ref, dtype=np.float32).reshape(-1, 1)
    ref_s    = np.array(hist_s_ref, dtype=np.float32).reshape(-1, 1)
    cv2.normalize(ref_h, ref_h)
    cv2.normalize(ref_s, ref_s)
    dist_h   = cv2.compareHist(hist_h_f, ref_h, cv2.HISTCMP_BHATTACHARYYA)
    dist_s   = cv2.compareHist(hist_s_f, ref_s, cv2.HISTCMP_BHATTACHARYYA)
    return ((1 - dist_h) + (1 - dist_s)) / 2.0


def hitung_bbox(frame_hsv, mask_kaca, lower, upper):
    """
    Cari bounding box kontur terbesar dari mask warna fase terbaik.
    Return dict normalized 0.0-1.0 relatif ke ukuran frame, atau None.
    """
    mask_fase = cv2.inRange(frame_hsv,
                            np.array(lower, dtype=np.uint8),
                            np.array(upper, dtype=np.uint8))
    mask_fase = cv2.bitwise_and(mask_fase, mask_kaca)

    kernel = np.ones((7, 7), np.uint8)
    mask_fase = cv2.morphologyEx(mask_fase, cv2.MORPH_OPEN,   kernel)
    mask_fase = cv2.morphologyEx(mask_fase, cv2.MORPH_DILATE, kernel)

    contours, _ = cv2.findContours(mask_fase, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    area    = cv2.contourArea(largest)

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

    hasil = []
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
        status = STATUS_MAP.get(label, "-")

        profil_terbaik = profil_data[fase_key_terbaik]
        bbox = hitung_bbox(hsv, mask_kaca,
                           profil_terbaik["lower"],
                           profil_terbaik["upper"])

        return label, status, round(skor_terbaik, 3), bbox

    return "tidak terdeteksi", "-", 0.0, None

# =============================================
#  SIMPAN / BACA HASIL
# =============================================

HASIL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hasil_warna.json")

def simpan_hasil(data: dict):
    with open(HASIL_PATH, "w") as f:
        json.dump(data, f)

def baca_hasil() -> dict:
    if not os.path.exists(HASIL_PATH):
        return _hasil_warna.copy()
    with open(HASIL_PATH, "r") as f:
        return json.load(f)

# =============================================
#  ROUTES
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

        # Deteksi warna + bbox sekaligus
        label, status, skor, bbox = deteksi_warna(jpeg_bytes)

        timestamp_now = datetime.now().isoformat()

        fase_sebelumnya_lama, fase_db_terakhir = get_fase_sebelumnya()
        fase_untuk_disimpan = fase_db_terakhir if fase_db_terakhir else None

        hasil = {
            "warna"       : label,
            "status_warna": status,
            "skor"        : skor,
            "device_id"   : device_id,
            "timestamp"   : timestamp_now,
            "bbox"        : bbox,   # normalized 0.0-1.0, atau null
        }
        simpan_hasil(hasil)

        with _lock:
            _hasil_warna.update(hasil)

        print(f"[DETEKSI] {label} | {status} | skor={skor} | bbox={bbox}")

        pompa_on, alasan, volume_ml = insert_sensor_db(
            label, status, skor, fase_untuk_disimpan
        )

        hasil["pompa_nutrisi"] = "ON" if pompa_on else "OFF"
        hasil["pompa_alasan"]  = alasan
        hasil["volume_ml"]     = volume_ml
        simpan_hasil(hasil)

        threading.Thread(
            target=upload_ke_gdrive,
            args=(jpeg_bytes, device_id, timestamp_now),
            daemon=True,
        ).start()

        return jsonify({
            "ok"           : True,
            "warna"        : label,
            "status_warna" : status,
            "skor"         : skor,
            "bbox"         : bbox,
            "pompa_nutrisi": "ON" if pompa_on else "OFF",
            "volume_ml"    : volume_ml,
            "alasan"       : alasan,
        })

    except Exception as e:
        print(f"[ERROR /upload_foto] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/status_pompa_nutrisi", methods=["GET"])
def status_pompa_nutrisi():
    try:
        data = baca_hasil()
        fase = data.get("warna", "tidak terdeteksi")
        pompa_on, alasan, volume_ml = cek_pompa_nutrisi(fase)
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
            ts             = datetime.fromisoformat(data["timestamp"])
            selisih        = (datetime.now() - ts).total_seconds()
            data["menit_lalu"] = int(selisih / 60)
        except:
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
#  MAIN — jalankan Flask + Bot Telegram
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
    print("Warna Endpoint + Bot Telegram + Pompa Nutrisi + BBox")
    print("=" * 50)
    print(f"[FLASK] Port: {port}")
    print(f"[MODE]  {'TESTING (3 menit)' if TESTING_MODE else 'PRODUKSI (3 hari)'}")
    print(f"[FLASK] Endpoints: /upload_foto | /hasil_warna | /health | /status_pompa_nutrisi")

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("[BOT] Thread dimulai")

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
