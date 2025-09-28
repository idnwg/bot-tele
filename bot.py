import os
import telebot
from dotenv import load_dotenv
from pathlib import Path
from threading import Thread
import subprocess
import time
from mega import Mega

# -------------------------------
# Environment
# -------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

JOB_QUEUE = []
MEGA = Mega()
MEGA_ACCOUNT = None

# -------------------------------
# Helper functions
# -------------------------------
def send_message(chat_id, text):
    bot.send_message(chat_id, text)

def login_mega(email, password):
    global MEGA_ACCOUNT
    try:
        MEGA_ACCOUNT = MEGA.login(email, password)
        return True, "Login Mega berhasil ✅"
    except Exception as e:
        return False, f"❌ Login gagal: {str(e)}"

def logout_mega():
    global MEGA_ACCOUNT
    MEGA_ACCOUNT = None
    return "✅ Logout Mega berhasil"

def list_mega_folders():
    if not MEGA_ACCOUNT:
        return []
    return [f['name'] for f in MEGA_ACCOUNT.get_files().values() if f['t'] == 1]  # t==1 is folder

def download_mega_folder(folder_name):
    if not MEGA_ACCOUNT:
        return False, "Mega belum login"
    files = MEGA_ACCOUNT.get_files()
    for f in files.values():
        if f.get('name') == folder_name:
            MEGA_ACCOUNT.download(f, dest=str(DOWNLOAD_PATH))
            return True, f"⬇️ Download folder {folder_name} selesai"
    return False, f"Folder {folder_name} tidak ditemukan"

def run_terabox_upload(local_path):
    try:
        proc = subprocess.run(
            ["python3", "teraboxcli/main.py", "upload", str(local_path)],
            capture_output=True,
            text=True
        )
        return proc.stdout
    except Exception as e:
        return str(e)

def run_dood_upload(file_path):
    # Placeholder: Implement Doodstream API
    return f"Doodstream uploaded: {file_path}"

def process_job(chat_id, jobs):
    for job in jobs:
        folder = job['folder']
        target = job['target']
        prefix = job.get('prefix', '')

        local_folder = DOWNLOAD_PATH / folder
        if not local_folder.exists():
            send_message(chat_id, f"❌ Folder {folder} tidak ada di downloads")
            continue

        # Rename files
        if prefix:
            for f in local_folder.iterdir():
                if f.is_file():
                    f.rename(f.parent / f"{prefix}_{f.name}")

        send_message(chat_id, f"⬇️ Mulai upload folder {folder} ke {', '.join(target)}")
        results = []

        if "terabox" in target:
            results.append(run_terabox_upload(local_folder))
        if "doodstream" in target:
            for f in local_folder.iterdir():
                if f.is_file():
                    results.append(run_dood_upload(f))

        send_message(chat_id, f"✅ Selesai folder {folder}!\n" + "\n".join(results))

# -------------------------------
# Bot Commands
# -------------------------------
@bot.message_handler(commands=["start", "help"])
def send_help(message):
    help_text = """
/loginmega <email> <password> - Login Mega
/logoutmega - Logout Mega
/listmega - List folder Mega
/download <folder1> <folder2> - Download folder Mega
/listfolders - Lihat folder lokal
/upload <folder1> ... <terabox/doodstream> - Upload folder lokal
/setprefix <prefix> - Prefix untuk rename
/status - Lihat job queue
"""
    send_message(message.chat.id, help_text)

@bot.message_handler(commands=["loginmega"])
def login_handler(message):
    try:
        _, email, password = message.text.split()
        ok, msg = login_mega(email, password)
        send_message(message.chat.id, msg)
    except Exception:
        send_message(message.chat.id, "Format salah! Gunakan: /loginmega email password")

@bot.message_handler(commands=["logoutmega"])
def logout_handler(message):
    msg = logout_mega()
    send_message(message.chat.id, msg)

@bot.message_handler(commands=["listmega"])
def list_mega_handler(message):
    folders = list_mega_folders()
    send_message(message.chat.id, "📂 Folder Mega:\n" + "\n".join(folders))

@bot.message_handler(commands=["download"])
def download_handler(message):
    try:
        args = message.text.split()[1:]
        if not args:
            send_message(message.chat.id, "Format salah! /download <folder1> ...")
            return
        for f in args:
            ok, msg = download_mega_folder(f)
            send_message(message.chat.id, msg)
    except Exception as e:
        send_message(message.chat.id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=["listfolders"])
def list_local_folders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    send_message(message.chat.id, "📂 Folder lokal:\n" + "\n".join(folders))

@bot.message_handler(commands=["upload"])
def upload_handler(message):
    try:
        args = message.text.split()[1:]
        folders = []
        targets = []
        for a in args:
            if a.lower() in ["terabox", "doodstream"]:
                targets.append(a.lower())
            else:
                folders.append(a)
        if not folders or not targets:
            send_message(message.chat.id, "Format salah! /upload <folder1> ... <terabox/doodstream>")
            return
        Thread(target=process_job, args=(message.chat.id, [{"folder": f, "target": targets} for f in folders])).start()
        send_message(message.chat.id, f"🔄 Job dimasukkan ke antrian: {folders} -> {targets}")
    except Exception as e:
        send_message(message.chat.id, f"❌ Error: {str(e)}")

# -------------------------------
# Run Bot
# -------------------------------
bot.infinity_polling()
