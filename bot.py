import os
import telebot
from dotenv import load_dotenv
from pathlib import Path
from threading import Thread, Lock
import time
import json
import requests
import queue
import subprocess
import shlex

# -------------------
# Load environment
# -------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# -------------------
# Global variables
# -------------------
job_queue = queue.Queue()
job_lock = Lock()
mega_logged_in = False
mega_email = ""
mega_password = ""
rename_prefix = ""
auto_delete = False

# -------------------
# Helper Functions
# -------------------
def send_status(chat_id):
    with job_lock:
        pending = list(job_queue.queue)
    if pending:
        msg = "📋 Job Queue:\n" + "\n".join(f"{i+1}. {j['folder']} → {j['upload_target']}" for i,j in enumerate(pending))
    else:
        msg = "✅ Tidak ada job aktif."
    bot.send_message(chat_id, msg)

def download_mega(folder, chat_id):
    # Mega CMD download
    folder_path = DOWNLOAD_PATH / folder
    folder_path.mkdir(exist_ok=True)
    bot.send_message(chat_id, f"⏳ Downloading Mega folder: {folder}")
    try:
        cmd = f'mega-get "{folder}" "{folder_path}"'
        subprocess.run(shlex.split(cmd), check=True)
    except subprocess.CalledProcessError:
        bot.send_message(chat_id, f"❌ Gagal download folder {folder}")
        return False
    return True

def upload_terabox(folder_path, chat_id):
    # Terabox upload via API
    bot.send_message(chat_id, f"⏳ Uploading {folder_path.name} → Terabox")
    files = {"file": open(folder_path, "rb")} if folder_path.is_file() else None
    data = {"connect_key": TERABOX_KEY}
    if folder_path.is_dir():
        # upload folder as zip
        zip_file = f"{folder_path}.zip"
        subprocess.run(["zip", "-r", zip_file, str(folder_path)], check=True)
        files = {"file": open(zip_file, "rb")}
    try:
        resp = requests.post("https://api.terabox.com/upload", data=data, files=files)
        if resp.status_code == 200:
            bot.send_message(chat_id, f"✅ Upload selesai: {folder_path.name}")
        else:
            bot.send_message(chat_id, f"❌ Upload gagal: {folder_path.name}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Upload error: {e}")

def upload_doodstream(file_path, chat_id):
    # Doodstream upload via API
    bot.send_message(chat_id, f"⏳ Uploading {file_path.name} → Doodstream")
    files = {"file": open(file_path, "rb")}
    data = {"key": DOOD_KEY}
    try:
        resp = requests.post("https://doodstream.com/api/upload", data=data, files=files)
        if resp.status_code == 200:
            bot.send_message(chat_id, f"✅ Upload selesai: {file_path.name}")
        else:
            bot.send_message(chat_id, f"❌ Upload gagal: {file_path.name}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Upload error: {e}")

def process_jobs():
    while True:
        job = job_queue.get()
        folder = job["folder"]
        target = job["upload_target"]
        chat_id = job["chat_id"]

        folder_path = DOWNLOAD_PATH / folder
        success = download_mega(folder, chat_id)
        if not success:
            job_queue.task_done()
            continue

        # rename if prefix set
        if rename_prefix:
            for f in folder_path.iterdir():
                f.rename(folder_path / f"{rename_prefix}_{f.name}")

        if target == "terabox":
            upload_terabox(folder_path, chat_id)
        elif target == "doodstream":
            for f in folder_path.iterdir():
                upload_doodstream(f, chat_id)

        if auto_delete:
            subprocess.run(["rm", "-rf", str(folder_path)])
        job_queue.task_done()

# Start background worker
Thread(target=process_jobs, daemon=True).start()

# -------------------
# Command Handlers
# -------------------
@bot.message_handler(commands=["start", "help"])
def send_help(message):
    help_text = """
📌 Perintah Bot:

/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login akun Mega
/logoutmega - Logout akun Mega
/download <folder> terabox|doodstream - Download Mega & upload
"""
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=["status"])
def handle_status(message):
    send_status(message.chat.id)

@bot.message_handler(commands=["setprefix"])
def handle_setprefix(message):
    global rename_prefix
    parts = message.text.strip().split(" ", 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan /setprefix <prefix>")
        return
    rename_prefix = parts[1].strip()
    bot.send_message(message.chat.id, f"✅ Prefix di-set: {rename_prefix}")

@bot.message_handler(commands=["set_delete"])
def handle_set_delete(message):
    global auto_delete
    parts = message.text.strip().split(" ", 1)
    if len(parts) < 2 or parts[1] not in ["on", "off"]:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan /set_delete on|off")
        return
    auto_delete = True if parts[1] == "on" else False
    bot.send_message(message.chat.id, f"✅ Auto delete di-set: {auto_delete}")

@bot.message_handler(commands=["loginmega"])
def handle_loginmega(message):
    global mega_logged_in, mega_email, mega_password
    parts = message.text.strip().split(" ", 2)
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan /loginmega email password")
        return
    email, password = parts[1], parts[2]
    try:
        subprocess.run(["mega-login", email, password], check=True)
        mega_logged_in = True
        mega_email = email
        mega_password = password
        bot.send_message(message.chat.id, f"✅ Login Mega berhasil: {email}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Login gagal: {e}")

@bot.message_handler(commands=["logoutmega"])
def handle_logoutmega(message):
    global mega_logged_in, mega_email, mega_password
    try:
        subprocess.run(["mega-logout"], check=True)
        mega_logged_in = False
        mega_email = ""
        mega_password = ""
        bot.send_message(message.chat.id, "✅ Logout Mega berhasil")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Logout gagal: {e}")

@bot.message_handler(commands=["listfolders"])
def handle_listfolders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.send_message(message.chat.id, "📂 Tidak ada folder di downloads")
        return
    bot.send_message(message.chat.id, "📂 Folder lokal:\n" + "\n".join(folders))

@bot.message_handler(commands=["download"])
def handle_download(message):
    parts = message.text.strip().split(" ", 2)
    if len(parts) < 3 or parts[2] not in ["terabox", "doodstream"]:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan /download <folder> terabox|doodstream")
        return
    folder = parts[1]
    target = parts[2]
    job_queue.put({"folder": folder, "upload_target": target, "chat_id": message.chat.id})
    bot.send_message(message.chat.id, f"✅ Job ditambahkan: {folder} → {target}")

@bot.message_handler(commands=["cleanup"])
def handle_cleanup(message):
    parts = message.text.strip().split(" ", 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan /cleanup list|all|<folder>")
        return
    arg = parts[1].strip()
    if arg == "list":
        folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
        bot.send_message(message.chat.id, "📂 Folder lokal:\n" + "\n".join(folders))
    elif arg == "all":
        subprocess.run(["rm", "-rf", str(DOWNLOAD_PATH)])
        DOWNLOAD_PATH.mkdir(exist_ok=True)
        bot.send_message(message.chat.id, "✅ Semua folder dihapus")
    else:
        folder_path = DOWNLOAD_PATH / arg
        if folder_path.exists():
            subprocess.run(["rm", "-rf", str(folder_path)])
            bot.send_message(message.chat.id, f"✅ Folder dihapus: {arg}")
        else:
            bot.send_message(message.chat.id, f"❌ Folder tidak ditemukan: {arg}")

# -------------------
# Start polling
# -------------------
print("Bot siap dijalankan...")
bot.infinity_polling()
