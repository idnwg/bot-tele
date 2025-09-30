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
from telegram.constants import ParseMode
from pathlib import Path
from queue import Queue
from dotenv import load_dotenv
import html


load_dotenv(os.path.join('/root/bot-tele', '.env'))

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
FOLDER_INDEX_FILE = os.path.join(BASE_DIR, "folder_index.json")

download_queue = Queue()
upload_queue = Queue()
active_jobs = {}

folder_cache = {}

PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg'}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def escape_markdown(text):
    """Escape special characters for MarkdownV2"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def safe_html(text):
    """Escape HTML special characters"""
    return html.escape(str(text))

async def safe_send_message(context, chat_id, text, reply_markup=None, parse_mode=ParseMode.HTML):
    """Safely send message with error handling"""
    try:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=None
            )
        except Exception as e2:
            logger.error(f"Error sending plain message: {e2}")
            return None

async def safe_edit_message(context, chat_id, message_id, text, reply_markup=None, parse_mode=ParseMode.HTML):
    """Safely edit message with error handling"""
    try:
        return await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        try:
            return await safe_send_message(context, chat_id, text, reply_markup, parse_mode)
        except Exception as e2:
            logger.error(f"Error sending fallback message: {e2}")
            return None


def save_folder_index(user_id, folders):
    """Save folder list with index for callback data"""
    try:
        if os.path.exists(FOLDER_INDEX_FILE):
            with open(FOLDER_INDEX_FILE, 'r') as f:
                index_data = json.load(f)
        else:
            index_data = {}
        
        index_data[str(user_id)] = {
            'folders': folders,
            'timestamp': time.time()
        }
        
        with open(FOLDER_INDEX_FILE, 'w') as f:
            json.dump(index_data, f, indent=2)
        
        return True
    except Exception as e:
        logger.error(f"Error saving folder index: {e}")
        return False

def get_folder_by_index(user_id, index):
    """Get folder name by index"""
    try:
        if os.path.exists(FOLDER_INDEX_FILE):
            with open(FOLDER_INDEX_FILE, 'r') as f:
                index_data = json.load(f)
            
            user_data = index_data.get(str(user_id))
            if user_data and 'folders' in user_data:
                folders = user_data['folders']
                if 0 <= index < len(folders):
                    return folders[index]
        return None
    except Exception as e:
        logger.error(f"Error getting folder by index: {e}")
        return None


def setup_environment():
    """Setup environment untuk snap installation dengan path yang lengkap"""
    try:
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
            os.environ['PATH'] = f"{':'.join(new_paths)}:{current_path}"
        
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

def find_mega_executable():
    """Find the correct mega-cmd executable path"""
    possible_paths = [
        'mega-cmd',
        '/snap/bin/mega-cmd',
        '/var/lib/snapd/snap/bin/mega-cmd',
        '/usr/local/bin/mega-cmd',
        '/usr/bin/mega-cmd'
    ]
    
    for path in possible_paths:
        try:
            result = subprocess.run(
                [path, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 or 'mega' in result.stdout.lower() or 'mega' in result.stderr.lower():
                logger.info(f"Found mega-cmd at: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            continue
    
    try:
        result = subprocess.run(['which', 'mega-cmd'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            logger.info(f"Found mega-cmd via which: {path}")
            return path
    except:
        pass
    
    logger.warning("mega-cmd executable not found, using 'mega-cmd' as default")
    return 'mega-cmd'

MEGA_CMD_PATH = None

def check_mega_cmd():
    """Check jika mega-cmd terinstall"""
    global MEGA_CMD_PATH
    try:
        setup_environment()
        MEGA_CMD_PATH = find_mega_executable()
        
        try:
            result = subprocess.run(
                [MEGA_CMD_PATH, '--version'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0 or 'mega' in result.stdout.lower() or 'mega' in result.stderr.lower():
                logger.info(f"Mega-cmd is available at: {MEGA_CMD_PATH}")
                return True
        except Exception as e:
            logger.debug(f"Version check failed: {e}")
        
        try:
            result = subprocess.run(
                [MEGA_CMD_PATH, '--help'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0 or 'usage' in result.stdout.lower():
                logger.info(f"Mega-cmd is available (via help): {MEGA_CMD_PATH}")
                return True
        except Exception as e:
            logger.debug(f"Help check failed: {e}")
            
        logger.error("Mega-cmd not responding properly")
        return False
        
    except Exception as e:
        logger.error(f"Error checking mega-cmd: {e}")
        return False

def run_mega_command(args, timeout=60, input_text=None):
    """Jalankan command mega-cmd dengan struktur yang benar"""
    global MEGA_CMD_PATH
    
    try:
        if MEGA_CMD_PATH is None:
            MEGA_CMD_PATH = find_mega_executable()
        
        if isinstance(args, str):
            args = args.split()
        
        full_command = [MEGA_CMD_PATH] + args
        
        logger.info(f"Running mega command: {' '.join(full_command)}")
        
        if input_text:
            result = subprocess.run(
                full_command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=os.environ
            )
        else:
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=os.environ
            )
        
        logger.debug(f"Command output: {result.stdout}")
        if result.stderr:
            logger.debug(f"Command stderr: {result.stderr}")
        
        return result
        
    except subprocess.TimeoutExpired:
        logger.error(f"Command timeout after {timeout}s: {' '.join(full_command)}")
        raise
    except Exception as e:
        logger.error(f"Error running mega command: {e}")
        raise

def check_mega_login():
    """Check status login Mega.nz"""
    try:
        logger.info("Checking Mega.nz login status...")
        result = run_mega_command(['whoami'], timeout=30)
        
        output = result.stdout.strip()
        error_output = result.stderr.strip().lower() if result.stderr else ""
        
        if result.returncode == 0 and output:
            if '@' in output and 'not logged in' not in output.lower():
                logger.info(f"Logged in as: {output}")
                return True, f"Logged in as: {output}"
            elif output and 'not logged in' not in output.lower() and 'not' not in output.lower():
                return True, f"Session active: {output}"
        
        if 'not logged in' in output.lower() or 'not logged in' in error_output:
            return False, "Not logged in"
        
        try:
            result = run_mega_command(['ls'], timeout=30)
            if result.returncode == 0 and 'not logged in' not in result.stdout.lower():
                return True, "Logged in (verified via ls)"
        except:
            pass
        
        return False, "Not logged in"
            
    except Exception as e:
        logger.error(f"Error checking login: {e}")
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
        
        os.environ['MEGA_EMAIL'] = email
        os.environ['MEGA_PASSWORD'] = password
        
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
                email = credentials.get('email')
                password = credentials.get('password')
                
                if email:
                    os.environ['MEGA_EMAIL'] = email
                if password:
                    os.environ['MEGA_PASSWORD'] = password
                
                return email, password
        return None, None
    except Exception as e:
        logger.error(f"Error loading mega credentials: {e}")
        return None, None

def delete_mega_credentials():
    """Hapus credential Mega"""
    try:
        if os.path.exists(MEGA_CREDENTIALS_FILE):
            os.remove(MEGA_CREDENTIALS_FILE)
        
        os.environ.pop('MEGA_EMAIL', None)
        os.environ.pop('MEGA_PASSWORD', None)
        
        logger.info("Mega credentials deleted")
        return True
    except Exception as e:
        logger.error(f"Error deleting mega credentials: {e}")
        return False

def login_to_mega(email, password):
    """Login ke Mega.nz dengan command yang benar"""
    try:
        logger.info(f"Attempting login to Mega.nz for: {email}")
        
        try:
            logger.info("Trying direct login...")
            result = run_mega_command(['login', email, password], timeout=45)
            
            output_combined = (result.stdout + result.stderr).lower()
            
            if (result.returncode == 0 or 
                'logged in' in output_combined or 
                'already logged in' in output_combined):
                
                time.sleep(3)
                is_logged_in, msg = check_mega_login()
                
                if is_logged_in:
                    logger.info(f"Login successful for: {email}")
                    return True, "Login successful"
                elif 'already logged in' in output_combined:
                    logger.info("Already logged in")
                    return True, "Already logged in"
            
            if 'invalid' in output_combined or 'incorrect' in output_combined:
                return False, "Invalid email or password"
            
            logger.warning(f"Login attempt result unclear: {result.stdout}")
            
        except subprocess.TimeoutExpired:
            logger.warning("Login command timeout")
        except Exception as e:
            logger.warning(f"Direct login error: {e}")
        
        try:
            logger.info("Trying logout then login...")
            run_mega_command(['logout'], timeout=10)
            time.sleep(2)
            
            result = run_mega_command(['login', email, password], timeout=45)
            
            if result.returncode == 0 or 'logged in' in (result.stdout + result.stderr).lower():
                time.sleep(3)
                is_logged_in, msg = check_mega_login()
                if is_logged_in:
                    logger.info("Login successful after logout")
                    return True, "Login successful"
        
        except Exception as e:
            logger.warning(f"Logout-login method error: {e}")
        
        try:
            logger.info("Trying session cleanup and login...")
            
            session_paths = [
                os.path.expanduser('~/.megaCmd'),
                os.path.expanduser('~/.megacmd'),
                '/root/.megaCmd',
                '/root/.megacmd'
            ]
            
            for session_path in session_paths:
                if os.path.exists(session_path):
                    try:
                        shutil.rmtree(session_path)
                        logger.info(f"Cleared session: {session_path}")
                    except:
                        pass
            
            time.sleep(2)
            result = run_mega_command(['login', email, password], timeout=45)
            
            if result.returncode == 0:
                time.sleep(3)
                is_logged_in, msg = check_mega_login()
                if is_logged_in:
                    logger.info("Login successful after session cleanup")
                    return True, "Login successful"
        
        except Exception as e:
            logger.warning(f"Session cleanup method error: {e}")
        
        return False, "All login methods failed. Please check credentials"
        
    except Exception as e:
        logger.error(f"Login process error: {e}")
        return False, f"Login error: {str(e)}"

def ensure_mega_session():
    """Pastikan session Mega.nz valid"""
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Checking mega session (attempt {attempt + 1}/{max_retries})...")
            
            is_logged_in, login_msg = check_mega_login()
            if is_logged_in:
                logger.info(f"Mega session valid: {login_msg}")
                return True
            
            email, password = load_mega_credentials()
            if email and password:
                logger.info(f"Attempting auto-login for: {email}")
                login_success, login_result = login_to_mega(email, password)
                
                if login_success:
                    logger.info("Auto-login successful")
                    return True
                else:
                    logger.warning(f"Auto-login failed: {login_result}")
            else:
                logger.warning("No saved credentials found")
                break
            
            if attempt < max_retries - 1:
                time.sleep(5)
                
        except Exception as e:
            logger.error(f"Error ensuring session (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    
    logger.error("Failed to establish mega session")
    return False


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
            await send_progress_message(chat_id, f"🧹 Folder berhasil dihapus: {safe_html(folder_name)}", context)
            logger.info(f"Cleanup completed: {folder_path}")
            return True
        else:
            logger.warning(f"Folder tidak ditemukan untuk cleanup: {folder_path}")
            return False
            
    except Exception as e:
        error_msg = f"❌ Gagal menghapus folder: {safe_html(str(e))}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Cleanup error: {e}")
        return False

def cleanup_old_downloads():
    """Bersihkan folder download yang sudah lama"""
    try:
        current_time = time.time()
        cleanup_threshold = 24 * 3600
        
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
                await safe_edit_message(
                    context,
                    chat_id=chat_id,
                    message_id=context.user_data['progress_msg_id'],
                    text=message
                )
            except:
                msg = await safe_send_message(context, chat_id=chat_id, text=message)
                if msg:
                    context.user_data['progress_msg_id'] = msg.message_id
        else:
            msg = await safe_send_message(context, chat_id=chat_id, text=message)
            if msg:
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

def parse_mega_ls_output(output_text):
    """
    Parse mega-cmd ls output dengan filter yang sangat ketat
    untuk menghindari ASCII art, banner, dan non-folder lines
    """
    if not output_text:
        return []
    
    lines = output_text.strip().split('\n')
    folders = []
    
    # Pattern untuk mendeteksi ASCII art dan banner
    banner_patterns = [
        r'^[=\-_*#]{3,}',  # Repeated special chars (===, ---, ___, ***, ###)
        r'^[\s]*[=\-_*#]+[\s]*$',  # Lines with only special chars and spaces
        r'https?://',  # URLs
        r'mega\.nz',  # Mega.nz references
        r'mega\.co\.nz',  # Mega.co.nz references
        r'MEGA\s+CMD',  # MEGA CMD text
        r'MEGA\s+Limited',  # Company name
        r'Welcome\s+to',  # Welcome messages
        r'^\s*\d+\s*(GB|MB|KB|TB)',  # Storage info (e.g., "50 GB")
        r'Used:',  # Storage usage
        r'Total:',  # Total storage
        r'Available:',  # Available storage
        r'^\s*\[',  # Lines starting with brackets
        r'@.*\.com',  # Email addresses in banner
        r'^\s*$',  # Empty lines
        r'^[\W_]+$',  # Lines with only non-alphanumeric chars
    ]
    
    compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in banner_patterns]
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines
        if not line or len(line) < 2:
            continue
        
        # Check against all banner patterns
        is_banner = False
        for pattern in compiled_patterns:
            if pattern.search(line):
                is_banner = True
                logger.debug(f"Filtered out banner line: {line}")
                break
        
        if is_banner:
            continue
        
        # Additional checks for valid folder names
        # Count alphanumeric vs special characters ratio
        alphanumeric_count = sum(c.isalnum() or c.isspace() for c in line)
        special_count = sum(not c.isalnum() and not c.isspace() for c in line)
        
        # If more than 40% special characters, likely not a folder name
        if len(line) > 0 and (special_count / len(line)) > 0.4:
            logger.debug(f"Filtered out (too many special chars): {line}")
            continue
        
        # Check for repeated characters (like ====== or ------)
        if re.search(r'(.)\1{4,}', line):  # Same char repeated 5+ times
            logger.debug(f"Filtered out (repeated chars): {line}")
            continue
        
        # Valid folder name should have at least some alphanumeric characters
        if alphanumeric_count < 2:
            logger.debug(f"Filtered out (not enough alphanumeric): {line}")
            continue
        
        # If we got here, it's likely a valid folder name
        if line not in folders:
            folders.append(line)
            logger.info(f"Valid folder found: {line}")
    
    return folders

async def list_mega_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List semua folder di akun Mega menggunakan mega-cmd dengan parsing yang robust"""
    try:
        chat_id = update.effective_chat.id
        
        if not check_mega_cmd():
            await safe_send_message(
                context,
                chat_id,
                "❌ <b>Mega-cmd tidak ditemukan!</b>\n\n"
                "Untuk instalasi:\n"
                "1. <code>sudo snap install mega-cmd</code>\n"
                "2. Atau: <code>sudo apt install megacmd</code>\n"
                "3. Restart bot setelah install"
            )
            return
        
        await send_progress_message(chat_id, "🔐 Memeriksa session Mega.nz...", context)
        
        if not ensure_mega_session():
            keyboard = [
                [InlineKeyboardButton("🔄 Coba Login Ulang", callback_data="retry_mega_login")],
                [InlineKeyboardButton("❌ Batalkan", callback_data="cancel_operation")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await safe_send_message(
                context,
                chat_id=chat_id,
                text="❌ <b>Tidak dapat terhubung ke Mega.nz!</b>\n\n"
                     "Silakan login dengan:\n"
                     "<code>/loginmega email password</code>",
                reply_markup=reply_markup
            )
            return
        
        await send_progress_message(chat_id, "📡 Mengambil daftar folder dari Mega.nz...", context)
        
        # Try with -l flag first for structured output
        try:
            result = run_mega_command(['ls', '-l', '/'], timeout=60)
            logger.info(f"mega-cmd ls -l output:\n{result.stdout}")
        except Exception as e:
            logger.warning(f"ls -l failed, trying without -l: {e}")
            result = run_mega_command(['ls', '/'], timeout=60)
            logger.info(f"mega-cmd ls output:\n{result.stdout}")
        
        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else result.stdout
            await send_progress_message(chat_id, f"❌ Gagal mengambil daftar folder: {safe_html(error_msg[:100])}", context)
            return
        
        # Use the new robust parsing function
        folders = parse_mega_ls_output(result.stdout)
        
        if not folders:
            await send_progress_message(
                chat_id, 
                "📭 Tidak ada folder di akun Mega.nz\n\n"
                "Jika Anda yakin ada folder, coba:\n"
                "1. <code>/logoutmega</code>\n"
                "2. <code>/loginmega email password</code>\n"
                "3. <code>/listmega</code>", 
                context
            )
            return
        
        # Save folder index for callback
        save_folder_index(chat_id, folders)
        
        # Create keyboard with folder buttons
        keyboard = []
        for idx, folder in enumerate(sorted(folders)[:30]):
            if folder and len(folder) > 0:
                display_name = folder[:25] + "..." if len(folder) > 25 else folder
                keyboard.append([InlineKeyboardButton(f"📁 {display_name}", callback_data=f"megadl_{idx}")])
        
        if not keyboard:
            await send_progress_message(chat_id, "❌ Tidak ada folder valid yang ditemukan", context)
            return
            
        keyboard.append([InlineKeyboardButton("🔄 Refresh List", callback_data="refresh_mega_list")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await safe_send_message(
            context,
            chat_id=chat_id,
            text=f"✅ <b>Ditemukan: {len(folders)} folder</b>\n"
                 f"📂 <b>Menampilkan: {min(len(folders), 30)} folder</b>\n\n"
                 f"Pilih folder dari <b>Mega.nz</b> untuk didownload:",
            reply_markup=reply_markup
        )
        
        logger.info(f"Successfully listed {len(folders)} folders for user {chat_id}")
        
    except subprocess.TimeoutExpired:
        await send_progress_message(chat_id, "❌ Timeout: Gagal mengambil daftar folder", context)
    except Exception as e:
        error_msg = f"❌ Error mengambil daftar folder: {safe_html(str(e))}"
        await send_progress_message(chat_id, error_msg, context)
        logger.error(f"List mega folders error: {e}")


async def download_mega_folder(folder_name, job_id, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download folder dari Mega.nz menggunakan mega-cmd"""
    try:
        chat_id = update.effective_chat.id
        user_settings = get_user_settings(chat_id)
        
        await send_progress_message(chat_id, "🔐 Memverifikasi session Mega.nz...", context)
        if not ensure_mega_session():
            await send_progress_message(chat_id, "❌ Session Mega.nz tidak valid. Silakan login ulang.", context)
            return
        
        await send_progress_message(chat_id, f"📥 Memulai download folder: {safe_html(folder_name)}", context)
        
        target_path = os.path.join(DOWNLOADS_DIR, folder_name)
        os.makedirs(target_path, exist_ok=True)
        
        await send_progress_message(chat_id, f"⏳ Download dalam proses... Ini mungkin butuh waktu lama.", context)
        
        try:
            result = run_mega_command(['get', f'/{folder_name}', target_path], timeout=10800)
            
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else result.stdout
                logger.error(f"Download failed: {error_msg}")
                
                await send_progress_message(chat_id, "🔄 Mencoba login ulang dan download lagi...", context)
                if ensure_mega_session():
                    result = run_mega_command(['get', f'/{folder_name}', target_path], timeout=10800)
                    
                    if result.returncode != 0:
                        raise Exception(f"Download gagal: {result.stderr}")
        
        except subprocess.TimeoutExpired:
            await send_progress_message(chat_id, "⏰ Download timeout, checking hasil...", context)
        
        await send_progress_message(chat_id, f"✅ Download selesai: {safe_html(folder_name)}", context)
        
        await send_progress_message(chat_id, f"🔄 Memulai rename file media...", context)
        rename_results = await auto_rename_media_files(target_path, user_settings['prefix'], update, context)
        
        await send_progress_message(chat_id, 
            f"✅ Rename selesai!\n"
            f"📸 Foto: {rename_results['photos']} files\n"
            f"🎬 Video: {rename_results['videos']} files", 
            context
        )
        
        if user_settings.get('auto_upload', False):
            platform = user_settings.get('platform', 'terabox')
            await send_progress_message(chat_id, f"📤 Auto-upload ke {platform}...", context)
            await process_auto_upload(target_path, platform, update, context)
        
        logger.info(f"Mega folder download completed: {folder_name}")
        
    except Exception as e:
        error_msg = f"❌ Gagal download folder: {safe_html(str(e))}"
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
                photo_files = [f for f in files 
                             if os.path.splitext(f)[1].lower() in PHOTO_EXTENSIONS]
                for i, filename in enumerate(sorted(photo_files), 1):
                    old_path = os.path.join(root, filename)
                    ext = os.path.splitext(filename)[1]
                    new_name = f"{prefix}pic_{i:04d}{ext}"
                    new_path = os.path.join(root, new_name)
                    
                    if safe_rename(old_path, new_path):
                        photo_count += 1
                
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


async def upload_to_terabox(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload ke Terabox menggunakan CLI"""
    try:
        chat_id = update.effective_chat.id
        folder_name = os.path.basename(folder_path)
        
        await send_progress_message(chat_id, f"📤 Upload ke Terabox: {safe_html(folder_name)}", context)
        
        remote_folder = f"/MegaUploads/{folder_name}"
        if not update_terabox_settings(folder_path, remote_folder):
            await send_progress_message(chat_id, "❌ Gagal update settings Terabox", context)
            return
        
        media_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_ext = os.path.splitext(file)[1].lower()
                if file_ext in PHOTO_EXTENSIONS or file_ext in VIDEO_EXTENSIONS:
                    media_files.append(os.path.join(root, file))
        
        if not media_files:
            await send_progress_message(chat_id, "❌ Tidak ada file media yang ditemukan", context)
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
                    f"🔄 Upload: {safe_html(os.path.basename(file_path))}", 
                    context
                )
                
                cmd = ['python3', TERABOX_CLI, 'upload', file_path]
                if TERABOX_CONNECT_KEY:
                    cmd.extend(['--connect-key', TERABOX_CONNECT_KEY])
                
                result = subprocess.run(cmd, capture_output=True, text=True, 
                                      cwd=os.path.dirname(TERABOX_CLI), timeout=300)
                
                if result.returncode == 0:
                    success_count += 1
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
        
        message = f"✅ Upload Terabox selesai!\n"
        message += f"📁 Folder: {safe_html(folder_name)}\n"
        message += f"📊 Hasil: {success_count} sukses, {failed_count} gagal\n"
        
        if uploaded_links:
            message += f"\n📎 Contoh links ({min(3, len(uploaded_links))} dari {len(uploaded_links)}):\n"
            for link in uploaded_links[:3]:
                message += f"🔗 {safe_html(link)}\n"
        
        await send_progress_message(chat_id, message, context)
        logger.info(f"Terabox upload completed: {success_count}/{total_files} files")
        
        user_settings = get_user_settings(chat_id)
        if success_count > 0 and (user_settings.get('auto_cleanup', True) or AUTO_CLEANUP):
            await send_progress_message(chat_id, "🧹 Membersihkan folder lokal...", context)
            await cleanup_after_upload(folder_path, update, context)
            
    except Exception as e:
        error_msg = f"❌ Error upload Terabox: {safe_html(str(e))}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Terabox upload error: {e}")

async def upload_to_doodstream(folder_path, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload ke Doodstream via API"""
    try:
        chat_id = update.effective_chat.id
        folder_name = os.path.basename(folder_path)
        
        await send_progress_message(chat_id, f"📤 Upload ke Doodstream: {safe_html(folder_name)}", context)
        
        video_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS:
                    video_files.append(os.path.join(root, file))
        
        if not video_files:
            await send_progress_message(chat_id, "❌ Tidak ada file video untuk Doodstream", context)
            return
        
        total_files = len(video_files)
        await send_progress_message(chat_id, f"📤 Mengupload {total_files} video ke Doodstream...", context)
        
        uploaded_links = []
        success_count = 0
        
        for i, file_path in enumerate(video_files, 1):
            try:
                await send_progress_message(chat_id, 
                    f"📤 Progress: {i}/{total_files}\n"
                    f"🔄 Upload: {safe_html(os.path.basename(file_path))}", 
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
            message += f"📁 Folder: {safe_html(folder_name)}\n"
            message += f"📹 Video: {success_count} berhasil\n"
            message += f"\n📎 Links ({min(3, len(uploaded_links))} dari {len(uploaded_links)}):\n"
            for link in uploaded_links[:3]:
                message += f"🔗 {safe_html(link)}\n"
            
            await send_progress_message(chat_id, message, context)
        else:
            await send_progress_message(chat_id, "❌ Tidak ada video yang berhasil diupload", context)
        
        user_settings = get_user_settings(chat_id)
        if success_count > 0 and (user_settings.get('auto_cleanup', True) or AUTO_CLEANUP):
            await send_progress_message(chat_id, "🧹 Membersihkan folder lokal...", context)
            await cleanup_after_upload(folder_path, update, context)
            
    except Exception as e:
        error_msg = f"❌ Error upload Doodstream: {safe_html(str(e))}"
        await send_progress_message(update.effective_chat.id, error_msg, context)
        logger.error(f"Doodstream upload error: {e}")

async def upload_single_file_to_doodstream(file_path):
    """Upload single file ke Doodstream"""
    try:
        if not DOODSTREAM_API_KEY:
            raise Exception("Doodstream API key tidak ditemukan")
        
        server_resp = requests.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_API_KEY}", timeout=30)
        server_data = server_resp.json()
        
        if server_data.get('status') != 200:
            raise Exception(f"Gagal dapat server: {server_data.get('msg', 'Unknown error')}")
        
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
            return upload_data['result']['download_url']
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


async def loginmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /loginmega"""
    if not context.args or len(context.args) < 2:
        await safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Format: <code>/loginmega &lt;email&gt; &lt;password&gt;</code>\n"
            "Contoh: <code>/loginmega myemail@example.com mypassword123</code>"
        )
        return
    
    email = context.args[0]
    password = ' '.join(context.args[1:])
    
    progress_msg = await safe_send_message(context, update.effective_chat.id, "🔐 Mencoba login ke Mega.nz...")
    
    if save_mega_credentials(email, password):
        if progress_msg:
            await safe_edit_message(context, update.effective_chat.id, progress_msg.message_id, "🔐 Credential tersimpan, mencoba login...")
        
        login_success, login_result = login_to_mega(email, password)
        
        if login_success:
            msg = (
                f"✅ <b>Login berhasil!</b>\n\n"
                f"Email: <code>{safe_html(email)}</code>\n"
                f"Status: {safe_html(login_result)}\n\n"
                f"Sekarang Anda bisa menggunakan:\n"
                f"• <code>/listmega</code> - Lihat folder\n"
                f"• <code>/download folder_name</code> - Download folder"
            )
        else:
            msg = (
                f"⚠️ <b>Login gagal</b>\n\n"
                f"Error: {safe_html(login_result)}\n\n"
                f"Credential sudah disimpan. Coba:\n"
                f"1. Periksa email dan password\n"
                f"2. Login manual di VPS:\n"
                f"<code>mega-cmd login {safe_html(email)} your_password</code>\n"
                f"3. Kemudian coba <code>/listmega</code>"
            )
        
        if progress_msg:
            await safe_edit_message(context, update.effective_chat.id, progress_msg.message_id, msg)
    else:
        if progress_msg:
            await safe_edit_message(context, update.effective_chat.id, progress_msg.message_id, "❌ Gagal menyimpan credential")

async def logoutmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /logoutmega"""
    try:
        progress_msg = await safe_send_message(context, update.effective_chat.id, "🔐 Mencoba logout dari Mega.nz...")
        
        try:
            run_mega_command(['logout'], timeout=10)
            if progress_msg:
                await safe_edit_message(context, update.effective_chat.id, progress_msg.message_id, "✅ Logout command berhasil")
        except Exception as e:
            if progress_msg:
                await safe_edit_message(context, update.effective_chat.id, progress_msg.message_id, f"⚠️ Logout command gagal: {safe_html(str(e))}")
        
        delete_mega_credentials()
        
        if progress_msg:
            await safe_edit_message(
                context,
                update.effective_chat.id,
                progress_msg.message_id,
                "✅ <b>Logout berhasil!</b>\n\n"
                "Credential Mega.nz telah dihapus.\n"
                "Gunakan <code>/loginmega email password</code> untuk login kembali."
            )
        logger.info("Mega.nz logout completed")
        
    except Exception as e:
        await safe_send_message(context, update.effective_chat.id, f"❌ Error saat logout: {safe_html(str(e))}")
        logger.error(f"Logout error: {e}")


async def setprefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /setprefix"""
    if not context.args:
        await safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Format: <code>/setprefix &lt;prefix&gt;</code>\n"
            "Contoh: <code>/setprefix vacation_</code>"
        )
        return
    
    prefix = ' '.join(context.args)
    user_id = update.effective_user.id
    
    if len(prefix) > 20:
        await safe_send_message(context, update.effective_chat.id, "❌ Prefix terlalu panjang (max 20 karakter)")
        return
    
    update_user_settings(user_id, {'prefix': prefix})
    await safe_send_message(context, update.effective_chat.id, f"✅ Prefix berhasil diatur ke: <code>{safe_html(prefix)}</code>")
    logger.info(f"User {user_id} set prefix to: {prefix}")

async def setplatform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /setplatform"""
    if not context.args or context.args[0].lower() not in ['terabox', 'doodstream']:
        await safe_send_message(
            context,
            update.effective_chat.id,
            "❌ Format: <code>/setplatform &lt;terabox|doodstream&gt;</code>\n"
            "Contoh: <code>/setplatform terabox</code>"
        )
        return
    
    platform = context.args[0].lower()
    user_id = update.effective_user.id
    
    update_user_settings(user_id, {'platform': platform})
    await safe_send_message(context, update.effective_chat.id, f"✅ Platform upload berhasil diatur ke: <b>{platform}</b>")
    logger.info(f"User {user_id} set platform to: {platform}")

async def autoupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /autoupload"""
    user_id = update.effective_user.id
    current_settings = get_user_settings(user_id)
    new_auto_upload = not current_settings.get('auto_upload', False)
    
    update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "AKTIF ✅" if new_auto_upload else "NONAKTIF ❌"
    await safe_send_message(
        context,
        update.effective_chat.id,
        f"✅ Auto-upload berhasil diatur ke: <b>{status}</b>\n"
        f"Platform: <b>{current_settings.get('platform', 'terabox')}</b>"
    )
    logger.info(f"User {user_id} set auto_upload to: {new_auto_upload}")

async def autocleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /autocleanup"""
    user_id = update.effective_user.id
    current_settings = get_user_settings(user_id)
    new_auto_cleanup = not current_settings.get('auto_cleanup', True)
    
    update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "AKTIF ✅" if new_auto_cleanup else "NONAKTIF ❌"
    await safe_send_message(context, update.effective_chat.id, f"✅ Auto-cleanup berhasil diatur ke: <b>{status}</b>")
    logger.info(f"User {user_id} set auto_cleanup to: {new_auto_cleanup}")

async def mysettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /mysettings"""
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available and is_logged_in:
        mega_status = f"✅ {safe_html(login_msg)}"
    elif mega_available:
        email, _ = load_mega_credentials()
        if email:
            mega_status = f"⚠️ Credential tersimpan ({safe_html(email)}), belum login"
        else:
            mega_status = "⚠️ Mega-cmd terinstall, belum login"
    else:
        mega_status = "❌ Mega-cmd tidak terinstall"
    
    settings_text = f"""
⚙️ <b>Settings Anda</b>

📛 Prefix: <code>{safe_html(settings.get('prefix', 'file_'))}</code>
📤 Platform: <b>{settings.get('platform', 'terabox').upper()}</b>
🔄 Auto-upload: <b>{'AKTIF ✅' if settings.get('auto_upload', False) else 'NONAKTIF ❌'}</b>
🧹 Auto-cleanup: <b>{'AKTIF ✅' if settings.get('auto_cleanup', True) else 'NONAKTIF ❌'}</b>

<b>Status Mega.nz:</b> {mega_status}

<b>Perintah:</b>
/setprefix - Atur prefix rename
/setplatform - Atur platform upload
/autoupload - Toggle auto-upload
/autocleanup - Toggle auto-cleanup
/loginmega - Login ke Mega.nz
/listmega - List folder Mega.nz
    """
    await safe_send_message(context, update.effective_chat.id, settings_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /start"""
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available and is_logged_in:
        mega_status = f"✅ {safe_html(login_msg)}"
    elif mega_available:
        email, _ = load_mega_credentials()
        if email:
            mega_status = f"⚠️ Terinstall, credential tersimpan ({safe_html(email)})"
        else:
            mega_status = "⚠️ Terinstall, belum login"
    else:
        mega_status = "❌ Mega-cmd tidak terinstall"
    
    welcome_text = f"""
🤖 <b>Bot Download &amp; Upload Manager</b>

<b>Status Mega.nz:</b> {mega_status}

<b>Fitur:</b>
📥 Download folder dari Mega.nz
🔄 Auto-rename file media
📤 Upload ke Terabox &amp; Doodstream
⚙️ Custom prefix &amp; auto-upload
🧹 Auto-cleanup setelah upload

<b>Perintah Utama:</b>
/loginmega &lt;email&gt; &lt;password&gt; - Login Mega.nz
/listmega - List folder Mega.nz
/download &lt;folder&gt; - Download folder
/upload - Upload interaktif
/mysettings - Lihat settings
/status - Status sistem
    """
    await safe_send_message(context, update.effective_chat.id, welcome_text)

async def listmega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /listmega"""
    await list_mega_folders(update, context)

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /download"""
    if not context.args:
        await safe_send_message(context, update.effective_chat.id, "❌ Format: <code>/download folder_name</code>")
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
    await safe_send_message(
        context,
        update.effective_chat.id,
        f"✅ Ditambahkan ke antrian download\nFolder: <code>{safe_html(folder_name)}</code>\nJob ID: <code>{job_id}</code>"
    )
    
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
    """Handler untuk /rename"""
    if len(context.args) != 2:
        await safe_send_message(context, update.effective_chat.id, "❌ Format: <code>/rename old_name new_name</code>")
        return
    
    old_name, new_name = context.args
    old_path = os.path.join(DOWNLOADS_DIR, old_name)
    new_path = os.path.join(DOWNLOADS_DIR, new_name)
    
    try:
        os.rename(old_path, new_path)
        await safe_send_message(context, update.effective_chat.id, f"✅ Berhasil rename: {safe_html(old_name)} → {safe_html(new_name)}")
        logger.info(f"Renamed {old_name} to {new_name}")
    except Exception as e:
        await safe_send_message(context, update.effective_chat.id, f"❌ Gagal rename: {safe_html(str(e))}")
        logger.error(f"Rename failed: {e}")

async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /upload"""
    folders, total_folders, current_page = get_folders_list()
    
    if not folders:
        await safe_send_message(context, update.effective_chat.id, "❌ Tidak ada folder di downloads/")
        return
    
    keyboard = []
    for folder in folders:
        display_name = folder[:20] + "..." if len(folder) > 20 else folder
        keyboard.append([InlineKeyboardButton(f"📁 {display_name}", callback_data=f"select_{folder[:30]}")])
    
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{current_page-1}"))
    if (current_page + 1) * 10 < total_folders:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{current_page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await safe_send_message(
        context,
        update.effective_chat.id,
        f"📂 Pilih folder untuk diupload (Halaman {current_page + 1}):",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler untuk button interactions"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("page_"):
        page = int(data.split("_")[1])
        folders, total_folders, _ = get_folders_list(page)
        
        keyboard = []
        for folder in folders:
            display_name = folder[:20] + "..." if len(folder) > 20 else folder
            keyboard.append([InlineKeyboardButton(f"📁 {display_name}", callback_data=f"select_{folder[:30]}")])
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
        if (page + 1) * 10 < total_folders:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await safe_edit_message(
            context,
            query.message.chat_id,
            query.message.message_id,
            f"📂 Pilih folder untuk diupload (Halaman {page + 1}):",
            reply_markup=reply_markup
        )
    
    elif data.startswith("select_"):
        folder_name = data.split("_", 1)[1]
        context.user_data['selected_folder'] = folder_name
        
        keyboard = [
            [
                InlineKeyboardButton("📤 Terabox", callback_data=f"target_terabox_{folder_name[:25]}"),
                InlineKeyboardButton("🎬 Doodstream", callback_data=f"target_doodstream_{folder_name[:25]}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await safe_edit_message(
            context,
            query.message.chat_id,
            query.message.message_id,
            f"📁 Folder: {safe_html(folder_name)}\nPilih target upload:",
            reply_markup=reply_markup
        )
    
    elif data.startswith("target_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            target = parts[1]
            folder_name = parts[2]
        else:
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Error: Invalid callback data")
            return
            
        folder_path = os.path.join(DOWNLOADS_DIR, folder_name)
        
        await safe_edit_message(context, query.message.chat_id, query.message.message_id, f"✅ Memulai upload {target}: {safe_html(folder_name)}")
        
        if target == "terabox":
            await upload_to_terabox(folder_path, update, context)
        elif target == "doodstream":
            await upload_to_doodstream(folder_path, update, context)
    
    elif data.startswith("megadl_"):
        try:
            folder_index = int(data.split("_")[1])
            folder_name = get_folder_by_index(query.from_user.id, folder_index)
            
            if not folder_name:
                await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Folder tidak ditemukan. Silakan refresh list.")
                return
            
            job_id = f"megadl_{query.id}"
            
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, f"✅ Menambahkan ke antrian download: {safe_html(folder_name)}")
            
            download_queue.put({
                'job_id': job_id,
                'folder_name': folder_name,
                'update': update,
                'context': context
            })
            active_jobs[job_id] = 'download'
            
            asyncio.create_task(process_download_queue())
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing megadl callback: {e}")
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Error: Invalid folder index")
    
    elif data == "refresh_mega_list":
        await safe_edit_message(context, query.message.chat_id, query.message.message_id, "🔄 Memuat ulang daftar folder...")
        await list_mega_folders(update, context)
    
    elif data == "retry_mega_login":
        await safe_edit_message(context, query.message.chat_id, query.message.message_id, "🔄 Mencoba login ulang ke Mega.nz...")
        if ensure_mega_session():
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, "✅ Login berhasil! Sekarang coba /listmega lagi")
        else:
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Login masih gagal. Coba: <code>/loginmega email password</code>")
    
    elif data == "cancel_operation":
        await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Operasi dibatalkan")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /status"""
    download_count = download_queue.qsize()
    upload_count = upload_queue.qsize()
    active_count = len(active_jobs)
    
    mega_available = check_mega_cmd()
    is_logged_in, login_msg = check_mega_login()
    
    if mega_available and is_logged_in:
        mega_status = f"✅ {safe_html(login_msg)}"
    elif mega_available:
        email, _ = load_mega_credentials()
        if email:
            mega_status = f"⚠️ Credential tersimpan ({safe_html(email)}), belum login"
        else:
            mega_status = "⚠️ Terinstall, belum login"
    else:
        mega_status = "❌ Tidak terinstall"
    
    status_text = f"""
📊 <b>Status Sistem</b>

📥 Antrian Download: {download_count}
📤 Antrian Upload: {upload_count}
🔄 Jobs Aktif: {active_count}

💾 Mega Status: {mega_status}
📁 Download Folder: <code>{safe_html(DOWNLOADS_DIR)}</code>
🧹 Auto-Cleanup: {'AKTIF ✅' if AUTO_CLEANUP else 'NONAKTIF ❌'}
    """
    await safe_send_message(context, update.effective_chat.id, status_text)

async def cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /cleanup"""
    try:
        folders = [f for f in os.listdir(DOWNLOADS_DIR) 
                  if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))]
        
        if not folders:
            await safe_send_message(context, update.effective_chat.id, "✅ Tidak ada folder untuk dibersihkan")
            return
        
        total_size = 0
        for folder in folders:
            folder_path = os.path.join(DOWNLOADS_DIR, folder)
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    total_size += os.path.getsize(os.path.join(root, file))
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Ya, Hapus Semua", callback_data="cleanup_confirm"),
                InlineKeyboardButton("❌ Batal", callback_data="cleanup_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await safe_send_message(
            context,
            update.effective_chat.id,
            f"🧹 <b>Manual Cleanup</b>\n\n"
            f"📁 Folder: {len(folders)}\n"
            f"💾 Total size: {total_size / (1024*1024):.2f} MB\n"
            f"⚠️ Yakin ingin menghapus SEMUA folder?",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        await safe_send_message(context, update.effective_chat.id, f"❌ Error: {safe_html(str(e))}")
        logger.error(f"Cleanup error: {e}")

async def cleanup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk callback cleanup"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cleanup_confirm":
        try:
            folders = [f for f in os.listdir(DOWNLOADS_DIR) 
                      if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))]
            
            deleted_count = 0
            for folder in folders:
                folder_path = os.path.join(DOWNLOADS_DIR, folder)
                shutil.rmtree(folder_path)
                deleted_count += 1
            
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, f"✅ Berhasil menghapus {deleted_count} folder")
            logger.info(f"Manual cleanup completed: {deleted_count} folders")
            
        except Exception as e:
            await safe_edit_message(context, query.message.chat_id, query.message.message_id, f"❌ Gagal menghapus folder: {safe_html(str(e))}")
            logger.error(f"Manual cleanup failed: {e}")
    
    elif query.data == "cleanup_cancel":
        await safe_edit_message(context, query.message.chat_id, query.message.message_id, "❌ Cleanup dibatalkan")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /cancel"""
    if not context.args:
        await safe_send_message(context, update.effective_chat.id, "❌ Format: <code>/cancel job_id</code>")
        return
    
    job_id = context.args[0]
    if job_id in active_jobs:
        active_jobs.pop(job_id)
        await safe_send_message(context, update.effective_chat.id, f"✅ Job {safe_html(job_id)} dibatalkan")
        logger.info(f"Job cancelled: {job_id}")
    else:
        await safe_send_message(context, update.effective_chat.id, f"❌ Job {safe_html(job_id)} tidak ditemukan")


def main():
    """Main function"""
    setup_environment()
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    logger.info("Checking Mega.nz setup...")
    if check_mega_cmd():
        logger.info("Mega-cmd detected successfully")
        
        is_logged_in, login_msg = check_mega_login()
        if is_logged_in:
            logger.info(f"Mega.nz login status: {login_msg}")
        else:
            email, _ = load_mega_credentials()
            if email:
                logger.info(f"Credentials found for: {email}, attempting auto-login...")
                if ensure_mega_session():
                    logger.info("Auto-login successful on startup")
                else:
                    logger.warning("Auto-login failed, manual login required")
            else:
                logger.info("Mega.nz not logged in")
    else:
        logger.warning("Mega-cmd not found. Install with: sudo snap install mega-cmd")
    
    try:
        cleanup_old_downloads()
    except Exception as e:
        logger.error(f"Error during startup cleanup: {e}")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("listmega", listmega))
    application.add_handler(CommandHandler("download", download))
    application.add_handler(CommandHandler("rename", rename))
    application.add_handler(CommandHandler("upload", upload))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("cleanup", cleanup))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("loginmega", loginmega))
    application.add_handler(CommandHandler("logoutmega", logoutmega))
    application.add_handler(CommandHandler("setprefix", setprefix))
    application.add_handler(CommandHandler("setplatform", setplatform))
    application.add_handler(CommandHandler("autoupload", autoupload))
    application.add_handler(CommandHandler("autocleanup", autocleanup))
    application.add_handler(CommandHandler("mysettings", mysettings))
    
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(page_|select_|target_|megadl_|refresh_mega_list|retry_mega_login|cancel_operation)"))
    application.add_handler(CallbackQueryHandler(cleanup_callback, pattern="^(cleanup_confirm|cleanup_cancel)$"))
    
    logger.info("🤖 Bot started successfully")
    print("🤖 Bot is running... Press Ctrl+C to stop")
    application.run_polling()

if __name__ == "__main__":
    main()
