import os
import telebot
from dotenv import load_dotenv
from mega import Mega
import requests
from pathlib import Path
from threading import Thread
import queue
import time
import shutil

# ===== Load environment =====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)

# ===== Paths =====
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# ===== Global variables =====
mega_instance = None
mega_account = None
job_queue = queue.Queue()
delete_after_upload = True
rename_prefix = ""

# ===== Helper functions =====
def safe_mega_login(email, password):
    global mega_instance, mega_account
    try:
        mega_instance = Mega()
        mega_account = mega_instance.login(email, password)
        return True, "Login Mega sukses!"
    except Exception as e:
        return False, f"Login gagal: {str(e)}"

def mega_logout():
    global mega_account
    mega_account = None
    return "Logout Mega berhasil!"

def list_local_folders():
    return [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]

def download_mega_folder(folder_name):
    if not mega_account:
        return False, "Belum login Mega!"
    try:
        folder_path = DOWNLOAD_PATH / folder_name
        folder_path.mkdir(exist_ok=True)
        # List files in Mega folder
        mega_files = mega_account.get_files()
        for file_id, meta in mega_files.items():
            if meta.get('t') == 0 and folder_name in meta.get('n', ''):  # file
                mega_account.download(file_id, dest=str(folder_path))
        return True, f"Download {folder_name} selesai!"
    except Exception as e:
        return False, f"Download gagal: {str(e)}"

def rename_files(folder: Path):
    for file in folder.iterdir():
        if file.is_file():
            new_name = f"{rename_prefix}{file.name}" if rename_prefix else file.name
            file.rename(folder / new_name)

def upload_terabox(file_path: Path):
    # Contoh minimal upload
    try:
        url = "https://api.terabox.com/v1/file/upload"
        headers = {"Authorization": f"Bearer {TERABOX_KEY}"}
        files = {"file": open(file_path, "rb")}
        r = requests.post(url, headers=headers, files=files)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def upload_doodstream(file_path: Path):
    try:
        url = f"https://doodstream.com/api/upload?key={DOOD_KEY}"
        files = {"file": open(file_path, "rb")}
        r = requests.post(url, files=files)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

# ===== Job worker =====
def job_worker():
    while True:
        try:
            job = job_queue.get()
            if job["action"] == "download":
                folder = job["folder"]
                bot.send_message(job["chat_id"], f"Mulai download folder: {folder}")
                success, msg = download_mega_folder(folder)
                bot.send_message(job["chat_id"], msg)
                if success:
                    folder_path = DOWNLOAD_PATH / folder
                    rename_files(folder_path)
                    target = job.get("target")
                    if target == "terabox":
                        for f in folder_path.iterdir():
                            s, msg = upload_terabox(f)
                            bot.send_message(job["chat_id"], f"Upload Terabox: {msg}")
                    elif target == "doodstream":
                        for f in folder_path.iterdir():
                            s, msg = upload_doodstream(f)
                            bot.send_message(job["chat_id"], f"Upload Doodstream: {msg}")
                    if delete_after_upload:
                        shutil.rmtree(folder_path)
            job_queue.task_done()
        except Exception as e:
            print(f"Worker error: {e}")

Thread(target=job_worker, daemon=True).start()

# ===== Bot commands =====
@bot.message_handler(commands=["help"])
def cmd_help(message):
    help_text = """
/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login akun Mega
/logoutmega - Logout akun Mega
/download <folder> <terabox|doodstream> - Pilih folder Mega untuk di-download & upload
"""
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=["loginmega"])
def cmd_loginmega(message):
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Format salah! Gunakan: /loginmega email password")
            return
        email, password = parts[1], parts[2]
        success, msg = safe_mega_login(email, password)
        bot.send_message(message.chat.id, msg)
    except Exception as e:
        bot.send_message(message.chat.id, f"Login error: {str(e)}")

@bot.message_handler(commands=["logoutmega"])
def cmd_logoutmega(message):
    msg = mega_logout()
    bot.send_message(message.chat.id, msg)

@bot.message_handler(commands=["listfolders"])
def cmd_listfolders(message):
    folders = list_local_folders()
    bot.send_message(message.chat.id, "Folder lokal:\n" + "\n".join(folders))

@bot.message_handler(commands=["setprefix"])
def cmd_setprefix(message):
    global rename_prefix
    parts = message.text.split(" ", 1)
    rename_prefix = parts[1] if len(parts) > 1 else ""
    bot.send_message(message.chat.id, f"Prefix rename di-set: {rename_prefix}")

@bot.message_handler(commands=["set_delete"])
def cmd_set_delete(message):
    global delete_after_upload
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Format: /set_delete on|off")
        return
    delete_after_upload = parts[1].lower() == "on"
    bot.send_message(message.chat.id, f"Delete after upload: {delete_after_upload}")

@bot.message_handler(commands=["download"])
def cmd_download(message):
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        bot.send_message(message.chat.id, "Format: /download <folder> <terabox|doodstream>")
        return
    folder, target = parts[1], parts[2].lower()
    job_queue.put({"action": "download", "folder": folder, "target": target, "chat_id": message.chat.id})
    bot.send_message(message.chat.id, f"Folder {folder} dimasukkan antrian untuk upload ke {target}")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    bot.send_message(message.chat.id, f"Ada {job_queue.qsize()} job dalam antrian.")

# ===== Polling =====
print("Bot started...")
bot.infinity_polling()
