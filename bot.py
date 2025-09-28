import os
import telebot
import subprocess
from dotenv import load_dotenv
from pathlib import Path
from threading import Thread
import time

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)

DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Simulasi job queue
job_queue = []

# Fungsi helper untuk upload via TeraboxUploaderCLI
def upload_terabox(folder_path: Path):
    try:
        # Panggil teraboxcli dengan recursive upload
        cmd = ["python3", "teraboxcli/main.py", "upload", str(folder_path), "--recursive"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        success = "UPLOAD      : Done" in result.stdout or "Program closing" in result.stdout
        return success, result.stdout
    except Exception as e:
        return False, str(e)

# Fungsi worker job queue
def process_jobs():
    while True:
        if job_queue:
            job = job_queue.pop(0)
            folder, user_id = job
            bot.send_message(user_id, f"🚀 Mulai upload folder: {folder.name}")
            success, output = upload_terabox(folder)
            if success:
                bot.send_message(user_id, f"✅ Upload selesai: {folder.name}\nOutput:\n{output}")
            else:
                bot.send_message(user_id, f"❌ Upload gagal: {folder.name}\nError:\n{output}")
        time.sleep(2)

Thread(target=process_jobs, daemon=True).start()

# Command start/help
@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    help_text = """
🛠️ *Bot Mega → TeraboxUploaderCLI*

Commands:
/listfolders - Lihat folder di downloads
/status - Lihat job queue
/download <folder1> <folder2> ... - Tambahkan folder ke queue upload
"""
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

# List folder lokal
@bot.message_handler(commands=["listfolders"])
def list_folders(message):
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if folders:
        bot.send_message(message.chat.id, "📂 Folder tersedia:\n" + "\n".join(folders))
    else:
        bot.send_message(message.chat.id, "📂 Tidak ada folder di downloads.")

# Status job queue
@bot.message_handler(commands=["status"])
def status(message):
    if job_queue:
        status_text = "📋 Job queue:\n" + "\n".join([f"{f.name}" for f, _ in job_queue])
    else:
        status_text = "📋 Job queue kosong."
    bot.send_message(message.chat.id, status_text)

# Download command (simulasi) dan tambah ke queue
@bot.message_handler(commands=["download"])
def download_cmd(message):
    args = message.text.split()[1:]
    if not args:
        bot.send_message(message.chat.id, "❌ Format salah! Gunakan: /download <folder1> <folder2> ...")
        return
    added = []
    for folder_name in args:
        folder_path = DOWNLOAD_PATH / folder_name
        if folder_path.exists():
            job_queue.append((folder_path, message.chat.id))
            added.append(folder_name)
        else:
            bot.send_message(message.chat.id, f"❌ Folder tidak ditemukan: {folder_name}")
    if added:
        bot.send_message(message.chat.id, f"✅ Folder ditambahkan ke queue: {', '.join(added)}")

# Jalankan bot polling
bot.infinity_polling()
