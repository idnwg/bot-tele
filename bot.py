import os
import json
import telebot
import subprocess
from dotenv import load_dotenv
from pathlib import Path
from threading import Thread
import time
import requests

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_PATH = os.getenv("TERABOX_PATH", "./teraboxcli/main.py")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN)

# Job Queue
job_queue = []

# Helper: send message safely
def send_safe(chat_id, text):
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print(f"Failed to send message: {e}")

# --- Telegram Commands ---

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = """
/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login Mega
/logoutmega - Logout Mega
/download <folder1> [folder2 ...] - Download folder dari Mega
/upload <folder1> [folder2 ...] - Upload folder ke Terabox/Doodstream
"""
    send_safe(message.chat.id, help_text)

@bot.message_handler(commands=['status'])
def status_cmd(message):
    if not job_queue:
        send_safe(message.chat.id, "Tidak ada job dalam antrian.")
    else:
        status = "\n".join([f"{i+1}. {job['type']} {job['folder']}" for i, job in enumerate(job_queue)])
        send_safe(message.chat.id, f"Job Queue:\n{status}")

# Example: login Mega
@bot.message_handler(commands=['loginmega'])
def login_mega(message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            send_safe(message.chat.id, "❌ Format salah! Gunakan: /loginmega email password")
            return
        email, password = parts[1], parts[2]
        # Save credentials locally
        cred_file = Path("./mega.json")
        with open(cred_file, "w") as f:
            json.dump({"email": email, "password": password}, f)
        send_safe(message.chat.id, "✅ Mega login info tersimpan. Bot siap mengakses Mega.")
    except Exception as e:
        send_safe(message.chat.id, f"❌ Login gagal: {e}")

# Logout Mega
@bot.message_handler(commands=['logoutmega'])
def logout_mega(message):
    try:
        cred_file = Path("./mega.json")
        if cred_file.exists():
            cred_file.unlink()
        send_safe(message.chat.id, "✅ Logout Mega berhasil.")
    except Exception as e:
        send_safe(message.chat.id, f"❌ Logout Mega gagal: {e}")

# Upload command
@bot.message_handler(commands=['upload'])
def upload_cmd(message):
    parts = message.text.split()[1:]
    if not parts:
        send_safe(message.chat.id, "❌ Harap sebutkan folder yang ingin di-upload.")
        return
    folders = [DOWNLOAD_PATH / f for f in parts if (DOWNLOAD_PATH / f).exists()]
    if not folders:
        send_safe(message.chat.id, "❌ Folder tidak ditemukan di downloads.")
        return

    # Pilih target upload
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add("Terabox", "Doodstream", "Keduanya")
    msg = bot.send_message(message.chat.id, "Pilih target upload:", reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: enqueue_upload(m, folders))

def enqueue_upload(msg, folders):
    target = msg.text.lower()
    for folder in folders:
        job_queue.append({"type": "upload", "folder": folder, "target": target})
    send_safe(msg.chat.id, f"✅ {len(folders)} folder ditambahkan ke antrian untuk upload ke {target}.")
    Thread(target=process_jobs, args=(msg.chat.id,)).start()

# --- Job Processing ---
def process_jobs(chat_id):
    while job_queue:
        job = job_queue.pop(0)
        folder = job['folder']
        target = job['target']
        send_safe(chat_id, f"🚀 Memproses job: {folder.name} -> {target}")

        # Upload Terabox
        if target in ["terabox", "keduanya"]:
            try:
                subprocess.run(["python3", TERABOX_PATH, "upload", str(folder), "--recursive"], check=True)
                send_safe(chat_id, f"✅ Terabox upload selesai: {folder}")
            except Exception as e:
                send_safe(chat_id, f"❌ Terabox upload gagal: {e}")

        # Upload Doodstream
        if target in ["doodstream", "keduanya"] and DOOD_KEY:
            try:
                files = list(folder.glob("**/*.*"))
                for f in files:
                    r = requests.post("https://doodapi.com/api/upload", files={"file": open(f, "rb")}, data={"key": DOOD_KEY})
                    if r.status_code == 200:
                        send_safe(chat_id, f"✅ Doodstream upload: {f.name}")
                    else:
                        send_safe(chat_id, f"❌ Doodstream gagal: {f.name}")
            except Exception as e:
                send_safe(chat_id, f"❌ Doodstream upload gagal: {e}")

    send_safe(chat_id, "🎉 Semua job selesai!")

# --- Start Polling ---
bot.infinity_polling()
