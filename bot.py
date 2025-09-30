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
from threading import Thread, Lock
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

# Global state
download_queue = Queue()
upload_queue = Queue()
active_jobs = {}
user_progress_messages = {}
account_manager = None
current_processor = None

class AccountStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    RENAMING = "renaming"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    ERROR = "error"

class AccountManager:
    def __init__(self):
        self.accounts_file = 'mega_accounts.json'
        self.account_status = {}  # INISIALISASI DI SINI SEBELUM load_accounts
        self.accounts = self.load_accounts()
        self.current_account_index = 0
        self.processing = False
        self.lock = Lock()
        self.current_platform = 'terabox'
        self.current_prefix = 'file_'
        
    def load_accounts(self) -> List[Dict]:
        try:
            with open(self.accounts_file, 'r') as f:
                accounts = json.load(f)
                # Initialize status for each account
                for i, account in enumerate(accounts):
                    self.account_status[i] = {
                        'status': AccountStatus.PENDING,
                        'progress': 'Menunggu',
                        'folders_downloaded': 0,
                        'files_renamed': 0,
                        'files_uploaded': 0,
                        'error': None
                    }
                return accounts
        except FileNotFoundError:
            logger.error(f"File {self.accounts_file} tidak ditemukan!")
            return []
        except Exception as e:
            logger.error(f"Error loading accounts: {e}")
            return []
    
    def save_accounts(self):
        with open(self.accounts_file, 'w') as f:
            json.dump(self.accounts, f, indent=4)
    
    def get_current_account(self) -> Optional[Dict]:
        with self.lock:
            if self.accounts and 0 <= self.current_account_index < len(self.accounts):
                return self.accounts[self.current_account_index]
            return None
    
    def get_next_account(self) -> Optional[Dict]:
        with self.lock:
            self.current_account_index += 1
            if self.current_account_index < len(self.accounts):
                return self.accounts[self.current_account_index]
            return None
    
    def reset_accounts(self):
        with self.lock:
            self.current_account_index = 0
            for i in range(len(self.accounts)):
                self.account_status[i] = {
                    'status': AccountStatus.PENDING,
                    'progress': 'Menunggu',
                    'folders_downloaded': 0,
                    'files_renamed': 0,
                    'files_uploaded': 0,
                    'error': None
                }
    
    def update_account_status(self, account_index: int, status: AccountStatus, progress: str = None, error: str = None):
        with self.lock:
            if account_index in self.account_status:
                self.account_status[account_index]['status'] = status
                if progress:
                    self.account_status[account_index]['progress'] = progress
                if error:
                    self.account_status[account_index]['error'] = error
    
    def get_account_status(self, account_index: int) -> Dict:
        with self.lock:
            return self.account_status.get(account_index, {})
    
    def get_all_status(self) -> Dict:
        with self.lock:
            return self.account_status.copy()

class MegaManager:
    def __init__(self):
        self.cred_file = 'mega_session.json'
    
    def check_mega_cmd(self) -> bool:
        try:
            result = subprocess.run(['mega-cmd', '--version'], 
                                  capture_output=True, text=True)
            return result.returncode == 0
        except FileNotFoundError:
            return False
    
    def login_to_mega(self, email: str, password: str) -> Tuple[bool, str]:
        try:
            # Logout dulu untuk memastikan session bersih
            subprocess.run('mega-logout', shell=True, capture_output=True, text=True)
            
            cmd = f'mega-login "{email}" "{password}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
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
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def logout_mega(self) -> Tuple[bool, str]:
        try:
            result = subprocess.run('mega-logout', shell=True, capture_output=True, text=True)
            
            # Clear session file
            if os.path.exists(self.cred_file):
                os.remove(self.cred_file)
            
            return True, "Logout berhasil!"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def ensure_mega_session(self) -> bool:
        try:
            # Check if session exists
            result = subprocess.run('mega-whoami', shell=True, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False
    
    def list_mega_folders(self) -> Tuple[bool, List[str]]:
        try:
            if not self.ensure_mega_session():
                return False, ["Session tidak valid. Silakan login ulang."]
            
            result = subprocess.run('mega-ls', shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                folders = [line.strip() for line in result.stdout.split('\n') if line.strip() and not line.startswith('//')]
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
            
            # Escape folder name untuk handle spasi dan karakter khusus
            escaped_folder_name = f'"{folder_name}"'
            cmd = f'mega-get {escaped_folder_name} "{download_path}"'
            
            logger.info(f"Executing: {cmd}")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600)  # 1 hour timeout
            
            if result.returncode == 0:
                return True, f"Download {folder_name} berhasil!"
            else:
                return False, f"Download gagal: {result.stderr}"
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
    
    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Upload files to Terabox using teraboxcli"""
        try:
            await self.send_progress_message(update, context, "📤 Memulai upload ke Terabox...")
            
            if not TERABOX_CLI_PATH.exists():
                await self.send_progress_message(update, context, "❌ Terabox CLI tidak ditemukan!")
                return []
            
            links = []
            files = list(folder_path.rglob('*'))
            total_files = len([f for f in files if f.is_file()])
            uploaded_count = 0
            
            for file_path in files:
                if file_path.is_file():
                    try:
                        cmd = ['python3', str(TERABOX_CLI_PATH), 'upload', str(file_path)]
                        result = subprocess.run(cmd, capture_output=True, text=True)
                        
                        if result.returncode == 0:
                            # Extract link from output
                            link_match = re.search(r'https?://[^\s]+', result.stdout)
                            if link_match:
                                links.append(link_match.group())
                                uploaded_count += 1
                                await self.send_progress_message(
                                    update, context, 
                                    f"📤 Upload progress: {uploaded_count}/{total_files}\n✅ {file_path.name}"
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
            video_files = [f for f in folder_path.rglob('*') 
                          if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            total_files = len(video_files)
            uploaded_count = 0
            
            for file_path in video_files:
                try:
                    link = await self.upload_single_file_to_doodstream(file_path)
                    if link:
                        links.append(link)
                        uploaded_count += 1
                        await self.send_progress_message(
                            update, context, 
                            f"📤 Upload progress: {uploaded_count}/{total_files}\n✅ {file_path.name}"
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

class AccountProcessor:
    def __init__(self, account_manager: AccountManager, mega_manager: MegaManager, 
                 file_manager: FileManager, upload_manager: UploadManager):
        self.account_manager = account_manager
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.is_processing = False
        self.current_update = None
        self.current_context = None
    
    async def start_processing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start processing all accounts sequentially"""
        if self.is_processing:
            await update.message.reply_text("⚠️ Proses sedang berjalan! Tunggu sampai selesai.")
            return
        
        self.is_processing = True
        self.current_update = update
        self.current_context = context
        self.account_manager.reset_accounts()
        
        await self.send_progress("🚀 Memulai proses untuk semua akun...")
        
        # Start processing in background
        import threading
        thread = threading.Thread(target=self._process_all_accounts)
        thread.daemon = True
        thread.start()
    
    def _process_all_accounts(self):
        """Process all accounts sequentially (run in thread)"""
        asyncio.run(self._async_process_all_accounts())
    
    async def _async_process_all_accounts(self):
        """Async version of account processing"""
        try:
            accounts = self.account_manager.accounts
            total_accounts = len(accounts)
            
            for account_index, account in enumerate(accounts):
                if not self.is_processing:
                    break
                    
                await self._process_single_account(account_index, account, total_accounts)
            
            if self.is_processing:
                await self.send_progress("✅ Semua akun telah diproses selesai!")
            
            self.is_processing = False
            
        except Exception as e:
            logger.error(f"Error in account processing: {e}")
            await self.send_progress(f"❌ Error dalam proses: {str(e)}")
            self.is_processing = False
    
    async def _process_single_account(self, account_index: int, account: Dict, total_accounts: int):
        """Process single account: login → download all → rename → upload → logout"""
        try:
            email = account['email']
            password = account['password']
            
            # Update status
            self.account_manager.update_account_status(
                account_index, AccountStatus.DOWNLOADING, 
                f"Login ke akun {email}"
            )
            
            await self.send_progress(
                f"🔐 **Akun {account_index + 1}/{total_accounts}**\n"
                f"Login: {email}\n"
                f"Status: Memulai proses..."
            )
            
            # Login to Mega.nz
            success, message = self.mega_manager.login_to_mega(email, password)
            if not success:
                self.account_manager.update_account_status(
                    account_index, AccountStatus.ERROR, error=message
                )
                await self.send_progress(f"❌ Login gagal untuk {email}: {message}")
                return
            
            await self.send_progress(f"✅ Login berhasil: {email}")
            
            # Get list of folders
            self.account_manager.update_account_status(
                account_index, AccountStatus.DOWNLOADING, "Mendapatkan daftar folder"
            )
            
            success, folders = self.mega_manager.list_mega_folders()
            if not success:
                self.account_manager.update_account_status(
                    account_index, AccountStatus.ERROR, error="Gagal mendapatkan daftar folder"
                )
                await self.send_progress(f"❌ Gagal mendapatkan daftar folder untuk {email}")
                self.mega_manager.logout_mega()
                return
            
            if not folders:
                await self.send_progress(f"📂 Tidak ada folder ditemukan di akun {email}")
                self.mega_manager.logout_mega()
                self.account_manager.update_account_status(
                    account_index, AccountStatus.COMPLETED, "Tidak ada folder"
                )
                return
            
            await self.send_progress(
                f"📂 Ditemukan {len(folders)} folder di akun {email}\n"
                f"Memulai download..."
            )
            
            # Process each folder
            total_folders = len(folders)
            for folder_index, folder_name in enumerate(folders, 1):
                if not self.is_processing:
                    break
                    
                await self._process_folder(account_index, folder_name, folder_index, total_folders, email)
            
            # Logout after processing all folders
            if self.is_processing:
                self.mega_manager.logout_mega()
                self.account_manager.update_account_status(
                    account_index, AccountStatus.COMPLETED, "Proses selesai"
                )
                await self.send_progress(f"✅ Selesai memproses akun {email}")
                
        except Exception as e:
            logger.error(f"Error processing account {account_index}: {e}")
            self.account_manager.update_account_status(
                account_index, AccountStatus.ERROR, error=str(e)
            )
            await self.send_progress(f"❌ Error memproses akun: {str(e)}")
            try:
                self.mega_manager.logout_mega()
            except:
                pass
    
    async def _process_folder(self, account_index: int, folder_name: str, folder_index: int, 
                            total_folders: int, email: str):
        """Process single folder: download → rename → upload"""
        try:
            # Create download path
            download_path = DOWNLOAD_BASE / f"account_{account_index}" / f"folder_{folder_index}"
            
            # Download folder
            self.account_manager.update_account_status(
                account_index, AccountStatus.DOWNLOADING, 
                f"Download folder {folder_index}/{total_folders}: {folder_name}"
            )
            
            await self.send_progress(
                f"📥 **Download Progress**\n"
                f"Akun: {email}\n"
                f"Folder: {folder_name} ({folder_index}/{total_folders})\n"
                f"Status: Downloading..."
            )
            
            success, message = self.mega_manager.download_mega_folder(folder_name, download_path)
            if not success:
                await self.send_progress(f"❌ Download gagal: {folder_name}\nError: {message}")
                return
            
            # Rename files
            self.account_manager.update_account_status(
                account_index, AccountStatus.RENAMING, 
                f"Renaming files di {folder_name}"
            )
            
            await self.send_progress(f"📝 Renaming files di folder: {folder_name}")
            
            prefix = self.account_manager.current_prefix
            rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
            
            # Update account status with rename counts
            status = self.account_manager.get_account_status(account_index)
            status['files_renamed'] += rename_result['photos'] + rename_result['videos']
            status['folders_downloaded'] += 1
            
            await self.send_progress(
                f"✅ Rename selesai:\n"
                f"📷 Foto: {rename_result['photos']} files\n"
                f"🎥 Video: {rename_result['videos']} files"
            )
            
            # Upload files
            self.account_manager.update_account_status(
                account_index, AccountStatus.UPLOADING, 
                f"Upload folder {folder_name}"
            )
            
            platform = self.account_manager.current_platform
            await self.send_progress(f"📤 Upload ke {platform}: {folder_name}")
            
            if platform == 'terabox':
                links = await self.upload_manager.upload_to_terabox(
                    download_path, self.current_update, self.current_context
                )
            else:
                links = await self.upload_manager.upload_to_doodstream(
                    download_path, self.current_update, self.current_context
                )
            
            # Update account status with upload counts
            status = self.account_manager.get_account_status(account_index)
            status['files_uploaded'] += len(links)
            
            await self.send_progress(
                f"✅ Upload selesai: {folder_name}\n"
                f"🔗 {len(links)} links generated"
            )
            
            # Cleanup
            if os.getenv('AUTO_CLEANUP', 'true').lower() == 'true':
                try:
                    shutil.rmtree(download_path)
                    await self.send_progress(f"🧹 Folder dihapus: {folder_name}")
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
            
            await self.send_progress(f"✅ Folder selesai: {folder_name}")
            
        except Exception as e:
            logger.error(f"Error processing folder {folder_name}: {e}")
            await self.send_progress(f"❌ Error memproses folder {folder_name}: {str(e)}")
    
    async def send_progress(self, message: str):
        """Send progress message"""
        if self.current_update and self.current_context:
            await self.upload_manager.send_progress_message(
                self.current_update, self.current_context, message
            )
    
    def stop_processing(self):
        """Stop the processing"""
        self.is_processing = False
        return True

# Initialize managers
def initialize_managers():
    global account_manager, mega_manager, file_manager, upload_manager, account_processor
    account_manager = AccountManager()
    mega_manager = MegaManager()
    file_manager = FileManager()
    upload_manager = UploadManager()
    account_processor = AccountProcessor(account_manager, mega_manager, file_manager, upload_manager)

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    
    # Check Mega.nz availability
    mega_available = mega_manager.check_mega_cmd()
    mega_status = "✅ Terpasang" if mega_available else "❌ Tidak terpasang"
    
    # Count accounts
    account_count = len(account_manager.accounts)
    
    welcome_text = f"""
🤖 **Mega Multi-Account Processor Bot**

**System Status:**
- Mega.nz CMD: {mega_status}
- Akun Tersedia: {account_count} akun
- Proses Berjalan: {'✅ Ya' if account_processor.is_processing else '❌ Tidak'}

**Fitur Baru:**
🔐 Process multiple Mega.nz accounts sequentially
📥 Download semua folder dari setiap akun
📝 Auto-rename file media
📤 Upload ke Terabox/Doodstream
🔄 Process berurutan akun 1 → 2 → 3 → ...

**Perintah:**
/startdownload - Mulai proses semua akun
/stopdownload - Hentikan proses
/status - Status detail proses
/setprefix - Set prefix rename
/setplatform - Pilih platform upload
/accountinfo - Info akun yang tersedia
/cleanup - Hapus folder download

Gunakan /help untuk bantuan lengkap!
    """
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = """
📚 **Daftar Perintah:**

**PROCESS COMMANDS**
/startdownload - Mulai proses SEMUA akun berurutan
/stopdownload - Hentikan proses yang berjalan
/status - Status detail proses & akun

**SETTINGS COMMANDS**
/setprefix <prefix> - Set custom prefix untuk rename
/setplatform <terabox|doodstream> - Pilih platform upload

**INFO COMMANDS**
/accountinfo - Lihat daftar akun yang tersedia
/currentaccount - Akun yang sedang diproses

**MAINTENANCE**
/cleanup - Hapus semua folder download
/start - Status sistem

**WORKFLOW:**
1. Bot login ke akun 1
2. Download semua folder di akun 1
3. Rename semua file media
4. Upload ke platform pilihan
5. Logout dari akun 1
6. Lanjut ke akun 2, dan seterusnya...
    """
    
    await update.message.reply_text(help_text)

async def start_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start processing all accounts"""
    if not account_manager.accounts:
        await update.message.reply_text("❌ Tidak ada akun yang dikonfigurasi! Periksa mega_accounts.json")
        return
    
    await account_processor.start_processing(update, context)

async def stop_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the current processing"""
    if not account_processor.is_processing:
        await update.message.reply_text("❌ Tidak ada proses yang berjalan!")
        return
    
    success = account_processor.stop_processing()
    if success:
        await update.message.reply_text("⏹️ Proses dihentikan!")
    else:
        await update.message.reply_text("❌ Gagal menghentikan proses!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed status of all accounts"""
    if not account_manager.accounts:
        await update.message.reply_text("❌ Tidak ada akun yang dikonfigurasi!")
        return
    
    status_text = "📊 **Status Detail Proses**\n\n"
    
    # Overall status
    status_text += f"**Overall Status:**\n"
    status_text += f"• Proses Aktif: {'✅ YA' if account_processor.is_processing else '❌ TIDAK'}\n"
    status_text += f"• Total Akun: {len(account_manager.accounts)}\n"
    status_text += f"• Platform: {account_manager.current_platform.upper()}\n"
    status_text += f"• Prefix: {account_manager.current_prefix}\n\n"
    
    # Account details
    all_status = account_manager.get_all_status()
    for i, account in enumerate(account_manager.accounts):
        acc_status = all_status.get(i, {})
        status_emoji = {
            AccountStatus.PENDING: "⏳",
            AccountStatus.DOWNLOADING: "📥",
            AccountStatus.RENAMING: "📝", 
            AccountStatus.UPLOADING: "📤",
            AccountStatus.COMPLETED: "✅",
            AccountStatus.ERROR: "❌"
        }.get(acc_status.get('status', AccountStatus.PENDING), "⏳")
        
        status_text += f"**Akun {i+1}: {account['email']}** {status_emoji}\n"
        status_text += f"• Status: {acc_status.get('progress', 'Menunggu')}\n"
        
        if acc_status.get('folders_downloaded', 0) > 0:
            status_text += f"• Folder: {acc_status['folders_downloaded']} downloaded\n"
        if acc_status.get('files_renamed', 0) > 0:
            status_text += f"• Files: {acc_status['files_renamed']} renamed\n"
        if acc_status.get('files_uploaded', 0) > 0:
            status_text += f"• Upload: {acc_status['files_uploaded']} links\n"
            
        if acc_status.get('error'):
            status_text += f"• Error: {acc_status['error']}\n"
            
        status_text += "\n"
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom prefix for auto-rename"""
    if not context.args:
        await update.message.reply_text("❌ Format: /setprefix <prefix>\nContoh: /setprefix 😍")
        return
    
    prefix = context.args[0]
    account_manager.current_prefix = prefix
    
    await update.message.reply_text(f"✅ Prefix diubah menjadi: `{prefix}`\n\nContoh: `{prefix}pic_0001.jpg`", parse_mode='Markdown')

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform"""
    if not context.args:
        await update.message.reply_text("❌ Format: /setplatform <terabox|doodstream>")
        return
    
    platform = context.args[0].lower()
    if platform not in ['terabox', 'doodstream']:
        await update.message.reply_text("❌ Platform harus: terabox atau doodstream")
        return
    
    account_manager.current_platform = platform
    await update.message.reply_text(f"✅ Platform diubah menjadi: {platform}")

async def account_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available accounts"""
    if not account_manager.accounts:
        await update.message.reply_text("❌ Tidak ada akun yang dikonfigurasi!")
        return
    
    info_text = "🔐 **Daftar Akun Mega.nz**\n\n"
    
    for i, account in enumerate(account_manager.accounts):
        status = account_manager.get_account_status(i)
        status_emoji = {
            AccountStatus.PENDING: "⏳",
            AccountStatus.DOWNLOADING: "📥", 
            AccountStatus.RENAMING: "📝",
            AccountStatus.UPLOADING: "📤",
            AccountStatus.COMPLETED: "✅",
            AccountStatus.ERROR: "❌"
        }.get(status.get('status', AccountStatus.PENDING), "⏳")
        
        info_text += f"**Akun {i+1}:** {status_emoji}\n"
        info_text += f"Email: `{account['email']}`\n"
        info_text += f"Status: {status.get('progress', 'Menunggu')}\n"
        
        if status.get('folders_downloaded', 0) > 0:
            info_text += f"Progress: {status['folders_downloaded']} folder\n"
            
        info_text += "\n"
    
    await update.message.reply_text(info_text, parse_mode='Markdown')

async def current_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current account being processed"""
    if not account_processor.is_processing:
        await update.message.reply_text("❌ Tidak ada proses yang berjalan!")
        return
    
    current_acc = account_manager.get_current_account()
    if not current_acc:
        await update.message.reply_text("❌ Tidak ada akun yang sedang diproses!")
        return
    
    current_index = account_manager.current_account_index
    status = account_manager.get_account_status(current_index)
    
    text = f"""
🔍 **Akun Sedang Diproses:**

**Akun {current_index + 1}:**
• Email: `{current_acc['email']}`
• Status: {status.get('progress', 'Unknown')}
• Platform: {account_manager.current_platform}
• Prefix: {account_manager.current_prefix}

**Progress:**
• Folder Downloaded: {status.get('folders_downloaded', 0)}
• Files Renamed: {status.get('files_renamed', 0)}
• Files Uploaded: {status.get('files_uploaded', 0)}
"""
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup all download folders"""
    if account_processor.is_processing:
        await update.message.reply_text("❌ Tidak bisa cleanup saat proses berjalan!")
        return
    
    if not DOWNLOAD_BASE.exists():
        await update.message.reply_text("📁 Tidak ada folder download")
        return
    
    try:
        # Count folders before deletion
        account_folders = list(DOWNLOAD_BASE.glob('account_*'))
        total_folders = 0
        
        for account_folder in account_folders:
            if account_folder.is_dir():
                folders = list(account_folder.glob('*'))
                total_folders += len(folders)
        
        # Delete all folders
        for account_folder in account_folders:
            shutil.rmtree(account_folder)
        
        await update.message.reply_text(f"✅ Berhasil menghapus {total_folders} folder dari {len(account_folders)} akun")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error cleanup: {str(e)}")

def main():
    """Start the bot"""
    # Create necessary directories
    DOWNLOAD_BASE.mkdir(exist_ok=True)
    
    # Initialize managers
    initialize_managers()
    
    # Check Mega.nz installation
    if not mega_manager.check_mega_cmd():
        logger.warning("Mega.nz CMD tidak terpasang! Install dengan: sudo snap install mega-cmd")
    
    # Check if accounts are configured
    if not account_manager.accounts:
        logger.error("Tidak ada akun yang dikonfigurasi di mega_accounts.json!")
        return
    
    # Initialize bot
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN tidak ditemukan di environment variables!")
        return
    
    application = Application.builder().token(token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("startdownload", start_download))
    application.add_handler(CommandHandler("stopdownload", stop_download))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("accountinfo", account_info))
    application.add_handler(CommandHandler("currentaccount", current_account))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    
    # Start bot
    logger.info("Bot started with %d accounts!", len(account_manager.accounts))
    application.run_polling()

if __name__ == '__main__':
    main()
