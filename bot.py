#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from queue import Queue
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)
from dotenv import load_dotenv
import requests
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
TERABOX_CLI_DIR = Path('TeraboxUploaderCLI')
MAX_CONCURRENT_DOWNLOADS = 2

# Global state
download_queue = Queue()
active_downloads: Dict[str, Dict] = {}
completed_downloads: Dict[str, Dict] = {}
user_settings = {}
user_progress_messages = {}

class DownloadStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    RENAMING = "renaming"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    ERROR = "error"

class UserSettingsManager:
    def __init__(self):
        self.settings_file = 'user_settings.json'
        self.settings = self.load_settings()
    
    def load_settings(self) -> Dict:
        try:
            with open(self.settings_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_settings(self):
        with open(self.settings_file, 'w') as f:
            json.dump(self.settings, f, indent=4)
    
    def get_user_settings(self, user_id: int) -> Dict:
        user_str = str(user_id)
        if user_str not in self.settings:
            self.settings[user_str] = {
                'prefix': 'file_',
                'platform': 'terabox',
                'auto_upload': True,
                'auto_cleanup': True
            }
            self.save_settings()
        return self.settings[user_str]
    
    def update_user_settings(self, user_id: int, new_settings: Dict):
        user_str = str(user_id)
        if user_str not in self.settings:
            self.settings[user_str] = {}
        self.settings[user_str].update(new_settings)
        self.save_settings()

class MegaManager:
    def __init__(self):
        self.cred_file = 'mega_session.json'
        # Load mega accounts from environment or file
        self.accounts = self.load_mega_accounts()
        self.current_account_index = 0
    
    def load_mega_accounts(self) -> List[Dict]:
        """Load mega accounts from environment variables"""
        accounts = []
        
        # Try to load from mega_accounts.json first
        try:
            with open('mega_accounts.json', 'r') as f:
                file_accounts = json.load(f)
                if isinstance(file_accounts, list):
                    accounts.extend(file_accounts)
        except FileNotFoundError:
            pass
        
        # Load from environment variables
        env_accounts = []
        i = 1
        while True:
            email = os.getenv(f'MEGA_EMAIL_{i}')
            password = os.getenv(f'MEGA_PASSWORD_{i}')
            if not email or not password:
                break
            env_accounts.append({'email': email, 'password': password})
            i += 1
        
        accounts.extend(env_accounts)
        
        if not accounts:
            logger.warning("No Mega.nz accounts found!")
        
        return accounts
    
    def check_mega_cmd(self) -> bool:
        try:
            result = subprocess.run(['mega-cmd', '--version'], 
                                  capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def get_current_account(self) -> Optional[Dict]:
        if not self.accounts:
            return None
        return self.accounts[self.current_account_index]
    
    def rotate_account(self):
        if len(self.accounts) > 1:
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
            logger.info(f"Rotated to account: {self.get_current_account()['email']}")
    
    def login_to_mega(self, email: str, password: str) -> Tuple[bool, str]:
        try:
            # Logout first to ensure clean session
            subprocess.run('mega-logout', shell=True, capture_output=True, text=True)
            
            cmd = f'mega-login "{email}" "{password}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                # Save session info
                session_info = {
                    'email': email,
                    'logged_in_at': datetime.now().isoformat()
                }
                with open(self.cred_file, 'w') as f:
                    json.dump(session_info, f)
                return True, f"Login berhasil ke: {email}"
            else:
                return False, f"Login gagal: {result.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Login timeout"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def ensure_mega_session(self) -> bool:
        try:
            result = subprocess.run('mega-whoami', shell=True, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except Exception:
            return False
    
    def download_mega_folder(self, folder_url: str, download_path: Path) -> Tuple[bool, str]:
        try:
            if not self.ensure_mega_session():
                # Try to login with current account
                current_account = self.get_current_account()
                if current_account:
                    success, message = self.login_to_mega(current_account['email'], current_account['password'])
                    if not success:
                        return False, f"Session invalid and login failed: {message}"
                else:
                    return False, "No Mega.nz account available"
            
            # Create download directory
            download_path.mkdir(parents=True, exist_ok=True)
            
            cmd = f'mega-get "{folder_url}" "{download_path}"'
            logger.info(f"Executing download: {cmd}")
            
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600)
            
            if result.returncode == 0:
                return True, f"Download berhasil!"
            else:
                error_msg = result.stderr if result.stderr else "Unknown error"
                return False, f"Download gagal: {error_msg}"
        except subprocess.TimeoutExpired:
            return False, "Download timeout (1 hour)"
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
                try:
                    if file_path != new_path:
                        file_path.rename(new_path)
                except Exception as e:
                    logger.error(f"Error renaming {file_path}: {e}")
                    continue
            
            return {'photos': photo_count, 'videos': video_count}
        except Exception as e:
            logger.error(f"Error in auto_rename: {e}")
            return {'photos': 0, 'videos': 0}

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_lock = threading.Lock()
    
    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox using TeraboxUploaderCLI"""
        try:
            if not TERABOX_CLI_DIR.exists():
                await self.send_progress_message(update, context, job_id, "❌ TeraboxUploaderCLI tidak ditemukan!")
                return []
            
            await self.send_progress_message(update, context, job_id, "📤 Memulai upload ke Terabox menggunakan TeraboxUploaderCLI...")
            
            # Use lock to prevent multiple concurrent Terabox uploads
            with self.terabox_lock:
                # Run TeraboxUploaderCLI
                old_cwd = os.getcwd()
                os.chdir(TERABOX_CLI_DIR)
                
                try:
                    # Run the uploader for the specific folder
                    cmd = ['python', 'main.py', '--source', str(folder_path)]
                    logger.info(f"Executing TeraboxUploaderCLI: {cmd}")
                    
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                    
                    if result.returncode == 0:
                        # Parse output for success
                        await self.send_progress_message(
                            update, context, job_id,
                            f"✅ Upload ke Terabox selesai!\n"
                            f"Folder: {folder_path.name}\n"
                            f"File telah diupload ke akun Terabox"
                        )
                        return ["Upload completed - check your Terabox account"]
                    else:
                        error_msg = result.stderr if result.stderr else result.stdout
                        raise Exception(f"TeraboxUploaderCLI failed: {error_msg}")
                        
                finally:
                    os.chdir(old_cwd)
                    
        except subprocess.TimeoutExpired:
            error_msg = "Upload timeout (1 hour)"
            logger.error(f"Terabox upload timeout: {error_msg}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload timeout: {error_msg}")
            return []
        except Exception as e:
            logger.error(f"Terabox upload error: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Error upload: {str(e)}")
            return []
    
    async def upload_to_doodstream(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload video files to Doodstream"""
        try:
            await self.send_progress_message(update, context, job_id, "📤 Memulai upload ke Doodstream...")
            
            if not self.doodstream_key:
                await self.send_progress_message(update, context, job_id, "❌ API Key Doodstream tidak ditemukan!")
                return []
            
            links = []
            video_files = [f for f in folder_path.rglob('*') 
                          if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            total_files = len(video_files)
            uploaded_count = 0
            
            if total_files == 0:
                await self.send_progress_message(update, context, job_id, "📭 Tidak ada file video untuk diupload")
                return []
            
            for file_path in video_files:
                if not self.is_job_active(job_id):
                    break
                    
                try:
                    link = await self.upload_single_file_to_doodstream(file_path)
                    if link:
                        links.append(link)
                        uploaded_count += 1
                        await self.send_progress_message(
                            update, context, job_id,
                            f"📤 Upload progress: {uploaded_count}/{total_files}\n✅ {file_path.name}"
                        )
                    else:
                        await self.send_progress_message(
                            update, context, job_id,
                            f"❌ Upload gagal: {file_path.name}"
                        )
                except Exception as e:
                    logger.error(f"Doodstream upload error for {file_path}: {e}")
            
            return links
        except Exception as e:
            logger.error(f"Doodstream upload error: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Error upload: {str(e)}")
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
    
    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, message: str):
        """Send or update progress message"""
        try:
            if job_id not in active_downloads:
                return
                
            chat_id = active_downloads[job_id]['chat_id']
            
            # Store the latest progress message for this job
            if 'progress_message_id' in active_downloads[job_id]:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=active_downloads[job_id]['progress_message_id'],
                        text=f"**{active_downloads[job_id]['folder_name']}**\n{message}",
                        parse_mode='Markdown'
                    )
                    return
                except Exception:
                    # If editing fails, send new message
                    pass
            
            # Send new message
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"**{active_downloads[job_id]['folder_name']}**\n{message}",
                parse_mode='Markdown'
            )
            active_downloads[job_id]['progress_message_id'] = msg.message_id
            
        except Exception as e:
            logger.error(f"Error sending progress message: {e}")
    
    def is_job_active(self, job_id: str) -> bool:
        return job_id in active_downloads and active_downloads[job_id]['status'] != DownloadStatus.COMPLETED

class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, file_manager: FileManager, upload_manager: UploadManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.settings_manager = settings_manager
        self.processing = False
        self.current_processes = 0
    
    def start_processing(self):
        """Start processing download queue"""
        if not self.processing:
            self.processing = True
            thread = threading.Thread(target=self._process_queue, daemon=True)
            thread.start()
    
    def _process_queue(self):
        """Process download queue continuously"""
        while self.processing:
            try:
                # Check if we can start new downloads
                if self.current_processes < MAX_CONCURRENT_DOWNLOADS and not download_queue.empty():
                    job_data = download_queue.get()
                    if job_data:
                        self.current_processes += 1
                        threading.Thread(
                            target=self._process_single_download,
                            args=(job_data,),
                            daemon=True
                        ).start()
                
                threading.Event().wait(5)  # Check every 5 seconds
            except Exception as e:
                logger.error(f"Error in queue processor: {e}")
                threading.Event().wait(10)
    
    def _process_single_download(self, job_data: Dict):
        """Process single download job"""
        asyncio.run(self._async_process_single_download(job_data))
    
    async def _async_process_single_download(self, job_data: Dict):
        """Async version of single download processing"""
        job_id = job_data['job_id']
        folder_name = job_data['folder_name']
        mega_url = job_data['mega_url']
        user_id = job_data['user_id']
        update = job_data['update']
        context = job_data['context']
        
        try:
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOADING
            active_downloads[job_id]['progress'] = "Memulai download dari Mega.nz"
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, "📥 Memulai download dari Mega.nz..."
            )
            
            # Create download path
            download_path = DOWNLOAD_BASE / folder_name
            
            # Download from Mega.nz
            success, message = self.mega_manager.download_mega_folder(mega_url, download_path)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"❌ Download gagal: {message}"
                )
                return
            
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.RENAMING
            active_downloads[job_id]['progress'] = "Renaming files"
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, "✅ Download selesai! Renaming files..."
            )
            
            # Auto-rename files
            user_settings = self.settings_manager.get_user_settings(user_id)
            prefix = user_settings.get('prefix', 'file_')
            rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
            
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"📝 Rename selesai:\n"
                f"📷 Foto: {rename_result['photos']} files\n"
                f"🎥 Video: {rename_result['videos']} files"
            )
            
            # Auto-upload if enabled
            if user_settings.get('auto_upload', True):
                active_downloads[job_id]['status'] = DownloadStatus.UPLOADING
                active_downloads[job_id]['progress'] = "Uploading files"
                
                platform = user_settings.get('platform', 'terabox')
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"📤 Upload ke {platform}..."
                )
                
                if platform == 'terabox':
                    links = await self.upload_manager.upload_to_terabox(download_path, update, context, job_id)
                else:
                    links = await self.upload_manager.upload_to_doodstream(download_path, update, context, job_id)
                
                # Don't send duplicate success message for Terabox
                if platform != 'terabox':
                    await self.upload_manager.send_progress_message(
                        update, context, job_id,
                        f"✅ Upload selesai!\n🔗 {len(links)} links generated"
                    )
            
            # Auto-cleanup if enabled
            if user_settings.get('auto_cleanup', True):
                try:
                    # For Terabox, files might be deleted by TeraboxUploaderCLI
                    # For Doodstream, we need to cleanup manually
                    if os.path.exists(download_path):
                        shutil.rmtree(download_path)
                        await self.upload_manager.send_progress_message(
                            update, context, job_id, "🧹 Auto-cleanup selesai!"
                        )
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
            
            # Mark as completed
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['progress'] = "Proses selesai"
            active_downloads[job_id]['completed_at'] = datetime.now().isoformat()
            
            # Move to completed downloads
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, "✅ Semua proses selesai!"
            )
            
        except Exception as e:
            logger.error(f"Error processing download {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['error'] = str(e)
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, f"❌ Error: {str(e)}"
            )
        
        finally:
            self.current_processes -= 1

# Initialize managers
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    welcome_text = """
🤖 **Mega Downloader Bot**

**Fitur:**
📥 Download folder dari Mega.nz via link
📝 Auto-rename file media
📤 Upload ke Terabox/Doodstream
⚡ Maksimal 2 download bersamaan
📊 System antrian otomatis

**Perintah:**
/download <nama_folder> <link_mega> - Download folder
/upload <nama_folder> - Upload manual
/status - Status sistem & antrian
/mysettings - Lihat pengaturan
/setprefix <prefix> - Set prefix rename
/setplatform <terabox|doodstream> - Pilih platform
/autoupload - Toggle auto-upload
/autocleanup - Toggle auto-cleanup
/cleanup - Hapus folder download
/help - Bantuan lengkap

**Contoh:**
`/download AMIBEL https://mega.nz/folder/abc123#xyz`
    """
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = """
📚 **Daftar Perintah:**

**DOWNLOAD COMMANDS**
/download <nama_folder> <link_mega> - Download folder dari Mega.nz
/upload <nama_folder> - Upload folder manual

**SETTINGS COMMANDS**
/setprefix <prefix> - Set custom prefix untuk rename
/setplatform <terabox|doodstream> - Pilih platform upload
/autoupload - Toggle auto-upload setelah download
/autocleanup - Toggle auto-cleanup setelah upload

**INFO COMMANDS**
/status - Status sistem & antrian download
/mysettings - Lihat pengaturan saat ini

**MAINTENANCE**
/cleanup - Hapus semua folder download
/cancel <job_id> - Batalkan download (soon)

**Contoh Download:**
`/download AMIBEL https://mega.nz/folder/syUExAxI#9LDA5zV_2CpgwDnn0py93w`

Bot akan:
1. Download folder dari link Mega.nz
2. Simpan dengan nama "AMIBEL"
3. Auto-rename semua file media
4. Auto-upload ke platform pilihan
5. Auto-cleanup folder
    """
    
    await update.message.reply_text(help_text)

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle download command"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: /download <nama_folder> <link_mega>\n"
            "Contoh: /download AMIBEL https://mega.nz/folder/abc123#xyz"
        )
        return
    
    folder_name = context.args[0]
    mega_url = context.args[1]
    
    # Validate Mega.nz folder URL
    if not mega_url.startswith('https://mega.nz/folder/'):
        await update.message.reply_text(
            "❌ Link harus berupa folder Mega.nz\n"
            "Contoh: https://mega.nz/folder/abc123#xyz"
        )
        return
    
    # Generate job ID
    job_id = f"dl_{update.effective_user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Add to active downloads
    active_downloads[job_id] = {
        'job_id': job_id,
        'folder_name': folder_name,
        'mega_url': mega_url,
        'user_id': update.effective_user.id,
        'chat_id': update.effective_chat.id,
        'status': DownloadStatus.PENDING,
        'progress': 'Menunggu di antrian',
        'created_at': datetime.now().isoformat(),
        'update': update,
        'context': context
    }
    
    # Add to queue
    download_queue.put(active_downloads[job_id])
    
    # Get queue position
    queue_list = list(download_queue.queue)
    queue_position = queue_list.index(active_downloads[job_id]) + 1 if active_downloads[job_id] in queue_list else 0
    
    await update.message.reply_text(
        f"✅ **Download Ditambahkan ke Antrian**\n\n"
        f"📁 Folder: `{folder_name}`\n"
        f"🔗 Link: {mega_url}\n"
        f"🆔 Job ID: `{job_id}`\n"
        f"📊 Posisi Antrian: #{queue_position + 1}\n"
        f"⚡ Download Aktif: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}",
        parse_mode='Markdown'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system status"""
    # Active downloads
    active_text = "**📥 DOWNLOAD AKTIF:**\n"
    if active_downloads:
        for job_id, job in list(active_downloads.items()):
            status_emoji = {
                DownloadStatus.PENDING: "⏳",
                DownloadStatus.DOWNLOADING: "📥",
                DownloadStatus.RENAMING: "📝",
                DownloadStatus.UPLOADING: "📤",
                DownloadStatus.COMPLETED: "✅",
                DownloadStatus.ERROR: "❌"
            }.get(job['status'], "⏳")
            
            active_text += f"{status_emoji} **{job['folder_name']}**\n"
            active_text += f"   Status: {job['progress']}\n"
            active_text += f"   ID: `{job_id}`\n\n"
    else:
        active_text += "Tidak ada download aktif\n\n"
    
    # Queue
    queue_list = list(download_queue.queue)
    queue_text = "**📊 ANTRIAN:**\n"
    if queue_list:
        for i, job in enumerate(queue_list):
            queue_text += f"#{i+1} {job['folder_name']}\n"
    else:
        queue_text += "Antrian kosong\n"
    
    # System info
    system_text = f"""
**⚙️ SISTEM INFO:**
• Download Aktif: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}
• Dalam Antrian: {download_queue.qsize()}
• Mega.nz CMD: {'✅' if mega_manager.check_mega_cmd() else '❌'}
• Akun Tersedia: {len(mega_manager.accounts)}
• Akun Saat Ini: {mega_manager.get_current_account()['email'] if mega_manager.get_current_account() else 'Tidak ada'}
• TeraboxUploaderCLI: {'✅' if TERABOX_CLI_DIR.exists() else '❌'}
    """
    
    full_text = active_text + "\n" + queue_text + "\n" + system_text
    await update.message.reply_text(full_text, parse_mode='Markdown')

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom prefix for auto-rename"""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: /setprefix <prefix>\n"
            "Contoh: /setprefix 😍\n"
            "Contoh: /setprefix mypic_"
        )
        return
    
    prefix = context.args[0]
    user_id = update.effective_user.id
    
    settings_manager.update_user_settings(user_id, {'prefix': prefix})
    
    await update.message.reply_text(
        f"✅ **Prefix Diubah**\n\n"
        f"Prefix baru: `{prefix}`\n"
        f"Contoh file: `{prefix}pic_0001.jpg`\n"
        f"`{prefix}vid_0001.mp4`",
        parse_mode='Markdown'
    )

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform"""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: /setplatform <terabox|doodstream>\n"
            "Contoh: /setplatform terabox"
        )
        return
    
    platform = context.args[0].lower()
    if platform not in ['terabox', 'doodstream']:
        await update.message.reply_text("❌ Platform harus: terabox atau doodstream")
        return
    
    user_id = update.effective_user.id
    settings_manager.update_user_settings(user_id, {'platform': platform})
    
    await update.message.reply_text(f"✅ Platform upload diubah menjadi: **{platform}**", parse_mode='Markdown')

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    new_auto_upload = not settings.get('auto_upload', True)
    settings_manager.update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "AKTIF" if new_auto_upload else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-upload: **{status}**", parse_mode='Markdown')

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-cleanup"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    new_auto_cleanup = not settings.get('auto_cleanup', True)
    settings_manager.update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "AKTIF" if new_auto_cleanup else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-cleanup: **{status}**", parse_mode='Markdown')

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    settings_text = f"""
⚙️ **PENGATURAN SAYA**

📝 Prefix: `{settings.get('prefix', 'file_')}`
📤 Platform: `{settings.get('platform', 'terabox')}`
🔄 Auto-upload: `{'✅ AKTIF' if settings.get('auto_upload', True) else '❌ NON-AKTIF'}`
🧹 Auto-cleanup: `{'✅ AKTIF' if settings.get('auto_cleanup', True) else '❌ NON-AKTIF'}`
    """
    
    await update.message.reply_text(settings_text, parse_mode='Markdown')

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup all download folders"""
    if not DOWNLOAD_BASE.exists():
        await update.message.reply_text("📁 Tidak ada folder download")
        return
    
    try:
        # Count folders before deletion
        folders = [f for f in DOWNLOAD_BASE.iterdir() if f.is_dir()]
        total_folders = len(folders)
        
        if total_folders == 0:
            await update.message.reply_text("📁 Tidak ada folder download")
            return
        
        # Delete all folders
        for folder in folders:
            shutil.rmtree(folder)
        
        await update.message.reply_text(f"✅ Berhasil menghapus {total_folders} folder download")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error cleanup: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual upload command"""
    if not context.args:
        await update.message.reply_text("❌ Format: /upload <nama_folder>")
        return
    
    folder_name = context.args[0]
    folder_path = DOWNLOAD_BASE / folder_name
    
    if not folder_path.exists():
        await update.message.reply_text(f"❌ Folder '{folder_name}' tidak ditemukan di downloads/")
        return
    
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    platform = settings.get('platform', 'terabox')
    
    # Generate job ID for upload
    job_id = f"up_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Add to active downloads for progress tracking
    active_downloads[job_id] = {
        'job_id': job_id,
        'folder_name': folder_name,
        'user_id': user_id,
        'chat_id': update.effective_chat.id,
        'status': DownloadStatus.UPLOADING,
        'progress': 'Memulai upload',
        'created_at': datetime.now().isoformat(),
        'update': update,
        'context': context
    }
    
    await update.message.reply_text(f"📤 Memulai upload {folder_name} ke {platform}...")
    
    # Perform upload
    if platform == 'terabox':
        links = await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
    else:
        links = await upload_manager.upload_to_doodstream(folder_path, update, context, job_id)
    
    # Cleanup if enabled
    if settings.get('auto_cleanup', True):
        try:
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
                await update.message.reply_text("🧹 Auto-cleanup selesai!")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
    
    # Remove from active downloads
    if job_id in active_downloads:
        del active_downloads[job_id]
    
    if platform == 'terabox':
        await update.message.reply_text(f"✅ Upload selesai! File telah diupload ke Terabox")
    else:
        await update.message.reply_text(f"✅ Upload selesai! {len(links)} links generated")

def main():
    """Start the bot"""
    # Create necessary directories
    DOWNLOAD_BASE.mkdir(exist_ok=True)
    
    # Check Mega.nz installation
    if not mega_manager.check_mega_cmd():
        logger.warning("Mega.nz CMD tidak terpasang! Install dengan: sudo snap install mega-cmd")
    
    # Check if accounts are configured
    if not mega_manager.accounts:
        logger.warning("Tidak ada akun Mega.nz yang dikonfigurasi!")
    
    # Check TeraboxUploaderCLI
    if not TERABOX_CLI_DIR.exists():
        logger.warning("TeraboxUploaderCLI tidak ditemukan! Pastikan sudah di-clone di direktori ini.")
    
    # Initialize bot
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN tidak ditemukan di environment variables!")
        return
    
    application = Application.builder().token(token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("autoupload", auto_upload_toggle))
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    
    # Start bot
    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    main()
