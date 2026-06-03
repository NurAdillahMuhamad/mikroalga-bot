"""
warna_endpoint.py
=================
Perubahan dari versi sebelumnya:
1. Logika pompa nutrisi dipisah jadi 2:
   - Logika 1 (Darurat): pH abnormal DAN fase mundur → pompa nyala, delay 3 jam, cek lagi
   - Logika 2 (Terjadwal): 3 hari stabil → pompa nyala, reset timer
2. Fix rumus volume: hapus ×1000 (satuan langsung mL)
3. Durasi pompa dihitung dari volume (bukan waktu tetap)
4. Semua timestamp disimpan di DB agar tahan Railway restart
5. Timer 3 hari direset jika kondisi darurat muncul di tengah periode
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
# KONFIGURASI
# =============================================
TESTING_MODE = False

# Logika 2: interval terjadwal
INTERVAL_TERJADWAL_DETIK = 3 * 24 * 3600   # 3 hari produksi
# INTERVAL_TERJADWAL_DETIK = 3 * 60         # 3 menit testing

# Logika 1: delay antar cek saat kondisi darurat
DELAY_DARURAT_DETIK = 3 * 3600              # 3 jam produksi
# DELAY_DARURAT_DETIK = 3 * 60             # 3 menit testing

# Durasi pompa dihitung dari volume
# Pompa mengalirkan 100 mL per menit
Q_ML_PER_MENIT = 100.0

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
PH_NORMAL_MAX = 10.5

# =============================================
# KONEKSI DATABASE
# =============================================
def get_db():
    return pymysql.connect(
        host     = os.environ.get("MYSQLHOST",     "localhost"),
        port     = int(os.environ.get("MYSQLPORT", "3306")),
        user     = os.environ.get("MYSQLUSER",     "root"),
        password = os.environ.get("MYSQLPASSWORD", ""),
        database = os.environ.get("MYSQLDATABASE", "railway"),
        charset  = "utf8mb4",
        cursorclass     = pymysql.cursors.DictCursor,
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

def get_fase_db_terakhir():
    """Ambil fase warna terakhir yang terdeteksi dari DB."""
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
    """Ambil id row terakhir dari mikroalga_sensor."""
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

def get_waktu_pompa_nutrisi_terakhir():
    """
    Ambil waktu terakhir pompa nutrisi nyala dari DB.
    Dipakai untuk hitung interval 3 hari (Logika 2)
    dan delay 3 jam (Logika 1).
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

def cek_stabilitas_3_hari():
    """
    Logika 2 — cek apakah selama 3 hari terakhir:
    1. pH selalu dalam rentang normal (8.5–10.5)
    2. Fase tidak pernah mundur

    Return: (stabil: bool, alasan: str)
    """
    try:
        db = get_db()
        with db.cursor() as cur:
            # Cek pH abnormal dalam 3 hari terakhir
            cur.execute(
                """
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN pH < %s OR pH > %s THEN 1 ELSE 0 END) as ph_abnormal
                FROM mikroalga_sensor
                WHERE waktu >= NOW() - INTERVAL 3 DAY
                AND pH IS NOT NULL
                """,
                (PH_NORMAL_MIN, PH_NORMAL_MAX)
            )
            row_ph = cur.fetchone()

            # Cek fase mundur dalam 3 hari terakhir
            # fase_sebelumnya lebih tinggi dari warna sekarang = mundur
            cur.execute(
                """
                SELECT COUNT(*) as fase_mundur
                FROM mikroalga_sensor
                WHERE waktu >= NOW() - INTERVAL 3 DAY
                AND warna IS NOT NULL
                AND warna != 'tidak terdeteksi'
                AND fase_sebelumnya IS NOT NULL
                AND fase_sebelumnya != ''
                AND (
                    FIELD(warna, %s, %s, %s, %s) <
                    FIELD(fase_sebelumnya, %s, %s, %s, %s)
                )
                """,
                (
                    *URUTAN_FASE,   # untuk warna
                    *URUTAN_FASE,   # untuk fase_sebelumnya
                )
            )
            row_fase = cur.fetchone()
        db.close()

        total        = row_ph["total"] if row_ph else 0
        ph_abnormal  = row_ph["ph_abnormal"] if row_ph else 0
        fase_mundur  = row_fase["fase_mundur"] if row_fase else 0

        if total == 0:
            return False, "belum ada data 3 hari"
        if ph_abnormal and ph_abnormal > 0:
            return False, f"pH abnormal {ph_abnormal}x dalam 3 hari"
        if fase_mundur and fase_mundur > 0:
            return False, f"fase mundur {fase_mundur}x dalam 3 hari"

        return True, "kondisi stabil 3 hari"

    except Exception as e:
        print(f"[DB] Gagal cek stabilitas: {e}")
        return False, f"db error: {e}"

# =============================================
# LOGIKA POMPA NUTRISI
# =============================================
def hitung_volume_nutrisi(fase_label):
    """
    FIX: hapus ×1000, satuan langsung mL.
    Contoh Fase 1: 0.2 × 45 = 9 mL
    """
    k = KONSTANTA_NUTRISI.get(fase_label, 0.0)
    return round(k * V_MEDIA_LITER, 2)

def hitung_durasi_detik(volume_ml):
    """
    Hitung durasi pompa dari volume.
    Pompa = 100 mL/menit.
    Contoh: 9 mL / 100 mL/menit × 60 detik = 5.4 detik
    """
    return round((volume_ml / Q_ML_PER_MENIT) * 60, 1)

def cek_fase_mundur(fase_sekarang, fase_sebelumnya):
    if not fase_sekarang or not fase_sebelumnya:
        return False
    if fase_sekarang not in URUTAN_FASE or fase_sebelumnya not in URUTAN_FASE:
        return False
    return URUTAN_FASE.index(fase_sekarang) < URUTAN_FASE.index(fase_sebelumnya)

def cek_pompa_nutrisi(fase_sekarang, fase_sebelumnya_val):
    """
    Dua logika pemberian nutrisi:

    LOGIKA 1 — DARURAT:
    Syarat: pH abnormal DAN fase mundur (keduanya harus terpenuhi)
    Setelah pompa nyala → delay 3 jam → cek lagi
    Jika masih darurat → pompa nyala lagi
    Jika sudah stabil → masuk Logika 2, reset timer 3 hari

    LOGIKA 2 — TERJADWAL:
    Syarat: sudah 3 hari sejak pemberian terakhir
            DAN selama 3 hari pH selalu normal
            DAN fase tidak pernah mundur
    Jika di tengah periode Logika 2 kondisi darurat muncul
    → langsung masuk Logika 1, timer 3 hari direset

    Return: (pompa_on: bool, alasan: str, volume_ml: float, durasi_detik: float)
    """
    if not fase_sekarang or fase_sekarang == "tidak terdeteksi":
        return False, "fase tidak terdeteksi", 0.0, 0.0

    volume     = hitung_volume_nutrisi(fase_sekarang)
    durasi     = hitung_durasi_detik(volume)
    ph         = get_ph_terbaru()
    fase_mundur = cek_fase_mundur(fase_sekarang, fase_sebelumnya_val)

    # Cek kondisi darurat (keduanya harus tidak normal)
    ph_abnormal = False
    if ph is not None:
        ph_abnormal = (ph < PH_NORMAL_MIN or ph > PH_NORMAL_MAX)

    kondisi_darurat = ph_abnormal and fase_mundur

    # Ambil waktu terakhir pompa nyala dari DB
    waktu_terakhir = get_waktu_pompa_nutrisi_terakhir()
    now            = datetime.now()

    if kondisi_darurat:
        # ── LOGIKA 1: DARURAT ────────────────────────────────────
        # Cek apakah delay 3 jam sudah lewat sejak darurat terakhir
        if waktu_terakhir is None:
            # Belum pernah nyala → langsung nyala
            alasan = f"DARURAT: pH={ph} + fase mundur ({fase_sebelumnya_val}→{fase_sekarang})"
            print(f"[POMPA] {alasan}")
            return True, alasan, volume, durasi

        selisih = (now - waktu_terakhir).total_seconds()
        if selisih >= DELAY_DARURAT_DETIK:
            # Delay 3 jam sudah lewat → pompa nyala lagi
            alasan = (
                f"DARURAT: pH={ph} + fase mundur ({fase_sebelumnya_val}→{fase_sekarang}) "
                f"| {int(selisih/3600)} jam sejak terakhir"
            )
            print(f"[POMPA] {alasan}")
            return True, alasan, volume, durasi
        else:
            # Masih dalam delay 3 jam
            sisa = int((DELAY_DARURAT_DETIK - selisih) / 60)
            alasan = f"DARURAT menunggu: sisa {sisa} menit delay 3 jam"
            print(f"[POMPA] {alasan}")
            return False, alasan, 0.0, 0.0

    else:
        # ── LOGIKA 2: TERJADWAL ──────────────────────────────────
        # Syarat 1: sudah 3 hari sejak pemberian terakhir
        if waktu_terakhir is None:
            sudah_3_hari = True
        else:
            selisih      = (now - waktu_terakhir).total_seconds()
            sudah_3_hari = selisih >= INTERVAL_TERJADWAL_DETIK

        if not sudah_3_hari:
            sisa_jam = int((INTERVAL_TERJADWAL_DETIK - selisih) / 3600)
            return False, f"terjadwal: sisa {sisa_jam} jam", 0.0, 0.0

        # Syarat 2: cek stabilitas 3 hari via query DB
        stabil, alasan_stabil = cek_stabilitas_3_hari()
        if not stabil:
            return False, f"terjadwal: {alasan_stabil}", 0.0, 0.0

        alasan = f"TERJADWAL 3 hari | {alasan_stabil}"
        print(f"[POMPA] {alasan}")
        return True, alasan, volume, durasi

def update_status_pompa_db(pompa_on: bool, volume_ml: float = 0.0, durasi_detik: float = 0.0):
    """Update tabel status_pompa untuk pompa_nutrisi."""
    try:
        db  = get_db()
        val = "ON" if pompa_on else "OFF"
        with db.cursor() as cur:
            # Cek apakah kolom vol_nutrisi ada di status_pompa
            # Jika belum ada, update tanpa kolom itu
            try:
                cur.execute(
                    """
                    UPDATE status_pompa
                    SET pompa_nutrisi = %s,
                        vol_nutrisi   = %s
                    WHERE id = 1
                    """,
                    (val, volume_ml)
                )
            except Exception:
                # Fallback jika kolom vol_nutrisi belum ada
                cur.execute(
                    "UPDATE status_pompa SET pompa_nutrisi = %s WHERE id = 1",
                    (val,)
                )
        db.commit()
        db.close()
        print(f"[POMPA] status_pompa.pompa_nutrisi = {val} | vol = {volume_ml} mL | durasi = {durasi_detik}s")
    except Exception as e:
        print(f"[DB] Gagal update status_pompa: {e}")

# =============================================
# UPDATE SENSOR DB
# =============================================
def update_sensor_db(label, status, skor, fase_sebelumnya_val):
    """
    UPDATE row terakhir di mikroalga_sensor dengan:
    - warna, status_warna (hasil deteksi)
    - pompa_nutrisi, vol_nutrisi (hasil keputusan pompa)
    - fase_sebelumnya
    - durasi_nutrisi_detik (berapa lama pompa harus nyala)
    """
    try:
        pompa_on, alasan, volume_ml, durasi_detik = cek_pompa_nutrisi(label, fase_sebelumnya_val)
        update_status_pompa_db(pompa_on, volume_ml, durasi_detik)

        pompa_nutrisi_val = "ON" if pompa_on else "OFF"

        max_id = get_id_sensor_terakhir()
        if not max_id:
            print("[DB] Tidak ada row untuk di-UPDATE, skip.")
            return False, "tidak ada row", 0.0, 0.0

        db = get_db()
        with db.cursor() as cur:
            # Update row terakhir dengan semua data lengkap
            try:
                cur.execute(
                    """
                    UPDATE mikroalga_sensor
                    SET
                        warna                = %s,
                        status_warna         = %s,
                        pompa_nutrisi        = %s,
                        vol_nutrisi          = %s,
                        durasi_nutrisi_detik = %s,
                        fase_sebelumnya      = %s
                    WHERE id = %s
                    """,
                    (
                        label,
                        status,
                        pompa_nutrisi_val,
                        volume_ml,
                        durasi_detik,
                        fase_sebelumnya_val,
                        max_id,
                    )
                )
            except Exception:
                # Fallback jika kolom durasi_nutrisi_detik belum ada
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

        print(f"[DB] UPDATE id={max_id} | warna={label} | pompa_nutrisi={pompa_nutrisi_val} | vol={volume_ml}mL | durasi={durasi_detik}s | alasan={alasan}")
        return pompa_on, alasan, volume_ml, durasi_detik

    except Exception as e:
        print(f"[DB] Gagal UPDATE sensor: {e}")
        return False, "db error", 0.0, 0.0

# =============================================
# STATE GLOBAL
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
    query = (
        f"name='{nama_folder}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    hasil = service.files().list(q=query, fields="files(id, name)").execute()
    files = hasil.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name"    : nama_folder,
        "mimeType": "application/vnd.google-apps.folder",
        "parents" : [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def upload_ke_gdrive(jpeg_bytes: bytes, device_id: str, timestamp: str, fase: str, status_warna: str, skor: float):
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
        file_id  = uploaded['id']
        
        # ★ TAMBAHAN: Simpan metadata ke foto_metadata table
        try:
            db = get_db()
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO foto_metadata 
                    (file_id, file_name, tanggal, jam, fase, status_warna, skor, kolam_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        file_id,
                        nama_file,
                        ts.date(),
                        ts.time(),
                        fase,
                        status_warna,
                        round(skor, 3),
                        1  # kolam_id
                    )
                )
            db.commit()
            db.close()
            print(f"[METADATA] Saved to foto_metadata: {file_id}")
        except Exception as e:
            print(f"[METADATA ERROR] {e}")
        
        print(f"[GDRIVE] Upload sukses: {nama_folder}/{nama_file} → {file_id}")
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
    mask_hsv = cv2.inRange(
        frame_hsv,
        np.array(lower, dtype=np.uint8),
        np.array(upper, dtype=np.uint8)
    )
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
    mask_fase = cv2.inRange(
        frame_hsv,
        np.array(lower, dtype=np.uint8),
        np.array(upper, dtype=np.uint8)
    )
    mask_fase   = cv2.bitwise_and(mask_fase, mask_kaca)
    kernel      = np.ones((7, 7), np.uint8)
    mask_fase   = cv2.morphologyEx(mask_fase, cv2.MORPH_OPEN,   kernel)
    mask_fase   = cv2.morphologyEx(mask_fase, cv2.MORPH_DILATE, kernel)
    contours, _ = cv2.findContours(mask_fase, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest          = max(contours, key=cv2.contourArea)
    area             = cv2.contourArea(largest)
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
        s_hist = skor_histogram(
            hsv, mask_kaca,
            profil.get("hist_h", []),
            profil.get("hist_s", [])
        )
        skor = BOBOT_HSV * s_hsv + BOBOT_HIST * s_hist
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
        status         = STATUS_MAP.get(label, "-")
        profil_terbaik = profil_data[fase_key_terbaik]
        bbox           = hitung_bbox(hsv, mask_kaca, profil_terbaik["lower"], profil_terbaik["upper"])
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
    """Baca hasil deteksi, fallback ke DB jika file tidak ada."""
    if os.path.exists(HASIL_PATH):
        try:
            with open(HASIL_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
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

        # Ambil fase DB terakhir SEBELUM deteksi → fase_sebelumnya
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
        pompa_on, alasan, volume_ml, durasi_detik = update_sensor_db(
            label, status, skor, fase_sebelumnya_val
        )

        hasil["pompa_nutrisi"]  = "ON" if pompa_on else "OFF"
        hasil["pompa_alasan"]   = alasan
        hasil["volume_ml"]      = volume_ml
        hasil["durasi_detik"]   = durasi_detik
        simpan_hasil(hasil)

        # Upload foto ke Google Drive (background thread)
        threading.Thread(
          target=upload_ke_gdrive,
          args=(jpeg_bytes, device_id, timestamp_now, label, status, skor),
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
            "durasi_detik" : durasi_detik,
            "alasan"       : alasan,
        })

    except Exception as e:
        print(f"[ERROR /upload_foto] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/status_pompa_nutrisi", methods=["GET"])
def status_pompa_nutrisi():
    """
    Endpoint yang dipoll ESP32 setiap 30 detik.
    Mengembalikan status pompa nutrisi beserta volume dan durasi.
    ESP32 pakai durasi_detik untuk hitung berapa lama relay nyala.
    """
    try:
        data       = baca_hasil()
        fase       = data.get("warna", "tidak terdeteksi")
        fase_sblm  = get_fase_db_terakhir()
        pompa_on, alasan, volume_ml, durasi_detik = cek_pompa_nutrisi(fase, fase_sblm)
        return jsonify({
            "pompa_nutrisi": "ON" if pompa_on else "OFF",
            "volume_ml"    : volume_ml,
            "durasi_detik" : durasi_detik,
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
        except Exception:
            data["menit_lalu"] = None
    return jsonify(data)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status"                  : "ok",
        "timestamp"               : datetime.now().isoformat(),
        "testing_mode"            : TESTING_MODE,
        "interval_terjadwal_detik": INTERVAL_TERJADWAL_DETIK,
        "delay_darurat_detik"     : DELAY_DARURAT_DETIK,
        "q_ml_per_menit"          : Q_ML_PER_MENIT,
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
    print("Warna Endpoint — Mikroalga Spirulina")
    print("=" * 50)
    print(f"[FLASK] Port               : {port}")
    print(f"[MODE]  {'TESTING' if TESTING_MODE else 'PRODUKSI'}")
    print(f"[pH]    Normal             : {PH_NORMAL_MIN} – {PH_NORMAL_MAX}")
    print(f"[POMPA] Interval terjadwal : {INTERVAL_TERJADWAL_DETIK // 3600} jam")
    print(f"[POMPA] Delay darurat      : {DELAY_DARURAT_DETIK // 3600} jam")
    print(f"[POMPA] Flow rate          : {Q_ML_PER_MENIT} mL/menit")

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
