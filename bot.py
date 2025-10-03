#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from queue import Queue
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path
from enum import Enum
import psutil

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)
from dotenv import load_dotenv
import requests
import aiohttp

# Load environment variables first
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg'}
DOWNLOAD_BASE = Path('downloads')
MAX_CONCURRENT_DOWNLOADS = 2

# Global state
download_queue = Queue()
active_downloads: Dict[str, Dict] = {}
completed_downloads: Dict[str, Dict] = {}

class DownloadStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading" 
    DOWNLOAD_COMPLETED = "download_completed"
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
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Failed to load user settings: {e}")
            return {}
    
    def save_settings(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save user settings: {e}")
    
    def get_user_settings(self, user_id: int) -> Dict:
        user_str = str(user_id)
        if user_str not in self.settings:
            self.settings[user_str] = {
                'prefix': 'file_',
                'platform': 'terabox',
                'auto_upload': True,
                'auto_cleanup': True,
                'max_retries': 3,
                'file_type': 'all_ages',
            }
            self.save_settings()
        return self.settings[user_str]
    
    def update_user_settings(self, user_id: int, new_settings: Dict):
        user_str = str(user_id)
        if user_str not in self.settings:
            self.settings[user_str] = {}
        self.settings[user_str].update(new_settings)
        self.save_settings()

class SystemMonitor:
    @staticmethod
    def get_system_status() -> Dict[str, Any]:
        try:
            disk = psutil.disk_usage('/')
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=1)
            
            return {
                'disk_free_gb': disk.free / (1024**3),
                'disk_total_gb': disk.total / (1024**3),
                'disk_used_percent': disk.percent,
                'memory_free_gb': memory.available / (1024**3),
                'memory_used_percent': memory.percent,
                'cpu_used_percent': cpu_percent,
                'active_downloads': len(active_downloads),
                'queue_size': download_queue.qsize(),
            }
        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return {}

class MegaManager:
    def __init__(self):
        self.accounts = self.load_mega_accounts()
        self.current_account_index = 0
        self.mega_get_path = self._get_mega_get_path()
        logger.info(f"MegaManager initialized with {len(self.accounts)} accounts, mega-get path: {self.mega_get_path}")
    
    def _get_mega_get_path(self) -> str:
        """Get the correct path for mega-get command"""
        possible_paths = [
            '/snap/bin/mega-get',
            '/usr/bin/mega-get', 
            '/usr/local/bin/mega-get',
            'mega-get'
        ]
        
        for path in possible_paths:
            try:
                result = subprocess.run(['which', path], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    logger.info(f"Found mega-get at: {path}")
                    return path
            except Exception as e:
                logger.warning(f"Error checking path {path}: {e}")
                continue
        
        logger.error("mega-get not found in any standard paths!")
        return "mega-get"
    
    def load_mega_accounts(self) -> List[Dict]:
        accounts = []
        
        # Try to load from mega_accounts.json first
        try:
            if os.path.exists('mega_accounts.json'):
                with open('mega_accounts.json', 'r', encoding='utf-8') as f:
                    file_accounts = json.load(f)
                    if isinstance(file_accounts, list):
                        accounts.extend(file_accounts)
                        logger.info(f"Loaded {len(file_accounts)} accounts from mega_accounts.json")
        except Exception as e:
            logger.error(f"Error loading mega_accounts.json: {e}")
        
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
        
        if env_accounts:
            logger.info(f"Loaded {len(env_accounts)} accounts from environment variables")
        accounts.extend(env_accounts)
        
        if not accounts:
            logger.warning("No Mega.nz accounts found!")
        else:
            logger.info(f"Total {len(accounts)} Mega.nz accounts available")
        
        return accounts
    
    def check_disk_space(self, required_gb: float = 5.0) -> Tuple[bool, float]:
        try:
            disk = psutil.disk_usage('/')
            free_gb = disk.free / (1024**3)
            has_space = free_gb >= required_gb
            return has_space, free_gb
        except Exception as e:
            logger.error(f"Error checking disk space: {e}")
            return False, 0.0
    
    def download_mega_folder(self, folder_url: str, download_path: Path, job_id: str) -> Tuple[bool, str]:
        logger.info(f"Starting download for job {job_id}")
        
        try:
            # Check disk space
            has_space, free_gb = self.check_disk_space(5.0)
            if not has_space:
                return False, f"Insufficient disk space: {free_gb:.2f}GB free"
            
            # Ensure download directory exists
            download_path.mkdir(parents=True, exist_ok=True)
            
            # Download using mega-get
            cmd = ['mega-get', folder_url, str(download_path)]
            
            logger.info(f"Executing: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            
            if result.returncode == 0:
                # Check if files were downloaded
                files = list(download_path.rglob('*'))
                file_count = len([f for f in files if f.is_file()])
                
                if file_count > 0:
                    return True, f"Download successful! {file_count} files downloaded"
                else:
                    return False, "Download completed but no files found"
            else:
                error_msg = result.stderr or result.stdout
                return False, f"Download failed: {error_msg}"
                
        except subprocess.TimeoutExpired:
            return False, "Download timeout (1 hour)"
        except Exception as e:
            return False, f"Download error: {str(e)}"

class FileManager:
    @staticmethod
    def auto_rename_media_files(folder_path: Path, prefix: str) -> Dict:
        try:
            media_files = []
            for ext in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                media_files.extend(folder_path.rglob(f'*{ext}'))
                media_files.extend(folder_path.rglob(f'*{ext.upper()}'))
            
            media_files = list(set(media_files))
            media_files.sort()
            
            renamed_count = 0
            for number, file_path in enumerate(media_files, 1):
                number_str = f"{number:02d}"
                new_name = f"{prefix} {number_str}{file_path.suffix}"
                new_path = file_path.parent / new_name
                
                try:
                    if file_path != new_path:
                        if new_path.exists():
                            timestamp = int(time.time())
                            new_name = f"{prefix} {number_str}_{timestamp}{file_path.suffix}"
                            new_path = file_path.parent / new_name
                        
                        file_path.rename(new_path)
                        renamed_count += 1
                except Exception as e:
                    logger.error(f"Error renaming {file_path}: {e}")
                    continue
            
            return {'renamed': renamed_count, 'total': len(media_files)}
        except Exception as e:
            logger.error(f"Error in auto_rename: {e}")
            return {'renamed': 0, 'total': 0}

class SimpleUploadManager:
    def __init__(self):
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        logger.info("UploadManager initialized")
    
    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Simplified Terabox upload"""
        logger.info(f"Terabox upload requested for {folder_path}")
        await self.send_progress_message(update, context, job_id, 
            "❌ Terabox upload sedang dalam perbaikan.\n"
            "📤 Silakan gunakan platform lain atau upload manual."
        )
        return []
    
    async def upload_to_doodstream(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload to Doodstream"""
        try:
            await self.send_progress_message(update, context, job_id, "📤 Memulai upload ke Doodstream...")
            
            if not self.doodstream_key:
                await self.send_progress_message(update, context, job_id, "❌ Doodstream API key tidak ditemukan")
                return []
            
            video_files = [f for f in folder_path.rglob('*') if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            
            if not video_files:
                await self.send_progress_message(update, context, job_id, "📭 Tidak ada file video untuk diupload")
                return []
            
            links = []
            for i, file_path in enumerate(video_files, 1):
                if not self.is_job_active(job_id):
                    break
                    
                await self.send_progress_message(update, context, job_id, f"📤 Upload {i}/{len(video_files)}: {file_path.name}")
                
                link = await self.upload_single_file_to_doodstream(file_path)
                if link:
                    links.append(link)
                    logger.info(f"Upload successful: {link}")
            
            if links:
                await self.send_progress_message(update, context, job_id, f"✅ Upload selesai! {len(links)} links")
            else:
                await self.send_progress_message(update, context, job_id, "❌ Tidak ada link yang dihasilkan")
            
            return links
            
        except Exception as e:
            logger.error(f"Doodstream upload error: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
            return []
    
    async def upload_single_file_to_doodstream(self, file_path: Path) -> str:
        """Upload single file to Doodstream"""
        try:
            url = "https://doodstream.com/api/upload"
            
            with open(file_path, 'rb') as f:
                files = {'file': f}
                data = {'key': self.doodstream_key}
                
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3600)) as session:
                    async with session.post(url, data=data, files=files) as response:
                        result = await response.json()
                        logger.info(f"Doodstream API response: {result}")
                        
                        if result.get('success'):
                            download_url = result.get('download_url', '')
                            logger.info(f"Doodstream upload successful: {download_url}")
                            return download_url
                        else:
                            logger.error(f"Doodstream API error: {result}")
                            return ""
        except Exception as e:
            logger.error(f"Doodstream single upload error: {e}")
            return ""
    
    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, message: str):
        """Send progress message"""
        try:
            if job_id not in active_downloads:
                return
                
            chat_id = active_downloads[job_id]['chat_id']
            
            if 'progress_message_id' in active_downloads[job_id]:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=active_downloads[job_id]['progress_message_id'],
                        text=f"{active_downloads[job_id]['folder_name']}\n{message}"
                    )
                    return
                except Exception:
                    pass
            
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{active_downloads[job_id]['folder_name']}\n{message}"
            )
            active_downloads[job_id]['progress_message_id'] = msg.message_id
            
        except Exception as e:
            logger.error(f"Error sending progress message: {e}")
    
    def is_job_active(self, job_id: str) -> bool:
        return job_id in active_downloads and active_downloads[job_id]['status'] != DownloadStatus.COMPLETED

# TAMBAHKAN CLASS DownloadProcessor YANG HILANG
class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, file_manager: FileManager, upload_manager: SimpleUploadManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.settings_manager = settings_manager
        self.processing = False
        self.current_processes = 0
        logger.info("DownloadProcessor initialized")
    
    def start_processing(self):
        """Start processing download queue"""
        if not self.processing:
            self.processing = True
            thread = threading.Thread(target=self._process_queue, daemon=True)
            thread.start()
            logger.info("Download processor started")
    
    def _process_queue(self):
        """Process download queue continuously"""
        logger.info("Queue processor thread started")
        while self.processing:
            try:
                if self.current_processes < MAX_CONCURRENT_DOWNLOADS and not download_queue.empty():
                    job_data = download_queue.get()
                    if job_data:
                        self.current_processes += 1
                        logger.info(f"Starting new download process, current: {self.current_processes}/{MAX_CONCURRENT_DOWNLOADS}")
                        threading.Thread(
                            target=self._process_single_download,
                            args=(job_data,),
                            daemon=True
                        ).start()
                
                time.sleep(5)
            except Exception as e:
                logger.error(f"Error in queue processor: {e}")
                time.sleep(10)
    
    def _process_single_download(self, job_data: Dict):
        """Process single download job"""
        logger.info(f"Starting single download process for job {job_data['job_id']}")
        asyncio.run(self._async_process_single_download(job_data))
    
    async def _async_process_single_download(self, job_data: Dict):
        """Async version of single download processing"""
        job_id = job_data['job_id']
        folder_name = job_data['folder_name']
        mega_url = job_data['mega_url']
        user_id = job_data['user_id']
        update = job_data['update']
        context = job_data['context']
        
        logger.info(f"Processing download job {job_id} for user {user_id}")
        
        try:
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOADING
            await self.upload_manager.send_progress_message(update, context, job_id, "📥 Memulai download dari Mega.nz...")
            
            # Download from Mega.nz
            success, message = self.mega_manager.download_mega_folder(mega_url, DOWNLOAD_BASE, job_id)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                await self.upload_manager.send_progress_message(update, context, job_id, f"❌ Download gagal: {message}")
                return
            
            # Find downloaded folder
            download_folders = [d for d in DOWNLOAD_BASE.iterdir() if d.is_dir()]
            target_folder = None
            
            if download_folders:
                download_folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                target_folder = download_folders[0]
                logger.info(f"Found download folder: {target_folder}")
            else:
                target_folder = DOWNLOAD_BASE
                logger.info(f"Using base directory: {target_folder}")
            
            # Count files
            files = list(target_folder.rglob('*'))
            file_count = len([f for f in files if f.is_file()])
            
            await self.upload_manager.send_progress_message(update, context, job_id, f"✅ Download selesai! {file_count} files downloaded")
            
            # Auto-rename files
            active_downloads[job_id]['status'] = DownloadStatus.RENAMING
            user_settings = self.settings_manager.get_user_settings(user_id)
            prefix = user_settings.get('prefix', 'file_')
            
            rename_result = self.file_manager.auto_rename_media_files(target_folder, prefix)
            await self.upload_manager.send_progress_message(update, context, job_id, f"📝 Rename selesai: {rename_result['renamed']}/{rename_result['total']} files")
            
            # Auto-upload if enabled
            if user_settings.get('auto_upload', True):
                active_downloads[job_id]['status'] = DownloadStatus.UPLOADING
                platform = user_settings.get('platform', 'terabox')
                
                await self.upload_manager.send_progress_message(update, context, job_id, f"📤 Uploading ke {platform}...")
                
                if platform == 'terabox':
                    links = await self.upload_manager.upload_to_terabox(target_folder, update, context, job_id)
                else:
                    links = await self.upload_manager.upload_to_doodstream(target_folder, update, context, job_id)
            
            # Auto-cleanup if enabled
            if user_settings.get('auto_cleanup', True):
                try:
                    if target_folder.exists() and target_folder != DOWNLOAD_BASE:
                        shutil.rmtree(target_folder)
                        await self.upload_manager.send_progress_message(update, context, job_id, "🧹 Auto-cleanup selesai!")
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
            
            # Mark as completed
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['completed_at'] = datetime.now().isoformat()
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            await self.upload_manager.send_progress_message(update, context, job_id, "✅ Semua proses selesai!")
            
        except Exception as e:
            logger.error(f"Error processing download {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['error'] = str(e)
            await self.upload_manager.send_progress_message(update, context, job_id, f"❌ Error: {str(e)}")
        
        finally:
            self.current_processes -= 1

# Initialize managers
logger.info("Initializing managers...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = SimpleUploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    user = update.effective_user
    welcome_text = f"""
🤖 Mega Downloader Bot

Halo {user.first_name}!

Fitur:
📥 Download folder dari Mega.nz
🔄 Auto-rename file media  
📤 Upload ke Doodstream
⚙️ Customizable settings

Commands:
/download <url> - Download folder Mega.nz
/status - Lihat status download
/mysettings - Lihat pengaturan
/setprefix <prefix> - Set file prefix
/setplatform <doodstream> - Set platform upload
/autoupload <on|off> - Toggle auto upload
/autocleanup <on|off> - Toggle auto cleanup
/debug - Info debug system

Contoh: /download https://mega.nz/folder/abc123
    """
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = """
📖 Bantuan Mega Downloader Bot

Cara penggunaan:
1. Kirim command /download diikuti URL folder Mega.nz
2. Bot akan otomatis mendownload, rename, dan upload file
3. Pantau progress melalui status message

Contoh commands:
/download https://mega.nz/folder/abc123
/setprefix my_files
/setplatform doodstream
/autoupload on
/status
    """
    await update.message.reply_text(help_text)

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /download command"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Harap sertakan URL Mega.nz\nContoh: /download https://mega.nz/folder/abc123")
            return
        
        mega_url = context.args[0]
        
        # Validate Mega.nz URL
        if not re.match(r'https://mega\.nz/folder/[a-zA-Z0-9_-]+', mega_url):
            await update.message.reply_text("❌ URL Mega.nz tidak valid!")
            return
        
        # Generate job ID
        job_id = f"job_{int(time.time())}_{update.effective_user.id}"
        folder_name = f"Folder_{int(time.time())}"
        
        # Add to download queue
        job_data = {
            'job_id': job_id,
            'folder_name': folder_name,
            'mega_url': mega_url,
            'user_id': update.effective_user.id,
            'chat_id': update.effective_chat.id,
            'update': update,
            'context': context,
            'created_at': datetime.now().isoformat()
        }
        
        # Initialize active download
        active_downloads[job_id] = {
            'job_id': job_id,
            'folder_name': folder_name,
            'mega_url': mega_url,
            'user_id': update.effective_user.id,
            'chat_id': update.effective_chat.id,
            'status': DownloadStatus.PENDING,
            'progress': 'Menunggu dalam antrian...',
            'created_at': datetime.now().isoformat()
        }
        
        download_queue.put(job_data)
        
        # Send confirmation
        user_settings = settings_manager.get_user_settings(update.effective_user.id)
        platform = user_settings.get('platform', 'doodstream')
        auto_upload = user_settings.get('auto_upload', True)
        
        response_text = (
            f"✅ Download Job Ditambahkan\n\n"
            f"📁 Folder: {folder_name}\n"
            f"🔗 URL: {mega_url}\n"
            f"🆔 Job ID: {job_id}\n"
            f"📊 Antrian: {download_queue.qsize() + 1}\n\n"
            f"⚙️ Pengaturan:\n"
            f"• Platform: {platform}\n"
            f"• Auto Upload: {'✅' if auto_upload else '❌'}\n\n"
            f"Gunakan /status untuk memantau progress."
        )
        
        await update.message.reply_text(response_text)
        logger.info(f"Added download job {job_id} for user {update.effective_user.id}")
        
    except Exception as e:
        logger.error(f"Error in download_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current download status"""
    try:
        user_id = update.effective_user.id
        
        # Filter jobs by user
        user_active_jobs = {k: v for k, v in active_downloads.items() if v['user_id'] == user_id}
        user_completed_jobs = {k: v for k, v in completed_downloads.items() if v['user_id'] == user_id}
        
        system_status = SystemMonitor.get_system_status()
        
        status_text = f"""
📊 System Status
💾 Disk: {system_status.get('disk_free_gb', 0):.1f}GB free
🔄 Active Downloads: {system_status.get('active_downloads', 0)}
📋 Queue Size: {system_status.get('queue_size', 0)}

👤 Your Jobs
⏳ Active: {len(user_active_jobs)}
✅ Completed: {len(user_completed_jobs)}
"""
        
        if user_active_jobs:
            status_text += "\nActive Jobs:\n"
            for job_id, job in list(user_active_jobs.items())[:3]:
                status_text += f"📁 {job['folder_name']}\n"
                status_text += f"📊 {job['status'].value}\n"
                status_text += f"⏰ {job.get('progress', 'Processing...')}\n\n"
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"Error in status_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set file prefix for user"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Harap sertakan prefix\nContoh: /setprefix my_files")
            return
        
        prefix = context.args[0]
        user_id = update.effective_user.id
        settings_manager.update_user_settings(user_id, {'prefix': prefix})
        await update.message.reply_text(f"✅ Prefix berhasil diubah menjadi: {prefix}")
        
    except Exception as e:
        logger.error(f"Error in set_prefix: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform for user"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Harap sertakan platform\nContoh: /setplatform doodstream")
            return
        
        platform = context.args[0].lower()
        if platform not in ['doodstream']:  # Hanya doodstream yang tersedia untuk sekarang
            await update.message.reply_text("❌ Platform tidak valid! Hanya 'doodstream' yang tersedia.")
            return
        
        user_id = update.effective_user.id
        settings_manager.update_user_settings(user_id, {'platform': platform})
        await update.message.reply_text(f"✅ Platform upload berhasil diubah ke: {platform}")
        
    except Exception as e:
        logger.error(f"Error in set_platform: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto upload setting"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Harap sertakan on/off\nContoh: /autoupload on")
            return
        
        toggle = context.args[0].lower()
        if toggle not in ['on', 'off']:
            await update.message.reply_text("❌ Pilihan: on atau off")
            return
        
        user_id = update.effective_user.id
        auto_upload = toggle == 'on'
        settings_manager.update_user_settings(user_id, {'auto_upload': auto_upload})
        
        status = "AKTIF" if auto_upload else "NON-AKTIF"
        await update.message.reply_text(f"✅ Auto upload diubah menjadi: {status}")
        
    except Exception as e:
        logger.error(f"Error in auto_upload_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto cleanup setting"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Harap sertakan on/off\nContoh: /autocleanup on")
            return
        
        toggle = context.args[0].lower()
        if toggle not in ['on', 'off']:
            await update.message.reply_text("❌ Pilihan: on atau off")
            return
        
        user_id = update.effective_user.id
        auto_cleanup = toggle == 'on'
        settings_manager.update_user_settings(user_id, {'auto_cleanup': auto_cleanup})
        
        status = "AKTIF" if auto_cleanup else "NON-AKTIF"
        await update.message.reply_text(f"✅ Auto cleanup diubah menjadi: {status}")
        
    except Exception as e:
        logger.error(f"Error in auto_cleanup_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings"""
    try:
        user_id = update.effective_user.id
        settings = settings_manager.get_user_settings(user_id)
        
        settings_text = f"""
⚙️ Pengaturan Anda

📝 Prefix: {settings.get('prefix', 'file_')}
📤 Platform: {settings.get('platform', 'doodstream')}
🔄 Auto Upload: {'✅' if settings.get('auto_upload', True) else '❌'}
🧹 Auto Cleanup: {'✅' if settings.get('auto_cleanup', True) else '❌'}
🔄 Max Retries: {settings.get('max_retries', 3)}

Commands untuk mengubah:
/setprefix <prefix> - Ubah file prefix
/setplatform <doodstream> - Ubah platform
/autoupload <on|off> - Toggle auto upload  
/autocleanup <on|off> - Toggle auto cleanup
        """
        
        await update.message.reply_text(settings_text)
        
    except Exception as e:
        logger.error(f"Error in my_settings: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show debug information"""
    try:
        system_status = SystemMonitor.get_system_status()
        
        debug_text = f"""
🐛 Debug Information

System Resources:
💾 Disk Free: {system_status.get('disk_free_gb', 0):.1f}GB
💾 Disk Used: {system_status.get('disk_used_percent', 0):.1f}%
🧠 Memory Used: {system_status.get('memory_used_percent', 0):.1f}%
⚡ CPU Used: {system_status.get('cpu_used_percent', 0):.1f}%

Bot Status:
🔄 Active Downloads: {system_status.get('active_downloads', 0)}
📋 Queue Size: {system_status.get('queue_size', 0)}

Mega.nz Status:
✅ mega-get Available: {os.path.exists('/usr/bin/mega-get')}
📧 Accounts: {len(mega_manager.accounts)}
        """
        
        await update.message.reply_text(debug_text)
        
    except Exception as e:
        logger.error(f"Error in debug_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    """Start the bot"""
    logger.info("Starting Mega Downloader Bot...")
    
    # Create base download directory
    DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
    logger.info(f"Base download directory: {DOWNLOAD_BASE}")
    
    # Check if mega-get is available
    if not os.path.exists('/usr/bin/mega-get'):
        logger.error("mega-get is not available! Please install mega-cmd")
        return
    
    # Check if accounts are configured
    if not mega_manager.accounts:
        logger.error("No Mega.nz accounts configured!")
        return
    
    # Initialize bot
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN not found in environment variables!")
        return
    
    application = Application.builder().token(token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("autoupload", auto_upload_toggle))
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    application.add_handler(CommandHandler("debug", debug_command))
    
    # Start bot
    logger.info("Bot started successfully!")
    application.run_polling()

if __name__ == '__main__':
    main()
