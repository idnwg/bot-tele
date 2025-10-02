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
        # Get mega command path
        self.mega_cmd_path = self._get_mega_cmd_path()
    
    def _get_mega_cmd_path(self) -> str:
        """Get the correct path for mega commands"""
        # Try multiple possible paths
        possible_paths = [
            '/snap/bin/mega-cmd',
            '/usr/bin/mega-cmd',
            '/usr/local/bin/mega-cmd',
            'mega-cmd'  # Fallback to PATH
        ]
        
        for path in possible_paths:
            try:
                result = subprocess.run(['which', path], capture_output=True, text=True)
                if result.returncode == 0:
                    logger.info(f"Found mega-cmd at: {path}")
                    return path
            except:
                continue
        
        logger.warning("Mega-cmd not found in standard paths, using 'mega-cmd'")
        return "mega-cmd"
    
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
            # Use the found mega-cmd path
            cmd = [self.mega_cmd_path, '--version']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error checking mega-cmd: {e}")
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
            logout_cmd = f'{self.mega_cmd_path} -logout'
            subprocess.run(logout_cmd, shell=True, capture_output=True, text=True, timeout=10)
            
            # Login using mega-cmd with full path
            login_cmd = f'{self.mega_cmd_path} -login "{email}" "{password}"'
            logger.info(f"Executing login: {login_cmd}")
            
            result = subprocess.run(login_cmd, shell=True, capture_output=True, text=True, timeout=30)
            
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
                error_msg = result.stderr if result.stderr else result.stdout
                logger.error(f"Login failed: {error_msg}")
                return False, f"Login gagal: {error_msg}"
        except subprocess.TimeoutExpired:
            return False, "Login timeout"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def ensure_mega_session(self) -> bool:
        try:
            whoami_cmd = f'{self.mega_cmd_path} -whoami'
            result = subprocess.run(whoami_cmd, shell=True, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"Mega session active: {result.stdout.strip()}")
                return True
            else:
                logger.warning("Mega session not active")
                return False
        except Exception as e:
            logger.error(f"Error checking mega session: {e}")
            return False
    
    def debug_mega_session(self) -> Dict:
        """Debug function to check mega session status"""
        debug_info = {}
        
        try:
            # Check whoami
            whoami_cmd = f'{self.mega_cmd_path} -whoami'
            result = subprocess.run(whoami_cmd, shell=True, capture_output=True, text=True, timeout=10)
            debug_info['whoami'] = {
                'returncode': result.returncode,
                'stdout': result.stdout.strip(),
                'stderr': result.stderr.strip()
            }
            
            # Check disk space
            df_result = subprocess.run(['df', '-h'], capture_output=True, text=True)
            debug_info['disk_space'] = df_result.stdout
            
            # Check if downloads directory exists and is writable
            download_test = DOWNLOAD_BASE / 'test_write'
            try:
                download_test.touch()
                debug_info['downloads_writable'] = True
                download_test.unlink()
            except Exception as e:
                debug_info['downloads_writable'] = False
                debug_info['downloads_error'] = str(e)
            
            return debug_info
            
        except Exception as e:
            debug_info['error'] = str(e)
            return debug_info
    
    def download_mega_folder(self, folder_url: str, download_path: Path, job_id: str) -> Tuple[bool, str]:
        """Download folder from Mega.nz with detailed debugging"""
        try:
            logger.info(f"Starting download for job {job_id}: {folder_url} -> {download_path}")
            
            # Debug session first
            debug_info = self.debug_mega_session()
            logger.info(f"Mega debug info for {job_id}: {debug_info}")
            
            if not self.ensure_mega_session():
                # Try to login with current account
                current_account = self.get_current_account()
                if current_account:
                    logger.info(f"Attempting login for {job_id} with: {current_account['email']}")
                    success, message = self.login_to_mega(current_account['email'], current_account['password'])
                    if not success:
                        return False, f"Session invalid and login failed: {message}"
                else:
                    return False, "No Mega.nz account available"
            
            # Create download directory
            download_path.mkdir(parents=True, exist_ok=True)
            
            # Test write permission
            test_file = download_path / 'write_test.txt'
            try:
                test_file.write_text('test')
                test_file.unlink()
            except Exception as e:
                return False, f"Cannot write to download directory: {str(e)}"
            
            # First, let's check what's in the Mega.nz folder
            logger.info(f"Checking Mega.nz folder contents for {job_id}")
            ls_cmd = f'{self.mega_cmd_path} -ls {folder_url}'
            ls_result = subprocess.run(ls_cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            logger.info(f"Mega-ls result for {job_id}: returncode={ls_result.returncode}, stdout={ls_result.stdout}, stderr={ls_result.stderr}")
            
            if ls_result.returncode != 0:
                return False, f"Cannot access Mega.nz folder: {ls_result.stderr}"
            
            # Now attempt download
            download_cmd = f'{self.mega_cmd_path} -get "{folder_url}" "{download_path}"'
            logger.info(f"Executing download for {job_id}: {download_cmd}")
            
            # Execute download with longer timeout
            start_time = time.time()
            result = subprocess.run(download_cmd, shell=True, capture_output=True, text=True, timeout=7200)  # 2 hours
            end_time = time.time()
            
            download_duration = end_time - start_time
            logger.info(f"Download completed for {job_id} in {download_duration:.2f}s: returncode={result.returncode}")
            
            if result.returncode == 0:
                # Wait for files to stabilize
                time.sleep(10)
                
                # Check if files were actually downloaded - search recursively
                all_files = list(download_path.rglob('*'))
                files = [f for f in all_files if f.is_file()]
                directories = [f for f in all_files if f.is_dir()]
                
                logger.info(f"File check for {job_id}: {len(files)} files, {len(directories)} directories found")
                
                # Log all files and directories for debugging
                for f in files:
                    logger.info(f"File: {f} ({f.stat().st_size} bytes)")
                for d in directories:
                    logger.info(f"Directory: {d}")
                
                # Check if files exist in subdirectories (common Mega.nz behavior)
                total_files = len(files)
                
                if total_files == 0:
                    # Check if there's exactly one subdirectory (Mega.nz often creates one)
                    if len(directories) == 1:
                        subdir = directories[0]
                        subdir_files = list(subdir.rglob('*'))
                        actual_files = [f for f in subdir_files if f.is_file()]
                        
                        if len(actual_files) > 0:
                            logger.info(f"Found {len(actual_files)} files in subdirectory {subdir}, moving to main directory")
                            
                            # Move all files from subdirectory to main download path
                            for file_path in actual_files:
                                try:
                                    relative_path = file_path.relative_to(subdir)
                                    new_path = download_path / relative_path
                                    new_path.parent.mkdir(parents=True, exist_ok=True)
                                    file_path.rename(new_path)
                                    logger.info(f"Moved {file_path} to {new_path}")
                                except Exception as e:
                                    logger.error(f"Error moving file {file_path}: {e}")
                            
                            # Remove empty subdirectory
                            try:
                                shutil.rmtree(subdir)
                            except:
                                pass
                            
                            # Re-count files
                            files = list(download_path.rglob('*'))
                            files = [f for f in files if f.is_file()]
                            total_files = len(files)
                    
                    if total_files == 0:
                        # Still no files? Check the output for clues
                        if "error" in result.stdout.lower() or "error" in result.stderr.lower():
                            return False, f"Download completed with errors in output: {result.stdout} {result.stderr}"
                        elif "no such file" in result.stdout.lower() or "no such file" in result.stderr.lower():
                            return False, "Folder not found or inaccessible"
                        else:
                            return False, "Download completed but no files were found in the folder"
                
                return True, f"Download berhasil! {total_files} files downloaded in {download_duration:.2f}s"
            else:
                error_msg = result.stderr if result.stderr else result.stdout
                logger.error(f"Download command failed for {job_id}: {error_msg}")
                return False, f"Download gagal: {error_msg}"
                
        except subprocess.TimeoutExpired:
            logger.error(f"Download timeout for {job_id}")
            return False, "Download timeout (2 hours)"
        except Exception as e:
            logger.error(f"Unexpected error in download for {job_id}: {e}")
            return False, f"Error: {str(e)}"

class FileManager:
    @staticmethod
    def auto_rename_media_files(folder_path: Path, prefix: str) -> Dict:
        try:
            # Find all media files recursively
            media_files = []
            for ext in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                media_files.extend(folder_path.rglob(f'*{ext}'))
                media_files.extend(folder_path.rglob(f'*{ext.upper()}'))
            
            # Sort files naturally
            media_files.sort()
            
            total_files = len(media_files)
            renamed_count = 0
            
            for number, file_path in enumerate(media_files, 1):
                # Format number with leading zero for 1-9
                number_str = f"{number:02d}"  # This will give 01, 02, ..., 10, 11, etc.
                
                # Create new name: prefix + space + number + extension
                new_name = f"{prefix} {number_str}{file_path.suffix}"
                new_path = file_path.parent / new_name
                
                # Rename file
                try:
                    if file_path != new_path:
                        file_path.rename(new_path)
                        renamed_count += 1
                        logger.info(f"Renamed: {file_path.name} -> {new_name}")
                except Exception as e:
                    logger.error(f"Error renaming {file_path}: {e}")
                    continue
            
            return {'renamed': renamed_count, 'total': total_files}
        except Exception as e:
            logger.error(f"Error in auto_rename: {e}")
            return {'renamed': 0, 'total': 0}

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
                    logger.info(f"Executing TeraboxUploaderCLI for {job_id}: {cmd}")
                    
                    # Execute with timeout
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
                    
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
            error_msg = "Upload timeout (2 hours)"
            logger.error(f"Terabox upload timeout for {job_id}: {error_msg}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload timeout: {error_msg}")
            return []
        except Exception as e:
            logger.error(f"Terabox upload error for {job_id}: {e}")
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
            logger.error(f"Doodstream upload error for {job_id}: {e}")
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
                        text=f"{active_downloads[job_id]['folder_name']}\n{message}"
                    )
                    return
                except Exception:
                    # If editing fails, send new message
                    pass
            
            # Send new message
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{active_downloads[job_id]['folder_name']}\n{message}"
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
            
            # Download from Mega.nz with debug info
            success, message = self.mega_manager.download_mega_folder(mega_url, download_path, job_id)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"❌ Download gagal: {message}"
                )
                return
            
            # Check if files actually exist
            files = list(download_path.rglob('*'))
            file_count = len([f for f in files if f.is_file()])
            
            if file_count == 0:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = "No files downloaded"
                await self.upload_manager.send_progress_message(
                    update, context, job_id, "❌ Download gagal: tidak ada file yang terdownload"
                )
                return
            
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOAD_COMPLETED
            active_downloads[job_id]['progress'] = "Download selesai, memulai rename"
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, f"✅ Download selesai! {file_count} files downloaded. Renaming files..."
            )
            
            # Auto-rename files
            active_downloads[job_id]['status'] = DownloadStatus.RENAMING
            active_downloads[job_id]['progress'] = "Renaming files"
            
            user_settings = self.settings_manager.get_user_settings(user_id)
            prefix = user_settings.get('prefix', 'file_')
            rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
            
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"📝 Rename selesai:\n"
                f"📁 {rename_result['renamed']} files renamed dari total {rename_result['total']} files"
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
                    # Wait a moment before cleanup
                    await asyncio.sleep(2)
                    
                    # Check if folder still exists and has files
                    if os.path.exists(download_path):
                        # Double check if upload really completed
                        files_after_upload = list(download_path.rglob('*'))
                        if files_after_upload:
                            shutil.rmtree(download_path)
                            await self.upload_manager.send_progress_message(
                                update, context, job_id, "🧹 Auto-cleanup selesai!"
                            )
                        else:
                            await self.upload_manager.send_progress_message(
                                update, context, job_id, "📁 Folder sudah kosong, skip cleanup"
                            )
                except Exception as e:
                    logger.error(f"Cleanup error for {job_id}: {e}")
                    await self.upload_manager.send_progress_message(
                        update, context, job_id, f"⚠️ Cleanup error: {str(e)}"
                    )
            
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
        f"✅ Download Ditambahkan ke Antrian\n\n"
        f"📁 Folder: {folder_name}\n"
        f"🔗 Link: {mega_url}\n"
        f"🆔 Job ID: {job_id}\n"
        f"📊 Posisi Antrian: #{queue_position + 1}\n"
        f"⚡ Download Aktif: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system status"""
    # Active downloads
    active_text = "📥 DOWNLOAD AKTIF:\n"
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
        active_text += "Tidak ada download aktif\n\n"
    
    # Queue
    queue_list = list(download_queue.queue)
    queue_text = "📊 ANTRIAN:\n"
    if queue_list:
        for i, job in enumerate(queue_list):
            queue_text += f"#{i+1} {job['folder_name']}\n"
    else:
        queue_text += "Antrian kosong\n"
    
    # System info
    system_text = f"""
⚙️ SISTEM INFO:
• Download Aktif: {download_processor.current_processes}/{MAX_CONCURRENT_DOWNLOADS}
• Dalam Antrian: {download_queue.qsize()}
• Mega.nz CMD: {'✅' if mega_manager.check_mega_cmd() else '❌'}
• Akun Tersedia: {len(mega_manager.accounts)}
• Akun Saat Ini: {mega_manager.get_current_account()['email'] if mega_manager.get_current_account() else 'Tidak ada'}
• TeraboxUploaderCLI: {'✅' if TERABOX_CLI_DIR.exists() else '❌'}
• Mega CMD Path: {mega_manager.mega_cmd_path}
    """
    
    full_text = active_text + "\n" + queue_text + "\n" + system_text
    await update.message.reply_text(full_text)

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to check system status"""
    user_id = update.effective_user.id
    
    # Check if user is authorized (you can add user ID checks here)
    debug_info = mega_manager.debug_mega_session()
    
    debug_text = "🔧 DEBUG INFORMATION:\n\n"
    
    # Mega.nz status
    debug_text += "MEGA.NZ STATUS:\n"
    debug_text += f"• Mega-cmd available: {'✅' if mega_manager.check_mega_cmd() else '❌'}\n"
    debug_text += f"• Mega-cmd path: {mega_manager.mega_cmd_path}\n"
    debug_text += f"• Session active: {'✅' if mega_manager.ensure_mega_session() else '❌'}\n"
    
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
    if not context.args:
        await update.message.reply_text(
            "❌ Format: /setprefix <prefix>\n"
            "Contoh: /setprefix TELEGRAM @missyhot22\n"
            "Contoh: /setprefix my files"
        )
        return
    
    prefix = " ".join(context.args)  # Support spaces in prefix
    user_id = update.effective_user.id
    
    settings_manager.update_user_settings(user_id, {'prefix': prefix})
    
    await update.message.reply_text(
        f"✅ Prefix Diubah\n\n"
        f"Prefix baru: {prefix}\n"
        f"Contoh file: {prefix} 01.jpg\n"
        f"{prefix} 02.mp4"
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
    
    await update.message.reply_text(f"✅ Platform upload diubah menjadi: {platform}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    new_auto_upload = not settings.get('auto_upload', True)
    settings_manager.update_user_settings(user_id, {'auto_upload': new_auto_upload})
    
    status = "AKTIF" if new_auto_upload else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-upload: {status}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-cleanup"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    new_auto_cleanup = not settings.get('auto_cleanup', True)
    settings_manager.update_user_settings(user_id, {'auto_cleanup': new_auto_cleanup})
    
    status = "AKTIF" if new_auto_cleanup else "NON-AKTIF"
    await update.message.reply_text(f"✅ Auto-cleanup: {status}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings"""
    user_id = update.effective_user.id
    settings = settings_manager.get_user_settings(user_id)
    
    settings_text = f"""
⚙️ PENGATURAN SAYA

📝 Prefix: {settings.get('prefix', 'file_')}
📤 Platform: {settings.get('platform', 'terabox')}
🔄 Auto-upload: {'✅ AKTIF' if settings.get('auto_upload', True) else '❌ NON-AKTIF'}
🧹 Auto-cleanup: {'✅ AKTIF' if settings.get('auto_cleanup', True) else '❌ NON-AKTIF'}
    """
    
    await update.message.reply_text(settings_text)

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
    mega_available = mega_manager.check_mega_cmd()
    if not mega_available:
        logger.warning("Mega.nz CMD tidak terpasang! Install dengan: sudo snap install mega-cmd")
    else:
        logger.info("Mega.nz CMD terdeteksi")
    
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
    application.add_handler(CommandHandler("debug", debug_command))
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
