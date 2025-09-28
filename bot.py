import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
from threading import Thread
import queue
import time
import requests

# --- Load environment ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# --- Global vars ---
download_queue = queue.Queue()
prefix = ""
auto_delete = False
mega_client = None
mega_logged_in = False
mega_email = None

# --- Utilities ---
def send_status(chat_id):
    qsize = download_queue.qsize()
    bot.send_message(chat_id, f"Antrian download: {qsize}")

def rename_file(file_path, prefix=""):
    new_name = f"{prefix}{file_path.name}" if prefix else file_path.name
    new_path = file_path.parent / new_name
    file_path.rename(new_path)
    return new_path

def upload_terabox(file_path):
    url = "https://api.terabox.com/upload"
    headers = {"Authorization": f"Bearer {TERABOX_KEY}"}
    with open(file_path, "rb") as f:
        r = requests.post(url, headers=headers, files={"file": f})
    return r.status_code == 200

def upload_doodstream(file_path):
    url = "https://doodapi.com/upload"
    headers = {"Authorization": f"Bearer {DOOD_KEY}"}
    with open(file_path, "rb") as f:
        r = requests.post(url, headers=headers, files={"file": f})
    return r.status_code == 200

# --- Mega ---
def login_mega(email, password):
    global mega_client, mega_logged_in, mega_email
    mega_client = Mega()
    mega_client = mega_client.login(email, password)
    mega_logged_in = True
    mega_email = email

def logout_mega():
    global mega_client, mega_logged_in, mega_email
    mega_client = None
    mega_logged_in = False
    mega_email = None

def list_mega_folders():
    if not mega_logged_in:
        return []
    files = mega_client.get_files()
    folders = set(f["a"]["n"] for f in files.values() if f.get("t") == 1)
    return list(folders)

def download_mega_folder(folder_name):
    if not mega_logged_in:
        return None
    folder_id = None
    for k, v in mega_client.get_files().items():
        if v.get("a", {}).get("n") == folder_name and v.get("t") == 1:
            folder_id = k
            break
    if not folder_id:
        return None
    dest = DOWNLOAD_PATH / folder_name
    dest.mkdir(exist_ok=True)
    mega_client.download_folder(folder_id, str(dest))
    return dest

# --- Worker thread ---
def worker():
    while True:
        item = download_queue.get()
        if not item:
            continue
        chat_id, folder_name, upload_target = item
        bot.send_message(chat_id, f"Download dimulai: {folder_name}")
        path = download_mega_folder(folder_name)
        if not path:
            bot.send_message(chat_id, "Folder Mega tidak ditemukan atau belum login!")
            download_queue.task_done()
            continue
        # rename all files
        for file in path.iterdir():
            rename_file(file, prefix)
        # upload
        success = False
        if upload_target.lower() == "terabox":
            success = all(upload_terabox(f) for f in path.iterdir())
        elif upload_target.lower() == "doodstream":
            success = all(upload_doodstream(f) for f in path.iterdir())
        bot.send_message(chat_id, f"Upload selesai: {folder_name}, success={success}")
        if auto_delete:
            for f in path.iterdir():
                f.unlink()
            path.rmdir()
        download_queue.task_done()

Thread(target=worker, daemon=True).start()

# --- Bot commands ---
@bot.message_handler(commands=["start", "help"])
def help_msg(message):
    msg = (
        "/status - Lihat job & antrian\n"
        "/cleanup list|all|<folder> - Kelola folder lokal\n"
        "/set_delete on|off - Set default delete after upload\n"
        "/setprefix <prefix> - Set prefix untuk rename foto/video\n"
        "/listfolders - Lihat folder di downloads\n"
        "/loginmega <email> <pass> - Login akun Mega\n"
        "/logoutmega - Logout akun Mega\n"
        "/download <folder1> [folder2 ...] <terabox|doodstream> - Pilih folder dan upload target\n"
    )
    bot.reply_to(message, msg)

@bot.message_handler(commands=["status"])
def status_cmd(message):
    send_status(message.chat.id)

@bot.message_handler(commands=["set_delete"])
def set_delete_cmd(message):
    global auto_delete
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: /set_delete on|off")
        return
    auto_delete = args[1].lower() == "on"
    bot.reply_to(message, f"Auto delete set to {auto_delete}")

@bot.message_handler(commands=["setprefix"])
def set_prefix_cmd(message):
    global prefix
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Usage: /setprefix <prefix>")
        return
    prefix = args[1]
    bot.reply_to(message, f"Prefix set to: {prefix}")

@bot.message_handler(commands=["listfolders"])
def listfolders_cmd(message):
    folders = [d.name for d in DOWNLOAD_PATH.iterdir() if d.is_dir()]
    if not folders:
        bot.reply_to(message, "Folder kosong")
    else:
        bot.reply_to(message, "\n".join(folders))

@bot.message_handler(commands=["cleanup"])
def cleanup_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: /cleanup list|all|<folder>")
        return
    cmd = args[1].lower()
    if cmd == "list":
        folders = [d.name for d in DOWNLOAD_PATH.iterdir() if d.is_dir()]
        bot.reply_to(message, "\n".join(folders) if folders else "Folder kosong")
    elif cmd == "all":
        for d in DOWNLOAD_PATH.iterdir():
            if d.is_dir():
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
        bot.reply_to(message, "Semua folder dihapus")
    else:
        folder = DOWNLOAD_PATH / args[1]
        if folder.exists() and folder.is_dir():
            for f in folder.iterdir():
                f.unlink()
            folder.rmdir()
            bot.reply_to(message, f"{folder} dihapus")
        else:
            bot.reply_to(message, "Folder tidak ditemukan")

@bot.message_handler(commands=["loginmega"])
def login_mega_cmd(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "Usage: /loginmega <email> <pass>")
        return
    try:
        login_mega(args[1], args[2])
        bot.reply_to(message, f"Mega login sukses: {args[1]}")
    except Exception as e:
        bot.reply_to(message, f"Gagal login Mega: {e}")

@bot.message_handler(commands=["logoutmega"])
def logout_mega_cmd(message):
    logout_mega()
    bot.reply_to(message, "Mega logout sukses")

@bot.message_handler(commands=["download"])
def download_cmd(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "Usage: /download <folder1> [folder2 ...] <terabox|doodstream>")
        return
    upload_target = args[-1]
    folders = args[1:-1]
    for folder in folders:
        download_queue.put((message.chat.id, folder, upload_target))
    bot.reply_to(message, f"{len(folders)} folder dimasukkan ke antrian")

# --- Start polling ---
bot.infinity_polling()
