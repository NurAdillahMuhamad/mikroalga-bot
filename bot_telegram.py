"""
Bot Telegram v2 - Monitoring Mikroalga Spirulina
Deploy: Railway.app
"""

import sys
import os
import io
import json
import base64
import requests
import threading
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================================================================
#  KONFIGURASI — baca dari environment variable
# ================================================================

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "7849657583:AAFlDt0HTEcD9Lu-sGx5JLGd_xQL9LEa9dk")
ALLOWED_CHAT_IDS = [int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "5073323779").split(",")]
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "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")
API_SENSOR_URL   = os.environ.get("API_SENSOR_URL", "https://mikroalga-monitor.infinityfreeapp.com/cek_sensor.php")
PARENT_FOLDER_ID = os.environ.get("PARENT_FOLDER_ID", "17ISXne7N15wOEdZwwdW_lGtJWGUHiTbc")

SCOPES           = ["https://www.googleapis.com/auth/drive.readonly"]
NOTIF_INTERVAL   = int(os.environ.get("NOTIF_INTERVAL", "3600"))

PH_MIN   = 8.5
PH_MAX   = 10.5
LUX_MIN  = 100

HASIL_WARNA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hasil_warna.json")

# ================================================================
#  GOOGLE DRIVE — baca token dari ENV, bukan file
# ================================================================

def get_drive_service():
    # Baca token dari environment variable
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        token_data = json.loads(token_json)
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes")
        )
    else:
        # Fallback: baca dari file (untuk development lokal)
        BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
        TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    # Refresh token kalau expired
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("drive", "v3", credentials=creds)


def get_latest_photo(drive_service):
    q = (
        f"'{PARENT_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res     = drive_service.files().list(q=q, fields="files(id,name)").execute()
    folders = res.get("files", [])
    if not folders:
        return None, None, "Belum ada folder di Drive."

    def parse_tanggal(nama):
        try:
            p = nama.split("-")
            return (int(p[2]), int(p[1]), int(p[0]))
        except:
            return (0, 0, 0)

    folders.sort(key=lambda f: parse_tanggal(f["name"]), reverse=True)
    folder      = folders[0]
    folder_id   = folder["id"]
    folder_name = folder["name"]

    q2 = (
        f"'{folder_id}' in parents and "
        f"mimeType='image/jpeg' and trashed=false"
    )
    res2   = drive_service.files().list(
        q=q2, fields="files(id,name)", orderBy="name desc", pageSize=1
    ).execute()
    photos = res2.get("files", [])
    if not photos:
        return None, None, f"Belum ada foto di folder {folder_name}."

    file_id    = photos[0]["id"]
    file_name  = photos[0]["name"]
    request    = drive_service.files().get_media(fileId=file_id)
    buf        = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)

    img_bytes  = buf.getvalue()
    img_base64 = base64.b64encode(img_bytes).decode("utf-8")
    buf.seek(0)

    return buf, img_base64, f"{folder_name} / {file_name}"


def get_latest_photos(drive_service, jumlah=5):
    q = (
        f"'{PARENT_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res     = drive_service.files().list(q=q, fields="files(id,name)").execute()
    folders = res.get("files", [])
    if not folders:
        return [], "Belum ada folder di Drive."

    def parse_tanggal(nama):
        try:
            p = nama.split("-")
            return (int(p[2]), int(p[1]), int(p[0]))
        except:
            return (0, 0, 0)

    folders.sort(key=lambda f: parse_tanggal(f["name"]), reverse=True)
    folder_id   = folders[0]["id"]
    folder_name = folders[0]["name"]

    q2 = (
        f"'{folder_id}' in parents and "
        f"mimeType='image/jpeg' and trashed=false"
    )
    res2   = drive_service.files().list(
        q=q2, fields="files(id,name)", orderBy="name desc", pageSize=jumlah
    ).execute()
    photos = res2.get("files", [])
    if not photos:
        return [], f"Belum ada foto di folder {folder_name}."

    hasil = []
    for p in photos:
        request = drive_service.files().get_media(fileId=p["id"])
        buf     = io.BytesIO()
        dl      = MediaIoBaseDownload(buf, request)
        done    = False
        while not done:
            _, done = dl.next_chunk()
        buf.seek(0)
        hasil.append((buf, f"{folder_name} / {p['name']}"))
    return hasil, None


# ================================================================
#  SENSOR DATA
# ================================================================

def get_sensor_data():
    
    """
    Baca hasil deteksi warna dari hasil_warna.json
    (file ini ditulis oleh warna_endpoint.py saat ESP32 upload foto).
    Juga tetap ambil data pH/cahaya dari API sensor lama (kalau ada).
    """
    hasil = {
        "warna"        : "tidak terdeteksi",
        "status_warna" : "-",
        "skor"         : 0.0,
        "menit_lalu"   : None,
        # Data sensor lain (default)
        "pH"           : "—",
        "pH_status"    : "normal",
        "cahaya"       : "—",
        "uv"           : "OFF",
        "pompa_basa"   : "IDLE",
        "pompa_normal" : "IDLE",
    }
 
    # 1. Baca hasil warna dari file lokal
    if os.path.exists(HASIL_WARNA_PATH):
        try:
            with open(HASIL_WARNA_PATH, "r") as f:
                data_warna = json.load(f)
            hasil.update({
                "warna"       : data_warna.get("warna", "tidak terdeteksi"),
                "status_warna": data_warna.get("status_warna", "-"),
                "skor"        : data_warna.get("skor", 0.0),
            })
            # Hitung menit_lalu
            ts_str = data_warna.get("timestamp")
            if ts_str:
                ts     = datetime.fromisoformat(ts_str)
                selisih = (datetime.now() - ts).total_seconds()
                hasil["menit_lalu"] = int(selisih / 60)
        except Exception as e:
            print(f"[SENSOR] Gagal baca hasil_warna.json: {e}")
 
    # 2. Tetap coba ambil data pH/cahaya dari sensor PHP (opsional)
    #    Kalau tidak pakai sensor PHP, hapus blok ini.
    API_SENSOR_URL = os.environ.get("API_SENSOR_URL", "")
    if API_SENSOR_URL:
        try:
            import requests
            r = requests.get(API_SENSOR_URL, timeout=8)
            data_sensor = r.json()
            # Merge data sensor, warna tetap dari ESP32
            for key in ["pH", "pH_status", "cahaya", "uv", "pompa_basa", "pompa_normal"]:
                if key in data_sensor:
                    hasil[key] = data_sensor[key]
            if "menit_lalu" in data_sensor and hasil["menit_lalu"] is None:
                hasil["menit_lalu"] = data_sensor["menit_lalu"]
        except Exception as e:
            print(f"[SENSOR] Gagal ambil data sensor pH: {e}")
 
    return hasil


def format_status(s: dict) -> str:
    if "error" in s:
        return f"❌ Gagal ambil data sensor:\n`{s['error']}`"

    mnt   = s.get("menit_lalu")
    waktu = "—" if mnt is None else ("baru saja" if mnt < 1 else f"{mnt} menit lalu")

    ph_val    = s.get("pH", "—")
    ph_status = s.get("pH_status", "")
    ph_ikon   = "🔴" if ph_status == "rendah" else ("🟡" if ph_status == "tinggi" else "🟢")

    lux_val = s.get("cahaya", "—")
    uv      = s.get("uv", "OFF")
    lux_ikon = "💡" if uv == "ON" else "☀️"

    warna        = s.get("warna", "—")
    status_warna = s.get("status_warna", "—")

    pompa_basa   = "🟢 DOSING" if s.get("pompa_basa")   == "DOSING" else "⚫ IDLE"
    pompa_normal = "🟢 DOSING" if s.get("pompa_normal") == "DOSING" else "⚫ IDLE"
    uv_status    = "🟢 ON"     if uv == "ON"             else "⚫ OFF"

    return (
        f"🌿 *Status Kolam Mikroalga* — Kolam 1\n"
        f"🕐 Update: _{waktu}_\n\n"
        f"{ph_ikon} *pH Air* : `{ph_val}` ({ph_status or 'normal'})\n"
        f"{lux_ikon} *Cahaya* : `{lux_val} Lux`\n"
        f"🎨 *Warna* : `{warna}` — _{status_warna}_\n\n"
        f"⚙️ *Kontrol Relay*\n"
        f"  • Pompa Basa   : {pompa_basa}\n"
        f"  • Pompa Netral : {pompa_normal}\n"
        f"  • Lampu UV     : {uv_status}\n"
    )


# ================================================================
#  GROQ AI
# ================================================================

def analisis_ai(sensor_data: dict, img_base64: str = None) -> str:
    ph           = sensor_data.get("pH", "—")
    cahaya       = sensor_data.get("cahaya", "—")
    warna        = sensor_data.get("warna", "—")
    status_warna = sensor_data.get("status_warna", "—")
    ph_status    = sensor_data.get("pH_status", "normal")

    prompt = (
        f"Kamu adalah ahli budidaya mikroalga Spirulina. "
        f"Berikut data sensor kolam saat ini:\n"
        f"- pH: {ph} (status: {ph_status})\n"
        f"- Intensitas cahaya: {cahaya} Lux\n"
        f"- Warna air: {warna} ({status_warna})\n\n"
        f"Berikan analisis singkat kondisi kolam (2-3 kalimat) "
        f"dan satu rekomendasi tindakan yang perlu dilakukan. "
        f"Jawab dalam Bahasa Indonesia."
    )

    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type" : "application/json"
        }
        body = {
            "model": "llama-3.3-70b-versatile",
            "messages"  : [{"role": "user", "content": prompt}],
            "max_tokens": 300
        }
        r   = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=body, headers=headers, timeout=30
        )
        res = r.json()
        return res["choices"][0]["message"]["content"].strip()
    except Exception as e:
        try:
            print(f"[GROQ STATUS] {r.status_code}")
            print(f"[GROQ RAW] {r.text}")
        except:
            pass
        return f"_(Analisis AI tidak tersedia: {e})_"


# ================================================================
#  CEK ALERT
# ================================================================

def cek_alert(sensor_data: dict):
    alerts = []
    try:
        ph = float(sensor_data.get("pH", 0))
        if ph == 0 or ph is None:
            pass  # data kosong, skip
        elif ph < PH_MIN:
            alerts.append(f"🔴 pH RENDAH: `{ph}` (batas bawah {PH_MIN})")
        elif ph > PH_MAX:
            alerts.append(f"🟡 pH TINGGI: `{ph}` (batas atas {PH_MAX})")
    except:
        pass

    try:
        lux = int(sensor_data.get("cahaya", 9999))
        if lux < LUX_MIN and lux != -1 and sensor_data.get("uv") != "ON":
            alerts.append(f"⚠️ CAHAYA KURANG: `{lux} Lux` (minimum {LUX_MIN})")
    except:
        pass

    if alerts:
        return "🚨 *ALERT — Parameter Abnormal!*\n\n" + "\n".join(alerts)
    return None


# ================================================================
#  NOTIFIKASI RUTIN
# ================================================================

async def kirim_notifikasi_rutin(context):
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            sensor    = get_sensor_data()
            alert_msg = cek_alert(sensor)
            if alert_msg:
                await context.bot.send_message(
                    chat_id=chat_id, text=alert_msg, parse_mode="Markdown"
                )

            svc                      = get_drive_service()
            buf, img_base64, caption = get_latest_photo(svc)
            ai_text                  = analisis_ai(sensor, img_base64)
            status_text              = format_status(sensor)
            full_caption = (
                f"🕐 *Laporan Rutin — {datetime.now().strftime('%d/%m/%Y %H:%M')}*\n\n"
                f"{status_text}\n"
                f"🤖 *Analisis AI:*\n{ai_text}"
            )

            if buf:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=buf,
                    caption=full_caption[:1024], parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, text=full_caption, parse_mode="Markdown"
                )
        except Exception as e:
            print(f"[NOTIF ERROR] {e}")


# ================================================================
#  GUARD
# ================================================================

def is_allowed(update: Update) -> bool:
    return update.effective_chat.id in ALLOWED_CHAT_IDS


# ================================================================
#  COMMAND HANDLERS
# ================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    kb = [
        [InlineKeyboardButton("📊 Status",     callback_data="status"),
         InlineKeyboardButton("📷 Foto",        callback_data="foto")],
        [InlineKeyboardButton("🤖 Analisis AI", callback_data="ai"),
         InlineKeyboardButton("ℹ️ Bantuan",      callback_data="help")],
    ]
    await update.message.reply_text(
        "👋 Halo! Saya bot monitoring *Mikroalga Spirulina* v2.\n\n"
        "Perintah tersedia:\n"
        "/status — data sensor real-time\n"
        "/foto   — foto terbaru\n"
        "/ai     — analisis kondisi kolam\n"
        "/notif  — atur notifikasi rutin\n"
        "/help   — bantuan",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg    = await update.message.reply_text("⏳ Mengambil data sensor...")
    sensor = get_sensor_data()
    teks   = format_status(sensor)
    kb     = [[InlineKeyboardButton("🔄 Refresh", callback_data="status")]]
    await msg.edit_text(teks, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb))


async def cmd_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("⏳ Mengambil foto terbaru...")
    try:
        svc             = get_drive_service()
        buf, _, caption = get_latest_photo(svc)
        if buf is None:
            await msg.edit_text(f"⚠️ {caption}")
            return
        await msg.delete()
        await update.message.reply_photo(
            photo=buf,
            caption=f"📷 *{caption}*\n_{datetime.now().strftime('%d/%m/%Y %H:%M')}_",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Gagal:\n`{e}`", parse_mode="Markdown")


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("🤖 Menganalisis kondisi kolam...")
    try:
        sensor               = get_sensor_data()
        svc                  = get_drive_service()
        buf, img_base64, _   = get_latest_photo(svc)
        ai_text              = analisis_ai(sensor, img_base64)
        teks = (
            f"🤖 *Analisis AI — Kondisi Kolam*\n"
            f"_{datetime.now().strftime('%d/%m/%Y %H:%M')}_\n\n"
            f"{ai_text}"
        )
        await msg.edit_text(teks, parse_mode=None)
    except Exception as e:
        await msg.edit_text(f"❌ Gagal:\n`{e}`", parse_mode="Markdown")


async def cmd_notif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    kb = [
        [InlineKeyboardButton("30 menit",   callback_data="notif_1800"),
         InlineKeyboardButton("1 jam",      callback_data="notif_3600")],
        [InlineKeyboardButton("2 jam",      callback_data="notif_7200"),
         InlineKeyboardButton("6 jam",      callback_data="notif_21600")],
        [InlineKeyboardButton("🔕 Matikan", callback_data="notif_off")],
    ]
    await update.message.reply_text(
        "⏰ *Atur interval notifikasi rutin:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "📋 *Daftar Perintah*\n\n"
        "/start  — menu utama\n"
        "/status — data sensor real-time\n"
        "/foto   — foto terbaru dari Drive\n"
        "/ai     — analisis kondisi kolam\n"
        "/notif  — atur interval notifikasi\n"
        "/help   — pesan ini\n\n"
        "💬 Atau ketik pertanyaan bebas tentang kolam.",
        parse_mode="Markdown"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    pertanyaan = update.message.text.strip()
    msg        = await update.message.reply_text("🤖 Sedang berpikir...")

    kata_foto = ["foto", "gambar", "capture", "lihat kolam", "kondisi kolam"]
    if any(k in pertanyaan.lower() for k in kata_foto):
        await msg.edit_text("⏳ Mengambil foto terbaru...")
        try:
            import re
            angka  = re.search(r'\d+', pertanyaan)
            jumlah = min(int(angka.group()) if angka else 1, 10)
            svc    = get_drive_service()
            photos, err = get_latest_photos(svc, jumlah)
            if err:
                await msg.edit_text(f"⚠️ {err}")
                return
            await msg.delete()
            for i, (buf, caption) in enumerate(photos):
                await update.message.reply_photo(
                    photo=buf,
                    caption=f"📷 {i+1}/{len(photos)} — {caption}"
                )
        except Exception as e:
            await msg.edit_text(f"❌ Gagal:\n`{e}`", parse_mode="Markdown")
        return

    sensor       = get_sensor_data()
    ph           = sensor.get("pH", "—")
    cahaya       = sensor.get("cahaya", "—")
    warna        = sensor.get("warna", "—")
    status_warna = sensor.get("status_warna", "—")
    ph_status    = sensor.get("pH_status", "normal")

    prompt = (
        f"Kamu adalah ahli budidaya mikroalga Spirulina. "
        f"Data sensor kolam saat ini:\n"
        f"- pH: {ph} (status: {ph_status})\n"
        f"- Cahaya: {cahaya} Lux\n"
        f"- Warna: {warna} ({status_warna})\n\n"
        f"Pertanyaan pengguna: {pertanyaan}\n\n"
        f"Jawab singkat, jelas, dalam Bahasa Indonesia."
    )

    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type" : "application/json"
        }
        body = {
            "model": "llama-3.3-70b-versatile",
            "messages"  : [{"role": "user", "content": prompt}],
            "max_tokens": 300
}
        r      = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=body, headers=headers, timeout=30
        )
        result  = r.json()
        jawaban = result["choices"][0]["message"]["content"].strip()
        await msg.edit_text(f"🤖 {jawaban}")
    except Exception as e:
        await msg.edit_text(f"❌ AI error:\n`{e}`", parse_mode="Markdown")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "status":
        sensor = get_sensor_data()
        teks   = format_status(sensor)
        kb     = [[InlineKeyboardButton("🔄 Refresh", callback_data="status")]]
        await q.edit_message_text(teks, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data == "foto":
        await q.edit_message_text("⏳ Mengambil foto...")
        try:
            svc             = get_drive_service()
            buf, _, caption = get_latest_photo(svc)
            if buf is None:
                await q.edit_message_text(f"⚠️ {caption}")
                return
            await q.message.reply_photo(
                photo=buf,
                caption=f"📷 *{caption}*\n_{datetime.now().strftime('%d/%m/%Y %H:%M')}_",
                parse_mode="Markdown"
            )
            await q.delete_message()
        except Exception as e:
            await q.edit_message_text(f"❌ Gagal:\n`{e}`", parse_mode="Markdown")

    elif data == "ai":
        await q.edit_message_text("🤖 Menganalisis kondisi kolam...")
        try:
            sensor               = get_sensor_data()
            svc                  = get_drive_service()
            buf, img_base64, _   = get_latest_photo(svc)
            ai_text              = analisis_ai(sensor, img_base64)
            await q.edit_message_text(
                f"🤖 *Analisis AI*\n_{datetime.now().strftime('%d/%m/%Y %H:%M')}_\n\n{ai_text}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await q.edit_message_text(f"❌ Gagal:\n`{e}`", parse_mode="Markdown")

    elif data == "help":
        await q.edit_message_text(
            "📋 /status /foto /ai /notif /help\n"
            "💬 Atau ketik pertanyaan bebas tentang kolam."
        )

    elif data.startswith("notif_"):
        val = data.replace("notif_", "")
        for job in context.job_queue.get_jobs_by_name("notif_rutin"):
            job.schedule_removal()

        if val == "off":
            await q.edit_message_text("🔕 Notifikasi rutin dimatikan.")
        else:
            interval = int(val)
            label    = {1800:"30 menit", 3600:"1 jam", 7200:"2 jam", 21600:"6 jam"}.get(interval, f"{interval}s")
            context.job_queue.run_repeating(
                kirim_notifikasi_rutin, interval=interval, first=10, name="notif_rutin"
            )
            await q.edit_message_text(
                f"✅ Notifikasi rutin aktif setiap *{label}*.",
                parse_mode="Markdown"
            )


# ================================================================
#  MAIN
# ================================================================

if __name__ == "__main__":
    print("=" * 50)
    print("Bot Telegram Mikroalga v2 - Railway")
    print("=" * 50)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("foto",   cmd_foto))
    app.add_handler(CommandHandler("ai",     cmd_ai))
    app.add_handler(CommandHandler("notif",  cmd_notif))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(
        kirim_notifikasi_rutin,
        interval=NOTIF_INTERVAL,
        first=60,
        name="notif_rutin"
    )

    print("Bot aktif.\n")
    # SESUDAH ✅
app.run_polling(
    allowed_updates=Update.ALL_TYPES,
    drop_pending_updates=True
)
