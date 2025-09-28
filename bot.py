#!/usr/bin/env python3
import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
from threading import Thread
import queue
import time
import requests

# --------------------------
# Load environment variables
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# --------------------------
# Paths & Global variables
# --------------------------
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

mega_account = {"email": None, "password": None}
mega_client = None
queue_jobs = queue.Queue()
default_delete = False
rename_prefix = ""

# --------------------------
# Helper functions
# --------------------------
def mega_login(email, password):
    global mega_client, mega_account
    mega_client = Mega().login(email, password)
    mega_account["email"] = email
    mega_account["password"] = password
    return True

def mega_logout():
    global mega_client, mega_account
    mega_client = None
    mega_account = {"email": None, "password": None}
    return True

def download_from_mega(url, dest_folder):
    """Download Mega file/folder"""
    if not mega_client:
        return "❌ Mega belum login"
    try:
        mega_client.download_url(url, dest_folder)
        return "✅ Download selesai"
    except Exception as e:
        return f"❌ Download gagal: {e}"

def upload_to_terabox(filepath):
    """Upload file to Terabox"""
    if not TERABOX_KEY:
        return "❌ Terabox key tidak tersedia"
    # Contoh request upload
    files = {'file': open(filepath, 'rb')}
    headers = {"Authorization": f"Bearer {TERABOX_KEY}"}
    response = requests.post("https://www.terabox.com/api/upload", headers=headers, files=files)
    if response.status_code == 200:
        return f"✅ Upload selesai: {filepath.name}"
    return f"❌ Upload gagal: {response.text}"

def upload_to_doodstream(filepath):
    """Upload file to DoodStream"""
    if not DOOD_KEY:
        return "❌ DoodStream key tidak tersedia"
    files = {'file': open(filepath, 'rb')}
    data = {'key': DOOD_KEY}
    response = requests.post("https://doodapi.com/api/upload", data=data, files=files)
    if response.status_code == 200:
        return f"✅ Upload selesai: {filepath.name}"
    return f"❌ Upload gagal: {response.text}"

def rename_file(filepath):
    global rename_prefix
    new_name = f"{rename_prefix}_{filepath.name}" if rename_prefix else filepath.name
    new_path = filepath.parent / new_name
    filepath.rename(new_path)
    return new_path

# --------------------------
# Job processor
# --------------------------
def process_jobs():
    while True:
        job = queue_jobs.get()
        if not job:
            continue
        file_path, upload_to = job
        renamed = rename_file(file_path)
        if upload_to == "terabox":
            upload_to_terabox(renamed)
        elif upload_to == "doodstream":
            upload_to_doodstream(renamed)
        if default_delete:
            os.remove(renamed)
        queue_jobs.task_done()

Thread(target=process_jobs, daemon=True).start()

# --------------------------
# Telegram Bot Handlers
# --------------------------
@bot.message_handler(commands=["start", "help"])
def send_help(message):
    help_text = (
        "/status - Lihat job & antrian\n"
        "/cleanup list|all|<folder> - Kelola folder lokal\n"
        "/set_delete on|off - Set default delete after upload\n"
        "/setprefix <prefix> - Set prefix untuk rename foto/video\n"
        "/listfolders - Lihat folder di downloads\n"
        "/loginmega <email> <password> - Login Mega\n"
        "/logoutmega - Logout Mega\n"
        "/download <mega_link> <terabox|doodstream> - Download & upload\n"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=["loginmega"])
def handle_login_mega(message):
    try:
        _, email, password = message.text.split()
        mega_login(email, password)
        bot.reply_to(message, "✅ Login Mega berhasil")
    except Exception as e:
        bot.reply_to(message, f"❌ Gagal login Mega: {e}")

@bot.message_handler(commands=["logoutmega"])
def handle_logout_mega(message):
    mega_logout()
    bot.reply_to(message, "✅ Logout Mega berhasil")

@bot.message_handler(commands=["download"])
def handle_download(message):
    try:
        parts = message.text.split()
        link = parts[1]
        target = parts[2].lower() if len(parts) > 2 else "terabox"
        # Simulasi download folder/file ke ./downloads
        filename = link.split("/")[-1]
        dest_path = DOWNLOAD_PATH / filename
        # Masukkan ke queue
        queue_jobs.put((dest_path, target))
        bot.reply_to(message, f"✅ Job ditambahkan ke queue: {filename} → {target}")
    except Exception as e:
        bot.reply_to(message, f"❌ Gagal menambahkan job: {e}")

@bot.message_handler(commands=["listfolders"])
def list_folders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.reply_to(message, "📂 Tidak ada folder di downloads")
    else:
        bot.reply_to(message, "📂 Folder:\n" + "\n".join(folders))

@bot.message_handler(commands=["setprefix"])
def set_prefix(message):
    global rename_prefix
    try:
        rename_prefix = message.text.split()[1]
        bot.reply_to(message, f"✅ Prefix di-set: {rename_prefix}")
    except:
        bot.reply_to(message, "❌ Gunakan: /setprefix <prefix>")

@bot.message_handler(commands=["set_delete"])
def set_delete(message):
    global default_delete
    arg = message.text.split()[1].lower()
    default_delete = True if arg == "on" else False
    bot.reply_to(message, f"✅ Delete after upload di-set: {default_delete}")

@bot.message_handler(commands=["cleanup"])
def cleanup(message):
    args = message.text.split()[1:]
    if not args:
        bot.reply_to(message, "❌ Gunakan: /cleanup list|all|<folder>")
        return
    cmd = args[0].lower()
    if cmd == "list":
        folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
        bot.reply_to(message, "Folder lokal:\n" + "\n".join(folders))
    elif cmd == "all":
        for f in DOWNLOAD_PATH.iterdir():
            if f.is_dir():
                for file in f.iterdir():
                    file.unlink()
                f.rmdir()
        bot.reply_to(message, "✅ Semua folder dihapus")
    else:
        folder = DOWNLOAD_PATH / cmd
        if folder.exists() and folder.is_dir():
            for file in folder.iterdir():
                file.unlink()
            folder.rmdir()
            bot.reply_to(message, f"✅ Folder {cmd} dihapus")
        else:
            bot.reply_to(message, "❌ Folder tidak ditemukan")

@bot.message_handler(commands=["status"])
def status(message):
    pending = queue_jobs.qsize()
    bot.reply_to(message, f"📌 Antrian job saat ini: {pending}")

# --------------------------
# Start bot polling
# --------------------------
print("Bot started...")
bot.infinity_polling()
