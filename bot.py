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
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)
from dotenv import load_dotenv
import requests
import aiohttp

# Load environment variables
load_dotenv()

# Configure logging with more detailed format
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
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
            with open(self.settings_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.info("User settings file not found, creating new one")
            return {}
    
    def save_settings(self):
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=4)
            logger.info("User settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save user settings: {e}")
    
    def get_user_settings(self, user_id: int) -> Dict:
        user_str = str(user_id)
        if user_str not in self.settings:
            logger.info(f"Creating default settings for user {user_id}")
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
        logger.info(f"Updated settings for user {user_id}: {new_settings}")
        self.save_settings()

class MegaManager:
    def __init__(self):
        self.cred_file = 'mega_session.json'
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
                result = subprocess.run(['which', path], capture_output=True, text=True)
                if result.returncode == 0:
                    logger.info(f"Found mega-get at: {path}")
                    return path
            except Exception as e:
                logger.warning(f"Error checking path {path}: {e}")
                continue
        
        logger.error("mega-get not found in any standard paths!")
        return "mega-get"
    
    def load_mega_accounts(self) -> List[Dict]:
        """Load mega accounts from environment variables"""
        accounts = []
        
        # Try to load from mega_accounts.json first
        try:
            with open('mega_accounts.json', 'r') as f:
                file_accounts = json.load(f)
                if isinstance(file_accounts, list):
                    accounts.extend(file_accounts)
                    logger.info(f"Loaded {len(file_accounts)} accounts from mega_accounts.json")
        except FileNotFoundError:
            logger.info("mega_accounts.json not found")
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
            logger.error("No Mega.nz accounts found!")
        else:
            logger.info(f"Total {len(accounts)} Mega.nz accounts available")
        
        return accounts
    
    def check_mega_get(self) -> bool:
        """Check if mega-get command is available and working"""
        try:
            cmd = [self.mega_get_path, '--version']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"mega-get check successful: {result.stdout.strip()}")
                return True
            else:
                logger.error(f"mega-get check failed: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("mega-get check timeout")
            return False
        except Exception as e:
            logger.error(f"mega-get check error: {e}")
            return False
    
    def get_current_account(self) -> Optional[Dict]:
        if not self.accounts:
            return None
        return self.accounts[self.current_account_index]
    
    def rotate_account(self):
        if len(self.accounts) > 1:
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
            logger.info(f"Rotated to account: {self.get_current_account()['email']}")
        else:
            logger.warning("Cannot rotate accounts: only one account available")
    
    def debug_mega_session(self) -> Dict:
        """Debug function to check mega session status"""
        debug_info = {}
        
        try:
            # Check mega-get version
            version_cmd = [self.mega_get_path, '--version']
            version_result = subprocess.run(version_cmd, capture_output=True, text=True, timeout=10)
            debug_info['version'] = {
                'returncode': version_result.returncode,
                'stdout': version_result.stdout.strip(),
                'stderr': version_result.stderr.strip()
            }
            
            # Check whoami
            whoami_cmd = [self.mega_get_path, '-whoami']
            whoami_result = subprocess.run(whoami_cmd, capture_output=True, text=True, timeout=10)
            debug_info['whoami'] = {
                'returncode': whoami_result.returncode,
                'stdout': whoami_result.stdout.strip(),
                'stderr': whoami_result.stderr.strip()
            }
            
            # Check disk space
            df_result = subprocess.run(['df', '-h', str(DOWNLOAD_BASE)], capture_output=True, text=True)
            debug_info['disk_space'] = df_result.stdout
            
            # Check if downloads directory exists and is writable
            download_test = DOWNLOAD_BASE / 'test_write'
            try:
                DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
                download_test.touch()
                debug_info['downloads_writable'] = True
                download_test.unlink()
                logger.info("Downloads directory is writable")
            except Exception as e:
                debug_info['downloads_writable'] = False
                debug_info['downloads_error'] = str(e)
                logger.error(f"Downloads directory not writable: {e}")
            
            return debug_info
            
        except Exception as e:
            debug_info['error'] = str(e)
            logger.error(f"Debug session error: {e}")
            return debug_info
    
    def download_mega_folder(self, folder_url: str, download_path: Path, job_id: str) -> Tuple[bool, str]:
        """Download folder from Mega.nz using mega-get with detailed logging"""
        logger.info(f"🚀 Starting download process for job {job_id}")
        logger.info(f"📥 URL: {folder_url}")
        logger.info(f"📁 Download path: {download_path}")
        
        try:
            # Debug session first
            debug_info = self.debug_mega_session()
            logger.info(f"🔧 Debug info for {job_id}: {json.dumps(debug_info, indent=2)}")
            
            # Create download directory
            download_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"📁 Created download directory: {download_path}")
            
            # Test write permission
            test_file = download_path / 'write_test.txt'
            try:
                test_file.write_text('test')
                test_file.unlink()
                logger.info("✅ Write test successful")
            except Exception as e:
                error_msg = f"Cannot write to download directory: {str(e)}"
                logger.error(f"❌ {error_msg}")
                return False, error_msg
            
            # Change to download directory for mega-get
            original_cwd = os.getcwd()
            os.chdir(download_path)
            logger.info(f"📂 Changed working directory to: {download_path}")
            
            try:
                # Now download using mega-get
                download_cmd = [self.mega_get_path, folder_url]
                logger.info(f"⚡ Executing download command: {' '.join(download_cmd)}")
                
                # Execute download with longer timeout
                start_time = time.time()
                logger.info(f"⏰ Download started at: {datetime.now()}")
                
                result = subprocess.run(download_cmd, capture_output=True, text=True, timeout=7200)  # 2 hours
                
                end_time = time.time()
                download_duration = end_time - start_time
                logger.info(f"⏰ Download completed at: {datetime.now()}, duration: {download_duration:.2f}s")
                
                # Log command results
                logger.info(f"📊 Download command return code: {result.returncode}")
                logger.info(f"📤 Download stdout: {result.stdout}")
                if result.stderr:
                    logger.warning(f"📥 Download stderr: {result.stderr}")
                
                # Return to original directory
                os.chdir(original_cwd)
                logger.info("📂 Returned to original working directory")
                
                if result.returncode == 0:
                    # Wait for files to stabilize
                    logger.info("⏳ Waiting for files to stabilize...")
                    time.sleep(5)
                    
                    # Check if files were actually downloaded
                    all_files = list(download_path.rglob('*'))
                    files = [f for f in all_files if f.is_file()]
                    directories = [f for f in all_files if f.is_dir()]
                    
                    logger.info(f"📊 File check results: {len(files)} files, {len(directories)} directories")
                    
                    # Log all files and directories for debugging
                    for f in files:
                        try:
                            file_size = f.stat().st_size
                            logger.info(f"📄 File: {f.relative_to(download_path)} ({file_size} bytes)")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not stat file {f}: {e}")
                    
                    for d in directories:
                        logger.info(f"📁 Directory: {d.relative_to(download_path)}")
                    
                    total_files = len(files)
                    
                    if total_files == 0:
                        error_msg = "Download completed but no files were found"
                        logger.error(f"❌ {error_msg}")
                        # Check output for clues
                        if "error" in result.stdout.lower() or "error" in result.stderr.lower():
                            error_msg = f"Download completed with errors: {result.stdout} {result.stderr}"
                        elif "no such file" in result.stdout.lower() or "no such file" in result.stderr.lower():
                            error_msg = "Folder not found or inaccessible"
                        return False, error_msg
                    
                    success_msg = f"Download successful! {total_files} files downloaded in {download_duration:.2f}s"
                    logger.info(f"✅ {success_msg}")
                    return True, success_msg
                else:
                    error_msg = result.stderr if result.stderr else result.stdout
                    logger.error(f"❌ Download command failed: {error_msg}")
                    
                    # Try to parse common errors
                    if "quota exceeded" in error_msg.lower():
                        logger.warning("🔄 Quota exceeded, rotating account...")
                        self.rotate_account()
                        return False, "Account quota exceeded, please try again later"
                    elif "not found" in error_msg.lower():
                        return False, "Folder not found or link invalid"
                    elif "login" in error_msg.lower():
                        return False, "Login session expired or invalid"
                    else:
                        return False, f"Download failed: {error_msg}"
                        
            except subprocess.TimeoutExpired:
                os.chdir(original_cwd)
                logger.error(f"⏰ Download timeout for {job_id} (2 hours)")
                return False, "Download timeout (2 hours)"
            except Exception as e:
                os.chdir(original_cwd)
                logger.error(f"💥 Unexpected error during download: {e}")
                return False, f"Unexpected error: {str(e)}"
                
        except Exception as e:
            logger.error(f"💥 Error in download process: {e}")
            return False, f"Process error: {str(e)}"

class FileManager:
    @staticmethod
    def auto_rename_media_files(folder_path: Path, prefix: str) -> Dict:
        logger.info(f"🔄 Starting auto-rename process in {folder_path} with prefix '{prefix}'")
        try:
            # Find all media files recursively
            media_files = []
            for ext in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                media_files.extend(folder_path.rglob(f'*{ext}'))
                media_files.extend(folder_path.rglob(f'*{ext.upper()}'))
            
            # Remove duplicates and sort
            media_files = list(set(media_files))
            media_files.sort()
            
            total_files = len(media_files)
            renamed_count = 0
            
            logger.info(f"📊 Found {total_files} media files to rename")
            
            for number, file_path in enumerate(media_files, 1):
                # Format number with leading zero for 1-9
                number_str = f"{number:02d}"
                
                # Create new name: prefix + space + number + extension
                new_name = f"{prefix} {number_str}{file_path.suffix}"
                new_path = file_path.parent / new_name
                
                # Rename file
                try:
                    if file_path != new_path:
                        file_path.rename(new_path)
                        renamed_count += 1
                        logger.info(f"✅ Renamed: {file_path.name} -> {new_name}")
                    else:
                        logger.info(f"ℹ️  File already has correct name: {file_path.name}")
                except Exception as e:
                    logger.error(f"❌ Error renaming {file_path}: {e}")
                    continue
            
            result = {'renamed': renamed_count, 'total': total_files}
            logger.info(f"📝 Rename process completed: {renamed_count}/{total_files} files renamed")
            return result
        except Exception as e:
            logger.error(f"💥 Error in auto_rename: {e}")
            return {'renamed': 0, 'total': 0}

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_lock = threading.Lock()
        logger.info("📤 UploadManager initialized")
    
    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox using TeraboxUploaderCLI"""
        logger.info(f"🚀 Starting Terabox upload for job {job_id}, folder: {folder_path}")
        try:
            if not TERABOX_CLI_DIR.exists():
                error_msg = "TeraboxUploaderCLI not found!"
                logger.error(f"❌ {error_msg}")
                await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                return []
            
            await self.send_progress_message(update, context, job_id, "📤 Starting upload to Terabox...")
            
            # Use lock to prevent multiple concurrent Terabox uploads
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                # Run TeraboxUploaderCLI
                old_cwd = os.getcwd()
                os.chdir(TERABOX_CLI_DIR)
                logger.info(f"📂 Changed to TeraboxUploaderCLI directory: {TERABOX_CLI_DIR}")
                
                try:
                    # Run the uploader for the specific folder
                    cmd = ['python', 'main.py', '--source', str(folder_path)]
                    logger.info(f"⚡ Executing TeraboxUploaderCLI: {' '.join(cmd)}")
                    
                    # Execute with timeout
                    start_time = time.time()
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
                    end_time = time.time()
                    upload_duration = end_time - start_time
                    
                    logger.info(f"📊 TeraboxUploaderCLI completed in {upload_duration:.2f}s, returncode: {result.returncode}")
                    logger.info(f"📤 TeraboxUploaderCLI stdout: {result.stdout}")
                    if result.stderr:
                        logger.warning(f"📥 TeraboxUploaderCLI stderr: {result.stderr}")
                    
                    if result.returncode == 0:
                        success_msg = f"Terabox upload completed successfully in {upload_duration:.2f}s"
                        logger.info(f"✅ {success_msg}")
                        await self.send_progress_message(
                            update, context, job_id,
                            f"✅ Upload to Terabox completed!\n"
                            f"Folder: {folder_path.name}\n"
                            f"Duration: {upload_duration:.2f}s"
                        )
                        return ["Upload completed - check your Terabox account"]
                    else:
                        error_msg = f"TeraboxUploaderCLI failed: {result.stderr if result.stderr else result.stdout}"
                        logger.error(f"❌ {error_msg}")
                        raise Exception(error_msg)
                        
                finally:
                    os.chdir(old_cwd)
                    logger.info("📂 Returned to original working directory")
                    
        except subprocess.TimeoutExpired:
            error_msg = "Upload timeout (2 hours)"
            logger.error(f"⏰ Terabox upload timeout for {job_id}: {error_msg}")
            await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
            return []
        except Exception as e:
            logger.error(f"💥 Terabox upload error for {job_id}: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
            return []
    
    async def upload_to_doodstream(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload video files to Doodstream"""
        logger.info(f"🚀 Starting Doodstream upload for job {job_id}, folder: {folder_path}")
        try:
            await self.send_progress_message(update, context, job_id, "📤 Starting upload to Doodstream...")
            
            if not self.doodstream_key:
                error_msg = "Doodstream API key not found!"
                logger.error(f"❌ {error_msg}")
                await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                return []
            
            links = []
            video_files = [f for f in folder_path.rglob('*') 
                          if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            total_files = len(video_files)
            
            logger.info(f"📊 Found {total_files} video files for Doodstream upload")
            
            if total_files == 0:
                logger.warning("📭 No video files found for Doodstream upload")
                await self.send_progress_message(update, context, job_id, "📭 No video files to upload")
                return []
            
            uploaded_count = 0
            
            for i, file_path in enumerate(video_files, 1):
                if not self.is_job_active(job_id):
                    logger.warning(f"⏹️  Upload cancelled for job {job_id}")
                    break
                    
                try:
                    logger.info(f"📤 Uploading file {i}/{total_files}: {file_path.name}")
                    link = await self.upload_single_file_to_doodstream(file_path)
                    if link:
                        links.append(link)
                        uploaded_count += 1
                        logger.info(f"✅ Upload successful: {file_path.name} -> {link}")
                        await self.send_progress_message(
                            update, context, job_id,
                            f"📤 Upload progress: {uploaded_count}/{total_files}\n✅ {file_path.name}"
                        )
                    else:
                        logger.error(f"❌ Upload failed: {file_path.name}")
                        await self.send_progress_message(
                            update, context, job_id,
                            f"❌ Upload failed: {file_path.name}"
                        )
                except Exception as e:
                    logger.error(f"💥 Error uploading {file_path}: {e}")
            
            logger.info(f"📊 Doodstream upload completed: {uploaded_count}/{total_files} files uploaded")
            return links
        except Exception as e:
            logger.error(f"💥 Doodstream upload error for {job_id}: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
            return []
    
    async def upload_single_file_to_doodstream(self, file_path: Path) -> str:
        """Upload single file to Doodstream API"""
        try:
            logger.info(f"📤 Uploading single file to Doodstream: {file_path}")
            url = "https://doodstream.com/api/upload"
            
            with open(file_path, 'rb') as f:
                files = {'file': f}
                data = {'key': self.doodstream_key}
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=data, files=files) as response:
                        result = await response.json()
                        logger.info(f"📊 Doodstream API response: {result}")
                        
                        if result.get('success'):
                            download_url = result.get('download_url', '')
                            logger.info(f"✅ Doodstream upload successful: {download_url}")
                            return download_url
                        else:
                            error_msg = f"Doodstream API error: {result}"
                            logger.error(f"❌ {error_msg}")
                            return ""
        except Exception as e:
            logger.error(f"💥 Doodstream single upload error: {e}")
            return ""
    
    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, message: str):
        """Send or update progress message"""
        try:
            if job_id not in active_downloads:
                logger.warning(f"⚠️  Job {job_id} not found in active_downloads, cannot send progress message")
                return
                
            chat_id = active_downloads[job_id]['chat_id']
            
            # Store the latest progress message for this job
            if 'progress_message_id' in active_downloads[job_id]:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=active_downloads[job_id]['progress_message_id'],
                        text=f"{active_downloads[job_id]['folder_name']}\n{message}"
                    )
                    logger.debug(f"📝 Updated progress message for job {job_id}")
                    return
                except Exception as e:
                    logger.warning(f"⚠️  Failed to edit progress message for job {job_id}: {e}")
                    # If editing fails, send new message
                    pass
            
            # Send new message
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{active_downloads[job_id]['folder_name']}\n{message}"
            )
            active_downloads[job_id]['progress_message_id'] = msg.message_id
            logger.debug(f"📤 Sent new progress message for job {job_id}, message_id: {msg.message_id}")
            
        except Exception as e:
            logger.error(f"💥 Error sending progress message for job {job_id}: {e}")
    
    def is_job_active(self, job_id: str) -> bool:
        is_active = job_id in active_downloads and active_downloads[job_id]['status'] != DownloadStatus.COMPLETED
        if not is_active:
            logger.info(f"⏹️  Job {job_id} is no longer active")
        return is_active

class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, file_manager: FileManager, upload_manager: UploadManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.settings_manager = settings_manager
        self.processing = False
        self.current_processes = 0
        logger.info("🔄 DownloadProcessor initialized")
    
    def start_processing(self):
        """Start processing download queue"""
        if not self.processing:
            self.processing = True
            thread = threading.Thread(target=self._process_queue, daemon=True)
            thread.start()
            logger.info("🚀 Download processor started")
    
    def _process_queue(self):
        """Process download queue continuously"""
        logger.info("🔄 Queue processor thread started")
        while self.processing:
            try:
                # Check if we can start new downloads
                if self.current_processes < MAX_CONCURRENT_DOWNLOADS and not download_queue.empty():
                    job_data = download_queue.get()
                    if job_data:
                        self.current_processes += 1
                        logger.info(f"📥 Starting new download process, current processes: {self.current_processes}/{MAX_CONCURRENT_DOWNLOADS}")
                        threading.Thread(
                            target=self._process_single_download,
                            args=(job_data,),
                            daemon=True
                        ).start()
                    else:
                        logger.warning("⚠️  Got empty job data from queue")
                
                threading.Event().wait(5)  # Check every 5 seconds
            except Exception as e:
                logger.error(f"💥 Error in queue processor: {e}")
                threading.Event().wait(10)
    
    def _process_single_download(self, job_data: Dict):
        """Process single download job"""
        logger.info(f"🔄 Starting single download process for job {job_data['job_id']}")
        asyncio.run(self._async_process_single_download(job_data))
    
    async def _async_process_single_download(self, job_data: Dict):
        """Async version of single download processing"""
        job_id = job_data['job_id']
        folder_name = job_data['folder_name']
        mega_url = job_data['mega_url']
        user_id = job_data['user_id']
        update = job_data['update']
        context = job_data['context']
        
        logger.info(f"🚀 Processing download job {job_id} for user {user_id}")
        logger.info(f"📁 Folder: {folder_name}, URL: {mega_url}")
        
        try:
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOADING
            active_downloads[job_id]['progress'] = "Starting download from Mega.nz"
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, "📥 Starting download from Mega.nz..."
            )
            
            # Create download path
            download_path = DOWNLOAD_BASE / folder_name
            
            # Download from Mega.nz with debug info
            logger.info(f"🔽 Starting Mega.nz download for job {job_id}")
            success, message = self.mega_manager.download_mega_folder(mega_url, download_path, job_id)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                logger.error(f"❌ Download failed for job {job_id}: {message}")
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"❌ Download failed: {message}"
                )
                return
            
            # Check if files actually exist
            files = list(download_path.rglob('*'))
            file_count = len([f for f in files if f.is_file()])
            
            if file_count == 0:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = "No files downloaded"
                logger.error(f"❌ No files downloaded for job {job_id}")
                await self.upload_manager.send_progress_message(
                    update, context, job_id, "❌ Download failed: no files were downloaded"
                )
                return
            
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOAD_COMPLETED
            active_downloads[job_id]['progress'] = "Download completed, starting rename"
            
            logger.info(f"✅ Download completed for job {job_id}, {file_count} files downloaded")
            await self.upload_manager.send_progress_message(
                update, context, job_id, f"✅ Download completed! {file_count} files downloaded. Renaming files..."
            )
            
            # Auto-rename files
            active_downloads[job_id]['status'] = DownloadStatus.RENAMING
            active_downloads[job_id]['progress'] = "Renaming files"
            
            user_settings = self.settings_manager.get_user_settings(user_id)
            prefix = user_settings.get('prefix', 'file_')
            logger.info(f"📝 Starting file rename with prefix '{prefix}' for job {job_id}")
            
            rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
            
            logger.info(f"📝 Rename completed for job {job_id}: {rename_result['renamed']}/{rename_result['total']} files renamed")
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"📝 Rename completed:\n"
                f"📁 {rename_result['renamed']} files renamed from total {rename_result['total']} files"
            )
            
            # Auto-upload if enabled
            if user_settings.get('auto_upload', True):
                active_downloads[job_id]['status'] = DownloadStatus.UPLOADING
                active_downloads[job_id]['progress'] = "Uploading files"
                
                platform = user_settings.get('platform', 'terabox')
                logger.info(f"📤 Starting auto-upload to {platform} for job {job_id}")
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"📤 Uploading to {platform}..."
                )
                
                if platform == 'terabox':
                    links = await self.upload_manager.upload_to_terabox(download_path, update, context, job_id)
                else:
                    links = await self.upload_manager.upload_to_doodstream(download_path, update, context, job_id)
                
                # Don't send duplicate success message for Terabox
                if platform != 'terabox':
                    logger.info(f"✅ Upload completed for job {job_id}: {len(links)} links generated")
                    await self.upload_manager.send_progress_message(
                        update, context, job_id,
                        f"✅ Upload completed!\n🔗 {len(links)} links generated"
                    )
            else:
                logger.info(f"⏭️  Auto-upload disabled for job {job_id}, skipping upload")
            
            # Auto-cleanup if enabled
            if user_settings.get('auto_cleanup', True):
                try:
                    # Wait a moment before cleanup
                    await asyncio.sleep(2)
                    
                    # Check if folder still exists and has files
                    if os.path.exists(download_path):
                        # Double check if upload really completed
                        files_after_upload = list(download_path.rglob('*'))
                        if files_after_upload:
                            logger.info(f"🧹 Starting auto-cleanup for job {job_id}, folder: {download_path}")
                            shutil.rmtree(download_path)
                            logger.info(f"✅ Auto-cleanup completed for job {job_id}")
                            await self.upload_manager.send_progress_message(
                                update, context, job_id, "🧹 Auto-cleanup completed!"
                            )
                        else:
                            logger.info(f"📁 Folder already empty for job {job_id}, skipping cleanup")
                            await self.upload_manager.send_progress_message(
                                update, context, job_id, "📁 Folder already empty, skip cleanup"
                            )
                    else:
                        logger.warning(f"⚠️  Folder not found during cleanup for job {job_id}: {download_path}")
                except Exception as e:
                    logger.error(f"💥 Cleanup error for {job_id}: {e}")
                    await self.upload_manager.send_progress_message(
                        update, context, job_id, f"⚠️ Cleanup error: {str(e)}"
                    )
            else:
                logger.info(f"⏭️  Auto-cleanup disabled for job {job_id}, skipping cleanup")
            
            # Mark as completed
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['progress'] = "All processes completed"
            active_downloads[job_id]['completed_at'] = datetime.now().isoformat()
            
            # Move to completed downloads
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            logger.info(f"🎉 All processes completed successfully for job {job_id}")
            await self.upload_manager.send_progress_message(
                update, context, job_id, "✅ All processes completed!"
            )
            
        except Exception as e:
            logger.error(f"💥 Error processing download {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['error'] = str(e)
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, f"❌ Error: {str(e)}"
            )
        
        finally:
            self.current_processes -= 1
            logger.info(f"📊 Download process completed, current processes: {self.current_processes}/{MAX_CONCURRENT_DOWNLOADS}")

# Initialize managers
logger.info("🔄 Initializing managers...")
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
    logger.info(f"👋 Start command from user {update.effective_user.id}")
    welcome_text = """
🤖 Mega Downloader Bot

Fitur:
📥 Download folder dari Mega.nz via link
📝 Auto-rename file media
📤 Upload ke Terabox/Doodstream
⚡ Maksimal 2 download bersamaan
📊 System antrian otomatis

Perintah:
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

Contoh:
/download AMIBEL https://mega.nz/folder/abc123#xyz
    """
    
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    logger.info(f"📖 Help command from user {update.effective_user.id}")
    help_text = """
📚 Daftar Perintah:

DOWNLOAD COMMANDS
/download <nama_folder> <link_mega> - Download folder dari Mega.nz
/upload <nama_folder> - Upload folder manual

SETTINGS COMMANDS
/setprefix <prefix> - Set custom prefix untuk rename
/setplatform <terabox|doodstream> - Pilih platform upload
/autoupload - Toggle auto-upload setelah download
/autocleanup - Toggle auto-cleanup setelah upload

INFO COMMANDS
/status - Status sistem & antrian download
/mysettings - Lihat pengaturan saat ini

MAINTENANCE
/cleanup - Hapus semua folder download
/cancel <job_id> - Batalkan download (soon)

Contoh Download:
/download AMIBEL https://mega.nz/folder/syUExAxI#9LDA5zV_2CpgwDnn0py93w

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
    user_id = update.effective_user.id
    logger.info(f"📥 Download command from user {user_id}, args: {context.args}")
    
    if not context.args or len(context.args) < 2:
        error_msg = "Invalid command format"
        logger.warning(f"⚠️  {error_msg} from user {user_id}")
        await update.message.reply_text(
            "❌ Format: /download <nama_folder> <link_mega>\n"
            "Contoh: /download AMIBEL https://mega.nz/folder/abc123#xyz"
        )
        return
    
    folder_name = context.args[0]
    mega_url = context.args[1]
    
    # Validate Mega.nz folder URL
    if not mega_url.startswith('https://mega.nz/folder/'):
        error_msg = "Invalid Mega.nz folder URL"
        logger.warning(f"⚠️  {error_msg} from user {user_id}: {mega_url}")
        await update.message.reply_text(
            "❌ Link harus berupa folder Mega.nz\n"
            "Contoh: https://mega.nz/folder/abc123#xyz"
        )
        return
    
    # Generate job ID
    job_id = f"dl_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info(f"🆔 Generated job ID: {job_id} for user {user_id}")
    
    # Add to active downloads
    active_downloads[job_id] = {
        'job_id': job_id,
        'folder_name': folder_name,
        'mega_url': mega_url,
        'user_id': user_id,
        'chat_id': update.effective_chat.id,
        'status': DownloadStatus.PENDING,
        'progress': 'Waiting in queue',
        'created_at': datetime.now().isoformat(),
        'update': update,
        'context': context
    }
    
    # Add to queue
    download_queue.put(active_downloads[job_id])
    
    # Get queue position
    queue_list = list(download_queue.queue)
    queue_position = queue_list.index(active_downloads[job_id]) + 1 if active_downloads[job_id] in queue_list else 0
    
    logger.info(f"📊 Added job {job_id} to queue, position: {queue_position + 1}")
    
    await update.message.reply_text(
        f"✅ Download Added to Queue\n\n"
        f"📁 Folder: {folder_name}\n"
        f"🔗 Link: {mega_url}\n"
        f"🆔 Job ID: {job_id}\n"
        f"📊 Queue Position: #{queue_position + 1}\n"
        f"⚡ Active Downloads: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system status"""
    logger.info(f"📊 Status command from user {update.effective_user.id}")
    
    # Active downloads
    active_text = "📥 ACTIVE DOWNLOADS:\n"
    if active_downloads:
        for job_id, job in list(active_downloads.items()):
            status_emoji = {
                DownloadStatus.PENDING: "⏳",
                DownloadStatus.DOWNLOADING: "📥",
                DownloadStatus.DOWNLOAD_COMPLETED: "✅",
                DownloadStatus.RENAMING: "📝",
                DownloadStatus.UPLOADING: "📤",
                DownloadStatus.COMPLETED: "🎉",
                DownloadStatus.ERROR: "❌"
            }.get(job['status'], "⏳")
            
            active_text += f"{status_emoji} {job['folder_name']}\n"
            active_text += f"   Status: {job['progress']}\n"
            active_text += f"   ID: {job_id}\n\n"
    else:
        active_text += "No active downloads\n\n"
    
    # Queue
    queue_list = list(download_queue.queue)
    queue_text = "📊 QUEUE:\n"
    if queue_list:
        for i, job in enumerate(queue_list):
            queue_text += f"#{i+1} {job['folder_name']}\n"
    else:
        queue_text += "Queue is empty\n"
    
    # System info
    system_text = f"""
⚙️ SYSTEM INFO:
• Active Downloads: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}
• In Queue: {download_queue.qsize()}
• Mega-get CMD: {'✅' if mega_manager.check_mega_get() else '❌'}
• Available Accounts: {len(mega_manager.accounts)}
• Current Account: {mega_manager.get_current_account()['email'] if mega_manager.get_current_account() else 'None'}
• Mega-get Path: {mega_manager.mega_get_path}
• TeraboxUploaderCLI: {'✅' if TERABOX_CLI_DIR.exists() else '❌'}
    """
    
    full_text = active_text + "\n" + queue_text + "\n" + system_text
    await update.message.reply_text(full_text)

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to check system status"""
    logger.info(f"🔧 Debug command from user {update.effective_user.id}")
    debug_info = mega_manager.debug_mega_session()
    
    debug_text = "🔧 DEBUG INFORMATION:\n\n"
    
    # Mega.nz status
    debug_text += "MEGA.NZ STATUS:\n"
    debug_text += f"• Mega-get available: {'✅' if mega_manager.check_mega_get() else '❌'}\n"
    debug_text += f"• Mega-get path: {mega_manager.mega_get_path}\n"
    
    if 'whoami' in debug_info:
        debug_text += f"• Whoami: {debug_info['whoami']['stdout']}\n"
    
    # Disk space
    debug_text += f"• Downloads writable: {'✅' if debug_info.get('downloads_writable', False) else '❌'}\n"
    
    # Accounts
    debug_text += f"• Accounts configured: {len(mega_manager.accounts)}\n"
    for i, acc in enumerate(mega_manager.accounts):
        debug_text += f"  Account {i+1}: {acc['email']}\n"
    
    await update.message.reply_text(debug_text)

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set custom prefix for auto-rename"""
    user_id = update.effective_user.id
    logger.info(f"📝 Set prefix command from user {user_id}, args: {context.args}")
    
    if not context.args:
        logger.warning(f"⚠️  Empty prefix from user {user_id}")
        await update.message.reply_text(
            "❌ Format: /setprefix <prefix>\n"
            "Contoh: /setprefix TELEGRAM @missyhot22\n"
            "Contoh: /setprefix my files"
        )
        return
    
    prefix = " ".join(context.args)
    settings_manager.update_user_settings(user_id, {'prefix': prefix})
    
    logger.info(f"✅ Prefix updated for user {user_id}: {prefix}")
    await update.message.reply_text(
        f"✅ Prefix Updated\n\n"
        f"New prefix: {prefix}\n"
        f"Example files: {prefix} 01.jpg\n"
        f"{prefix} 02.mp4"
    )

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform"""
    user_id = update.effective_user.id
    logger.info(f"📤 Set platform command from user {user_id}, args: {context.args}")
    
    if not context.args:
        logger.warning(f"⚠️  Empty platform from user {user_id}")
        await update.message.reply_text(
            "❌ Format: /setplatform <terabox|doodstream>\n"
            "Contoh: /setplatform terabox"
        )
        return
    
    platform = context.args[0].lower()
    if platform not in ['terabox', 'doodstream']:
        logger.warning(f"⚠️  Invalid platform from user {user_id}: {platform}")
        await update.message.reply_text("❌ Platform harus: terabox atau doodstream")
        return
    
    settings_manager.update_user_settings(user_id, {'platform': platform})
    logger.info(f"✅ Platform updated for user {user_id}: {platform}")
    await update.message.reply_text(f"✅ Upload platform changed to: {platform}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload"""
    user_id = update.effective_user.id
    logger.info(f"🔄 Auto-upload toggle command from user {user_id}")
    
    settings = settings_manager.get_user_settings(user_id)
    new_auto_upload = not settings.get('auto_upload', True)
    settings_manager.update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "ACTIVE" if new_auto_upload else "INACTIVE"
    logger.info(f"✅ Auto-upload toggled for user {user_id}: {status}")
    await update.message.reply_text(f"✅ Auto-upload: {status}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-cleanup"""
    user_id = update.effective_user.id
    logger.info(f"🧹 Auto-cleanup toggle command from user {user_id}")
    
    settings = settings_manager.get_user_settings(user_id)
    new_auto_cleanup = not settings.get('auto_cleanup', True)
    settings_manager.update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "ACTIVE" if new_auto_cleanup else "INACTIVE"
    logger.info(f"✅ Auto-cleanup toggled for user {user_id}: {status}")
    await update.message.reply_text(f"✅ Auto-cleanup: {status}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings"""
    user_id = update.effective_user.id
    logger.info(f"⚙️  My settings command from user {user_id}")
    
    settings = settings_manager.get_user_settings(user_id)
    
    settings_text = f"""
⚙️ MY SETTINGS

📝 Prefix: {settings.get('prefix', 'file_')}
📤 Platform: {settings.get('platform', 'terabox')}
🔄 Auto-upload: {'✅ ACTIVE' if settings.get('auto_upload', True) else '❌ INACTIVE'}
🧹 Auto-cleanup: {'✅ ACTIVE' if settings.get('auto_cleanup', True) else '❌ INACTIVE'}
    """
    
    await update.message.reply_text(settings_text)

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup all download folders"""
    user_id = update.effective_user.id
    logger.info(f"🧹 Cleanup command from user {user_id}")
    
    if not DOWNLOAD_BASE.exists():
        logger.info("📁 No download directory found")
        await update.message.reply_text("📁 No download folders")
        return
    
    try:
        # Count folders before deletion
        folders = [f for f in DOWNLOAD_BASE.iterdir() if f.is_dir()]
        total_folders = len(folders)
        
        if total_folders == 0:
            logger.info("📁 No folders to clean up")
            await update.message.reply_text("📁 No download folders")
            return
        
        logger.info(f"🧹 Starting cleanup of {total_folders} folders")
        # Delete all folders
        for folder in folders:
            shutil.rmtree(folder)
            logger.info(f"✅ Deleted folder: {folder}")
        
        logger.info(f"✅ Cleanup completed: {total_folders} folders deleted")
        await update.message.reply_text(f"✅ Successfully deleted {total_folders} download folders")
        
    except Exception as e:
        logger.error(f"💥 Cleanup error: {e}")
        await update.message.reply_text(f"❌ Cleanup error: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual upload command"""
    user_id = update.effective_user.id
    logger.info(f"📤 Upload command from user {user_id}, args: {context.args}")
    
    if not context.args:
        logger.warning(f"⚠️  Empty upload command from user {user_id}")
        await update.message.reply_text("❌ Format: /upload <nama_folder>")
        return
    
    folder_name = context.args[0]
    folder_path = DOWNLOAD_BASE / folder_name
    
    if not folder_path.exists():
        logger.warning(f"⚠️  Folder not found: {folder_path}")
        await update.message.reply_text(f"❌ Folder '{folder_name}' not found in downloads/")
        return
    
    user_settings = settings_manager.get_user_settings(user_id)
    platform = user_settings.get('platform', 'terabox')
    
    # Generate job ID for upload
    job_id = f"up_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info(f"🆔 Generated upload job ID: {job_id}")
    
    # Add to active downloads for progress tracking
    active_downloads[job_id] = {
        'job_id': job_id,
        'folder_name': folder_name,
        'user_id': user_id,
        'chat_id': update.effective_chat.id,
        'status': DownloadStatus.UPLOADING,
        'progress': 'Starting upload',
        'created_at': datetime.now().isoformat(),
        'update': update,
        'context': context
    }
    
    await update.message.reply_text(f"📤 Starting upload {folder_name} to {platform}...")
    
    # Perform upload
    if platform == 'terabox':
        links = await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
    else:
        links = await upload_manager.upload_to_doodstream(folder_path, update, context, job_id)
    
    # Cleanup if enabled
    if user_settings.get('auto_cleanup', True):
        try:
            if os.path.exists(folder_path):
                logger.info(f"🧹 Auto-cleanup after upload for {folder_path}")
                shutil.rmtree(folder_path)
                await update.message.reply_text("🧹 Auto-cleanup completed!")
        except Exception as e:
            logger.error(f"💥 Cleanup error after upload: {e}")
    
    # Remove from active downloads
    if job_id in active_downloads:
        del active_downloads[job_id]
        logger.info(f"✅ Removed upload job {job_id} from active downloads")
    
    if platform == 'terabox':
        logger.info(f"✅ Terabox upload completed for {folder_name}")
        await update.message.reply_text(f"✅ Upload completed! Files uploaded to Terabox")
    else:
        logger.info(f"✅ Doodstream upload completed for {folder_name}: {len(links)} links")
        await update.message.reply_text(f"✅ Upload completed! {len(links)} links generated")

def main():
    """Start the bot"""
    logger.info("🚀 Starting Mega Downloader Bot...")
    
    # Create necessary directories
    DOWNLOAD_BASE.mkdir(exist_ok=True)
    logger.info(f"📁 Download base directory: {DOWNLOAD_BASE}")
    
    # Check Mega.nz installation
    mega_available = mega_manager.check_mega_get()
    if not mega_available:
        logger.error("❌ mega-get is not available! Please install mega-cmd: sudo snap install mega-cmd")
    else:
        logger.info("✅ mega-get is available")
    
    # Check if accounts are configured
    if not mega_manager.accounts:
        logger.error("❌ No Mega.nz accounts configured!")
    else:
        logger.info(f"✅ {len(mega_manager.accounts)} Mega.nz accounts available")
    
    # Check TeraboxUploaderCLI
    if not TERABOX_CLI_DIR.exists():
        logger.warning("⚠️  TeraboxUploaderCLI not found! Please clone it in this directory.")
    else:
        logger.info("✅ TeraboxUploaderCLI found")
    
    # Initialize bot
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("❌ BOT_TOKEN not found in environment variables!")
        return
    
    application = Application.builder().token(token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("autoupload", auto_upload_toggle))
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    
    # Start bot
    logger.info("✅ Bot started successfully!")
    application.run_polling()

if __name__ == '__main__':
    main()
