#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from queue import Queue
from threading import Thread
from typing import Dict, List, Tuple
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)
from dotenv import load_dotenv
import requests
from PIL import Image
import aiohttp

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg'}
DOWNLOAD_BASE = Path('downloads')
TERABOX_CLI_PATH = Path('teraboxcli/main.py')

# Global queues and state
download_queue = Queue()
upload_queue = Queue()
active_jobs = {}
user_progress_messages = {}

class UserSettings:
    def __init__(self):
        self.settings_file = 'user_settings.json'
        self.load_settings()
    
    def load_settings(self) -> Dict:
        try:
            with open(self.settings_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_settings(self, settings: Dict):
        with open(self.settings_file, 'w') as f:
            json.dump(settings, f, indent=4)
    
    def get_user_settings(self, user_id: int) -> Dict:
        settings = self.load_settings()
        user_str = str(user_id)
        if user_str not in settings:
            settings[user_str] = {
                'prefix': 'file_',
                'platform': 'terabox',
                'auto_upload': False,
                'auto_cleanup': True
            }
            self.save_settings(settings)
        return settings[user_str]
    
    def update_user_settings(self, user_id: int, new_settings: Dict):
        settings = self.load_settings()
        user_str = str(user_id)
        settings[user_str] = {**settings.get(user_str, {}), **new_settings}
        self.save_settings(settings)

class MegaManager:
    def __init__(self):
        self.cred_file = 'mega.json'
        self.load_credentials()
    
    def load_credentials(self) -> Dict:
        try:
            with open(self.cred_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_credentials(self, creds: Dict):
        with open(self.cred_file, 'w') as f:
            json.dump(creds, f, indent=4)
    
    def check_mega_cmd(self) -> bool:
        try:
            result = subprocess.run(['mega-cmd', '--version'], 
                                  capture_output=True, text=True)
            return result.returncode == 0
        except FileNotFoundError:
            return False
    
    def login_to_mega(self, email: str, password: str) -> Tuple[bool, str]:
        try:
            # Save credentials
            creds = self.load_credentials()
            creds['email'] = email
            creds['password'] = password
            self.save_credentials(creds)
            
            # Login using mega-cmd
            cmd = f'mega-login "{email}" "{password}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, "Login berhasil!"
            else:
                return False, f"Login gagal: {result.stderr}"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def logout_mega(self) -> Tuple[bool, str]:
        try:
            result = subprocess.run('mega-logout', shell=True, capture_output=True, text=True)
            
            # Clear saved credentials
            creds = self.load_credentials()
            creds.clear()
            self.save_credentials(creds)
            
            return True, "Logout berhasil!"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def ensure_mega_session(self) -> bool:
        try:
            # Check if session exists, if not try to login with saved credentials
            result = subprocess.run('mega-whoami', shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                return True
            
            # Try to login with saved credentials
            creds = self.load_credentials()
            if 'email' in creds and 'password' in creds:
                success, message = self.login_to_mega(creds['email'], creds['password'])
                return success
            
            return False
        except Exception:
            return False
    
    def list_mega_folders(self) -> Tuple[bool, List[str]]:
        try:
            if not self.ensure_mega_session():
                return False, ["Session tidak valid. Silakan login ulang."]
            
            result = subprocess.run('mega-ls', shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                folders = [line.strip() for line in result.stdout.split('\n') if line.strip()]
                return True, folders
            else:
                return False, [f"Error: {result.stderr}"]
        except Exception as e:
            return False, [f"Exception: {str(e)}"]
    
    def download_mega_folder(self, folder_name: str, download_path: Path) -> Tuple[bool, str]:
        try:
            if not self.ensure_mega_session():
                return False, "Session tidak valid"
            
            # Create download directory
            download_path.mkdir(parents=True, exist_ok=True)
            
            cmd = f'mega-get /{folder_name} "{download_path}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, f"Download {folder_name} berhasil!"
            else:
                return False, f"Download gagal: {result.stderr}"
        except Exception as e:
            return False, f"Error: {str(e)}"

class FileManager:
    @staticmethod
    def auto_rename_media_files(folder_path: Path, prefix: str) -> Dict:
        try:
            photo_count = 0
            video_count = 0
            
            # Find all media files recursively
            media_files = []
            for ext in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                media_files.extend(folder_path.rglob(f'*{ext}'))
                media_files.extend(folder_path.rglob(f'*{ext.upper()}'))
            
            # Sort files naturally
            media_files.sort()
            
            # Rename photos and videos separately
            for file_path in media_files:
                if file_path.suffix.lower() in PHOTO_EXTENSIONS:
                    photo_count += 1
                    new_name = f"{prefix}pic_{photo_count:04d}{file_path.suffix}"
                    new_path = file_path.parent / new_name
                elif file_path.suffix.lower() in VIDEO_EXTENSIONS:
                    video_count += 1
                    new_name = f"{prefix}vid_{video_count:04d}{file_path.suffix}"
                    new_path = file_path.parent / new_name
                else:
                    continue
                
                # Rename file
                file_path.rename(new_path)
            
            return {'photos': photo_count, 'videos': video_count}
        except Exception as e:
            logger.error(f"Error in auto_rename: {e}")
            return {'photos': 0, 'videos': 0}

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
    
    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Upload files to Terabox using teraboxcli"""
        try:
            await self.send_progress_message(update, context, "📤 Memulai upload ke Terabox...")
            
            if not TERABOX_CLI_PATH.exists():
                await self.send_progress_message(update, context, "❌ Terabox CLI tidak ditemukan!")
                return []
            
            links = []
            # Upload each file in folder
            for file_path in folder_path.rglob('*'):
                if file_path.is_file():
                    try:
                        cmd = ['python3', str(TERABOX_CLI_PATH), 'upload', str(file_path)]
                        result = subprocess.run(cmd, capture_output=True, text=True)
                        
                        if result.returncode == 0:
                            # Extract link from output (adjust based on teraboxcli output)
                            link_match = re.search(r'https?://[^\s]+', result.stdout)
                            if link_match:
                                links.append(link_match.group())
                                await self.send_progress_message(
                                    update, context, 
                                    f"✅ Upload berhasil: {file_path.name}"
                                )
                        else:
                            await self.send_progress_message(
                                update, context, 
                                f"❌ Upload gagal: {file_path.name}"
                            )
                    except Exception as e:
                        logger.error(f"Upload error for {file_path}: {e}")
            
            return links
        except Exception as e:
            logger.error(f"Terabox upload error: {e}")
            await self.send_progress_message(update, context, f"❌ Error upload: {str(e)}")
            return []
    
    async def upload_to_doodstream(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Upload video files to Doodstream"""
        try:
            await self.send_progress_message(update, context, "📤 Memulai upload ke Doodstream...")
            
            if not self.doodstream_key:
                await self.send_progress_message(update, context, "❌ API Key Doodstream tidak ditemukan!")
                return []
            
            links = []
            # Upload only video files
            for file_path in folder_path.rglob('*'):
                if file_path.is_file() and file_path.suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        link = await self.upload_single_file_to_doodstream(file_path)
                        if link:
                            links.append(link)
                            await self.send_progress_message(
                                update, context, 
                                f"✅ Upload berhasil: {file_path.name}"
                            )
                        else:
                            await self.send_progress_message(
                                update, context, 
                                f"❌ Upload gagal: {file_path.name}"
                            )
                    except Exception as e:
                        logger.error(f"Doodstream upload error for {file_path}: {e}")
            
            return links
        except Exception as e:
            logger.error(f"Doodstream upload error: {e}")
            await self.send_progress_message(update, context, f"❌ Error upload: {str(e)}")
            return []
    
    async def upload_single_file_to_doodstream(self, file_path: Path) -> str:
        """Upload single file to Doodstream API"""
        try:
            url = "https://doodstream.com/api/upload"
            
            with open(file_path, 'rb') as f:
                files = {'file': f}
                data = {'key': self.doodstream_key}
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=data, files=files) as response:
                        result = await response.json()
                        
                        if result.get('success'):
                            return result.get('download_url', '')
                        else:
                            logger.error(f"Doodstream API error: {result}")
                            return ""
        except Exception as e:
            logger.error(f"Doodstream single upload error: {e}")
            return ""
    
    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, message: str):
        """Send or update progress message"""
        try:
            chat_id = update.effective_chat.id
            
            if chat_id in user_progress_messages:
                # Edit existing message
                msg_id = user_progress_messages[chat_id]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=message
                )
            else:
                # Send new message
                msg = await context.bot.send_message(chat_id=chat_id, text=message)
                user_progress_messages[chat_id] = msg.message_id
        except Exception as e:
            logger.error(f"Error sending progress message: {e}")

# Initialize managers
user_settings = UserSettings()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()

# Queue processors
def process_download_queue():
    while True:
        try:
            job_data = download_queue.get()
            if job_data is None:
                break
            
            job_id, folder_name, user_id, update, context = job_data
            asyncio.run(process_single_download(job_id, folder_name, user_id, update, context))
            download_queue.task_done()
        except Exception as e:
            logger.error(f"Download queue error: {e}")

def process_upload_queue():
    while True:
        try:
            job_data = upload_queue.get()
            if job_data is None:
                break
            
            job_id, folder_path, platform, user_id, update, context = job_data
            asyncio.run(process_single_upload(job_id, folder_path, platform, user_id, update, context))
            upload_queue.task_done()
        except Exception as e:
            logger.error(f"Upload queue error: {e}")

async def process_single_download(job_id: str, folder_name: str, user_id: int, 
                                update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process single download job"""
    try:
        settings = user_settings.get_user_settings(user_id)
        
        # Create job directory
        job_path = DOWNLOAD_BASE / job_id
        job_path.mkdir(parents=True, exist_ok=True)
        
        await upload_manager.send_progress_message(
            update, context, f"📥 Downloading: {folder_name}"
        )
        
        # Download from Mega.nz
        success, message = mega_manager.download_mega_folder(folder_name, job_path)
        
        if success:
            await upload_manager.send_progress_message(
                update, context, f"✅ Download selesai! Renaming files..."
            )
            
            # Auto-rename files
            rename_result = file_manager.auto_rename_media_files(
                job_path, settings['prefix']
            )
            
            await upload_manager.send_progress_message(
                update, context,
                f"📁 Rename selesai: {rename_result['photos']} foto, "
                f"{rename_result['videos']} video"
            )
            
            # Auto-upload if enabled
            if settings['auto_upload']:
                upload_job_id = f"upload_{job_id}"
                upload_queue.put((
                    upload_job_id, job_path, settings['platform'], 
                    user_id, update, context
                ))
                active_jobs[upload_job_id] = 'uploading'
                
                await upload_manager.send_progress_message(
                    update, context,
                    f"🔄 Auto-upload ke {settings['platform']} dimulai..."
                )
            else:
                await upload_manager.send_progress_message(
                    update, context,
                    f"✅ Download selesai! Gunakan /upload untuk mengupload files."
                )
        else:
            await upload_manager.send_progress_message(
                update, context, f"❌ Download gagal: {message}"
            )
        
        # Cleanup
        if job_id in active_jobs:
            del active_jobs[job_id]
            
    except Exception as e:
        logger.error(f"Download job error: {e}")
        await upload_manager.send_progress_message(
            update, context, f"❌ Error: {str(e)}"
        )

async def process_single_upload(job_id: str, folder_path: Path, platform: str,
                              user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process single upload job"""
    try:
        settings = user_settings.get_user_settings(user_id)
        
        # Upload based on platform
        if platform == 'terabox':
            links = await upload_manager.upload_to_terabox(folder_path, update, context)
        else:  # doodstream
            links = await upload_manager.upload_to_doodstream(folder_path, update, context)
        
        # Send results
        if links:
            links_text = "\n".join([f"🔗 {link}" for link in links[:10]])  # Limit to 10 links
            if len(links) > 10:
                links_text += f"\n... dan {len(links) - 10} link lainnya"
            
            await upload_manager.send_progress_message(
                update, context,
                f"✅ Upload selesai!\n\nLinks:\n{links_text}"
            )
        else:
            await upload_manager.send_progress_message(
                update, context, "❌ Tidak ada file yang berhasil diupload"
            )
        
        # Auto-cleanup
        if settings['auto_cleanup']:
            try:
                shutil.rmtree(folder_path)
                await upload_manager.send_progress_message(
                    update, context, "🧹 Auto-cleanup selesai!"
                )
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
        
        # Cleanup
        if job_id in active_jobs:
            del active_jobs[job_id]
            
    except Exception as e:
        logger.error(f"Upload job error: {e}")
        await upload_manager.send_progress_message(
            update, context, f"❌ Upload error: {str(e)}"
        )

# Start queue processors
download_thread = Thread(target=process_download_queue, daemon=True)
upload_thread = Thread(target=process_upload_queue, daemon=True)
download_thread.start()
upload_thread.start()

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user_id = update.effective_user.id
    
    # Check Mega.nz availability
    mega_available = mega_manager.check_mega_cmd()
    mega_status = "✅ Terpasang" if mega_available else "❌ Tidak terpasang"
    
    welcome_text = f"""
🤖 **Mega Downloader Bot**

**System Status:**
- Mega.nz CMD: {mega_status}
- Download Queue: {download_queue.qsize()}
- Upload Queue: {upload_queue.qsize()}

**Fitur:**
📥 Download folder dari Mega.nz
📝 Auto-rename file media
📤 Upload ke Terabox & Doodstream
⚙️ Management antrian & settings

Gunakan /help untuk melihat semua command!
    """
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = """
📚 **Daftar Perintah:**

**MEGA.NZ COMMANDS**
/loginmega <email> <password> - Login ke akun Mega.nz
/logoutmega - Logout dari Mega.nz  
/listmega - List semua folder di Mega.nz
/download <folder_name> - Download folder spesifik

**FILE MANAGEMENT**
/upload - Pilih folder untuk upload
/rename <old> <new> - Rename folder manual  
/cleanup - Hapus semua folder (dengan konfirmasi)

**SETTINGS**
/setprefix <prefix> - Set custom prefix untuk rename
/setplatform <terabox|doodstream> - Pilih platform upload
/autoupload - Toggle auto-upload setelah download
/autocleanup - Toggle auto-cleanup setelah upload  
/mysettings - Lihat settings saat ini
/status - Status sistem & antrian

**SYSTEM**
/cancel <job_id> - Batalkan job yang berjalan
/help - Bantuan lengkap
    """
    
    await update.message.reply_text(help_text)

async def login_mega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Login to Mega.nz"""
    if len(context.args) < 2:
        await update.message.reply_text("❌ Format: /loginmega <email> <password>")
        return
    
    email = context.args[0]
    password = context.args[1]
    
    success, message = mega_manager.login_to_mega(email, password)
    
    if success:
        await update.message.reply_text(f"✅ {message}")
    else:
        await update.message.reply_text(f"❌ {message}")

async def logout_mega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logout from Mega.nz"""
    success, message = mega_manager.logout_mega()
    
    if success:
        await update.message.reply_text(f"✅ {message}")
    else:
        await update.message.reply_text(f"❌ {message}")

async def list_mega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List Mega.nz folders"""
    if not mega_manager.ensure_mega_session():
        # Try auto-login with env credentials
        email = os.getenv('MEGA_EMAIL')
        password = os.getenv('MEGA_PASSWORD')
        
        if email and password:
            success, message = mega_manager.login_to_mega(email, password)
            if not success:
                await update.message.reply_text(
                    "❌ Session expired. Silakan login dengan /loginmega"
                )
                return
        else:
            await update.message.reply_text(
                "❌ Session expired. Silakan login dengan /loginmega"
            )
            return
    
    success, folders = mega_manager.list_mega_folders()
    
    if success:
        if folders:
            # Create inline keyboard with folders
            keyboard = []
            for folder in folders[:10]:  # Limit to 10 folders
                keyboard.append([InlineKeyboardButton(
                    f"📁 {folder}", 
                    callback_data=f"megadl_{folder}"
                )])
            
            # Add refresh button
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_mega_list")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "📂 Pilih folder untuk download:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("📂 Tidak ada folder ditemukan")
    else:
        await update.message.reply_text(f"❌ Error: {folders[0] if folders else 'Unknown error'}")

async def download_mega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download specific Mega.nz folder"""
    if not context.args:
        await update.message.reply_text("❌ Format: /download <folder_name>")
        return
    
    folder_name = " ".join(context.args)
    user_id = update.effective_user.id
    
    # Generate job ID
    job_id = f"dl_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    active_jobs[job_id] = 'downloading'
    
    # Add to download queue
    download_queue.put((job_id, folder_name, user_id, update, context))
    
    await update.message.reply_text(
        f"✅ Ditambahkan ke antrian! Job ID: {job_id}\n"
        f"Antrian download: {download_queue.qsize()}"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard buttons"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data.startswith('megadl_'):
        # Mega.nz folder download
        folder_name = data[7:]  # Remove 'megadl_' prefix
        
        # Generate job ID
        job_id = f"dl_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        active_jobs[job_id] = 'downloading'
        
        # Add to download queue
        download_queue.put((job_id, folder_name, user_id, update, context))
        
        await query.edit_message_text(
            f"✅ Download dimulai: {folder_name}\nJob ID: {job_id}"
        )
    
    elif data == 'refresh_mega_list':
        # Refresh Mega.nz list
        success, folders = mega_manager.list_mega_folders()
        
        if success and folders:
            keyboard = []
            for folder in folders[:10]:
                keyboard.append([InlineKeyboardButton(
                    f"📁 {folder}", 
                    callback_data=f"megadl_{folder}"
                )])
            
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_mega_list")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "📂 Pilih folder untuk download:",
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text("❌ Gagal refresh daftar folder")

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom prefix for auto-rename"""
    if not context.args:
        await update.message.reply_text("❌ Format: /setprefix <prefix>")
        return
    
    prefix = context.args[0]
    user_id = update.effective_user.id
    
    user_settings.update_user_settings(user_id, {'prefix': prefix})
    await update.message.reply_text(f"✅ Prefix diubah menjadi: {prefix}")

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set default upload platform"""
    if not context.args:
        await update.message.reply_text("❌ Format: /setplatform <terabox|doodstream>")
        return
    
    platform = context.args[0].lower()
    if platform not in ['terabox', 'doodstream']:
        await update.message.reply_text("❌ Platform harus: terabox atau doodstream")
        return
    
    user_id = update.effective_user.id
    user_settings.update_user_settings(user_id, {'platform': platform})
    await update.message.reply_text(f"✅ Platform diubah menjadi: {platform}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload"""
    user_id = update.effective_user.id
    settings = user_settings.get_user_settings(user_id)
    
    new_auto_upload = not settings['auto_upload']
    user_settings.update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "AKTIF" if new_auto_upload else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-upload: {status}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-cleanup"""
    user_id = update.effective_user.id
    settings = user_settings.get_user_settings(user_id)
    
    new_auto_cleanup = not settings['auto_cleanup']
    user_settings.update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "AKTIF" if new_auto_cleanup else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-cleanup: {status}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current user settings"""
    user_id = update.effective_user.id
    settings = user_settings.get_user_settings(user_id)
    
    settings_text = f"""
⚙️ **User Settings:**

📝 Prefix: `{settings['prefix']}`
📤 Platform: `{settings['platform']}`
🔄 Auto-upload: `{'AKTIF' if settings['auto_upload'] else 'NON-AKTIF'}`
🧹 Auto-cleanup: `{'AKTIF' if settings['auto_cleanup'] else 'NON-AKTIF'}`
    """
    
    await update.message.reply_text(settings_text, parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system status"""
    mega_available = mega_manager.check_mega_cmd()
    mega_status = "✅ Terpasang" if mega_available else "❌ Tidak terpasang"
    
    # Count download folders
    download_count = len(list(DOWNLOAD_BASE.glob('*'))) if DOWNLOAD_BASE.exists() else 0
    
    status_text = f"""
📊 **System Status**

**Services:**
- Mega.nz CMD: {mega_status}
- Terabox CLI: {'✅ Terpasang' if TERABOX_CLI_PATH.exists() else '❌ Tidak terpasang'}
- Doodstream API: {'✅ Terkonfigurasi' if os.getenv('DOODSTREAM_API_KEY') else '❌ Tidak dikonfigurasi'}

**Queues:**
- Download Queue: {download_queue.qsize()} jobs
- Upload Queue: {upload_queue.qsize()} jobs
- Active Jobs: {len(active_jobs)} jobs

**Storage:**
- Download Folders: {download_count} folders
- Auto-cleanup: {'AKTIF' if os.getenv('AUTO_CLEANUP', 'true').lower() == 'true' else 'NON-AKTIF'}
    """
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup all download folders"""
    if not DOWNLOAD_BASE.exists():
        await update.message.reply_text("📁 Tidak ada folder download")
        return
    
    # Create confirmation keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Ya, hapus semua", callback_data="cleanup_confirm"),
            InlineKeyboardButton("❌ Batal", callback_data="cleanup_cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ **HAPUS SEMUA FOLDER DOWNLOAD?**\n\n"
        "Tindakan ini akan menghapus SEMUA folder di direktori downloads!",
        reply_markup=reply_markup
    )

async def cleanup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cleanup confirmation"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cleanup_confirm':
        try:
            if DOWNLOAD_BASE.exists():
                # Count folders before deletion
                folders = list(DOWNLOAD_BASE.glob('*'))
                folder_count = len(folders)
                
                # Delete all folders
                for folder in folders:
                    shutil.rmtree(folder)
                
                await query.edit_message_text(f"✅ Berhasil menghapus {folder_count} folder")
            else:
                await query.edit_message_text("📁 Tidak ada folder untuk dihapus")
                
        except Exception as e:
            await query.edit_message_text(f"❌ Error cleanup: {str(e)}")
    
    else:  # cleanup_cancel
        await query.edit_message_text("❌ Cleanup dibatalkan")

def main():
    """Start the bot"""
    # Create necessary directories
    DOWNLOAD_BASE.mkdir(exist_ok=True)
    
    # Check Mega.nz installation
    if not mega_manager.check_mega_cmd():
        logger.warning("Mega.nz CMD tidak terpasang! Install dengan: sudo snap install mega-cmd")
    
    # Initialize bot
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN tidak ditemukan di environment variables!")
        return
    
    application = Application.builder().token(token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("loginmega", login_mega))
    application.add_handler(CommandHandler("logoutmega", logout_mega))
    application.add_handler(CommandHandler("listmega", list_mega))
    application.add_handler(CommandHandler("download", download_mega))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("autoupload", auto_upload_toggle))
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("cleanup", cleanup))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(megadl_|refresh_mega_list)"))
    application.add_handler(CallbackQueryHandler(cleanup_handler, pattern="^cleanup_"))
    
    # Start bot
    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    main()
