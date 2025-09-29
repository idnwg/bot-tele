#!/usr/bin/env python3
import os
import logging
import asyncio
import requests
import subprocess
import re
import json
import time
import shutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from pathlib import Path
from queue import Queue
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.join('/root/bot-tele', '.env'))

# Konfigurasi dari .env
BOT_TOKEN = os.getenv('BOT_TOKEN')
TERABOX_CONNECT_KEY = os.getenv('TERABOX_CONNECT_KEY')
DOODSTREAM_API_KEY = os.getenv('DOODSTREAM_API_KEY')
AUTO_CLEANUP = os.getenv('AUTO_CLEANUP', 'true').lower() == 'true'

BASE_DIR = "/root/bot-tele"
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
TERABOX_CLI = os.path.join(BASE_DIR, "teraboxcli", "main.py")
TERABOX_SETTINGS = os.path.join(BASE_DIR, "teraboxcli", "settings.json")
USER_SETTINGS_FILE = os.path.join(BASE_DIR, "user_settings.json")

# Antrian jobs
download_queue = Queue()
upload_queue = Queue()
active_jobs = {}

# Ekstensi file yang didukung untuk rename
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg'}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== USER SETTINGS MANAGEMENT ==========

def load_user_settings():
    """Load user settings from JSON file"""
    try:
        if os.path.exists(USER_SETTINGS_FILE):
            with open(USER_SETTINGS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading user settings: {e}")
        return {}

def save_user_settings(settings):
    """Save user settings to JSON file"""
    try:
        with open(USER_SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving user settings: {e}")

def get_user_settings(user_id):
    """Get settings for specific user"""
    settings = load_user_settings()
    if str(user_id) not in settings:
        # Default settings
        settings[str(user_id)] = {
            'prefix': 'file_',
            'platform': 'terabox',
            'auto_upload': False,
            'auto_cleanup': True
        }
        save_user_settings(settings)
    return settings[str(user_id)]

def update_user_settings(user_id, new_settings):
    """Update settings for specific user"""
    settings = load_user_settings()
    settings[str(user_id)] = {**get_user_settings(user_id), **new_settings}
    save_user_settings(settings)

# ========== TERABOX SETTINGS MANAGEMENT ==========

def update_terabox_settings(local_folder, remote_folder):
    """Update Terabox settings.json untuk folder tertentu"""
    try:
        with open(TERABOX_SETTINGS, 'r') as f:
            settings = json.load(f)
        
        settings['directories']['sourcedir'] = local_folder
        settings['directories']['remotedir'] = remote_folder
        settings['files']['deletesource'] = "false"
        
        with open(TERABOX_SETTINGS, 'w') as f:
            json.dump(settings, f, indent=2)
        
        logger.info(f"Updated Terabox settings: {local_folder} -> {remote_folder}")
        return True
    except Exception as e:
        logger.error(f"Error updating Terabox settings: {e}")
        return False

# ========== CLEANUP FUNCTIONS ==========

async def cleanup_after_upload(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hapus folder setelah upload selesai"""
    try:
        chat_id = update.effective_chat.id
        
        if not folder_path.startswith(DOWNLOADS_DIR):
            logger.error(f"Invalid folder path for cleanup: {folder_path}")
            return False
            
        if os.path.exists(folder_path):
            folder_name = os.path.basename(folder_path)
            shutil.rmtree(folder_path)
            await send_progress_message(chat_id, f"🧹 Folder berhasil dihapus: {folder_name}", context)
            logger.info(f"Cleanup completed: {folder_path}")
            return True
        else:
            logger.warning(f"Folder tidak ditemukan untuk cleanup: {folder_path}")
            return False
            
    except Exception as e:
        error_msg = f"❌ Gagal menghapus folder: {str(e)}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Cleanup error: {e}")
        return False

async def cleanup_old_downloads():
    """Bersihkan folder download yang sudah lama - FIXED VERSION"""
    try:
        current_time = time.time()
        cleanup_threshold = 24 * 3600  # 24 jam
        
        # Gunakan asyncio.to_thread untuk operasi blocking
        def sync_cleanup():
            folders_to_remove = []
            for folder_name in os.listdir(DOWNLOADS_DIR):
                folder_path = os.path.join(DOWNLOADS_DIR, folder_name)
                if os.path.isdir(folder_path):
                    folder_time = os.path.getmtime(folder_path)
                    if current_time - folder_time > cleanup_threshold:
                        folders_to_remove.append(folder_path)
            
            for folder_path in folders_to_remove:
                try:
                    shutil.rmtree(folder_path)
                    logger.info(f"Cleaned up old folder: {os.path.basename(folder_path)}")
                except Exception as e:
                    logger.error(f"Error cleaning up folder {folder_path}: {e}")
            
            return len(folders_to_remove)
        
        # Jalankan cleanup di thread terpisah
        removed_count = await asyncio.get_event_loop().run_in_executor(None, sync_cleanup)
        if removed_count > 0:
            logger.info(f"Auto-cleanup completed: {removed_count} folders removed")
        
    except Exception as e:
        logger.error(f"Error in cleanup_old_downloads: {e}")

# ========== HELPER FUNCTIONS ==========

def get_folders_list(page=0, items_per_page=10):
    """Mendapatkan daftar folder dengan pagination"""
    try:
        folders = [f for f in os.listdir(DOWNLOADS_DIR) 
                  if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))]
        folders.sort()
        
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        return folders[start_idx:end_idx], len(folders), page
    except Exception as e:
        logger.error(f"Error getting folders: {e}")
        return [], 0, 0

async def send_progress_message(chat_id, message, context):
    """Mengirim/memperbarui pesan progress"""
    try:
        if 'progress_msg_id' in context.user_data:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=context.user_data['progress_msg_id'],
                text=message
            )
        else:
            msg = await context.bot.send_message(chat_id=chat_id, text=message)
            context.user_data['progress_msg_id'] = msg.message_id
    except Exception as e:
        logger.error(f"Error sending progress: {e}")

def safe_rename(old_path, new_path):
    """Safe file rename dengan handling error"""
    try:
        os.rename(old_path, new_path)
        return True
    except Exception as e:
        logger.error(f"Rename failed {old_path} -> {new_path}: {e}")
        return False

# ========== MEGA.NZ FUNCTIONS ==========

async def list_mega_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List semua folder di akun Mega menggunakan mega-cmd"""
    try:
        # Gunakan mega-cmd untuk list folder
        result = subprocess.run(['mega-ls'], capture_output=True, text=True)
        
        if result.returncode != 0:
            await update.message.reply_text("❌ Gagal mengambil daftar folder dari Mega.nz")
            logger.error(f"mega-ls failed: {result.stderr}")
            return
        
        lines = result.stdout.strip().split('\n')
        folders = []
        
        for line in lines:
            line = line.strip()
            if line and not line.startswith('[') and not line.startswith('Used') and not line.startswith('Total'):
                # Filter hanya folder (tidak ada ekstensi file)
                if '.' not in line or any(line.endswith(ext) for ext in ['.txt', '.zip', '.rar']):
                    # Skip file, hanya ambil folder
                    if '/' not in line and '\\' not in line:
                        folders.append(line)
        
        if not folders:
            await update.message.reply_text("📭 Tidak ada folder di akun Mega.nz")
            return
        
        # Buat keyboard untuk pilihan folder
        keyboard = []
        for folder in sorted(folders):
            keyboard.append([InlineKeyboardButton(f"📁 {folder}", callback_data=f"megadl_{folder}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📂 Pilih folder dari Mega.nz untuk didownload:",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error mengambil daftar folder: {str(e)}")
        logger.error(f"List mega folders error: {e}")

async def download_mega_folder(folder_name, job_id, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download folder dari Mega.nz menggunakan mega-cmd"""
    try:
        chat_id = update.effective_chat.id
        user_settings = get_user_settings(chat_id)
        
        await send_progress_message(chat_id, f"📥 Memulai download folder: {folder_name}", context)
        
        # Download folder dari Mega menggunakan mega-get
        target_path = os.path.join(DOWNLOADS_DIR, folder_name)
        os.makedirs(target_path, exist_ok=True)
        
        # Download folder recursive
        cmd = ['mega-get', f'/{folder_name}', target_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise Exception(f"Download gagal: {result.stderr}")
        
        await send_progress_message(chat_id, f"✅ Download selesai: {folder_name}", context)
        
        # Auto rename files media
        await send_progress_message(chat_id, f"🔄 Memulai rename file media...", context)
        rename_results = await auto_rename_media_files(target_path, user_settings['prefix'], update, context)
        
        await send_progress_message(chat_id, 
            f"✅ Rename selesai!\n"
            f"📸 Foto: {rename_results['photos']} files\n"
            f"🎬 Video: {rename_results['videos']} files", 
            context
        )
        
        # Auto upload jika dienable
        if user_settings.get('auto_upload', False):
            platform = user_settings.get('platform', 'terabox')
            await send_progress_message(chat_id, f"📤 Auto-upload ke {platform}...", context)
            await process_auto_upload(target_path, platform, update, context)
        
        logger.info(f"Mega folder download completed: {folder_name}")
        
    except Exception as e:
        error_msg = f"❌ Gagal download folder: {str(e)}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Mega folder download failed: {e}")

async def auto_rename_media_files(folder_path, prefix, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto rename semua file media dalam folder recursive"""
    photo_count = 0
    video_count = 0
    
    try:
        # Gunakan thread executor untuk operasi file yang blocking
        def sync_rename():
            nonlocal photo_count, video_count
            for root, dirs, files in os.walk(folder_path):
                # Process photos
                photo_files = [f for f in files 
                             if os.path.splitext(f)[1].lower() in PHOTO_EXTENSIONS]
                for i, filename in enumerate(sorted(photo_files), 1):
                    old_path = os.path.join(root, filename)
                    ext = os.path.splitext(filename)[1]
                    new_name = f"{prefix}pic_{i:04d}{ext}"
                    new_path = os.path.join(root, new_name)
                    
                    if safe_rename(old_path, new_path):
                        photo_count += 1
                
                # Process videos
                video_files = [f for f in files 
                             if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS]
                for i, filename in enumerate(sorted(video_files), 1):
                    old_path = os.path.join(root, filename)
                    ext = os.path.splitext(filename)[1]
                    new_name = f"{prefix}vid_{i:04d}{ext}"
                    new_path = os.path.join(root, new_name)
                    
                    if safe_rename(old_path, new_path):
                        video_count += 1
            return {'photos': photo_count, 'videos': video_count}
        
        # Jalankan rename di thread terpisah
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_rename)
        return result
        
    except Exception as e:
        logger.error(f"Auto rename error: {e}")
        return {'photos': 0, 'videos': 0}

# ========== UPLOAD FUNCTIONS ==========

async def upload_to_terabox(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload ke Terabox menggunakan CLI - File per file dengan folder di Terabox"""
    try:
        chat_id = update.effective_chat.id
        folder_name = os.path.basename(folder_path)
        
        await send_progress_message(chat_id, f"📤 Upload ke Terabox: {folder_name}", context)
        
        # Update Terabox settings untuk folder ini
        remote_folder = f"/MegaUploads/{folder_name}"
        if not update_terabox_settings(folder_path, remote_folder):
            await send_progress_message(chat_id, "❌ Gagal update settings Terabox", context)
            return
        
        # Kumpulkan semua file media (foto dan video) untuk diupload
        media_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_ext = os.path.splitext(file)[1].lower()
                if file_ext in PHOTO_EXTENSIONS or file_ext in VIDEO_EXTENSIONS:
                    media_files.append(os.path.join(root, file))
        
        if not media_files:
            await send_progress_message(chat_id, "❌ Tidak ada file media (foto/video) yang ditemukan", context)
            return
        
        total_files = len(media_files)
        await send_progress_message(chat_id, f"📤 Mengupload {total_files} file ke Terabox...", context)
        
        success_count = 0
        failed_count = 0
        uploaded_links = []
        
        for i, file_path in enumerate(media_files, 1):
            try:
                await send_progress_message(chat_id, 
                    f"📤 Progress: {i}/{total_files}\n"
                    f"🔄 Upload: {os.path.basename(file_path)}", 
                    context
                )
                
                # Upload file menggunakan TeraboxUploaderCLI
                cmd = ['python3', TERABOX_CLI, 'upload', file_path]
                if TERABOX_CONNECT_KEY:
                    cmd.extend(['--connect-key', TERABOX_CONNECT_KEY])
                
                result = subprocess.run(cmd, capture_output=True, text=True, 
                                      cwd=os.path.dirname(TERABOX_CLI))
                
                if result.returncode == 0:
                    success_count += 1
                    # Extract link dari output
                    link = extract_terabox_link(result.stdout)
                    if link:
                        uploaded_links.append(link)
                    
                    logger.info(f"Terabox upload successful: {os.path.basename(file_path)}")
                else:
                    failed_count += 1
                    logger.error(f"Terabox upload failed for {file_path}: {result.stderr}")
                    
            except Exception as e:
                failed_count += 1
                logger.error(f"Error uploading {file_path} to Terabox: {e}")
        
        # Buat laporan hasil upload
        message = f"✅ Upload Terabox selesai!\n"
        message += f"📁 Folder: {folder_name}\n"
        message += f"📊 Hasil: {success_count} sukses, {failed_count} gagal\n"
        
        if uploaded_links:
            message += f"\n📎 Contoh links ({min(3, len(uploaded_links))} dari {len(uploaded_links)}):\n"
            for link in uploaded_links[:3]:
                message += f"🔗 {link}\n"
        
        await send_progress_message(chat_id, message, context)
        logger.info(f"Terabox upload completed: {success_count}/{total_files} files")
        
        # AUTO CLEANUP setelah upload selesai
        user_settings = get_user_settings(chat_id)
        if success_count > 0 and (user_settings.get('auto_cleanup', True) or AUTO_CLEANUP):
            await send_progress_message(chat_id, "🧹 Membersihkan folder lokal...", context)
            await cleanup_after_upload(folder_path, update, context)
            
    except Exception as e:
        error_msg = f"❌ Error upload Terabox: {str(e)}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Terabox upload error: {e}")

async def upload_to_doodstream(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload ke Doodstream via API - File video saja"""
    try:
        chat_id = update.effective_chat.id
        folder_name = os.path.basename(folder_path)
        
        await send_progress_message(chat_id, f"📤 Upload ke Doodstream: {folder_name}", context)
        
        # Kumpulkan hanya file video
        video_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS:
                    video_files.append(os.path.join(root, file))
        
        if not video_files:
            await send_progress_message(chat_id, "❌ Tidak ada file video yang ditemukan untuk Doodstream", context)
            return
        
        total_files = len(video_files)
        await send_progress_message(chat_id, f"📤 Mengupload {total_files} video ke Doodstream...", context)
        
        uploaded_links = []
        success_count = 0
        
        for i, file_path in enumerate(video_files, 1):
            try:
                await send_progress_message(chat_id, 
                    f"📤 Progress: {i}/{total_files}\n"
                    f"🔄 Upload: {os.path.basename(file_path)}", 
                    context
                )
                
                result = await upload_single_file_to_doodstream(file_path)
                if result:
                    uploaded_links.append(result)
                    success_count += 1
                    logger.info(f"Doodstream upload successful: {result}")
                else:
                    logger.error(f"Doodstream upload failed for: {file_path}")
                    
            except Exception as e:
                logger.error(f"Error uploading {file_path} to Doodstream: {e}")
        
        if uploaded_links:
            message = f"✅ Upload Doodstream selesai!\n"
            message += f"📁 Folder: {folder_name}\n"
            message += f"📹 Video: {success_count} berhasil\n"
            message += f"\n📎 Links ({min(3, len(uploaded_links))} dari {len(uploaded_links)}):\n"
            for link in uploaded_links[:3]:
                message += f"🔗 {link}\n"
            
            await send_progress_message(chat_id, message, context)
        else:
            await send_progress_message(chat_id, "❌ Tidak ada video yang berhasil diupload ke Doodstream", context)
        
        # AUTO CLEANUP setelah upload selesai
        user_settings = get_user_settings(chat_id)
        if success_count > 0 and (user_settings.get('auto_cleanup', True) or AUTO_CLEANUP):
            await send_progress_message(chat_id, "🧹 Membersihkan folder lokal...", context)
            await cleanup_after_upload(folder_path, update, context)
            
    except Exception as e:
        error_msg = f"❌ Error upload Doodstream: {str(e)}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Doodstream upload error: {e}")

async def upload_single_file_to_doodstream(file_path):
    """Upload single file ke Doodstream"""
    try:
        if not DOODSTREAM_API_KEY:
            raise Exception("Doodstream API key tidak ditemukan")
        
        # Dapatkan server upload
        server_resp = requests.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_API_KEY}")
        server_data = server_resp.json()
        
        if server_data.get('status') != 200:
            raise Exception(f"Gagal dapat server: {server_data.get('msg', 'Unknown error')}")
        
        # Upload file
        with open(file_path, 'rb') as f:
            files = {'file': f}
            upload_resp = requests.post(
                server_data['result'],
                files=files,
                data={'api_key': DOODSTREAM_API_KEY}
            )
        upload_data = upload_resp.json()
        
        if upload_data.get('status') == 200:
            download_url = upload_data['result']['download_url']
            return download_url
        else:
            raise Exception(f"Upload gagal: {upload_data.get('msg', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"Doodstream single file upload error: {e}")
        return None

def extract_terabox_link(output):
    """Extract link dari output TeraboxUploaderCLI"""
    try:
        patterns = [
            r'https?://[^\s]*terabox[^\s]*',
            r'https?://[^\s]*1024tera[^\s]*',
            r'Link: (https?://[^\s]+)',
            r'URL: (https?://[^\s]+)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, output)
            if matches:
                return matches[0]
        
        lines = output.split('\n')
        for line in lines:
            if 'http' in line and '://' in line:
                url_match = re.search(r'(https?://[^\s]+)', line)
                if url_match:
                    return url_match.group(1)
        
        return None
    except Exception as e:
        logger.error(f"Error extracting Terabox link: {e}")
        return None

async def process_auto_upload(folder_path, platform, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process auto upload setelah rename"""
    if platform == 'terabox':
        await upload_to_terabox(folder_path, update, context)
    elif platform == 'doodstream':
        await upload_to_doodstream(folder_path, update, context)

# ========== SETTINGS HANDLERS ==========

async def setprefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /setprefix"""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: /setprefix <prefix>\n"
            "Contoh: /setprefix 😍\n"
            "Contoh: /setprefix vacation_\n"
            "Contoh: /setprefix 📸2024_"
        )
        return
    
    prefix = ' '.join(context.args)
    user_id = update.effective_user.id
    
    if len(prefix) > 20:
        await update.message.reply_text("❌ Prefix terlalu panjang (max 20 karakter)")
        return
    
    update_user_settings(user_id, {'prefix': prefix})
    
    await update.message.reply_text(f"✅ Prefix berhasil diatur ke: `{prefix}`", parse_mode='Markdown')
    logger.info(f"User {user_id} set prefix to: {prefix}")

async def setplatform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /setplatform"""
    if not context.args or context.args[0].lower() not in ['terabox', 'doodstream']:
        await update.message.reply_text(
            "❌ Format: /setplatform <terabox|doodstream>\n"
            "Contoh: /setplatform terabox\n"
            "Contoh: /setplatform doodstream"
        )
        return
    
    platform = context.args[0].lower()
    user_id = update.effective_user.id
    
    update_user_settings(user_id, {'platform': platform})
    
    await update.message.reply_text(f"✅ Platform upload berhasil diatur ke: **{platform}**", parse_mode='Markdown')
    logger.info(f"User {user_id} set platform to: {platform}")

async def autoupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /autoupload"""
    user_id = update.effective_user.id
    current_settings = get_user_settings(user_id)
    new_auto_upload = not current_settings.get('auto_upload', False)
    
    update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "AKTIF ✅" if new_auto_upload else "NONAKTIF ❌"
    await update.message.reply_text(
        f"✅ Auto-upload berhasil diatur ke: **{status}**\n"
        f"Platform: **{current_settings.get('platform', 'terabox')}**\n\n"
        f"Bot akan {'secara otomatis mengupload ' if new_auto_upload else 'TIDAK otomatis mengupload '}"
        f"setelah download dan rename selesai."
    )
    logger.info(f"User {user_id} set auto_upload to: {new_auto_upload}")

async def autocleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /autocleanup"""
    user_id = update.effective_user.id
    current_settings = get_user_settings(user_id)
    new_auto_cleanup = not current_settings.get('auto_cleanup', True)
    
    update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "AKTIF ✅" if new_auto_cleanup else "NONAKTIF ❌"
    await update.message.reply_text(
        f"✅ Auto-cleanup berhasil diatur ke: **{status}**\n\n"
        f"Bot akan {'secara otomatis menghapus folder ' if new_auto_cleanup else 'TIDAK otomatis menghapus folder '}"
        f"setelah upload selesai."
    )
    logger.info(f"User {user_id} set auto_cleanup to: {new_auto_cleanup}")

async def mysettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /mysettings"""
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    
    settings_text = f"""
⚙️ **Settings Anda**

📛 Prefix: `{settings.get('prefix', 'file_')}`
📤 Platform: **{settings.get('platform', 'terabox').upper()}**
🔄 Auto-upload: **{'AKTIF ✅' if settings.get('auto_upload', False) else 'NONAKTIF ❌'}**
🧹 Auto-cleanup: **{'AKTIF ✅' if settings.get('auto_cleanup', True) else 'NONAKTIF ❌'}**

**Perintah Settings:**
/setprefix <prefix> - Atur prefix rename
/setplatform <terabox|doodstream> - Atur platform upload
/autoupload - Aktif/nonaktif auto-upload
/autocleanup - Aktif/nonaktif auto-cleanup
    """
    await update.message.reply_text(settings_text, parse_mode='Markdown')

# ========== TELEGRAM BOT HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /start"""
    welcome_text = """
🤖 **Bot Download & Upload Manager**

**Fitur:**
📥 Download folder dari Mega.nz
🔄 Auto-rename file media
📤 Upload ke Terabox & Doodstream
⚙️ Custom prefix & auto-upload
🧹 Auto-cleanup setelah upload

**Perintah:**
/listmega - List folder di Mega.nz
/download <folder> - Download folder
/rename <old> <new> - Rename manual
/upload - Upload interaktif

**Settings:**
/setprefix <prefix> - Atur prefix rename
/setplatform <terabox|doodstream> - Pilih platform
/autoupload - Aktif/nonaktif auto-upload
/autocleanup - Aktif/nonaktif auto-cleanup
/mysettings - Lihat settings

/status - Status sistem
/cleanup - Bersihkan folder manual
/cancel <jobid> - Batalkan job
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def listmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /listmega"""
    await list_mega_folders(update, context)

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /download"""
    if not context.args:
        await update.message.reply_text("❌ Format: /download folder_name")
        return
    
    folder_name = context.args[0]
    job_id = f"download_{update.update_id}"
    
    download_queue.put({
        'job_id': job_id,
        'folder_name': folder_name,
        'update': update,
        'context': context
    })
    
    active_jobs[job_id] = 'download'
    await update.message.reply_text(f"✅ Ditambahkan ke antrian download\nFolder: `{folder_name}`\nJob ID: `{job_id}`", parse_mode='Markdown')
    
    asyncio.create_task(process_download_queue())

async def process_download_queue():
    """Proses antrian download"""
    while not download_queue.empty():
        job = download_queue.get()
        await download_mega_folder(
            job['folder_name'], 
            job['job_id'], 
            job['update'], 
            job['context']
        )
        download_queue.task_done()
        active_jobs.pop(job['job_id'], None)

async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /rename - manual rename"""
    if len(context.args) != 2:
        await update.message.reply_text("❌ Format: /rename old_name new_name")
        return
    
    old_name, new_name = context.args
    old_path = os.path.join(DOWNLOADS_DIR, old_name)
    new_path = os.path.join(DOWNLOADS_DIR, new_name)
    
    try:
        os.rename(old_path, new_path)
        await update.message.reply_text(f"✅ Berhasil rename: {old_name} → {new_name}")
        logger.info(f"Renamed {old_name} to {new_name}")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal rename: {str(e)}")
        logger.error(f"Rename failed: {e}")

async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /upload - Tampilkan pilihan folder"""
    folders, total_folders, current_page = get_folders_list()
    
    if not folders:
        await update.message.reply_text("❌ Tidak ada folder di downloads/")
        return
    
    keyboard = []
    for folder in folders:
        keyboard.append([InlineKeyboardButton(f"📁 {folder}", callback_data=f"select_{folder}")])
    
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{current_page-1}"))
    if (current_page + 1) * 10 < total_folders:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{current_page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📂 Pilih folder untuk diupload (Halaman {current_page + 1}):",
        reply_markup=reply_markup
    )

async def upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk callback upload"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("page_"):
        page = int(data.split("_")[1])
        folders, total_folders, _ = get_folders_list(page)
        
        keyboard = []
        for folder in folders:
            keyboard.append([InlineKeyboardButton(f"📁 {folder}", callback_data=f"select_{folder}")])
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
        if (page + 1) * 10 < total_folders:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"📂 Pilih folder untuk diupload (Halaman {page + 1}):",
            reply_markup=reply_markup
        )
    
    elif data.startswith("select_"):
        folder_name = data.split("_", 1)[1]
        context.user_data['selected_folder'] = folder_name
        
        keyboard = [
            [
                InlineKeyboardButton("📤 Terabox", callback_data=f"target_terabox_{folder_name}"),
                InlineKeyboardButton("🎬 Doodstream", callback_data=f"target_doodstream_{folder_name}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"📁 Folder: {folder_name}\nPilih target upload:",
            reply_markup=reply_markup
        )
    
    elif data.startswith("target_"):
        target, folder_name = data.split("_", 2)[1:]
        folder_path = os.path.join(DOWNLOADS_DIR, folder_name)
        
        await query.edit_message_text(f"✅ Memulai upload {target}: {folder_name}")
        
        if target == "terabox":
            await upload_to_terabox(folder_path, update, context)
        elif target == "doodstream":
            await upload_to_doodstream(folder_path, update, context)
    
    elif data.startswith("megadl_"):
        folder_name = data.split("_", 1)[1]
        job_id = f"megadl_{query.id}"
        
        await query.edit_message_text(f"✅ Menambahkan ke antrian download: {folder_name}")
        
        download_queue.put({
            'job_id': job_id,
            'folder_name': folder_name,
            'update': update,
            'context': context
        })
        active_jobs[job_id] = 'download'
        
        asyncio.create_task(process_download_queue())

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /status"""
    download_count = download_queue.qsize()
    upload_count = upload_queue.qsize()
    active_count = len(active_jobs)
    
    # Check mega-cmd status
    mega_status = "❌ Unknown"
    try:
        result = subprocess.run(['mega-whoami'], capture_output=True, text=True)
        if result.returncode == 0:
            mega_status = f"✅ {result.stdout.strip()}"
        else:
            mega_status = "❌ Not logged in"
    except:
        mega_status = "❌ mega-cmd not available"
    
    status_text = f"""
📊 **Status Sistem**

📥 Antrian Download: {download_count}
📤 Antrian Upload: {upload_count}
🔄 Jobs Aktif: {active_count}

💾 Mega Status: {mega_status}
📁 Download Folder: {DOWNLOADS_DIR}
🧹 Auto-Cleanup: {'AKTIF ✅' if AUTO_CLEANUP else 'NONAKTIF ❌'}
    """
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /cleanup - Manual cleanup"""
    try:
        folders = [f for f in os.listdir(DOWNLOADS_DIR) 
                  if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))]
        
        if not folders:
            await update.message.reply_text("✅ Tidak ada folder untuk dibersihkan")
            return
        
        total_size = 0
        for folder in folders:
            folder_path = os.path.join(DOWNLOADS_DIR, folder)
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    total_size += os.path.getsize(os.path.join(root, file))
        
        # Konfirmasi cleanup
        keyboard = [
            [
                InlineKeyboardButton("✅ Ya, Hapus Semua", callback_data="cleanup_confirm"),
                InlineKeyboardButton("❌ Batal", callback_data="cleanup_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🧹 **Manual Cleanup**\n\n"
            f"📁 Folder ditemukan: {len(folders)}\n"
            f"💾 Total size: {total_size / (1024*1024):.2f} MB\n"
            f"⚠️ Yakin ingin menghapus SEMUA folder di downloads?",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        logger.error(f"Cleanup error: {e}")

async def cleanup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk callback cleanup"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cleanup_confirm":
        try:
            folders = [f for f in os.listdir(DOWNLOADS_DIR) 
                      if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))]
            
            deleted_count = 0
            for folder in folders:
                folder_path = os.path.join(DOWNLOADS_DIR, folder)
                shutil.rmtree(folder_path)
                deleted_count += 1
            
            await query.edit_message_text(f"✅ Berhasil menghapus {deleted_count} folder")
            logger.info(f"Manual cleanup completed: {deleted_count} folders")
            
        except Exception as e:
            await query.edit_message_text(f"❌ Gagal menghapus folder: {str(e)}")
            logger.error(f"Manual cleanup failed: {e}")
    
    elif data == "cleanup_cancel":
        await query.edit_message_text("❌ Cleanup dibatalkan")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /cancel"""
    if not context.args:
        await update.message.reply_text("❌ Format: /cancel job_id")
        return
    
    job_id = context.args[0]
    if job_id in active_jobs:
        active_jobs.pop(job_id)
        await update.message.reply_text(f"✅ Job {job_id} dibatalkan")
        logger.info(f"Job cancelled: {job_id}")
    else:
        await update.message.reply_text(f"❌ Job {job_id} tidak ditemukan")

# ========== STARTUP TASKS ==========

async def run_startup_tasks():
    """Jalankan task saat startup"""
    try:
        logger.info("Running startup tasks...")
        # Jalankan cleanup untuk folder lama
        await cleanup_old_downloads()
    except Exception as e:
        logger.error(f"Error in startup tasks: {e}")

# ========== MAIN FUNCTION ==========

def main():
    """Main function - FIXED VERSION"""
    # Pastikan direktori ada
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    # Buat aplikasi bot
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Tambah handler commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("listmega", listmega))
    application.add_handler(CommandHandler("download", download))
    application.add_handler(CommandHandler("rename", rename))
    application.add_handler(CommandHandler("upload", upload))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("cleanup", cleanup))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # Settings handlers
    application.add_handler(CommandHandler("setprefix", setprefix))
    application.add_handler(CommandHandler("setplatform", setplatform))
    application.add_handler(CommandHandler("autoupload", autoupload))
    application.add_handler(CommandHandler("autocleanup", autocleanup))
    application.add_handler(CommandHandler("mysettings", mysettings))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(upload_callback, pattern="^(page_|select_|target_|megadl_)"))
    application.add_handler(CallbackQueryHandler(cleanup_callback, pattern="^(cleanup_confirm|cleanup_cancel)$"))
    
    # Jalankan startup tasks setelah bot berjalan
    async def post_init(application):
        asyncio.create_task(run_startup_tasks())
    
    application.post_init(post_init)
    
    # Jalankan bot
    logger.info("🤖 Bot started successfully with Mega-cmd integration and Auto-Cleanup")
    print("🤖 Bot is running... Press Ctrl+C to stop")
    application.run_polling()

if __name__ == "__main__":
    main()
