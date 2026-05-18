"""
warna_endpoint.py
=================
Flask endpoint untuk Railway:
  POST /upload_foto  ← terima JPEG dari ESP32-CAM
  GET  /hasil_warna  ← baca hasil deteksi terakhir (untuk dashboard)
  GET  /health       ← cek server hidup

Cara deploy:
1. Tambahkan file ini ke repo Railway kamu
2. Di Procfile / railway.toml: jalankan kedua service sekaligus
   (lihat catatan di bawah)

Dependencies tambahan (tambahkan ke requirements.txt):
  flask
  numpy
  opencv-python-headless   ← HEADLESS (tanpa GUI), cocok untuk server
  pillow
"""

import os
import io
import json
import time
import threading
import numpy as np
import cv2
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# =============================================
#  STATE — simpan hasil deteksi terakhir
# =============================================

_lock         = threading.Lock()
_hasil_warna  = {
    "warna"        : "tidak terdeteksi",
    "status_warna" : "-",
    "skor"         : 0.0,
    "device_id"    : "-",
    "timestamp"    : None,
    "menit_lalu"   : None,
}

# =============================================
#  PROFIL HSV FALLBACK
#  (kalau profil_hsv.json tidak ada, pakai ini)
# =============================================

PROFIL_FALLBACK = {
    "fase1": {
        "label"  : "Fase 1: Pembibitan",
        "lower"  : [25, 30, 80],
        "upper"  : [45, 120, 200],
        "hist_h" : [],
        "hist_s" : [],
    },
    "fase2": {
        "label"  : "Fase 2: Pertumbuhan",
        "lower"  : [40, 60, 60],
        "upper"  : [75, 180, 180],
        "hist_h" : [],
        "hist_s" : [],
    },
    "fase3": {
        "label"  : "Fase 3: Optimal",
        "lower"  : [75, 80, 40],
        "upper"  : [100, 220, 160],
        "hist_h" : [],
        "hist_s" : [],
    },
    "fase4": {
        "label"  : "Fase 4: Panen",
        "lower"  : [90, 100, 20],
        "upper"  : [120, 255, 100],
        "hist_h" : [],
        "hist_s" : [],
    },
}

BOBOT_HSV  = 0.55
BOBOT_HIST = 0.45
SKOR_MIN   = 0.35

# =============================================
#  LOAD PROFIL
# =============================================

def load_profil():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profil_hsv.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        print(f"[PROFIL] Dimuat dari file: {list(data.keys())}")
        return data
    else:
        print("[PROFIL] profil_hsv.json tidak ditemukan, pakai profil fallback.")
        return PROFIL_FALLBACK

profil_data = load_profil()

# =============================================
#  FUNGSI DETEKSI (tanpa GUI)
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
    mask_hsv = cv2.inRange(frame_hsv, np.array(lower, dtype=np.uint8),
                                       np.array(upper, dtype=np.uint8))
    mask_hsv = cv2.bitwise_and(mask_hsv, mask_kaca)
    k        = np.ones((5, 5), np.uint8)
    mask_hsv = cv2.morphologyEx(mask_hsv, cv2.MORPH_OPEN,   k)
    mask_hsv = cv2.morphologyEx(mask_hsv, cv2.MORPH_DILATE, k)
    return cv2.countNonZero(mask_hsv) / total


def skor_histogram(frame_hsv, mask_kaca, hist_h_ref, hist_s_ref):
    if not hist_h_ref or not hist_s_ref:
        return 0.0   # tidak ada referensi histogram, skip
    valid_px = frame_hsv[mask_kaca == 255]
    if len(valid_px) < 100:
        return 0.0
    h_arr = valid_px[:, 0]
    s_arr = valid_px[:, 1]
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


def deteksi_warna(jpeg_bytes: bytes):
    """Proses bytes JPEG → return (label, status, skor)."""
    img_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame     = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if frame is None:
        return "tidak terdeteksi", "-", 0.0

    hsv       = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask_kaca = buat_mask_kaca(hsv)

    hasil = []
    for fase_key, profil in profil_data.items():
        s_hsv  = skor_hsv(hsv, mask_kaca, profil["lower"], profil["upper"])
        s_hist = skor_histogram(hsv, mask_kaca,
                                profil.get("hist_h", []),
                                profil.get("hist_s", []))
        skor   = BOBOT_HSV * s_hsv + BOBOT_HIST * s_hist
        hasil.append((skor, profil["label"]))

    hasil.sort(reverse=True)

    if hasil and hasil[0][0] >= SKOR_MIN:
        skor_terbaik, label = hasil[0]
        STATUS_MAP = {
            "Fase 1: Pembibitan" : "pembibitan",
            "Fase 2: Pertumbuhan": "pertumbuhan",
            "Fase 3: Optimal"    : "optimal",
            "Fase 4: Panen"      : "siap panen",
        }
        status = STATUS_MAP.get(label, "-")
        return label, status, round(skor_terbaik, 3)

    return "tidak terdeteksi", "-", 0.0

# =============================================
#  SIMPAN HASIL KE FILE (untuk bot_telegram baca)
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
#  ENDPOINT: POST /upload_foto
# =============================================

@app.route("/upload_foto", methods=["POST"])
def upload_foto():
    """Terima foto JPEG dari ESP32-CAM, proses warna, simpan hasil."""
    try:
        device_id = request.form.get("device_id", "unknown")

        # Ambil file foto
        if "foto" not in request.files:
            return jsonify({"ok": False, "error": "Tidak ada field 'foto'"}), 400

        foto_file  = request.files["foto"]
        jpeg_bytes = foto_file.read()

        if len(jpeg_bytes) < 100:
            return jsonify({"ok": False, "error": "File terlalu kecil"}), 400

        print(f"[UPLOAD] {device_id} — {len(jpeg_bytes)} bytes")

        # Deteksi warna
        label, status, skor = deteksi_warna(jpeg_bytes)

        # Simpan hasil
        hasil = {
            "warna"       : label,
            "status_warna": status,
            "skor"        : skor,
            "device_id"   : device_id,
            "timestamp"   : datetime.now().isoformat(),
        }
        simpan_hasil(hasil)

        with _lock:
            _hasil_warna.update(hasil)

        print(f"[DETEKSI] {label} | {status} | skor={skor}")

        return jsonify({
            "ok"          : True,
            "warna"       : label,
            "status_warna": status,
            "skor"        : skor,
        })

    except Exception as e:
        print(f"[ERROR /upload_foto] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# =============================================
#  ENDPOINT: GET /hasil_warna
# =============================================

@app.route("/hasil_warna", methods=["GET"])
def hasil_warna():
    """Kembalikan hasil deteksi warna terakhir (dipanggil bot_telegram)."""
    data = baca_hasil()

    # Hitung menit_lalu
    if data.get("timestamp"):
        try:
            ts        = datetime.fromisoformat(data["timestamp"])
            selisih   = (datetime.now() - ts).total_seconds()
            data["menit_lalu"] = int(selisih / 60)
        except:
            data["menit_lalu"] = None

    return jsonify(data)


# =============================================
#  ENDPOINT: GET /health
# =============================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# =============================================
#  MAIN
# =============================================

def run_bot_thread():
    import subprocess, sys
    subprocess.Popen([sys.executable, "bot_telegram.py"])

if __name__ == "__main__":
    import threading
    port = int(os.environ.get("PORT", 5000))
    print(f"[WARNA ENDPOINT] Jalan di port {port}")
    
    # Jalankan bot telegram sebagai subprocess
    t = threading.Thread(target=run_bot_thread, daemon=True)
    t.start()
    print("[BOT] Bot telegram dimulai sebagai subprocess")
    
    app.run(host="0.0.0.0", port=port, debug=False)
