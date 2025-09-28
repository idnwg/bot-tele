import os
import telebot
from dotenv import load_dotenv
from mega import Mega
import requests
from pathlib import Path
from threading import Thread
import time
from queue import Queue

# ---------------------- Load environment ----------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)

# ---------------------- Path ----------------------
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# ---------------------- Global Settings ----------------------
DELETE_AFTER_UPLOAD = True
PREFIX_RENAME = ""
mega_instance = Mega()
mega_account = None  # akan di set saat login
job_queue = Queue()

# ---------------------- Helper ----------------------
def upload_to_terabox(user_id, file_path):
    # placeholder upload logic
    bot.send_message(user_id, f"Uploading {file_path} to Terabox...")

def upload_to_doodstream(user_id, file_path):
    # placeholder upload logic
    bot.send_message(user_id, f"Uploading {file_path} to Doodstream...")

def download_from_mega(folder_link):
    global mega_account
    if not mega_account:
        return None, "Mega account belum login ❌"
    # logic download
    folder_name = folder_link.split("/")[-1]
    folder_path = DOWNLOAD_PATH / folder_name
    folder_path.mkdir(exist_ok=True)
    # simulate download
    time.sleep(1)
    return folder_name, None

# ---------------------- Queue Processing ----------------------
def process_queue():
    while True:
        if not job_queue.empty():
            job = job_queue.get()
            user_id = job['user']
            folder = job['folder']
            bot.send_message(user_id, f"Mulai proses folder: {folder}")
            folder_path = DOWNLOAD_PATH / folder
            for f in os.listdir(folder_path):
                file_path = folder_path / f
                upload_to_terabox(user_id, file_path)
                upload_to_doodstream(user_id, file_path)
                if DELETE_AFTER_UPLOAD:
                    file_path.unlink()
            bot.send_message(user_id, f"Folder selesai diproses: {folder} ✅")
        time.sleep(1)

Thread(target=process_queue, daemon=True).start()

# ---------------------- Commands ----------------------
@bot.message_handler(commands=['start', 'help'])
def cmd_help(message):
    help_text = """
Daftar perintah:

/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/pilihfolder - Pilih folder untuk download & upload
/loginmega <email> <password> - Login akun Mega
/logoutmega - Logout akun Mega
"""
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['status'])
def cmd_status(message):
    pending = list(job_queue.queue)
    text = f"Ada {len(pending)} job dalam antrian:\n"
    for i, job in enumerate(pending, 1):
        text += f"{i}. {job['folder']}\n"
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['listfolders'])
def cmd_listfolders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.send_message(message.chat.id, "Tidak ada folder di downloads ❌")
    else:
        text = "Folder di downloads:\n" + "\n".join(folders)
        bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['set_delete'])
def cmd_setdelete(message):
    global DELETE_AFTER_UPLOAD
    args = message.text.split()
    if len(args) != 2 or args[1] not in ["on", "off"]:
        bot.send_message(message.chat.id, "Usage: /set_delete on|off")
        return
    DELETE_AFTER_UPLOAD = args[1] == "on"
    bot.send_message(message.chat.id, f"Delete after upload di set: {DELETE_AFTER_UPLOAD}")

@bot.message_handler(commands=['setprefix'])
def cmd_setprefix(message):
    global PREFIX_RENAME
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        bot.send_message(message.chat.id, "Usage: /setprefix <prefix>")
        return
    PREFIX_RENAME = args[1]
    bot.send_message(message.chat.id, f"Prefix rename di set: {PREFIX_RENAME}")

@bot.message_handler(commands=['cleanup'])
def cmd_cleanup(message):
    args = message.text.split()
    if len(args) != 2:
        bot.send_message(message.chat.id, "Usage: /cleanup list|all|<folder>")
        return
    action = args[1]
    if action == "list":
        folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
        bot.send_message(message.chat.id, "Folder lokal:\n" + "\n".join(folders))
    elif action == "all":
        for f in DOWNLOAD_PATH.iterdir():
            if f.is_dir():
                for file in f.iterdir():
                    file.unlink()
                f.rmdir()
        bot.send_message(message.chat.id, "Semua folder dihapus ✅")
    else:
        folder_path = DOWNLOAD_PATH / action
        if folder_path.exists() and folder_path.is_dir():
            for file in folder_path.iterdir():
                file.unlink()
            folder_path.rmdir()
            bot.send_message(message.chat.id, f"Folder {action} dihapus ✅")
        else:
            bot.send_message(message.chat.id, "Folder tidak ditemukan ❌")

@bot.message_handler(commands=['loginmega'])
def cmd_loginmega(message):
    global mega_account
    args = message.text.split()
    if len(args) != 3:
        bot.send_message(message.chat.id, "Usage: /loginmega <email> <password>")
        return
    try:
        mega_account = mega_instance.login(args[1], args[2])
        bot.send_message(message.chat.id, "Login Mega berhasil ✅")
    except Exception as e:
        bot.send_message(message.chat.id, f"Gagal login Mega ❌: {str(e)}")

@bot.message_handler(commands=['logoutmega'])
def cmd_logoutmega(message):
    global mega_account
    mega_account = None
    bot.send_message(message.chat.id, "Logout Mega berhasil ✅")

@bot.message_handler(commands=['pilihfolder'])
def cmd_pilihfolder(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.send_message(message.chat.id, "Tidak ada folder untuk dipilih ❌")
        return

    folder_list = "\n".join(f"{i+1}. {f}" for i, f in enumerate(folders))
    msg = bot.send_message(message.chat.id, f"Pilih folder yang akan di-upload (misal: 1,3,5):\n{folder_list}")

    @bot.message_handler(func=lambda m: m.reply_to_message and m.reply_to_message.message_id == msg.message_id)
    def folder_selection(reply):
        try:
            choices = [int(x.strip())-1 for x in reply.text.split(",")]
            selected_folders = [folders[i] for i in choices if 0 <= i < len(folders)]
            if not selected_folders:
                bot.send_message(reply.chat.id, "Pilihan tidak valid ❌")
                return
            for folder in selected_folders:
                job_queue.put({'user': reply.chat.id, 'folder': folder})
            bot.send_message(reply.chat.id, f"{len(selected_folders)} folder ditambahkan ke antrian ✅")
        except Exception as e:
            bot.send_message(reply.chat.id, f"Terjadi error: {str(e)} ❌")

# ---------------------- Start Bot ----------------------
bot.send_message(BOT_TOKEN, "Bot started...")  # optional startup message
bot.infinity_polling()
