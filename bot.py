import os
import telebot
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
import threading
import requests
import time

# ----------------- Load Environment -----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TERABOX_KEY = os.getenv("TERABOX_CONNECT_KEY")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan di .env")

bot = telebot.TeleBot(BOT_TOKEN)

# ----------------- Paths -----------------
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# ----------------- Global Variables -----------------
jobs_queue = []
mega_instance = Mega()
mega_account = None
delete_after_upload = True
rename_prefix = ""

# ----------------- Helpers -----------------
def add_job(job):
    jobs_queue.append(job)
    threading.Thread(target=process_jobs).start()

def process_jobs():
    while jobs_queue:
        job = jobs_queue.pop(0)
        try:
            download_file(job)
            rename_file(job)
            upload_file(job)
            if delete_after_upload:
                os.remove(job['local_path'])
        except Exception as e:
            bot.send_message(job['chat_id'], f"❌ Error processing job: {e}")

def download_file(job):
    # Mega download
    if job['source'] == 'mega':
        global mega_account
        if not mega_account:
            raise Exception("❌ Mega belum login!")
        file = mega_account.find(job['link'].split("/")[-1])
        local_path = mega_account.download(file, str(DOWNLOAD_PATH))
        job['local_path'] = local_path
    else:
        # URL download
        response = requests.get(job['link'], stream=True)
        local_path = DOWNLOAD_PATH / job['link'].split("/")[-1]
        with open(local_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)
        job['local_path'] = str(local_path)

def rename_file(job):
    if rename_prefix:
        base = Path(job['local_path']).stem
        ext = Path(job['local_path']).suffix
        new_name = f"{rename_prefix}_{base}{ext}"
        new_path = Path(job['local_path']).parent / new_name
        os.rename(job['local_path'], new_path)
        job['local_path'] = str(new_path)

def upload_file(job):
    if job['target'] == 'terabox':
        headers = {"Authorization": f"Bearer {TERABOX_KEY}"}
        with open(job['local_path'], "rb") as f:
            requests.post("https://terabox.com/api/upload", headers=headers, files={"file": f})
    elif job['target'] == 'doodstream':
        files = {'file': open(job['local_path'], 'rb')}
        data = {'key': DOOD_KEY}
        requests.post("https://doodstream.com/api/upload", data=data, files=files)

# ----------------- Command Handlers -----------------
@bot.message_handler(commands=["start", "help"])
def send_help(message):
    help_text = (
        "/status - Lihat job & antrian\n"
        "/cleanup list|all|<folder> - Kelola folder lokal\n"
        "/set_delete on|off - Set default delete after upload\n"
        "/setprefix <prefix> - Set prefix untuk rename foto/video\n"
        "/listfolders - Lihat folder di downloads\n"
        "/loginmega <email> <password> - Login akun Mega\n"
        "/logoutmega - Logout akun Mega\n"
        "/download <link> - Download link Mega/URL\n"
        "/upload <terabox|doodstream> - Upload file terakhir\n"
        "Bisa pilih beberapa folder/file, di antrian diproses satu per satu."
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=["loginmega"])
def login_mega(message):
    try:
        args = message.text[len("/loginmega "):]
        email, password = args.split(" ", 1)
        global mega_account
        mega_account = mega_instance.login(email, password)
        bot.reply_to(message, "✅ Login Mega berhasil")
    except ValueError:
        bot.reply_to(message, "❌ Format salah! Gunakan: /loginmega email password")
    except Exception as e:
        bot.reply_to(message, f"❌ Gagal login Mega: {e}")

@bot.message_handler(commands=["logoutmega"])
def logout_mega(message):
    global mega_account
    mega_account = None
    bot.reply_to(message, "✅ Logout Mega berhasil")

@bot.message_handler(commands=["set_delete"])
def set_delete_cmd(message):
    global delete_after_upload
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Gunakan: /set_delete on|off")
        return
    delete_after_upload = args[1].lower() == "on"
    bot.reply_to(message, f"✅ delete_after_upload = {delete_after_upload}")

@bot.message_handler(commands=["setprefix"])
def set_prefix_cmd(message):
    global rename_prefix
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❌ Gunakan: /setprefix <prefix>")
        return
    rename_prefix = args[1]
    bot.reply_to(message, f"✅ Prefix rename di-set ke: {rename_prefix}")

@bot.message_handler(commands=["download"])
def download_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❌ Gunakan: /download <link>")
        return
    link = args[1]
    source = "mega" if "mega.nz" in link else "url"
    add_job({"chat_id": message.chat.id, "link": link, "source": source, "target": None})
    bot.reply_to(message, "✅ Job ditambahkan ke antrian")

@bot.message_handler(commands=["upload"])
def upload_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❌ Gunakan: /upload <terabox|doodstream>")
        return
    target = args[1].lower()
    if not jobs_queue:
        bot.reply_to(message, "❌ Tidak ada file untuk di-upload")
        return
    jobs_queue[-1]["target"] = target
    bot.reply_to(message, f"✅ File terakhir akan di-upload ke {target}")

@bot.message_handler(commands=["status"])
def status_cmd(message):
    text = f"Jobs di antrian: {len(jobs_queue)}"
    bot.reply_to(message, text)

@bot.message_handler(commands=["listfolders"])
def list_folders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.reply_to(message, "📂 Tidak ada folder")
        return
    bot.reply_to(message, "📂 Folder lokal:\n" + "\n".join(folders))

@bot.message_handler(commands=["cleanup"])
def cleanup_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Gunakan: /cleanup list|all|<folder>")
        return
    param = args[1]
    if param == "list":
        list_folders(message)
    elif param == "all":
        for f in DOWNLOAD_PATH.iterdir():
            if f.is_dir():
                for file in f.iterdir():
                    file.unlink()
                f.rmdir()
        bot.reply_to(message, "✅ Semua folder dihapus")
    else:
        folder = DOWNLOAD_PATH / param
        if folder.exists() and folder.is_dir():
            for file in folder.iterdir():
                file.unlink()
            folder.rmdir()
            bot.reply_to(message, f"✅ Folder {param} dihapus")
        else:
            bot.reply_to(message, f"❌ Folder {param} tidak ditemukan")

# ----------------- Start Bot -----------------
if __name__ == "__main__":
    bot.infinity_polling()
