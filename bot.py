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
MEGA_CREDENTIALS_FILE = os.path.join(BASE_DIR, "mega.json")

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

# ========== IMPROVED MEGA LOGIN SYSTEM ==========

def setup_environment():
    """Setup environment untuk snap installation dengan path yang lengkap"""
    try:
        # Tambahkan semua kemungkinan path untuk snap
        snap_paths = [
            '/snap/bin',
            '/var/lib/snapd/snap/bin',
            '/usr/local/bin',
            '/usr/bin',
            '/bin',
            os.path.expanduser('~/snap/bin'),
            os.path.expanduser('~/.local/bin')
        ]
        
        current_path = os.environ.get('PATH', '')
        new_paths = []
        for path in snap_paths:
            if os.path.exists(path) and path not in current_path:
                new_paths.append(path)
        
        if new_paths:
            os.environ['PATH'] = f"{current_path}:{':'.join(new_paths)}"
        
        # Set environment variables untuk MEGA
        mega_email = os.getenv('MEGA_EMAIL', '')
        mega_password = os.getenv('MEGA_PASSWORD', '')
        
        if mega_email:
            os.environ['MEGA_EMAIL'] = mega_email
        if mega_password:
            os.environ['MEGA_PASSWORD'] = mega_password
            
        logger.info("Environment setup completed")
        return True
    except Exception as e:
        logger.error(f"Environment setup error: {e}")
        return False

def check_mega_cmd():
    """Check jika mega-cmd terinstall dengan berbagai metode"""
    try:
        setup_environment()
        
        # Test berbagai command mega-cmd
        test_commands = [
            ['mega-ls', '--help'],
            ['mega-whoami'],
            ['mega-version'],
            ['mega-cmd', '--help'],
            ['snap', 'run', 'mega-cmd', '--help'],
            ['/snap/bin/mega-cmd', '--help'],
            ['/snap/bin/mega-ls', '--help']
        ]
        
        for cmd in test_commands:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0 or any(x in result.stdout.lower() for x in ['mega', 'usage:']) or any(x in result.stderr.lower() for x in ['mega', 'usage:']):
                    logger.info(f"Mega-cmd available via: {' '.join(cmd)}")
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
                logger.debug(f"Command failed: {' '.join(cmd)} - {e}")
                continue
        
        # Cek snap installation
        try:
            result = subprocess.run(['snap', 'list', 'mega-cmd'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and 'mega-cmd' in result.stdout:
                logger.info("Mega-cmd found in snap list")
                return True
        except:
            pass
            
        logger.error("Mega-cmd not found in any location")
        return False
        
    except Exception as e:
        logger.error(f"Error checking mega-cmd: {e}")
        return False

def get_mega_command():
    """Dapatkan command mega yang work dengan prioritas"""
    try:
        # Prioritaskan command yang tersedia
        possible_commands = [
            'mega-ls',
            'mega-get', 
            'mega-whoami',
            'mega-login',
            'mega-logout',
            'mega-cmd',
            '/snap/bin/mega-ls',
            '/snap/bin/mega-get',
            '/snap/bin/mega-whoami',
            '/snap/bin/mega-login',
            '/snap/bin/mega-cmd'
        ]
        
        for cmd in possible_commands:
            try:
                result = subprocess.run([cmd, '--help'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 or any(x in result.stdout.lower() for x in ['mega', 'usage:']) or any(x in result.stderr.lower() for x in ['mega', 'usage:']):
                    logger.info(f"Using mega command: {cmd}")
                    return cmd
            except:
                continue
        
        # Fallback
        logger.info("Using snap run mega-cmd as fallback")
        return 'snap run mega-cmd'
        
    except Exception as e:
        logger.error(f"Error getting mega command: {e}")
        return 'snap run mega-cmd'

def run_mega_command(command_args, timeout=60):
    """Jalankan command mega dengan error handling yang lebih baik"""
    try:
        if isinstance(command_args, str):
            command_args = command_args.split()
        
        base_command = get_mega_command()
        
        if base_command.startswith('/') or base_command.startswith('snap run'):
            if base_command.startswith('snap run'):
                actual_command = base_command.split() + command_args
            else:
                actual_command = [base_command] + command_args
        else:
            actual_command = [base_command] + command_args
        
        logger.info(f"Running mega command: {' '.join(actual_command)}")
        
        result = subprocess.run(
            actual_command,
            capture_output=True, 
            text=True, 
            timeout=timeout,
            env=os.environ
        )
        
        return result
        
    except subprocess.TimeoutExpired:
        logger.error(f"Command timeout: {' '.join(actual_command)}")
        raise
    except Exception as e:
        logger.error(f"Error running command: {e}")
        raise

def check_mega_login():
    """Check status login Mega.nz dengan metode yang lebih reliable"""
    try:
        logger.info("Checking Mega.nz login status...")
        
        # Method 1: Try mega-whoami
        try:
            result = run_mega_command(['whoami'], timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                if '@' in output:
                    return True, f"Logged in as: {output}"
                elif output and 'not logged in' not in output.lower():
                    return True, f"Status: {output}"
        except Exception as e:
            logger.debug(f"whoami failed: {e}")

        # Method 2: Try to list root directory
        try:
            result = run_mega_command(['ls', '/'], timeout=30)
            if result.returncode == 0 and 'error' not in result.stdout.lower():
                return True, "Logged in (verified via directory listing)"
        except Exception as e:
            logger.debug(f"ls check failed: {e}")

        return False, "Not logged in"
            
    except Exception as e:
        return False, f"Error: {str(e)}"

def save_mega_credentials(email, password):
    """Simpan credential Mega ke file JSON"""
    try:
        credentials = {
            "email": email,
            "password": password,
            "saved_at": time.time()
        }
        with open(MEGA_CREDENTIALS_FILE, 'w') as f:
            json.dump(credentials, f, indent=2)
        logger.info(f"Mega credentials saved for: {email}")
        return True
    except Exception as e:
        logger.error(f"Error saving mega credentials: {e}")
        return False

def load_mega_credentials():
    """Load credential Mega dari file JSON"""
    try:
        if os.path.exists(MEGA_CREDENTIALS_FILE):
            with open(MEGA_CREDENTIALS_FILE, 'r') as f:
                credentials = json.load(f)
                return credentials.get('email'), credentials.get('password')
        return None, None
    except Exception as e:
        logger.error(f"Error loading mega credentials: {e}")
        return None, None

def delete_mega_credentials():
    """Hapus credential Mega"""
    try:
        if os.path.exists(MEGA_CREDENTIALS_FILE):
            os.remove(MEGA_CREDENTIALS_FILE)
            logger.info("Mega credentials deleted")
            return True
        return True
    except Exception as e:
        logger.error(f"Error deleting mega credentials: {e}")
        return False

def login_to_mega(email, password):
    """Login ke Mega.nz dengan multiple methods dan better error handling"""
    try:
        logger.info(f"Attempting login to Mega.nz for: {email}")
        
        # Method 1: Direct login dengan mega-login
        try:
            logger.info("Trying direct login with mega-login...")
            result = run_mega_command(['login', email, password], timeout=30)
            
            if result.returncode == 0:
                time.sleep(3)  # Tunggu session terinisialisasi
                is_logged_in, msg = check_mega_login()
                if is_logged_in:
                    logger.info(f"Direct login successful for: {email}")
                    return True, "Login successful"
            else:
                error_output = result.stderr.lower() if result.stderr else result.stdout.lower()
                if 'already logged in' in error_output:
                    logger.info("Already logged in, continuing...")
                    return True, "Already logged in"
                else:
                    logger.warning(f"Direct login failed: {result.stderr}")
        except Exception as e:
            logger.warning(f"Direct login error: {e}")

        # Method 2: Interactive login dengan mega-cmd
        try:
            logger.info("Trying interactive login with mega-cmd...")
            process = subprocess.Popen(
                ['mega-cmd'],  # Gunakan mega-cmd langsung untuk interactive mode
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Kirim perintah login
            commands = [
                f"login {email} {password}\n",
                "quit\n"
            ]
            
            output, error = process.communicate(
                input='\n'.join(commands),
                timeout=30
            )
            
            if process.returncode == 0 or 'logged in' in output.lower():
                time.sleep(3)
                is_logged_in, msg = check_mega_login()
                if is_logged_in:
                    logger.info(f"Interactive login successful for: {email}")
                    return True, "Login successful"
            else:
                logger.warning(f"Interactive login failed: {error}")
        except Exception as e:
            logger.warning(f"Interactive login error: {e}")

        # Method 3: Manual session recovery
        try:
            logger.info("Trying session recovery...")
            # Coba logout dulu kemudian login lagi
            run_mega_command(['logout'], timeout=10)
            time.sleep(2)
            result = run_mega_command(['login', email, password], timeout=30)
            
            if result.returncode == 0:
                time.sleep(3)
                is_logged_in, msg = check_mega_login()
                if is_logged_in:
                    logger.info(f"Session recovery successful for: {email}")
                    return True, "Login successful via recovery"
        except Exception as e:
            logger.warning(f"Session recovery error: {e}")

        return False, "All login methods failed. Please try manual login on VPS with: mega-login email password"
        
    except Exception as e:
        logger.error(f"Login process error: {e}")
        return False, f"Login error: {str(e)}"

def ensure_mega_session():
    """Pastikan session Mega.nz valid dengan improved auto-retry"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Check if already logged in
            is_logged_in, login_msg = check_mega_login()
            if is_logged_in:
                logger.info(f"Mega session valid: {login_msg}")
                return True
            
            # Try to login with saved credentials
            email, password = load_mega_credentials()
            if email and password:
                logger.info(f"Attempting auto-login (attempt {attempt + 1}) for: {email}")
                login_success, login_result = login_to_mega(email, password)
                if login_success:
                    logger.info(f"Auto-login successful on attempt {attempt + 1}")
                    return True
                else:
                    logger.warning(f"Auto-login attempt {attempt + 1} failed: {login_result}")
                    
                    # Jika gagal, coba logout dulu lalu login lagi
                    if attempt < max_retries - 1:
                        logger.info("Trying logout and re-login...")
                        try:
                            run_mega_command(['logout'], timeout=10)
                            time.sleep(3)
                        except:
                            pass
            else:
                logger.warning("No saved Mega credentials found")
                break
                
        except Exception as e:
            logger.error(f"Error ensuring mega session (attempt {attempt + 1}): {e}")
            
        # Tunggu sebelum retry
        if attempt < max_retries - 1:
            time.sleep(5)
    
    logger.error("All mega session attempts failed")
    return False

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

def cleanup_old_downloads():
    """Bersihkan folder download yang sudah lama"""
    try:
        current_time = time.time()
        cleanup_threshold = 24 * 3600  # 24 jam
        
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
        
        if folders_to_remove:
            logger.info(f"Auto-cleanup completed: {len(folders_to_remove)} folders removed")
        
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
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=context.user_data['progress_msg_id'],
                    text=message
                )
            except:
                msg = await context.bot.send_message(chat_id=chat_id, text=message)
                context.user_data['progress_msg_id'] = msg.message_id
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

# ========== IMPROVED MEGA.NZ FUNCTIONS ==========

async def list_mega_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List semua folder di akun Mega menggunakan mega-cmd"""
    try:
        chat_id = update.effective_chat.id
        
        # Check mega-cmd installation
        if not check_mega_cmd():
            await update.message.reply_text(
                "❌ **Mega-cmd tidak ditemukan!**\n\n"
                "Untuk instalasi snap:\n"
                "1. `sudo snap install mega-cmd`\n"
                "2. Restart bot setelah install\n"
                "3. Atau login manual di terminal: `mega-login email password`"
            )
            return
        
        # Ensure valid session dengan improved method
        await send_progress_message(chat_id, "🔐 Memeriksa session Mega.nz...", context)
        session_status = ensure_mega_session()
        
        if not session_status:
            # Berikan opsi untuk login manual
            keyboard = [
                [InlineKeyboardButton("🔄 Coba Login Ulang", callback_data="retry_mega_login")],
                [InlineKeyboardButton("❌ Batalkan", callback_data="cancel_operation")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ **Tidak dapat terhubung ke Mega.nz!**\n\n"
                     "Silakan coba:\n"
                     "1. Login manual dengan `/loginmega email password`\n"
                     "2. Atau login manual di VPS: `mega-login email_password_anda`\n"
                     "3. Kemudian coba lagi /listmega",
                reply_markup=reply_markup
            )
            return
        
        await send_progress_message(chat_id, "📡 Mengambil daftar folder dari Mega.nz...", context)
        
        # List folders using mega-ls dengan timeout lebih lama
        result = run_mega_command(['ls', '/'], timeout=60)
        
        if result.returncode != 0:
            await send_progress_message(chat_id, "❌ Gagal mengambil daftar folder. Session mungkin expired.", context)
            return
        
        lines = result.stdout.strip().split('\n')
        folders = []
        
        for line in lines:
            line = line.strip()
            if (line and 
                not line.startswith('[') and 
                not line.startswith('Used') and 
                not line.startswith('Total') and
                not line.startswith('MEGA') and
                '://' not in line and
                len(line) > 1):  # Exclude single character lines
                
                # Filter untuk folder (bukan file)
                if '.' not in line or any(x in line for x in [' ', '/']):
                    if line and line not in folders:
                        folders.append(line)
        
        if not folders:
            await send_progress_message(chat_id, "📭 Tidak ada folder di akun Mega.nz", context)
            return
        
        # Buat keyboard untuk pilihan folder
        keyboard = []
        for folder in sorted(folders)[:30]:  # Limit to 30 folders
            if folder and len(folder) > 0:
                display_name = folder[:30] + "..." if len(folder) > 30 else folder
                keyboard.append([InlineKeyboardButton(f"📁 {display_name}", callback_data=f"megadl_{folder}")])
        
        if not keyboard:
            await send_progress_message(chat_id, "❌ Tidak ada folder valid yang ditemukan", context)
            return
            
        # Tambahkan button untuk refresh
        keyboard.append([InlineKeyboardButton("🔄 Refresh List", callback_data="refresh_mega_list")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📂 Pilih folder dari Mega.nz untuk didownload ({len(folders)} folder ditemukan):",
            reply_markup=reply_markup
        )
        
        await send_progress_message(chat_id, f"✅ Berhasil mendapatkan {len(folders)} folder", context)
        
    except subprocess.TimeoutExpired:
        await send_progress_message(chat_id, "❌ Timeout: Gagal mengambil daftar folder", context)
    except Exception as e:
        error_msg = f"❌ Error mengambil daftar folder: {str(e)}"
        await send_progress_message(chat_id, error_msg, context)
        logger.error(f"List mega folders error: {e}")

async def download_mega_folder(folder_name, job_id, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download folder dari Mega.nz menggunakan mega-cmd dengan improved error handling"""
    try:
        chat_id = update.effective_chat.id
        user_settings = get_user_settings(chat_id)
        
        # Ensure valid session before download
        await send_progress_message(chat_id, "🔐 Memverifikasi session Mega.nz...", context)
        if not ensure_mega_session():
            await send_progress_message(chat_id, "❌ Session Mega.nz tidak valid. Silakan login ulang.", context)
            return
        
        await send_progress_message(chat_id, f"📥 Memulai download folder: {folder_name}", context)
        
        # Download folder dari Mega
        target_path = os.path.join(DOWNLOADS_DIR, folder_name)
        os.makedirs(target_path, exist_ok=True)
        
        # Download menggunakan mega-get dengan improved error handling
        await send_progress_message(chat_id, f"⏳ Download dalam proses... Ini mungkin butuh waktu lama.", context)
        
        try:
            result = run_mega_command(['get', f'/{folder_name}', target_path], timeout=10800)  # 3 hours timeout
            
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else result.stdout
                logger.error(f"Download failed: {error_msg}")
                
                # Try to re-authenticate and retry once
                await send_progress_message(chat_id, "🔄 Session expired, mencoba login ulang...", context)
                if ensure_mega_session():
                    await send_progress_message(chat_id, "🔄 Mencoba download lagi...", context)
                    result = run_mega_command(['get', f'/{folder_name}', target_path], timeout=10800)
                
                if result.returncode != 0:
                    raise Exception(f"Download gagal setelah retry: {result.stderr}")
        
        except subprocess.TimeoutExpired:
            await send_progress_message(chat_id, "⏰ Download timeout, tapi mungkin masih berjalan. Checking hasil...", context)
            # Continue processing even if timeout
        
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
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_rename)
        return result
        
    except Exception as e:
        logger.error(f"Auto rename error: {e}")
        return {'photos': 0, 'videos': 0}

# ========== UPLOAD FUNCTIONS ==========

async def upload_to_terabox(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload ke Terabox menggunakan CLI"""
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
                                      cwd=os.path.dirname(TERABOX_CLI), timeout=300)
                
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
                    
            except subprocess.TimeoutExpired:
                failed_count += 1
                logger.error(f"Terabox upload timeout for {file_path}")
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
        server_resp = requests.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_API_KEY}", timeout=30)
        server_data = server_resp.json()
        
        if server_data.get('status') != 200:
            raise Exception(f"Gagal dapat server: {server_data.get('msg', 'Unknown error')}")
        
        # Upload file
        with open(file_path, 'rb') as f:
            files = {'file': f}
            upload_resp = requests.post(
                server_data['result'],
                files=files,
                data={'api_key': DOODSTREAM_API_KEY},
                timeout=300
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

# ========== IMPROVED MEGA LOGIN/LOGOUT HANDLERS ==========

async def loginmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Improved handler untuk /loginmega - Login ke Mega.nz dengan better feedback"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: /loginmega <email> <password>\n"
            "Contoh: /loginmega myemail@example.com mypassword123\n\n"
            "Atau untuk login manual di VPS:\n"
            "`mega-login email_password_anda`"
        )
        return
    
    email = context.args[0]
    password = ' '.join(context.args[1:])
    
    progress_msg = await update.message.reply_text("🔐 Mencoba login ke Mega.nz...")
    
    # Save credentials first
    if save_mega_credentials(email, password):
        await progress_msg.edit_text("🔐 Credential tersimpan, mencoba login...")
        
        # Try to login dengan berbagai metode
        login_success, login_result = login_to_mega(email, password)
        
        if login_success:
            await progress_msg.edit_text(
                f"✅ **Login berhasil!**\n\n"
                f"Email: `{email}`\n"
                f"Status: {login_result}\n\n"
                f"Sekarang Anda bisa menggunakan:\n"
                f"• `/listmega` - Lihat folder\n"
                f"• `/download folder_name` - Download folder\n\n"
                f"Credential telah disimpan untuk auto-login selanjutnya."
            )
        else:
            await progress_msg.edit_text(
                f"⚠️ **Login otomatis gagal**\n\n"
                f"Error: {login_result}\n\n"
                f"**Tapi credential sudah disimpan!**\n\n"
                f"Silakan lakukan salah satu:\n"
                f"1. Login manual di VPS dengan:\n"
                f"`mega-login {email} your_password`\n\n"
                f"2. Atau coba perintah:\n"
                f"`/listmega` - Bot akan coba auto-login\n\n"
                f"3. Setelah login manual, bot akan bisa akses Mega.nz."
            )
    else:
        await progress_msg.edit_text("❌ Gagal menyimpan credential Mega.nz")

async def logoutmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /logoutmega - Logout dari Mega.nz"""
    try:
        progress_msg = await update.message.reply_text("🔐 Mencoba logout dari Mega.nz...")
        
        # Try to logout from mega
        try:
            run_mega_command(['logout'], timeout=10)
            await progress_msg.edit_text("✅ Logout command berhasil")
        except Exception as e:
            await progress_msg.edit_text(f"⚠️ Logout command gagal: {e}")
        
        # Delete saved credentials
        delete_mega_credentials()
        
        await progress_msg.edit_text(
            "✅ **Logout berhasil!**\n\n"
            "Credential Mega.nz telah dihapus.\n"
            "Gunakan `/loginmega email password` untuk login kembali."
        )
        logger.info("Mega.nz logout completed")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error saat logout: {str(e)}")
        logger.error(f"Logout error: {e}")

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
    
    # Check mega status
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available:
        if is_logged_in:
            mega_status = f"✅ {login_msg}"
        else:
            email, _ = load_mega_credentials()
            if email:
                mega_status = f"⚠️ Credential tersimpan untuk {email}, tapi belum login"
            else:
                mega_status = "❌ Belum login"
    else:
        mega_status = "❌ Mega-cmd tidak terinstall"
    
    settings_text = f"""
⚙️ **Settings Anda**

📛 Prefix: `{settings.get('prefix', 'file_')}`
📤 Platform: **{settings.get('platform', 'terabox').upper()}**
🔄 Auto-upload: **{'AKTIF ✅' if settings.get('auto_upload', False) else 'NONAKTIF ❌'}**
🧹 Auto-cleanup: **{'AKTIF ✅' if settings.get('auto_cleanup', True) else 'NONAKTIF ❌'}**

**Status Mega.nz:** {mega_status}

**Perintah Settings:**
/setprefix <prefix> - Atur prefix rename
/setplatform <terabox|doodstream> - Atur platform upload
/autoupload - Aktif/nonaktif auto-upload
/autocleanup - Aktif/nonaktif auto-cleanup

**Perintah Mega.nz:**
/loginmega <email> <password> - Login ke Mega.nz
/logoutmega - Logout dari Mega.nz
/listmega - List folder di Mega.nz
    """
    await update.message.reply_text(settings_text, parse_mode='Markdown')

# ========== TELEGRAM BOT HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /start"""
    # Check mega-cmd status
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available:
        if is_logged_in:
            mega_status = f"✅ Terinstall & Login sebagai: {login_msg}"
        else:
            email, _ = load_mega_credentials()
            if email:
                mega_status = f"⚠️ Terinstall & Credential tersimpan ({email})"
            else:
                mega_status = "⚠️ Terinstall tapi belum login"
    else:
        mega_status = "❌ Mega-cmd tidak terinstall"
    
    welcome_text = f"""
🤖 **Bot Download & Upload Manager**

**Status Mega.nz:** {mega_status}

**Fitur:**
📥 Download folder dari Mega.nz
🔄 Auto-rename file media
📤 Upload ke Terabox & Doodstream
⚙️ Custom prefix & auto-upload
🧹 Auto-cleanup setelah upload

**Perintah Mega.nz:**
/loginmega <email> <password> - Login ke Mega.nz
/logoutmega - Logout dari Mega.nz
/listmega - List folder di Mega.nz
/download <folder> - Download folder

**Perintah Lain:**
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Improved callback handler untuk semua button interactions"""
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
    
    elif data == "refresh_mega_list":
        await query.edit_message_text("🔄 Memuat ulang daftar folder...")
        await list_mega_folders(update, context)
    
    elif data == "retry_mega_login":
        await query.edit_message_text("🔄 Mencoba login ulang ke Mega.nz...")
        if ensure_mega_session():
            await query.edit_message_text("✅ Login berhasil! Sekarang coba /listmega lagi")
        else:
            await query.edit_message_text("❌ Login masih gagal. Coba login manual di VPS.")
    
    elif data == "cancel_operation":
        await query.edit_message_text("❌ Operasi dibatalkan")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /status"""
    download_count = download_queue.qsize()
    upload_count = upload_queue.qsize()
    active_count = len(active_jobs)
    
    # Check mega-cmd status
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available and is_logged_in:
        mega_status = f"✅ {login_msg}"
    elif mega_available:
        email, _ = load_mega_credentials()
        if email:
            mega_status = f"⚠️ Credential tersimpan ({email}), tapi belum login"
        else:
            mega_status = "⚠️ Mega-cmd terinstall tapi belum login"
    else:
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

# ========== MAIN FUNCTION ==========

def main():
    """Main function - IMPROVED MEGA LOGIN VERSION"""
    # Setup environment pertama kali
    setup_environment()
    
    # Pastikan direktori ada
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    # Check mega-cmd pada startup dengan improved method
    logger.info("Checking Mega.nz setup with improved login system...")
    if check_mega_cmd():
        logger.info("Mega-cmd detected successfully")
        
        # Check login status dengan retry mechanism
        is_logged_in, login_msg = check_mega_login()
        if is_logged_in:
            logger.info(f"Mega.nz login status: {login_msg}")
        else:
            email, _ = load_mega_credentials()
            if email:
                logger.info(f"Mega credentials found for: {email}, attempting auto-login...")
                if ensure_mega_session():
                    logger.info("Auto-login successful on startup")
                else:
                    logger.warning("Auto-login failed on startup, manual login required")
            else:
                logger.info("Mega.nz not logged in and no credentials saved")
    else:
        logger.warning("Mega-cmd not found. Mega.nz functionality will be disabled.")
    
    # Jalankan cleanup untuk folder lama
    try:
        cleanup_old_downloads()
    except Exception as e:
        logger.error(f"Error during startup cleanup: {e}")
    
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
    
    # Mega login/logout handlers
    application.add_handler(CommandHandler("loginmega", loginmega))
    application.add_handler(CommandHandler("logoutmega", logoutmega))
    
    # Settings handlers
    application.add_handler(CommandHandler("setprefix", setprefix))
    application.add_handler(CommandHandler("setplatform", setplatform))
    application.add_handler(CommandHandler("autoupload", autoupload))
    application.add_handler(CommandHandler("autocleanup", autocleanup))
    application.add_handler(CommandHandler("mysettings", mysettings))
    
    # Callback handlers - improved dengan pattern yang lebih comprehensive
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(page_|select_|target_|megadl_|refresh_mega_list|retry_mega_login|cancel_operation)"))
    application.add_handler(CallbackQueryHandler(cleanup_callback, pattern="^(cleanup_confirm|cleanup_cancel)$"))
    
    # Jalankan bot
    logger.info("🤖 Bot started successfully with Improved Mega Login System")
    print("🤖 Bot is running... Press Ctrl+C to stop")
    application.run_polling()

if __name__ == "__main__":
    main()
