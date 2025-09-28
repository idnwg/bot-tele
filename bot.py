#!/usr/bin/env python3
"""
Final bot.py
- Mega.nz -> Terabox / Doodstream
- Prefix rename untuk semua foto/video (FORMAT: "<PREFIX> 01.ext")
- Queue worker, per-user settings
- Commands: /start, /help, /status, /cleanup, /set_delete, /setprefix, /listfolders, /service
"""

import os
import re
import shlex
import subprocess
import threading
import queue
import shutil
import time
from datetime import datetime

import requests
import telebot
from shutil import which

# ================== CONFIG ==================
BOT_TOKEN = "ISI_TOKEN_BOT_MU"
TERABOX_CONNECT_KEY = "ISI_CONNECT_KEY_TERABOX"
DOODSTREAM_API_KEY = "ISI_API_KEY_DOODSTREAM"

BASE_DOWNLOAD_DIR = "downloads"   # local download root
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

# downloader candidates
DL_CANDIDATES = ["megadl", "mega-get", "megatools"]

# default behaviour
DELETE_AFTER_UPLOAD_DEFAULT = False

# media extensions considered for renaming
MEDIA_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
             ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".flv")

# =============== STATE ====================
bot = telebot.TeleBot(BOT_TOKEN)
user_state = {}   # per-chat state: { chat_id: { "prefix": "...", "service": "terabox"/"doodstream", "delete_after": bool } }
job_q = queue.Queue()
current_job = None
state_lock = threading.Lock()

# ============== UTIL FUNCTIONS ==============
def send(chat_id, text, **kwargs):
    """Send plain text (no Markdown parsing) to avoid formatting errors."""
    try:
        bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        print(f"[send] Telegram error: {e}")

def find_downloader():
    for c in DL_CANDIDATES:
        if which(c):
            return c
    return None

DL_CMD = find_downloader()

def run_cmd(cmd, cwd=None, timeout=None):
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, "", f"TimeoutExpired: {e}"

def extract_mega_link(text):
    """Extract Mega.nz file or folder link from a text blob."""
    if not text:
        return None
    # Accept keys with various chars after # until whitespace
    m = re.search(r"(https?://mega\.nz/(?:file|folder)/[A-Za-z0-9_-]+(?:#[A-Za-z0-9!@\$%^&*()_\-+=\.,~]+)?)", text)
    return m.group(1) if m else None

def sanitize_filename(name):
    name = name.strip()
    # allow spaces and @ and - and underscore; remove slashes and other illegal chars
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"\|\?\*]', '_', name)
    # collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "untitled"

def is_media_file(filename):
    return filename.lower().endswith(MEDIA_EXT)

# ============ RENAME MEDIA FILES ============
def rename_media_files_for_job(chat_id, local_dir):
    """
    Rename all media files in local_dir using user's prefix.
    Format: "<PREFIX> 01.ext", "<PREFIX> 02.ext", ...
    Only affects files matching MEDIA_EXT. Renaming is per-job folder.
    """
    prefix = user_state.get(chat_id, {}).get("prefix")
    if not prefix:
        return  # no prefix set for this user

    # collect media files
    files = sorted([f for f in os.listdir(local_dir) if is_media_file(f)])
    if not files:
        return

    # prepare sanitized prefix (keep @ and spaces)
    prefix_safe = sanitize_filename(prefix)
    # If prefix had spaces, keep them (we sanitized to single spaces)
    # rename sequentially
    for idx, fname in enumerate(files, start=1):
        old_path = os.path.join(local_dir, fname)
        ext = os.path.splitext(fname)[1]
        new_name = f"{prefix_safe} {idx:02d}{ext}"
        new_path = os.path.join(local_dir, new_name)
        # Avoid overwriting: if exists, append timestamp (unlikely)
        if os.path.exists(new_path):
            tstamp = datetime.now().strftime("%Y%m%d%H%M%S")
            new_name = f"{prefix_safe} {idx:02d}_{tstamp}{ext}"
            new_path = os.path.join(local_dir, new_name)
        try:
            os.rename(old_path, new_path)
        except Exception as e:
            print(f"[rename] Failed rename {old_path} -> {new_path}: {e}")

# ============ UPLOAD HELPERS ================
def upload_terabox_path(local_path, remote_path):
    """
    Upload file or folder to Terabox using teraboxcli and connect key.
    This assumes teraboxcli supports:
      teraboxcli --connect-key <KEY> upload <local> <remote>
    Adjust command if your CLI differs.
    """
    # Use same command for file or folder
    cmd = f'teraboxcli --connect-key {shlex.quote(TERABOX_CONNECT_KEY)} upload {shlex.quote(local_path)} {shlex.quote(remote_path)}'
    rc, out, err = run_cmd(cmd, timeout=60*60*2)
    return rc, out, err

def upload_doodstream_file(local_file):
    """
    Upload single file to Doodstream API.
    Steps: GET server URL -> POST file -> parse response.
    """
    try:
        resp = requests.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_API_KEY}", timeout=30)
        resp.raise_for_status()
        js = resp.json()
        upload_url = js.get("result")
        if not upload_url:
            return False, f"No upload server: {js}"
        # upload multipart
        with open(local_file, "rb") as f:
            files = {"file": f}
            r = requests.post(upload_url, data={"api_key": DOODSTREAM_API_KEY}, files=files, timeout=60*60)
            r.raise_for_status()
            jr = r.json()
            # expected structure: {"status":200, "result": {"filecode": "..."}}
            if jr.get("status") in (200, "200") and "result" in jr:
                filecode = jr["result"].get("filecode") or jr["result"].get("file_code") or jr["result"].get("fileid")
                if filecode:
                    link = f"https://doodstream.com/d/{filecode}"
                    return True, link
                return True, jr
            return False, jr
    except Exception as e:
        return False, str(e)

# ============ WORKER ================
def process_job(job):
    """
    job keys:
      - chat_id
      - link
      - terabox_path (remote path, default '/')
      - delete_after (bool)
      - service ('terabox'|'doodstream')
      - folder_name optional
    """
    chat_id = job["chat_id"]
    link = job["link"]
    terabox_path = job.get("terabox_path", "/")
    delete_after = job.get("delete_after", DELETE_AFTER_UPLOAD_DEFAULT)
    service = job.get("service", user_state.get(chat_id, {}).get("service", "terabox"))

    send(chat_id, f"🔔 Job mulai: {link}\nService: {service}\nDest: {terabox_path}")
    # prepare local folder
    is_folder = "/folder/" in link
    if is_folder:
        token = link.rstrip("/").split("/")[-1]
        folder_name = f"mega_folder_{token}"
    else:
        # file -> use timestamp or provided folder_name
        folder_name = job.get("folder_name") or datetime.now().strftime("job_%Y%m%d_%H%M%S")
    local_dir = os.path.join(BASE_DOWNLOAD_DIR, folder_name)
    os.makedirs(local_dir, exist_ok=True)

    # Download
    if DL_CMD is None:
        send(chat_id, "❌ Downloader Mega tidak ditemukan (megadl/mega-get/megatools).")
        return
    if DL_CMD in ("megadl", "megatools"):
        dl_cmd = f'megadl "{link}" --path "{local_dir}"'
    elif DL_CMD == "mega-get":
        dl_cmd = f'mega-get "{link}" "{local_dir}"'
    else:
        dl_cmd = f'{DL_CMD} "{link}" --path "{local_dir}"'
    send(chat_id, f"⬇ Men-download: {dl_cmd}")
    rc, out, err = run_cmd(dl_cmd, timeout=60*60*6)
    if rc != 0:
        send(chat_id, f"❌ Download gagal (rc={rc}).\n{err[:800]}")
        # cleanup empty dir if created
        try:
            if os.path.isdir(local_dir) and not os.listdir(local_dir):
                os.rmdir(local_dir)
        except: pass
        return
    send(chat_id, "✅ Download selesai.")

    # Rename media files if user set prefix
    try:
        rename_media_files_for_job(chat_id, local_dir)
    except Exception as e:
        print("[rename] error:", e)

    # list items
    try:
        items = sorted(os.listdir(local_dir))
    except Exception as e:
        send(chat_id, f"❌ Gagal baca folder lokal: {e}")
        return
    if not items:
        send(chat_id, "⚠ Hasil download kosong.")
        return

    # Upload
    if service == "terabox":
        # if folder (or more than 1 item) -> upload folder; else upload file
        if is_folder or len(items) > 1:
            send(chat_id, f"⬆ Upload folder ke Terabox: {terabox_path}")
            rc2, out2, err2 = upload_terabox_path(local_dir, terabox_path)
            if rc2 != 0:
                send(chat_id, f"❌ Upload folder gagal (rc={rc2}).")
            else:
                send(chat_id, f"✅ Upload folder selesai.")
        else:
            file_path = os.path.join(local_dir, items[0])
            send(chat_id, f"⬆ Upload file ke Terabox: {items[0]}")
            rc2, out2, err2 = upload_terabox_path(file_path, terabox_path)
            if rc2 != 0:
                send(chat_id, f"❌ Upload file gagal (rc={rc2}).")
            else:
                send(chat_id, f"✅ Upload file selesai.")
    else:  # doodstream
        # doodstream: upload files individually (folder uploads not supported here)
        for fname in items:
            fpath = os.path.join(local_dir, fname)
            if os.path.isfile(fpath):
                send(chat_id, f"⬆ Upload ke Doodstream: {fname}")
                ok, res = upload_doodstream_file(fpath)
                if ok:
                    send(chat_id, f"✅ Doodstream: {res}")
                else:
                    send(chat_id, f"❌ Doodstream gagal: {res}")

    # cleanup local if requested
    if delete_after:
        try:
            shutil.rmtree(local_dir, ignore_errors=True)
            send(chat_id, f"🗑 Folder lokal `{folder_name}` dihapus.")
        except Exception as e:
            send(chat_id, f"⚠ Gagal hapus lokal: {e}")

    send(chat_id, f"🎉 Job selesai: {folder_name}")

def worker_loop():
    global current_job
    while True:
        job = job_q.get()
        if job is None:
            break
        with state_lock:
            current_job = job
        try:
            process_job(job)
        except Exception as e:
            try:
                send(job.get("chat_id"), f"Job error: {e}")
            except:
                print("[worker] notify error:", e)
        with state_lock:
            current_job = None
        job_q.task_done()

# start worker thread
t = threading.Thread(target=worker_loop, daemon=True)
t.start()

# ============ TELEGRAM HANDLERS ==============
@bot.message_handler(commands=['start'])
def cmd_start(m):
    send(m.chat.id, "👋 Halo! Kirim link Mega.nz (file/folder) atau kirim foto/video.\nGunakan /help untuk panduan.")

@bot.message_handler(commands=['help'])
def cmd_help(m):
    text = (
        "📌 *Perintah*:\n"
        "/start - Mulai\n"
        "/help - Bantuan\n"
        "/status - Lihat job & antrian\n"
        "/cleanup list|all|<folder> - Kelola folder lokal\n"
        "/set_delete on|off - Set default delete after upload\n"
        "/setprefix <prefix> - Set prefix untuk rename foto/video\n"
        "/listfolders - Lihat folder di downloads\n"
        "/service terabox|doodstream - Set default service upload\n\n"
        "Contoh:\n"
        "/setprefix TELEGRAM@missyhot22\n"
        "https://mega.nz/folder/XXXXX#KEY\n"
    )
    # send as plain text to avoid markdown parsing issues
    send(m.chat.id, text)

@bot.message_handler(commands=['status'])
def cmd_status(m):
    chat_id = m.chat.id
    with state_lock:
        cur = current_job
        qlist = list(job_q.queue)
    lines = []
    if cur:
        lines.append(f"▶ Sedang berjalan: {cur.get('link')}")
    else:
        lines.append("▶ Tidak ada job berjalan.")
    if qlist:
        lines.append("⏳ Antrian:")
        for i, j in enumerate(qlist, start=1):
            lines.append(f"{i}. {j.get('link')} (service={j.get('service','terabox')})")
    else:
        lines.append("⏳ Antrian kosong.")
    send(chat_id, "\n".join(lines))

@bot.message_handler(commands=['cleanup'])
def cmd_cleanup(m):
    chat_id = m.chat.id
    args = m.text.split(maxsplit=1)
    if len(args) == 1 or args[1].strip().lower() == "list":
        if not os.path.exists(BASE_DOWNLOAD_DIR):
            send(chat_id, "Folder kosong.")
            return
        folders = os.listdir(BASE_DOWNLOAD_DIR)
        send(chat_id, "Folder di downloads:\n" + ("\n".join(folders) if folders else "(kosong)"))
        return
    param = args[1].strip()
    if param.lower() == "all":
        shutil.rmtree(BASE_DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)
        send(chat_id, "🗑 Semua folder dihapus.")
        return
    # else delete specific folder
    target = os.path.join(BASE_DOWNLOAD_DIR, param)
    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
        send(chat_id, f"🗑 Folder `{param}` dihapus.")
    else:
        send(chat_id, f"Folder `{param}` tidak ditemukan.")

@bot.message_handler(commands=['set_delete'])
def cmd_set_delete(m):
    chat_id = m.chat.id
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        send(chat_id, "Usage: /set_delete on|off")
        return
    val = parts[1].lower() == "on"
    user_state.setdefault(chat_id, {})["delete_after"] = val
    send(chat_id, f"Delete after upload set to {val}")

@bot.message_handler(commands=['setprefix', 'prefix'])
def cmd_setprefix(m):
    chat_id = m.chat.id
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        send(chat_id, "Usage: /setprefix <prefix>\nExample: /setprefix TELEGRAM@missyhot22")
        return
    prefix = parts[1].strip()
    user_state.setdefault(chat_id, {})["prefix"] = prefix
    send(chat_id, f"Prefix set to: {prefix}")

@bot.message_handler(commands=['listfolders'])
def cmd_listfolders(m):
    chat_id = m.chat.id
    if not os.path.exists(BASE_DOWNLOAD_DIR):
        send(chat_id, "Downloads folder kosong.")
        return
    folders = os.listdir(BASE_DOWNLOAD_DIR)
    if not folders:
        send(chat_id, "Downloads folder kosong.")
    else:
        send(chat_id, "Daftar folder:\n" + "\n".join(folders))

@bot.message_handler(commands=['service'])
def cmd_service(m):
    chat_id = m.chat.id
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in ("terabox", "doodstream"):
        send(chat_id, "Usage: /service terabox|doodstream")
        return
    svc = parts[1].lower()
    user_state.setdefault(chat_id, {})["service"] = svc
    send(chat_id, f"Service default set to: {svc}")

# ========== Message handlers: media & mega links ==========
@bot.message_handler(content_types=['photo','video'])
def handle_media(message):
    chat_id = message.chat.id
    prefix = user_state.get(chat_id, {}).get("prefix")
    # determine file_id and extension from file path
    if message.content_type == 'photo':
        file_id = message.photo[-1].file_id
    else:
        file_id = message.video.file_id
    try:
        file_info = bot.get_file(file_id)
    except Exception as e:
        send(chat_id, f"Error getting file info: {e}")
        return
    # extension from file path (may be empty). fallback to jpg/mp4
    ext = os.path.splitext(file_info.file_path)[1] or (".jpg" if message.content_type == 'photo' else ".mp4")
    try:
        data = bot.download_file(file_info.file_path)
    except Exception as e:
        send(chat_id, f"Error downloading file: {e}")
        return

    folder_path = os.path.join(BASE_DOWNLOAD_DIR, str(chat_id))
    os.makedirs(folder_path, exist_ok=True)

    # collect existing media files of same extension or any media for numbering:
    existing_media = sorted([f for f in os.listdir(folder_path) if is_media_file(f)])
    new_index = len(existing_media) + 1

    # determine filename
    if prefix:
        safe_prefix = sanitize_filename(prefix)
        filename = f"{safe_prefix} {new_index:02d}{ext}"
    else:
        # fallback name
        filename = f"file_{new_index:02d}{ext}"

    save_path = os.path.join(folder_path, filename)
    with open(save_path, "wb") as fw:
        fw.write(data)

    send(chat_id, f"✅ Disimpan: {filename}")

@bot.message_handler(func=lambda m: bool(extract_mega_link(m.text) if m.text else False))
def handle_mega_message(m):
    chat_id = m.chat.id
    link = extract_mega_link(m.text)
    if not link:
        send(chat_id, "Link Mega tidak valid.")
        return
    # build job
    user_cfg = user_state.get(chat_id, {})
    job = {
        "chat_id": chat_id,
        "link": link,
        "terabox_path": user_cfg.get("terabox_path", "/"),
        "delete_after": user_cfg.get("delete_after", DELETE_AFTER_UPLOAD_DEFAULT),
        "service": user_cfg.get("service", "terabox"),
        "folder_name": None
    }
    job_q.put(job)
    send(chat_id, f"✅ Link diterima, job dimasukkan ke antrian:\n{link}\nService: {job['service']}")

@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_text_default(m):
    # catch-all for other text messages
    send(m.chat.id, "Perintah/tidak dikenali. Gunakan /help.")

# ============ MAIN ============
if __name__ == "__main__":
    print("Bot started...")
    bot.polling(none_stop=True)
