import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
from threading import Thread
import time
import json
import requests

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan di .env")

bot = telebot.TeleBot(BOT_TOKEN)

DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Globals
mega_client = None
mega_email = None
task_queue = []

DELETE_AFTER_UPLOAD = True
PREFIX_RENAME = ""

# ---------------- Mega Login/Logout ----------------

def mega_login(email, password):
    global mega_client, mega_email
    try:
        mega = Mega()
        m = mega.login(email, password)
        if not m:
            return False, "Login gagal: Mega tidak merespon atau credentials salah"
        mega_client = m
        mega_email = email
        return True, f"Login sukses sebagai {email}"
    except Exception as e:
        return False, f"Login gagal: {str(e)}"

def mega_logout():
    global mega_client, mega_email
    mega_client = None
    mega_email = None
    return True, "Logout Mega sukses."

# ---------------- Download/Upload ----------------

def download_mega_file(url, folder):
    global mega_client
    if not mega_client:
        return False, "Silakan login Mega terlebih dahulu!"
    try:
        info = mega_client.get_url_info(url)
        filename = info['name']
        path = DOWNLOAD_PATH / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        mega_client.download_url(url, dest=str(path.parent))
        return True, str(path)
    except Exception as e:
        return False, f"Download gagal: {str(e)}"

def upload_terabox(file_path):
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://www.terabox.com/share/upload",
                headers={"Authorization": f"Bearer {TERABOX_KEY}"},
                files={"file": f}
            )
        if resp.status_code == 200:
            return True, "Upload Terabox sukses"
        else:
            return False, f"Upload Terabox gagal: {resp.text}"
    except Exception as e:
        return False, f"Upload Terabox gagal: {str(e)}"

def upload_doodstream(file_path):
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://doodstream.com/api/upload",
                headers={"Authorization": DOOD_KEY},
                files={"file": f}
            )
        if resp.status_code == 200:
            return True, "Upload Doodstream sukses"
        else:
            return False, f"Upload Doodstream gagal: {resp.text}"
    except Exception as e:
        return False, f"Upload Doodstream gagal: {str(e)}"

# ---------------- Bot Handlers ----------------

@bot.message_handler(commands=["help"])
def help_handler(message):
    help_text = """
/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login akun Mega
/logoutmega - Logout Mega
/download <url> <folder> <terabox|doodstream> - Download dari Mega & upload
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=["loginmega"])
def login_mega_handler(message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "❌ Format salah! Gunakan: /loginmega email password")
            return
        email = parts[1]
        password = parts[2]
        success, text = mega_login(email, password)
        bot.reply_to(message, text)
    except Exception as e:
        bot.reply_to(message, f"Login error: {str(e)}")

@bot.message_handler(commands=["logoutmega"])
def logout_mega_handler(message):
    success, text = mega_logout()
    bot.reply_to(message, text)

@bot.message_handler(commands=["download"])
def download_handler(message):
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            bot.reply_to(message, "Format: /download <url> <folder> <terabox|doodstream>")
            return
        url, folder, target = parts[1], parts[2], parts[3].lower()
        task_queue.append((url, folder, target, message.chat.id))
        bot.reply_to(message, f"Task ditambahkan ke antrian: {url} → {target}")
    except Exception as e:
        bot.reply_to(message, f"Download error: {str(e)}")

# ---------------- Worker Thread ----------------

def task_worker():
    while True:
        if task_queue:
            url, folder, target, chat_id = task_queue.pop(0)
            bot.send_message(chat_id, f"Mulai proses: {url} → {target}")
            success, result = download_mega_file(url, folder)
            if not success:
                bot.send_message(chat_id, result)
                continue
            file_path = result
            if PREFIX_RENAME:
                new_path = file_path.parent / (PREFIX_RENAME + "_" + file_path.name)
                file_path.rename(new_path)
                file_path = new_path
            if target == "terabox":
                success, msg = upload_terabox(file_path)
            elif target == "doodstream":
                success, msg = upload_doodstream(file_path)
            else:
                msg = f"Target {target} tidak dikenal!"
                success = False
            bot.send_message(chat_id, msg)
            if DELETE_AFTER_UPLOAD and success:
                os.remove(file_path)
        time.sleep(1)

Thread(target=task_worker, daemon=True).start()

# ---------------- Start Bot ----------------
print("Bot started...")
bot.infinity_polling()
