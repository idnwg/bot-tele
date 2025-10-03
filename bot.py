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
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from enum import Enum

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables
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
DOWNLOAD_BASE = Path('downloads')
MAX_CONCURRENT_DOWNLOADS = 2

# Global state
download_queue = Queue()
active_downloads: Dict[str, Dict] = {}
completed_downloads: Dict[str, Dict] = {}

class DownloadStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
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
                'auto_cleanup': True,
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
        self.mega_get_path = self._find_mega_get()
        logger.info(f"MegaManager initialized with mega-get path: {self.mega_get_path}")
    
    def _find_mega_get(self) -> str:
        """Find mega-get executable"""
        # Check common paths
        paths = [
            '/snap/bin/mega-get',
            '/usr/bin/mega-get',
            '/usr/local/bin/mega-get'
        ]
        
        for path in paths:
            if os.path.exists(path):
                logger.info(f"Found mega-get at: {path}")
                return path
        
        # Try using which command
        try:
            result = subprocess.run(['which', 'mega-get'], capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip()
                logger.info(f"Found mega-get via which: {path}")
                return path
        except:
            pass
        
        logger.error("mega-get not found in any standard paths!")
        return 'mega-get'  # Fallback
    
    def download_mega_folder(self, folder_url: str, download_path: Path, job_id: str) -> Tuple[bool, str]:
        logger.info(f"Starting download for job {job_id}")
        
        try:
            # Ensure download directory exists
            download_path.mkdir(parents=True, exist_ok=True)
            
            # Download using mega-get
            cmd = [self.mega_get_path, folder_url, str(download_path)]
            
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

class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
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
            await self.send_progress_message(update, context, job_id, "📥 Memulai download dari Mega.nz...")
            
            # Download from Mega.nz
            success, message = self.mega_manager.download_mega_folder(mega_url, DOWNLOAD_BASE, job_id)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                await self.send_progress_message(update, context, job_id, f"❌ Download gagal: {message}")
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
            
            await self.send_progress_message(update, context, job_id, f"✅ Download selesai! {file_count} files downloaded")
            
            # Auto-cleanup if enabled
            user_settings = self.settings_manager.get_user_settings(user_id)
            if user_settings.get('auto_cleanup', True):
                try:
                    if target_folder.exists() and target_folder != DOWNLOAD_BASE:
                        shutil.rmtree(target_folder)
                        await self.send_progress_message(update, context, job_id, "🧹 Auto-cleanup selesai!")
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
            
            # Mark as completed
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['completed_at'] = datetime.now().isoformat()
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            await self.send_progress_message(update, context, job_id, "✅ Semua proses selesai!")
            
        except Exception as e:
            logger.error(f"Error processing download {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['error'] = str(e)
            await self.send_progress_message(update, context, job_id, f"❌ Error: {str(e)}")
        
        finally:
            self.current_processes -= 1
    
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

# Initialize managers
logger.info("Initializing managers...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
download_processor = DownloadProcessor(mega_manager, settings_manager)

# Start download processor
download_processor.start_processing()

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    user = update.effective_user
    welcome_text = f"""
🤖 Mega Downloader Bot

Halo {user.first_name}!

Saya adalah bot untuk mendownload folder dari Mega.nz.

Fitur:
📥 Download folder dari Mega.nz
🧹 Auto-cleanup file setelah download

Commands:
/download <url> - Download folder Mega.nz
/status - Lihat status download
/mysettings - Lihat pengaturan
/autocleanup <on|off> - Toggle auto cleanup

Contoh: /download https://mega.nz/folder/abc123
    """
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = """
📖 Bantuan Mega Downloader Bot

Cara penggunaan:
1. Kirim command /download diikuti URL folder Mega.nz
2. Bot akan otomatis mendownload folder
3. Pantau progress melalui status message

Contoh commands:
/download https://mega.nz/folder/abc123
/autocleanup on
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
        auto_cleanup = user_settings.get('auto_cleanup', True)
        
        response_text = (
            f"✅ Download Job Ditambahkan\n\n"
            f"📁 Folder: {folder_name}\n"
            f"🔗 URL: {mega_url}\n"
            f"🆔 Job ID: {job_id}\n"
            f"📊 Antrian: {download_queue.qsize() + 1}\n\n"
            f"⚙️ Pengaturan:\n"
            f"• Auto Cleanup: {'✅' if auto_cleanup else '❌'}\n\n"
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
        
        status_text = f"""
📊 Status

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

🧹 Auto Cleanup: {'✅' if settings.get('auto_cleanup', True) else '❌'}

Commands untuk mengubah:
/autocleanup <on|off> - Toggle auto cleanup
        """
        
        await update.message.reply_text(settings_text)
        
    except Exception as e:
        logger.error(f"Error in my_settings: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    """Start the bot"""
    logger.info("Starting Mega Downloader Bot...")
    
    # Create base download directory
    DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
    logger.info(f"Base download directory: {DOWNLOAD_BASE}")
    
    # Check if mega-get is available
    if not os.path.exists(mega_manager.mega_get_path):
        logger.warning(f"mega-get not found at {mega_manager.mega_get_path}. Please install mega-cmd.")
    
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
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    
    # Start bot
    logger.info("Bot started successfully!")
    application.run_polling()

if __name__ == '__main__':
    main()
