import os
import telebot
from dotenv import load_dotenv
from mega import Mega
import requests
from pathlib import Path
from threading import Thread
import time

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)

DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

HELP_TEXT = """
🤖 *Bot Commands:*

/help - Tampilkan daftar perintah

/login_mega <email> <password> - Login ke akun MEGA  
/logout_mega - Logout dari akun MEGA

/list_folders - List folder MEGA akunmu  
/download_folder - Pilih folder MEGA untuk di-download dan upload  
/download_file <link> - Download file langsung dari MEGA atau Doodstream

/status - Lihat job & antrian  
/cleanup list|all|<folder> - Kelola folder lokal  
/set_delete on|off - Set default delete after upload  
/setprefix <prefix> - Set prefix untuk rename foto/video  
/listfolders - Lihat folder di downloads

*Upload / Download Options:*  
- Setelah memilih folder atau file, bot akan menanyakan platform upload: Terabox atau Doodstream  
- File akan otomatis di-rename sebelum di-upload  

*Catatan:*  
- Pastikan akun MEGA sudah login sebelum mencoba download folder  
- Token Terabox / Doodstream sudah harus diisi di .env
"""

# Global state
mega_client = None
mega_account = None
prefix = ""
delete_after_upload = True
job_queue = []

def upload_terabox(file_path):
    # Dummy function, sesuaikan dengan API Terabox
    print(f"Uploading {file_path} to Terabox...")
    time.sleep(2)
    return f"https://terabox.fake/{file_path.name}"

def upload_doodstream(file_path):
    # Dummy function, sesuaikan dengan API Doodstream
    print(f"Uploading {file_path} to Doodstream...")
    time.sleep(2)
    return f"https://dood.fake/{file_path.name}"

def rename_file(file_path):
    new_name = f"{prefix}{file_path.name}" if prefix else file_path.name
    new_path = file_path.parent / new_name
    file_path.rename(new_path)
    return new_path

def download_mega_file(url, dest_folder):
    if not mega_client:
        return None, "Mega belum login"
    try:
        dest_folder.mkdir(parents=True, exist_ok=True)
        file_path = dest_folder / url.split("/")[-1]
        mega_client.download_url(url, dest_folder=str(dest_folder))
        renamed = rename_file(file_path)
        return renamed, None
    except Exception as e:
        return None, str(e)

# --- Handlers ---

@bot.message_handler(commands=["help"])
def handle_help(msg):
    bot.send_message(msg.chat.id, HELP_TEXT, parse_mode="Markdown")

@bot.message_handler(commands=["login_mega"])
def handle_login_mega(msg):
    global mega_client, mega_account
    try:
        _, email, password = msg.text.split(maxsplit=2)
        mega_client = Mega()
        mega_account = mega_client.login(email, password)
        bot.reply_to(msg, f"Login sukses sebagai {email}")
    except Exception as e:
        bot.reply_to(msg, f"Login gagal: {e}")

@bot.message_handler(commands=["logout_mega"])
def handle_logout_mega(msg):
    global mega_client, mega_account
    mega_client = None
    mega_account = None
    bot.reply_to(msg, "Logout Mega berhasil")

@bot.message_handler(commands=["list_folders"])
def handle_list_folders(msg):
    if not mega_account:
        bot.reply_to(msg, "Belum login Mega")
        return
    try:
        folders = mega_account.get_files()
        folder_list = [f"{fid}: {f['a']['n']}" for fid,f in folders.items() if f['t']==1]
        bot.reply_to(msg, "\n".join(folder_list) if folder_list else "Folder kosong")
    except Exception as e:
        bot.reply_to(msg, f"Gagal ambil folder: {e}")

@bot.message_handler(commands=["download_file"])
def handle_download_file(msg):
    try:
        _, link = msg.text.split(maxsplit=1)
        dest = DOWNLOAD_PATH
        bot.reply_to(msg, f"Mulai download {link}...")
        file_path, err = download_mega_file(link, dest)
        if err:
            bot.reply_to(msg, f"Download gagal: {err}")
            return
        bot.reply_to(msg, f"Download selesai: {file_path}")
        # Upload ke platform
        dood_url = upload_doodstream(file_path)
        terabox_url = upload_terabox(file_path)
        bot.reply_to(msg, f"Upload selesai:\nDoodstream: {dood_url}\nTerabox: {terabox_url}")
        if delete_after_upload:
            file_path.unlink()
    except Exception as e:
        bot.reply_to(msg, f"Error: {e}")

@bot.message_handler(commands=["setprefix"])
def handle_setprefix(msg):
    global prefix
    try:
        _, pfx = msg.text.split(maxsplit=1)
        prefix = pfx
        bot.reply_to(msg, f"Prefix rename diset ke: {prefix}")
    except:
        bot.reply_to(msg, "Gunakan: /setprefix <prefix>")

@bot.message_handler(commands=["set_delete"])
def handle_set_delete(msg):
    global delete_after_upload
    try:
        _, val = msg.text.split(maxsplit=1)
        delete_after_upload = val.lower() == "on"
        bot.reply_to(msg, f"Delete after upload: {delete_after_upload}")
    except:
        bot.reply_to(msg, "Gunakan: /set_delete on|off")

@bot.message_handler(commands=["status"])
def handle_status(msg):
    bot.reply_to(msg, f"Jobs antrian: {len(job_queue)}")

@bot.message_handler(commands=["cleanup"])
def handle_cleanup(msg):
    try:
        parts = msg.text.split()
        action = parts[1] if len(parts)>1 else "list"
        if action=="list":
            folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
            bot.reply_to(msg, "\n".join(folders) if folders else "Folder kosong")
        elif action=="all":
            for f in DOWNLOAD_PATH.iterdir():
                if f.is_dir():
                    for file in f.iterdir(): file.unlink()
                    f.rmdir()
            bot.reply_to(msg, "Semua folder dihapus")
        else:
            folder = DOWNLOAD_PATH / action
            if folder.exists() and folder.is_dir():
                for file in folder.iterdir(): file.unlink()
                folder.rmdir()
                bot.reply_to(msg, f"Folder {action} dihapus")
            else:
                bot.reply_to(msg, "Folder tidak ditemukan")
    except Exception as e:
        bot.reply_to(msg, f"Gagal cleanup: {e}")

@bot.message_handler(commands=["listfolders"])
def handle_listfolders(msg):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    bot.reply_to(msg, "\n".join(folders) if folders else "Folder kosong")

# --- Main ---
if __name__ == "__main__":
    print("Bot started...")
    bot.infinity_polling()
