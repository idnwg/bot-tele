import os
import shlex
import subprocess
import threading
import queue
import telebot
from telebot import types

BOT_TOKEN = "8428173231:AAEyiNXxukheJU76REXPO_x01qx6uc5owUQ"

BASE_DOWNLOAD_DIR = "downloads"


TERABOX_CONNECT_KEY = "AyWEJg_MOdKloS4RqEyfC_LNF_XFo-XeRCcvUaWq-U4="
DOODSTREAM_API_KEY = "505679k4qcw3ztifhfvoyf"


TERABOX_UPLOADER_CMD_DIR  = f"python3 terabox_uploader.py --connect {TERABOX_CONNECT_KEY} --dir {{local}} --dest {{remote}}"
TERABOX_UPLOADER_CMD_FILE = f"python3 terabox_uploader.py --connect {TERABOX_CONNECT_KEY} --file {{local}} --dest {{remote}}"

DOODSTREAM_UPLOADER_CMD_FILE = f"python3 doodstream_uploader.py --api {DOODSTREAM_API_KEY} --file {{local}}"

DELETE_AFTER_UPLOAD_DEFAULT = True

bot = telebot.TeleBot(BOT_TOKEN)
job_queue = queue.Queue()
job_status = {}
DELETE_AFTER_UPLOAD = DELETE_AFTER_UPLOAD_DEFAULT

def send(chat_id, text, **kwargs):
    bot.send_message(chat_id, text, **kwargs)

def run_cmd(cmd):
    """jalankan perintah shell"""
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return 1, "", str(e)

def prepare_local_folder_for_job(link, provided_folder=None):
    """buat folder local download"""
    if provided_folder:
        folder_name = provided_folder
    else:
        folder_name = "job_" + str(abs(hash(link)) % (10**8))
    local_dir = os.path.join(BASE_DOWNLOAD_DIR, folder_name)
    os.makedirs(local_dir, exist_ok=True)
    return local_dir, folder_name

def worker():
    while True:
        job = job_queue.get()
        if job is None:
            break
        try:
            process_job(job)
        except Exception as e:
            send(job["chat_id"], f"❌ Error: {str(e)}")
        finally:
            job_queue.task_done()

threading.Thread(target=worker, daemon=True).start()

def process_job(job):
    chat_id = job["chat_id"]
    link = job["link"]
    provided_folder = job.get("folder_name")
    delete_after = job.get("delete_after", DELETE_AFTER_UPLOAD_DEFAULT)
    dest_service = job.get("service", "terabox")  
    terabox_path = job.get("terabox_path", "/")

    send(chat_id, f"🚀 Mulai job:\nLink: `{link}`\nDest: `{terabox_path}`\nService: {dest_service}", parse_mode="Markdown")

    local_dir, chosen_folder = prepare_local_folder_for_job(link, provided_folder)

    if "mega.nz" in link:
        cmd = f"megadl --path {shlex.quote(local_dir)} {shlex.quote(link)}"
        send(chat_id, "⬇ Download dari Mega.nz ...")
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            send(chat_id, f"❌ Gagal download Mega.nz\n{err[:500]}")
            return
    elif "terabox" in link:
        # TODO: tambahkan downloader terabox jika perlu
        send(chat_id, "⚠ Downloader Terabox belum diimplementasikan.")
        return
    else:
        send(chat_id, "❌ Link tidak dikenali.")
        return

    if dest_service == "terabox":
        items = os.listdir(local_dir)
        for fname in items:
            local_file = os.path.join(local_dir, fname)
            if os.path.isdir(local_file):
                cmd = TERABOX_UPLOADER_CMD_DIR.format(local=shlex.quote(local_file), remote=shlex.quote(terabox_path))
            else:
                cmd = TERABOX_UPLOADER_CMD_FILE.format(local=shlex.quote(local_file), remote=shlex.quote(terabox_path))
            send(chat_id, f"⬆ Upload `{fname}` ke Terabox ...", parse_mode="Markdown")
            rc, out, err = run_cmd(cmd)
            if rc != 0:
                send(chat_id, f"❌ Upload gagal\n{err[:500]}")
            else:
                send(chat_id, f"✅ Upload selesai: `{fname}`", parse_mode="Markdown")

    elif dest_service == "doodstream":
        items = os.listdir(local_dir)
        for fname in items:
            local_file = os.path.join(local_dir, fname)
            cmd = DOODSTREAM_UPLOADER_CMD_FILE.format(local=shlex.quote(local_file))
            send(chat_id, f"⬆ Upload `{fname}` ke Doodstream ...", parse_mode="Markdown")
            rc, out, err = run_cmd(cmd)
            if rc != 0:
                send(chat_id, f"❌ Upload ke Doodstream gagal\n{err[:500]}")
            else:
                send(chat_id, f"✅ Upload ke Doodstream selesai: `{fname}`", parse_mode="Markdown")

    if delete_after:
        try:
            import shutil
            shutil.rmtree(local_dir)
            send(chat_id, f"🧹 Folder `{chosen_folder}` dihapus", parse_mode="Markdown")
        except Exception as e:
            send(chat_id, f"⚠ Gagal hapus folder: {str(e)}")

@bot.message_handler(commands=["start"])
def cmd_start(message):
    send(message.chat.id, "👋 Halo! Kirim link Mega.nz atau Terabox ke sini.\nGunakan /help untuk panduan.")

@bot.message_handler(commands=["help"])
def cmd_help(message):
    help_text = (
        "📖 Panduan Bot\n\n"
        "/start - mulai bot\n"
        "/help - lihat bantuan\n"
        "/status - status job\n"
        "/cleanup <folder/all> - hapus folder\n"
        "/set_delete on|off - auto hapus setelah upload\n\n"
        "📌 Contoh Input:\n"
        "`https://mega.nz/file/xxxxx` → auto download & upload ke Terabox (default)\n"
        "`doodstream <link-mega>` → upload hasilnya ke Doodstream\n"
        "`terabox <link-mega>` → upload hasilnya ke Terabox\n"
    )
    send(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    if job_queue.empty():
        send(message.chat.id, "✅ Tidak ada job antrian.")
    else:
        send(message.chat.id, f"📌 Job dalam antrian: {job_queue.qsize()}")

@bot.message_handler(commands=["cleanup"])
def cmd_cleanup(message):
    args = message.text.split()
    if len(args) < 2:
        send(message.chat.id, "⚠ Usage: /cleanup <folder|all>")
        return
    target = args[1]
    if target == "all":
        import shutil
        shutil.rmtree(BASE_DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)
        send(message.chat.id, "🧹 Semua folder dibersihkan.")
    else:
        folder = os.path.join(BASE_DOWNLOAD_DIR, target)
        if os.path.isdir(folder):
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
            send(message.chat.id, f"🧹 Folder `{target}` dihapus.", parse_mode="Markdown")
        else:
            send(message.chat.id, f"⚠ Folder `{target}` tidak ditemukan.", parse_mode="Markdown")

@bot.message_handler(commands=["set_delete"])
def cmd_set_delete(message):
    global DELETE_AFTER_UPLOAD
    args = message.text.split()
    if len(args) < 2:
        send(message.chat.id, f"Status sekarang: {DELETE_AFTER_UPLOAD}")
        return
    val = args[1].lower()
    if val in ["on", "true", "yes", "1"]:
        DELETE_AFTER_UPLOAD = True
    else:
        DELETE_AFTER_UPLOAD = False
    send(message.chat.id, f"Set auto delete: {DELETE_AFTER_UPLOAD}")

@bot.message_handler(func=lambda m: True)
def handle_link(message):
    text = message.text.strip()
    if text.startswith("doodstream "):
        link = text.split(" ", 1)[1]
        service = "doodstream"
    elif text.startswith("terabox "):
        link = text.split(" ", 1)[1]
        service = "terabox"
    else:
        link = text
        service = "terabox"

    job = {
        "chat_id": message.chat.id,
        "link": link,
        "delete_after": DELETE_AFTER_UPLOAD,
        "service": service,
        "terabox_path": "/"
    }
    job_queue.put(job)
    send(message.chat.id, f"✅ Job ditambahkan ke antrian.\nService: {service}")

if __name__ == "__main__":
    os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)
    bot.polling(none_stop=True)
