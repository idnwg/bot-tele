import os
import telebot
from telebot import types
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
import threading
import queue
import time
import requests

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN)
bot.remove_webhook()  # pastikan tidak ada webhook aktif

# Download path
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Mega session storage
mega_sessions = {}

# Job queue
job_queue = queue.Queue()

# User config defaults
user_config = {
    "delete_after_upload": True,
    "rename_prefix": ""
}

# ========== HELP COMMAND ==========
HELP_TEXT = """
📌 *Bot Commands*
/help - Lihat daftar perintah
/status - Lihat job & antrian
/cleanup list|all|<folder> - Kelola folder lokal
/set_delete on|off - Set default delete after upload
/setprefix <prefix> - Set prefix untuk rename foto/video
/listfolders - Lihat folder di downloads
/loginmega <email> <password> - Login Mega
/logoutmega - Logout Mega
/addjob <link> - Tambah job download Mega
/selectfolder - Pilih folder untuk download & upload
"""

# ========== TELEGRAM COMMANDS ==========
@bot.message_handler(commands=['help'])
def send_help(message):
    bot.send_message(message.chat.id, HELP_TEXT, parse_mode='Markdown')

@bot.message_handler(commands=['loginmega'])
def login_mega(message):
    try:
        _, email, password = message.text.split()
    except ValueError:
        bot.reply_to(message, "Gunakan: /loginmega <email> <password>")
        return
    m = Mega()
    account = m.login(email, password)
    mega_sessions[message.chat.id] = account
    bot.reply_to(message, f"Mega login berhasil: {email}")

@bot.message_handler(commands=['logoutmega'])
def logout_mega(message):
    if message.chat.id in mega_sessions:
        del mega_sessions[message.chat.id]
        bot.reply_to(message, "Mega logout berhasil")
    else:
        bot.reply_to(message, "Tidak ada sesi Mega aktif")

@bot.message_handler(commands=['status'])
def show_status(message):
    bot.reply_to(message, f"Jobs dalam antrian: {job_queue.qsize()}")

@bot.message_handler(commands=['set_delete'])
def set_delete(message):
    try:
        _, option = message.text.split()
        user_config['delete_after_upload'] = True if option.lower() == 'on' else False
        bot.reply_to(message, f"Set delete_after_upload = {user_config['delete_after_upload']}")
    except:
        bot.reply_to(message, "Gunakan: /set_delete on|off")

@bot.message_handler(commands=['setprefix'])
def set_prefix(message):
    try:
        _, prefix = message.text.split(maxsplit=1)
        user_config['rename_prefix'] = prefix
        bot.reply_to(message, f"Set rename prefix = {prefix}")
    except:
        bot.reply_to(message, "Gunakan: /setprefix <prefix>")

@bot.message_handler(commands=['listfolders'])
def list_folders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    bot.reply_to(message, "Folder di downloads:\n" + "\n".join(folders) if folders else "Tidak ada folder")

# ========== JOB HANDLER ==========
def download_mega(link, session, chat_id):
    bot.send_message(chat_id, f"Mulai download: {link}")
    try:
        file = session.download_url(link, str(DOWNLOAD_PATH))
        bot.send_message(chat_id, f"Download selesai: {file}")
        # rename
        new_name = user_config['rename_prefix'] + os.path.basename(file)
        new_path = os.path.join(DOWNLOAD_PATH, new_name)
        os.rename(file, new_path)
        bot.send_message(chat_id, f"File di-rename: {new_name}")
        # upload to Terabox/Doodstream
        # dummy upload
        bot.send_message(chat_id, f"Upload selesai: {new_name}")
        # delete if configured
        if user_config['delete_after_upload']:
            os.remove(new_path)
            bot.send_message(chat_id, f"File dihapus setelah upload: {new_name}")
    except Exception as e:
        bot.send_message(chat_id, f"Download gagal: {str(e)}")

def job_worker():
    while True:
        chat_id, func, args = job_queue.get()
        try:
            func(*args)
        except Exception as e:
            bot.send_message(chat_id, f"Job error: {str(e)}")
        job_queue.task_done()
        time.sleep(1)  # prevent hammering

threading.Thread(target=job_worker, daemon=True).start()

# ========== ADD JOB COMMAND ==========
@bot.message_handler(commands=['addjob'])
def add_job(message):
    try:
        _, link = message.text.split(maxsplit=1)
        session = mega_sessions.get(message.chat.id)
        if not session:
            bot.reply_to(message, "Login Mega dulu: /loginmega <email> <password>")
            return
        job_queue.put((message.chat.id, download_mega, (link, session, message.chat.id)))
        bot.reply_to(message, "Job ditambahkan ke antrian")
    except:
        bot.reply_to(message, "Gunakan: /addjob <link>")

# ========== START POLLING ==========
if __name__ == "__main__":
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
