import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
from threading import Thread
import requests
import json
import time
import queue

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)

DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Queue untuk job download/upload
job_queue = queue.Queue()
mega_sessions = {}  # simpan sesi login Mega per user

# Pengaturan default
delete_after_upload = True
rename_prefix = ""

# --- UTILITIES ---
def safe_send_message(chat_id, text):
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print(f"[send_message error] {str(e)}")

def format_help():
    return (
        "/status - Lihat job & antrian\n"
        "/cleanup list|all|<folder> - Kelola folder lokal\n"
        "/set_delete on|off - Set default delete after upload\n"
        "/setprefix <prefix> - Set prefix untuk rename foto/video\n"
        "/listfolders - Lihat folder di downloads\n"
        "/loginmega email password - Login Mega (support karakter khusus)\n"
        "/logoutmega - Logout Mega\n"
        "/download <folder> - Pilih folder Mega untuk download\n"
        "/upload <service> <file/folder> - Upload ke Terabox/Doodstream\n"
    )

# --- MEGA LOGIN/LOGOUT ---
@bot.message_handler(commands=['loginmega'])
def login_mega(message):
    chat_id = message.chat.id
    try:
        # Split command: /loginmega email password
        parts = message.text.split(' ', 2)
        if len(parts) < 3:
            safe_send_message(chat_id, "❌ Format salah! Gunakan: /loginmega email password")
            return
        email = parts[1]
        password = parts[2]
        mega = Mega()
        try:
            # login dengan try-except JSON parsing
            m = mega.login(email, password)
            if not m:
                safe_send_message(chat_id, "❌ Login gagal: respons Mega kosong atau salah")
                return
            mega_sessions[chat_id] = m
            safe_send_message(chat_id, "✅ Login Mega berhasil!")
        except json.JSONDecodeError:
            safe_send_message(chat_id, "❌ Login gagal: respons Mega tidak valid (JSON error)")
        except Exception as e:
            safe_send_message(chat_id, f"❌ Login gagal: {str(e)}")
    except Exception as e:
        safe_send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['logoutmega'])
def logout_mega(message):
    chat_id = message.chat.id
    if chat_id in mega_sessions:
        del mega_sessions[chat_id]
        safe_send_message(chat_id, "✅ Logout Mega berhasil!")
    else:
        safe_send_message(chat_id, "❌ Anda belum login Mega")

# --- DOWNLOAD/UPLOAD JOB HANDLER ---
def process_jobs():
    while True:
        job = job_queue.get()
        if job is None:
            break
        chat_id, mega_folder, upload_service = job
        safe_send_message(chat_id, f"🔄 Memulai job: {mega_folder} → {upload_service}")
        try:
            # --- Download dari Mega ---
            if chat_id not in mega_sessions:
                safe_send_message(chat_id, "❌ Anda belum login Mega")
                job_queue.task_done()
                continue
            mega = mega_sessions[chat_id]
            folder_path = DOWNLOAD_PATH / mega_folder.replace('/', '_')
            folder_path.mkdir(exist_ok=True)
            # Download seluruh folder (simplifikasi)
            files = mega.get_files()  # dictionary file
            for f_id, f in files.items():
                if mega_folder in f['a']['n']:  # check folder
                    try:
                        mega.download(f, dest=str(folder_path))
                    except Exception as e:
                        safe_send_message(chat_id, f"❌ Download gagal: {f['a']['n']} ({str(e)})")
            safe_send_message(chat_id, "✅ Download selesai!")

            # --- Rename sesuai prefix ---
            for file in folder_path.iterdir():
                if rename_prefix:
                    new_name = f"{rename_prefix}_{file.name}"
                    file.rename(folder_path / new_name)

            # --- Upload ---
            if upload_service.lower() == 'terabox':
                safe_send_message(chat_id, "🔄 Upload ke Terabox...")
                # Implementasi upload Terabox (simplifikasi)
                # contoh: requests.post(...) dengan TERABOX_KEY
            elif upload_service.lower() == 'doodstream':
                safe_send_message(chat_id, "🔄 Upload ke Doodstream...")
                # Implementasi upload Doodstream
                # contoh: requests.post(...) dengan DOOD_KEY
            else:
                safe_send_message(chat_id, f"❌ Service upload {upload_service} tidak dikenali")

            # --- Hapus lokal jika delete_after_upload ---
            if delete_after_upload:
                for file in folder_path.iterdir():
                    file.unlink()
                folder_path.rmdir()
        except Exception as e:
            safe_send_message(chat_id, f"❌ Job gagal: {str(e)}")
        finally:
            job_queue.task_done()

# Thread untuk job queue
Thread(target=process_jobs, daemon=True).start()

# --- COMMANDS ---
@bot.message_handler(commands=['help'])
def cmd_help(message):
    safe_send_message(message.chat.id, format_help())

@bot.message_handler(commands=['status'])
def cmd_status(message):
    safe_send_message(message.chat.id, f"Ada {job_queue.qsize()} job dalam antrian")

@bot.message_handler(commands=['set_delete'])
def cmd_set_delete(message):
    global delete_after_upload
    parts = message.text.split()
    if len(parts) < 2:
        safe_send_message(message.chat.id, "Gunakan: /set_delete on|off")
        return
    delete_after_upload = True if parts[1].lower() == 'on' else False
    safe_send_message(message.chat.id, f"Delete after upload: {delete_after_upload}")

@bot.message_handler(commands=['setprefix'])
def cmd_setprefix(message):
    global rename_prefix
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        safe_send_message(message.chat.id, "Gunakan: /setprefix <prefix>")
        return
    rename_prefix = parts[1].strip()
    safe_send_message(message.chat.id, f"Prefix rename di-set: {rename_prefix}")

@bot.message_handler(commands=['listfolders'])
def cmd_listfolders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    safe_send_message(message.chat.id, "📂 Folder lokal:\n" + "\n".join(folders))

@bot.message_handler(commands=['cleanup'])
def cmd_cleanup(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        safe_send_message(message.chat.id, "Gunakan: /cleanup list|all|<folder>")
        return
    arg = parts[1].strip().lower()
    if arg == 'list':
        folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
        safe_send_message(message.chat.id, "📂 Folder lokal:\n" + "\n".join(folders))
    elif arg == 'all':
        for f in DOWNLOAD_PATH.iterdir():
            if f.is_dir():
                for file in f.iterdir():
                    file.unlink()
                f.rmdir()
        safe_send_message(message.chat.id, "✅ Semua folder dihapus")
    else:
        folder_path = DOWNLOAD_PATH / arg
        if folder_path.exists() and folder_path.is_dir():
            for file in folder_path.iterdir():
                file.unlink()
            folder_path.rmdir()
            safe_send_message(message.chat.id, f"✅ Folder {arg} dihapus")
        else:
            safe_send_message(message.chat.id, f"❌ Folder {arg} tidak ditemukan")

@bot.message_handler(commands=['download'])
def cmd_download(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        safe_send_message(message.chat.id, "Gunakan: /download <folder> <upload_service>")
        return
    folder, service = parts[1], parts[2]
    job_queue.put((message.chat.id, folder, service))
    safe_send_message(message.chat.id, f"✅ Job ditambahkan: {folder} → {service}")

# --- START BOT ---
print("Bot started...")
bot.infinity_polling(timeout=10, long_polling_timeout=5)
