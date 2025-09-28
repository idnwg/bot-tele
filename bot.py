#!/usr/bin/env python3
import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from mega import Mega
from pathlib import Path
import subprocess
import threading
import requests
import json
import time
import shlex

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOOD_KEY = os.getenv("DOODSTREAM_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_PATH = Path("./downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

mega_instance = Mega()
mega_account = None
job_queue = []
lock = threading.Lock()
user_context = {}  # menyimpan state interaktif user

# ---------- Helper Functions ----------

def add_job(func, args):
    with lock:
        job_queue.append((func, args))

def process_jobs():
    while True:
        if job_queue:
            with lock:
                func, args = job_queue.pop(0)
            func(*args)
        time.sleep(1)

def send_status(chat_id, text):
    bot.send_message(chat_id, text)

def login_mega(email, password):
    global mega_account
    try:
        mega_account = mega_instance.login(email, password)
        return True, "✅ Login berhasil!"
    except Exception as e:
        return False, f"❌ Login gagal: {str(e)}"

def logout_mega():
    global mega_account
    mega_account = None
    return "✅ Logout berhasil."

def download_mega(url, dest_folder):
    if not mega_account:
        return "❌ Belum login Mega!"
    try:
        dest_path = DOWNLOAD_PATH / dest_folder
        dest_path.mkdir(exist_ok=True)
        mega_account.download_url(url, str(dest_path))
        return f"✅ Download selesai: {dest_folder}"
    except Exception as e:
        return f"❌ Download gagal: {str(e)}"

def rename_file_or_folder(path, prefix=""):
    target = Path(path)
    if prefix:
        new_name = prefix + "_" + target.name
        target.rename(target.parent / new_name)
        return str(target.parent / new_name)
    return str(target)

def upload_terabox(path):
    cli_path = Path("./teraboxcli/main.py")
    if not cli_path.exists():
        return "❌ TeraboxUploaderCLI tidak ditemukan!"
    try:
        cmd = f"python3 {cli_path} upload {shlex.quote(str(path))} --recursive"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return result.stdout + result.stderr
    except Exception as e:
        return f"❌ Upload Terabox gagal: {str(e)}"

def upload_doodstream(path):
    files_to_upload = []
    if Path(path).is_file():
        files_to_upload = [path]
    else:
        for f in Path(path).rglob("*"):
            if f.is_file():
                files_to_upload.append(f)
    responses = []
    for file_path in files_to_upload:
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    f"https://doodapi.com/api/upload?key={DOOD_KEY}",
                    files={"file": f}
                )
                data = resp.json()
                if data.get("status") == "success":
                    responses.append(f"{file_path} → {data.get('result')}")
                else:
                    responses.append(f"❌ {file_path} → {data}")
        except Exception as e:
            responses.append(f"❌ {file_path} → {str(e)}")
    return "\n".join(responses)

# ---------- Telegram Commands ----------

@bot.message_handler(commands=["help"])
def cmd_help(msg):
    text = """
💡 Daftar Perintah:
/loginmega <email> <password> - Login Mega
/logoutmega - Logout Mega
/select - Pilih folder untuk download/upload
/status - Lihat job queue
    """
    bot.send_message(msg.chat.id, text)

@bot.message_handler(commands=["loginmega"])
def cmd_loginmega(msg):
    try:
        _, email, password = msg.text.split(maxsplit=2)
        ok, text = login_mega(email, password)
        bot.send_message(msg.chat.id, text)
    except ValueError:
        bot.send_message(msg.chat.id, "❌ Format salah! Gunakan: /loginmega email password")

@bot.message_handler(commands=["logoutmega"])
def cmd_logoutmega(msg):
    bot.send_message(msg.chat.id, logout_mega())

# ---------- Interactive Multi-folder Selection ----------

@bot.message_handler(commands=["select"])
def cmd_select(msg):
    user_context[msg.chat.id] = {"stage": "choose_folder"}
    folders = [f.name for f in DOWNLOAD_PATH.iterdir() if f.is_dir()]
    if not folders:
        bot.send_message(msg.chat.id, "❌ Tidak ada folder tersedia untuk dipilih.")
        return

    markup = InlineKeyboardMarkup()
    for folder in folders:
        markup.add(InlineKeyboardButton(folder, callback_data=f"folder|{folder}"))
    bot.send_message(msg.chat.id, "Pilih folder yang ingin diproses:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    if chat_id not in user_context:
        return

    context = user_context[chat_id]

    if call.data.startswith("folder|"):
        folder = call.data.split("|")[1]
        context["selected_folder"] = folder
        context["stage"] = "choose_target"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Terabox", callback_data="target|terabox"))
        markup.add(InlineKeyboardButton("Doodstream", callback_data="target|doodstream"))
        markup.add(InlineKeyboardButton("Both", callback_data="target|both"))
        bot.edit_message_text("Pilih target upload:", chat_id=chat_id,
                              message_id=call.message.message_id,
                              reply_markup=markup)

    elif call.data.startswith("target|"):
        target = call.data.split("|")[1]
        folder = context.get("selected_folder")
        if not folder:
            bot.send_message(chat_id, "❌ Folder tidak ditemukan di context.")
            return

        def job_func(path, target, chat_id):
            output = ""
            if target in ("terabox", "both"):
                output += upload_terabox(DOWNLOAD_PATH / path) + "\n"
            if target in ("doodstream", "both"):
                output += upload_doodstream(DOWNLOAD_PATH / path)
            bot.send_message(chat_id, f"✅ Job selesai:\n{output}")

        add_job(job_func, (folder, target, chat_id))
        bot.edit_message_text(f"✅ Job ditambahkan untuk folder {folder} ke {target}", chat_id=chat_id,
                              message_id=call.message.message_id)
        del user_context[chat_id]

@bot.message_handler(commands=["status"])
def cmd_status(msg):
    if not job_queue:
        bot.send_message(msg.chat.id, "✅ Tidak ada job antrian")
    else:
        jobs = "\n".join([str(j[1]) for j in job_queue])
        bot.send_message(msg.chat.id, f"⏳ Job queue:\n{jobs}")

# ---------- Start Job Processor Thread ----------

threading.Thread(target=process_jobs, daemon=True).start()

# ---------- Start Bot Polling ----------

bot.infinity_polling()
