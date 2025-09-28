import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
from threading import Thread
import time
import queue
import requests

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan di .env")

bot = telebot.TeleBot(BOT_TOKEN)

# Paths
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Globals
mega_client = None
mega_email = None
download_queue = queue.Queue()
delete_after_upload = True
prefix_rename = ""

# Help text
HELP_TEXT = """
/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login akun Mega
/logoutmega - Logout akun Mega
"""

# --------------------- Mega Functions ---------------------
def mega_login(email, password):
    global mega_client, mega_email
    try:
        mega = Mega()
        m = mega.login(email, password)
        mega_client = m
        mega_email = email
        return True, f"Login sukses sebagai {email}"
    except Exception as e:
        return False, f"Login gagal: {str(e)}"

def mega_logout():
    global mega_client, mega_email
    mega_client = None
    mega_email = None
    return "Logout Mega berhasil"

def list_mega_folders():
    if not mega_client:
        return []
    try:
        files = mega_client.get_files()
        folders = set()
        for f in files.values():
            if f['t'] == 1:  # folder
                folders.add(f['a']['n'])
        return list(folders)
    except:
        return []

def download_mega_folder(folder_name):
    if not mega_client:
        return False, "Belum login Mega"
    try:
        folder_path = DOWNLOAD_PATH / folder_name
        folder_path.mkdir(exist_ok=True)
        files = mega_client.get_files()
        for f in files.values():
            if f['t'] == 0 and f.get('p') and files[f['p']]['a']['n'] == folder_name:
                mega_client.download(f, dest=str(folder_path))
        return True, f"Download folder {folder_name} selesai"
    except Exception as e:
        return False, f"Download gagal: {str(e)}"

# --------------------- Queue Worker ---------------------
def worker():
    while True:
        job = download_queue.get()
        if not job:
            continue
        folder, service = job
        bot.send_message(job[2], f"Memproses folder {folder} → {service}")
        success, msg = download_mega_folder(folder)
        bot.send_message(job[2], msg)
        if success:
            # Upload
            if service.lower() == "terabox":
                # Implement Terabox upload here
                bot.send_message(job[2], f"Upload {folder} ke Terabox selesai")
            elif service.lower() == "doodstream":
                # Implement Doodstream upload here
                bot.send_message(job[2], f"Upload {folder} ke Doodstream selesai")
            if delete_after_upload:
                import shutil
                shutil.rmtree(DOWNLOAD_PATH / folder)
        download_queue.task_done()

# Start worker thread
Thread(target=worker, daemon=True).start()

# --------------------- Bot Commands ---------------------
@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(message, HELP_TEXT)

@bot.message_handler(commands=["status"])
def cmd_status(message):
    bot.reply_to(message, f"Job dalam antrian: {download_queue.qsize()}")

@bot.message_handler(commands=["listfolders"])
def cmd_listfolders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if folders:
        bot.reply_to(message, "\n".join(folders))
    else:
        bot.reply_to(message, "Tidak ada folder di downloads")

@bot.message_handler(commands=["set_delete"])
def cmd_set_delete(message):
    global delete_after_upload
    args = message.text.split()
    if len(args) == 2 and args[1].lower() in ["on", "off"]:
        delete_after_upload = args[1].lower() == "on"
        bot.reply_to(message, f"Delete after upload di-set {args[1].lower()}")
    else:
        bot.reply_to(message, "Gunakan: /set_delete on|off")

@bot.message_handler(commands=["setprefix"])
def cmd_setprefix(message):
    global prefix_rename
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        prefix_rename = args[1]
        bot.reply_to(message, f"Prefix rename di-set ke: {prefix_rename}")
    else:
        bot.reply_to(message, "Gunakan: /setprefix <prefix>")

@bot.message_handler(commands=["cleanup"])
def cmd_cleanup(message):
    args = message.text.split(maxsplit=1)
    if len(args) == 1 or args[1].lower() == "list":
        folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
        bot.reply_to(message, "Folder lokal:\n" + "\n".join(folders))
    elif args[1].lower() == "all":
        import shutil
        for f in DOWNLOAD_PATH.iterdir():
            if f.is_dir():
                shutil.rmtree(f)
        bot.reply_to(message, "Semua folder dihapus")
    else:
        folder = args[1]
        path = DOWNLOAD_PATH / folder
        if path.exists():
            import shutil
            shutil.rmtree(path)
            bot.reply_to(message, f"Folder {folder} dihapus")
        else:
            bot.reply_to(message, f"Folder {folder} tidak ada")

@bot.message_handler(commands=["loginmega"])
def cmd_loginmega(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "❌ Format salah! Gunakan: /loginmega email password")
        return
    email, password = parts[1], parts[2]
    success, msg = mega_login(email, password)
    bot.reply_to(message, msg)

@bot.message_handler(commands=["logoutmega"])
def cmd_logoutmega(message):
    msg = mega_logout()
    bot.reply_to(message, msg)

@bot.message_handler(commands=["download"])
def cmd_download(message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Gunakan: /download <folder> <service: Terabox|Doodstream>")
        return
    folder, service = args[1], args[2]
    if not (DOWNLOAD_PATH / folder).exists():
        bot.reply_to(message, f"Folder {folder} tidak ada di lokal, menambahkan ke antrian download")
    download_queue.put((folder, service, message.chat.id))
    bot.reply_to(message, f"Folder {folder} dimasukkan ke antrian → {service}")

# --------------------- Start Bot ---------------------
print("Bot started...")
bot.infinity_polling()
