"""
warna_endpoint.py - FINAL VERSION
====================================
Flask endpoint + Bot Telegram dalam satu process.
- Flask terima foto dari ESP32-CAM (POST /upload_foto)
- Bot Telegram jalan sebagai subprocess
- Hasil deteksi disimpan ke hasil_warna.json
"""

import os
import io
import json
import threading
import subprocess
import sys
import numpy as np
import cv2
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

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

BOBOT_HSV = 0.55
BOBOT_HIST = 0.45
SKOR_MIN  = 0.35

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


def deteksi_warna(jpeg_bytes: bytes):
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
        device_id  = request.form.get("device_id", "unknown")

        if "foto" not in request.files:
            return jsonify({"ok": False, "error": "Tidak ada field 'foto'"}), 400

        jpeg_bytes = request.files["foto"].read()
        if len(jpeg_bytes) < 100:
            return jsonify({"ok": False, "error": "File terlalu kecil"}), 400

        print(f"[UPLOAD] {device_id} — {len(jpeg_bytes)} bytes")

        label, status, skor = deteksi_warna(jpeg_bytes)

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
        return jsonify({"ok": True, "warna": label, "status_warna": status, "skor": skor})

    except Exception as e:
        print(f"[ERROR /upload_foto] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# =============================================
#  MAIN — jalankan Flask + Bot Telegram
# =============================================

# SESUDAH ✅
def run_bot():
    import time
    # Tunggu 20 detik dulu — beri waktu instance Railway lama benar-benar mati
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
    port = int(os.environ.get("PORT", 5000))

    print("=" * 50)
    print("Warna Endpoint + Bot Telegram - Railway")
    print("=" * 50)
    print(f"[FLASK] Port: {port}")
    print(f"[FLASK] Endpoints: /upload_foto | /hasil_warna | /health")

    # Bot telegram di background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("[BOT] Thread dimulai")

    # Flask di main thread
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
