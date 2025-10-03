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

# Load environment variables
load_dotenv()

# Configure logging with more detailed format
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    level=logging.INFO,
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
TERABOX_CLI_DIR = Path('TeraboxUploaderCLI')
MAX_CONCURRENT_DOWNLOADS = 2
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024 * 1024  # 50GB

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
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:
                logger.info("User settings file not found, creating new one")
                return {}
        except Exception as e:
            logger.error(f"Failed to load user settings: {e}")
            return {}
    
    def save_settings(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
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
                'auto_cleanup': True,
                'max_retries': 3
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

class SystemMonitor:
    @staticmethod
    def get_system_status() -> Dict[str, Any]:
        """Get system resource status"""
        try:
            disk = psutil.disk_usage(str(DOWNLOAD_BASE))
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
                'active_processes': threading.active_count()
            }
        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return {}

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
        """Load mega accounts from environment variables"""
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
            logger.error("No Mega.nz accounts found!")
        else:
            logger.info(f"Total {len(accounts)} Mega.nz accounts available")
        
        return accounts
    
    def check_mega_get(self) -> bool:
        """Check if mega-get command is available and working"""
        try:
            cmd = [self.mega_get_path, '--help']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            logger.info(f"mega-get executable check passed")
            return True
        except Exception as e:
            logger.error(f"mega-get check error: {e}")
            return False
    
    def get_current_account(self) -> Optional[Dict]:
        if not self.accounts:
            return None
        return self.accounts[self.current_account_index]
    
    def rotate_account(self):
        if len(self.accounts) > 1:
            old_email = self.get_current_account()['email']
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
            new_email = self.get_current_account()['email']
            logger.info(f"🔄 Rotated account: {old_email} -> {new_email}")
        else:
            logger.warning("Cannot rotate accounts: only one account available")
    
    def check_disk_space(self, required_gb: float = 5.0) -> Tuple[bool, float]:
        """Check if there's enough disk space"""
        try:
            disk = psutil.disk_usage(str(DOWNLOAD_BASE))
            free_gb = disk.free / (1024**3)
            has_space = free_gb >= required_gb
            logger.info(f"Disk space check: {free_gb:.2f}GB free, required: {required_gb}GB")
            return has_space, free_gb
        except Exception as e:
            logger.error(f"Error checking disk space: {e}")
            return False, 0.0
    
    def debug_mega_session(self) -> Dict:
        """Debug function to check mega session status"""
        debug_info = {}
        
        try:
            # Check if mega-get executable exists and is accessible
            debug_info['mega_get_path'] = self.mega_get_path
            debug_info['mega_get_exists'] = os.path.exists(self.mega_get_path)
            debug_info['mega_get_executable'] = os.access(self.mega_get_path, os.X_OK)
            
            # Check disk space
            has_space, free_gb = self.check_disk_space()
            debug_info['disk_free_gb'] = free_gb
            debug_info['has_sufficient_space'] = has_space
            
            # Check if downloads directory exists and is writable
            download_test = DOWNLOAD_BASE / 'test_write'
            try:
                DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
                download_test.touch()
                debug_info['downloads_writable'] = True
                download_test.unlink()
                logger.info("✅ Downloads directory is writable")
            except Exception as e:
                debug_info['downloads_writable'] = False
                debug_info['downloads_error'] = str(e)
                logger.error(f"❌ Downloads directory not writable: {e}")
            
            # Check account status
            debug_info['current_account'] = self.get_current_account()['email'] if self.get_current_account() else None
            debug_info['total_accounts'] = len(self.accounts)
            
            # System status
            debug_info.update(SystemMonitor.get_system_status())
            
            return debug_info
            
        except Exception as e:
            debug_info['error'] = str(e)
            logger.error(f"❌ Debug session error: {e}")
            return debug_info
    
    def download_mega_folder(self, folder_url: str, download_path: Path, job_id: str) -> Tuple[bool, str]:
        """Download folder from Mega.nz using mega-get with detailed logging"""
        logger.info(f"🚀 Starting download process for job {job_id}")
        logger.info(f"📥 URL: {folder_url}")
        logger.info(f"📁 Download path: {download_path}")
        
        max_retries = 3
        retry_count = 0
        
        # Check disk space before starting
        has_space, free_gb = self.check_disk_space(required_gb=5.0)
        if not has_space:
            error_msg = f"Insufficient disk space: {free_gb:.2f}GB free, need at least 5GB"
            logger.error(f"❌ {error_msg}")
            return False, error_msg
        
        while retry_count < max_retries:
            try:
                # Debug session first
                debug_info = self.debug_mega_session()
                logger.info(f"🔧 Debug info for {job_id}: {json.dumps(debug_info, indent=2)}")
                
                # Ensure base download directory exists
                DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
                logger.info(f"📁 Base download directory ready: {DOWNLOAD_BASE}")
                
                # Test write permission in base directory
                test_file = DOWNLOAD_BASE / 'test_write.txt'
                try:
                    test_file.write_text('test')
                    test_file.unlink()
                    logger.info("✅ Write test successful")
                except Exception as e:
                    error_msg = f"Cannot write to download directory: {str(e)}"
                    logger.error(f"❌ {error_msg}")
                    return False, error_msg
                
                # Change to base download directory for mega-get
                original_cwd = os.getcwd()
                os.chdir(DOWNLOAD_BASE)
                logger.info(f"📂 Changed working directory to base: {DOWNLOAD_BASE}")
                
                try:
                    # Download using mega-get
                    download_cmd = [self.mega_get_path, folder_url]
                    logger.info(f"⚡ Executing download command: {' '.join(download_cmd)}")
                    
                    # Execute download with timeout
                    start_time = time.time()
                    logger.info(f"⏰ Download started at: {datetime.now()}")
                    
                    result = subprocess.run(download_cmd, capture_output=True, text=True, timeout=7200)  # 2 hours
                    
                    end_time = time.time()
                    download_duration = end_time - start_time
                    logger.info(f"⏰ Download completed at: {datetime.now()}, duration: {download_duration:.2f}s")
                    
                    # Log command results
                    logger.info(f"📊 Download command return code: {result.returncode}")
                    if result.stdout:
                        logger.info(f"📤 Download stdout: {result.stdout[-1000:]}")  # Last 1000 chars
                    if result.stderr:
                        logger.warning(f"📥 Download stderr: {result.stderr[-1000:]}")
                    
                    # Return to original directory
                    os.chdir(original_cwd)
                    logger.info("📂 Returned to original working directory")
                    
                    if result.returncode == 0:
                        # Wait for files to stabilize
                        logger.info("⏳ Waiting for files to stabilize...")
                        time.sleep(5)
                        
                        # Check if files were actually downloaded
                        all_files = list(DOWNLOAD_BASE.rglob('*'))
                        files = [f for f in all_files if f.is_file()]
                        directories = [f for f in all_files if f.is_dir()]
                        
                        logger.info(f"📊 File check results: {len(files)} files, {len(directories)} directories")
                        
                        # Log all files and directories for debugging
                        for f in files[:10]:  # Log first 10 files only
                            try:
                                file_size = f.stat().st_size
                                logger.info(f"📄 File: {f.relative_to(DOWNLOAD_BASE)} ({file_size} bytes)")
                            except Exception as e:
                                logger.warning(f"⚠️ Could not stat file {f}: {e}")
                        
                        for d in directories[:5]:  # Log first 5 directories
                            logger.info(f"📁 Directory: {d.relative_to(DOWNLOAD_BASE)}")
                        
                        total_files = len(files)
                        
                        if total_files == 0:
                            error_msg = "Download completed but no files were found"
                            logger.error(f"❌ {error_msg}")
                            # Check output for clues
                            if "error" in result.stdout.lower() or "error" in result.stderr.lower():
                                error_msg = f"Download completed with errors: {result.stdout[-500]} {result.stderr[-500]}"
                            elif "no such file" in result.stdout.lower() or "no such file" in result.stderr.lower():
                                error_msg = "Folder not found or inaccessible"
                            return False, error_msg
                        
                        success_msg = f"Download successful! {total_files} files downloaded in {download_duration:.2f}s"
                        logger.info(f"✅ {success_msg}")
                        return True, success_msg
                    else:
                        error_msg = result.stderr if result.stderr else result.stdout
                        logger.error(f"❌ Download command failed: {error_msg}")
                        
                        # Check for specific errors and handle them
                        if "quota exceeded" in error_msg.lower() or "storage" in error_msg.lower():
                            logger.warning("🔄 Quota exceeded, rotating account...")
                            self.rotate_account()
                            retry_count += 1
                            if retry_count < max_retries:
                                logger.info(f"🔄 Retrying download with different account (attempt {retry_count + 1}/{max_retries})")
                                continue
                            else:
                                return False, "All accounts have exceeded storage quota. Please try again later."
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
        
        return False, f"Download failed after {max_retries} retries due to quota issues"

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
            media_files.sort(key=lambda x: x.name.lower())
            
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
                        # Check if target file already exists
                        if new_path.exists():
                            # Add timestamp to avoid conflicts
                            timestamp = int(time.time())
                            new_name = f"{prefix} {number_str}_{timestamp}{file_path.suffix}"
                            new_path = file_path.parent / new_name
                        
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
        
        # Counter global untuk urutan job upload ke Terabox
        self._job_counter = 1
        self._counter_lock = threading.Lock()
        
        logger.info("📤 UploadManager initialized dengan job counter dan struktur folder /bot/")

    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox menggunakan TeraboxUploaderCLI dengan mapping folder /bot/1 sampai /bot/10"""
        logger.info(f"🚀 Starting Terabox upload untuk job {job_id}, folder: {folder_path}")
        
        try:
            if not TERABOX_CLI_DIR.exists():
                error_msg = "TeraboxUploaderCLI directory tidak ditemukan!"
                logger.error(f"❌ {error_msg}")
                await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                return []

            # Check jika main.py exists
            main_py = TERABOX_CLI_DIR / 'main.py'
            if not main_py.exists():
                error_msg = "main.py tidak ditemukan di directory TeraboxUploaderCLI!"
                logger.error(f"❌ {error_msg}")
                await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                return []

            # Dapatkan nomor job dan tentukan folder tujuan di dalam /bot/
            with self._counter_lock:
                job_number = self._job_counter
                self._job_counter += 1
                if self._job_counter > 1000:  # Reset setelah 1000 job
                    self._job_counter = 1

            # Mapping job number ke folder number (1-10) dalam /bot/
            folder_number = (job_number - 1) % 10 + 1
            destination_folder = f"/bot/{folder_number}"

            logger.info(f"📁 Mapping job {job_number} ke Terabox folder: {destination_folder}")
            
            await self.send_progress_message(
                update, context, job_id, 
                f"📤 Memulai upload ke Terabox...\n"
                f"📂 Folder tujuan: {destination_folder}\n"
                f"🔢 Urutan Job: #{job_number}\n"
                f"🏷️ Base Directory: /bot"
            )

            # Gunakan lock untuk mencegah multiple concurrent Terabox uploads
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                # Run TeraboxUploaderCLI
                old_cwd = os.getcwd()
                os.chdir(TERABOX_CLI_DIR)
                logger.info(f"📂 Changed ke TeraboxUploaderCLI directory: {TERABOX_CLI_DIR}")
                
                try:
                    # Check jika Python tersedia
                    check_cmd = ['python3', '--version']  # Try python3 first
                    try:
                        subprocess.run(check_cmd, capture_output=True, timeout=10)
                        python_cmd = 'python3'
                    except:
                        python_cmd = 'python'
                    
                    logger.info(f"🐍 Using Python command: {python_cmd}")

                    # Install dependencies jika diperlukan
                    requirements_file = TERABOX_CLI_DIR / 'requirements.txt'
                    if requirements_file.exists():
                        logger.info("📦 Requirements.txt ditemukan, checking dependencies...")
                        install_cmd = [python_cmd, '-m', 'pip', 'install', '-r', 'requirements.txt']
                        install_result = subprocess.run(install_cmd, capture_output=True, text=True, timeout=300)
                        if install_result.returncode == 0:
                            logger.info("✅ Dependencies installed successfully")
                        else:
                            logger.warning(f"⚠️ Dependency installation issues: {install_result.stderr}")

                    # Run uploader dengan parameter destination folder yang baru
                    cmd = [
                        python_cmd, 'main.py', 
                        '--source', str(folder_path),
                        '--destination', destination_folder
                    ]
                    
                    logger.info(f"⚡ Executing TeraboxUploaderCLI: {' '.join(cmd)}")
                    
                    # Execute dengan timeout dan capture output
                    start_time = time.time()
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        universal_newlines=True
                    )
                    
                    # Read output real-time
                    stdout_lines = []
                    stderr_lines = []
                    
                    try:
                        while True:
                            if process.poll() is not None:
                                break
                            
                            # Read stdout
                            stdout_line = process.stdout.readline()
                            if stdout_line:
                                stdout_lines.append(stdout_line.strip())
                                if "upload" in stdout_line.lower() or "progress" in stdout_line.lower():
                                    logger.info(f"📤 TeraboxUploaderCLI: {stdout_line.strip()}")
                            
                            # Read stderr  
                            stderr_line = process.stderr.readline()
                            if stderr_line:
                                stderr_lines.append(stderr_line.strip())
                                logger.warning(f"📥 TeraboxUploaderCLI stderr: {stderr_line.strip()}")
                            
                            time.sleep(0.1)
                        
                        # Get remaining output setelah proses selesai
                        remaining_stdout, remaining_stderr = process.communicate()
                        if remaining_stdout:
                            stdout_lines.extend(remaining_stdout.splitlines())
                            for line in remaining_stdout.splitlines():
                                logger.info(f"📤 TeraboxUploaderCLI stdout: {line}")
                        if remaining_stderr:
                            stderr_lines.extend(remaining_stderr.splitlines())
                            for line in remaining_stderr.splitlines():
                                logger.warning(f"📥 TeraboxUploaderCLI stderr: {line}")
                                
                    except Exception as e:
                        logger.error(f"💥 Error reading process output: {e}")
                        process.kill()
                        process.wait()
                    
                    end_time = time.time()
                    upload_duration = end_time - start_time
                    returncode = process.returncode
                    
                    logger.info(f"📊 TeraboxUploaderCLI completed dalam {upload_duration:.2f}s, returncode: {returncode}")
                    
                    if returncode == 0:
                        success_msg = (
                            f"✅ Upload ke Terabox berhasil!\n"
                            f"📂 Folder tujuan: {destination_folder}\n"
                            f"⏱️ Durasi: {upload_duration:.2f}s\n"
                            f"🔢 Job Number: #{job_number}"
                        )
                        logger.info(f"✅ {success_msg}")
                        await self.send_progress_message(update, context, job_id, success_msg)
                        return [f"Upload completed - Terabox folder: {destination_folder}"]
                    else:
                        error_output = "\n".join(stderr_lines[-500:]) if stderr_lines else "\n".join(stdout_lines[-500:])
                        error_msg = f"TeraboxUploaderCLI gagal dengan return code {returncode}: {error_output}"
                        logger.error(f"❌ {error_msg}")
                        
                        # Error messages yang lebih helpful
                        if "login" in error_output.lower() or "auth" in error_output.lower():
                            error_msg = "Authentication gagal. Periksa kredensial Terabox di konfigurasi TeraboxUploaderCLI."
                        elif "quota" in error_output.lower():
                            error_msg = "Storage quota Terabox habis. Mohon free up space di akun Terabox."
                        elif "network" in error_output.lower() or "connection" in error_output.lower():
                            error_msg = "Network connection error. Periksa koneksi internet."
                        elif "folder" in error_output.lower() or "destination" in error_output.lower():
                            error_msg = f"Folder tujuan {destination_folder} tidak ditemukan. Pastikan folder /bot/1 sampai /bot/10 sudah dibuat manual di Terabox."
                        
                        raise Exception(error_msg)
                        
                except subprocess.TimeoutExpired:
                    error_msg = "Upload timeout (2 jam)"
                    logger.error(f"⏰ Terabox upload timeout untuk {job_id}: {error_msg}")
                    await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                    return []
                except Exception as e:
                    logger.error(f"💥 Terabox upload error untuk {job_id}: {e}")
                    await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
                    return []
                finally:
                    os.chdir(old_cwd)
                    logger.info("📂 Returned ke original working directory")
                    
        except Exception as e:
            logger.error(f"💥 Terabox upload setup error untuk {job_id}: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload setup error: {str(e)}")
            return []

    async def upload_to_doodstream(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload video files ke Doodstream"""
        logger.info(f"🚀 Starting Doodstream upload untuk job {job_id}, folder: {folder_path}")
        try:
            await self.send_progress_message(update, context, job_id, "📤 Memulai upload ke Doodstream...")
            
            if not self.doodstream_key:
                error_msg = "Doodstream API key tidak ditemukan!"
                logger.error(f"❌ {error_msg}")
                await self.send_progress_message(update, context, job_id, f"❌ {error_msg}")
                return []
            
            links = []
            video_files = [f for f in folder_path.rglob('*') 
                          if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            total_files = len(video_files)
            
            logger.info(f"📊 Found {total_files} video files untuk Doodstream upload")
            
            if total_files == 0:
                logger.warning("📭 No video files found untuk Doodstream upload")
                await self.send_progress_message(update, context, job_id, "📭 Tidak ada file video untuk diupload")
                return []
            
            uploaded_count = 0
            
            for i, file_path in enumerate(video_files, 1):
                if not self.is_job_active(job_id):
                    logger.warning(f"⏹️  Upload cancelled untuk job {job_id}")
                    break
                    
                try:
                    logger.info(f"📤 Uploading file {i}/{total_files}: {file_path.name}")
                    await self.send_progress_message(
                        update, context, job_id,
                        f"📤 Upload progress: {i}/{total_files}\n📹 Processing: {file_path.name}"
                    )
                    
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
                            f"❌ Upload gagal: {file_path.name}"
                        )
                except Exception as e:
                    logger.error(f"💥 Error uploading {file_path}: {e}")
            
            logger.info(f"📊 Doodstream upload completed: {uploaded_count}/{total_files} files uploaded")
            
            if uploaded_count > 0:
                await self.send_progress_message(
                    update, context, job_id,
                    f"✅ Doodstream upload selesai!\n🔗 {uploaded_count} links generated"
                )
            
            return links
        except Exception as e:
            logger.error(f"💥 Doodstream upload error untuk {job_id}: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
            return []
    
    async def upload_single_file_to_doodstream(self, file_path: Path) -> str:
        """Upload single file ke Doodstream API"""
        try:
            logger.info(f"📤 Uploading single file ke Doodstream: {file_path}")
            url = "https://doodstream.com/api/upload"
            
            # Check file size
            file_size = file_path.stat().st_size
            if file_size > 500 * 1024 * 1024:  # 500MB limit
                logger.error(f"❌ File too large for Doodstream: {file_size} bytes")
                return ""
            
            with open(file_path, 'rb') as f:
                files = {'file': f}
                data = {'key': self.doodstream_key}
                
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3600)) as session:
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
        """Send atau update progress message"""
        try:
            if job_id not in active_downloads:
                logger.warning(f"⚠️  Job {job_id} not found in active_downloads, cannot send progress message")
                return
                
            chat_id = active_downloads[job_id]['chat_id']
            
            # Store the latest progress message untuk job ini
            if 'progress_message_id' in active_downloads[job_id]:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=active_downloads[job_id]['progress_message_id'],
                        text=f"{active_downloads[job_id]['folder_name']}\n{message}"
                    )
                    logger.debug(f"📝 Updated progress message untuk job {job_id}")
                    return
                except Exception as e:
                    logger.warning(f"⚠️  Failed to edit progress message untuk job {job_id}: {e}")
                    # If editing fails, send new message
                    pass
            
            # Send new message
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{active_downloads[job_id]['folder_name']}\n{message}"
            )
            active_downloads[job_id]['progress_message_id'] = msg.message_id
            logger.debug(f"📤 Sent new progress message untuk job {job_id}, message_id: {msg.message_id}")
            
        except Exception as e:
            logger.error(f"💥 Error sending progress message untuk job {job_id}: {e}")
    
    def is_job_active(self, job_id: str) -> bool:
        is_active = job_id in active_downloads and active_downloads[job_id]['status'] != DownloadStatus.COMPLETED
        if not is_active:
            logger.info(f"⏹️  Job {job_id} is no longer active")
        return is_active

    # Method untuk monitoring job counter
    def get_job_counter_status(self) -> Dict:
        """Get current status job counter untuk debugging"""
        return {
            'current_job_counter': self._job_counter,
            'next_folder': f"/bot/{(self._job_counter - 1) % 10 + 1}",
            'counter_locked': self._counter_lock.locked()
        }

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
                # Check jika kita bisa start new downloads
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
        logger.info(f"🔄 Starting single download process untuk job {job_data['job_id']}")
        asyncio.run(self._async_process_single_download(job_data))
    
    async def _async_process_single_download(self, job_data: Dict):
        """Async version of single download processing"""
        job_id = job_data['job_id']
        folder_name = job_data['folder_name']
        mega_url = job_data['mega_url']
        user_id = job_data['user_id']
        update = job_data['update']
        context = job_data['context']
        
        logger.info(f"🚀 Processing download job {job_id} untuk user {user_id}")
        logger.info(f"📁 Folder: {folder_name}, URL: {mega_url}")
        
        try:
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOADING
            active_downloads[job_id]['progress'] = "Memulai download dari Mega.nz"
            
            await self.upload_manager.send_progress_message(
                update, context, job_id, "📥 Memulai download dari Mega.nz..."
            )
            
            # Download dari Mega.nz dengan debug info
            logger.info(f"🔽 Starting Mega.nz download untuk job {job_id}")
            
            # mega-get akan otomatis membuat folder berdasarkan nama folder di Mega.nz
            success, message = self.mega_manager.download_mega_folder(mega_url, DOWNLOAD_BASE, job_id)
            
            if not success:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = message
                logger.error(f"❌ Download failed untuk job {job_id}: {message}")
                await self.upload_manager.send_progress_message(
                    update, context, job_id, f"❌ Download gagal: {message}"
                )
                return
            
            # Check jika files actually exist - cari folder yang dibuat oleh mega-get
            all_files = list(DOWNLOAD_BASE.rglob('*'))
            files = [f for f in all_files if f.is_file()]
            directories = [f for f in all_files if f.is_dir()]
            
            file_count = len(files)
            
            if file_count == 0:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['error'] = "No files downloaded"
                logger.error(f"❌ No files downloaded untuk job {job_id}")
                await self.upload_manager.send_progress_message(
                    update, context, job_id, "❌ Download gagal: tidak ada file yang terdownload"
                )
                return
            
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.DOWNLOAD_COMPLETED
            active_downloads[job_id]['progress'] = "Download selesai, mencari folder untuk rename"
            
            logger.info(f"✅ Download completed untuk job {job_id}, {file_count} files downloaded")
            await self.upload_manager.send_progress_message(
                update, context, job_id, f"✅ Download selesai! {file_count} files downloaded. Mencari folder untuk rename..."
            )
            
            # Cari folder yang berisi file-file yang didownload
            download_folders = [d for d in DOWNLOAD_BASE.iterdir() if d.is_dir()]
            target_folder = None
            
            if download_folders:
                # Ambil folder terbaru (yang paling baru dibuat)
                download_folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                target_folder = download_folders[0]
                logger.info(f"📁 Found download folder: {target_folder}")
            else:
                # Jika tidak ada folder, gunakan base directory
                target_folder = DOWNLOAD_BASE
                logger.info(f"📁 Using base directory for files: {target_folder}")
            
            # Auto-rename files di folder yang ditemukan
            active_downloads[job_id]['status'] = DownloadStatus.RENAMING
            active_downloads[job_id]['progress'] = "Renaming files"
            
            user_settings = self.settings_manager.get_user_settings(user_id)
            prefix = user_settings.get('prefix', 'file_')
            logger.info(f"📝 Starting file rename dengan prefix '{prefix}' untuk job {job_id} di folder {target_folder}")
            
            rename_result = self.file_manager.auto_rename_media_files(target_folder, prefix)
            
            logger.info(f"📝 Rename completed untuk job {job_id}: {rename_result['renamed']}/{rename_result['total']} files renamed")
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"📝 Rename selesai:\n"
                f"📁 {rename_result['renamed']} files renamed dari total {rename_result['total']} files"
            )
            
            # Auto-upload jika enabled
            if user_settings.get('auto_upload', True):
                active_downloads[job_id]['status'] = DownloadStatus.UPLOADING
                active_downloads[job_id]['progress'] = "Uploading files"
                
                platform = user_settings.get('platform', 'terabox')
                logger.info(f"📤 Starting auto-upload ke {platform} untuk job {job_id}")
                
                # Tambahkan info job counter
                counter_status = self.upload_manager.get_job_counter_status()
                logger.info(f"🔢 Job counter status: {counter_status}")
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id, 
                    f"📤 Uploading ke {platform}...\n"
                    f"🔢 Urutan Job: #{counter_status['current_job_counter']}\n"
                    f"📂 Folder Tujuan: {counter_status['next_folder']}"
                )
                
                if platform == 'terabox':
                    links = await self.upload_manager.upload_to_terabox(target_folder, update, context, job_id)
                else:
                    links = await self.upload_manager.upload_to_doodstream(target_folder, update, context, job_id)
                
                # Jangan kirim duplicate success message untuk Terabox
                if platform != 'terabox' and links:
                    logger.info(f"✅ Upload completed untuk job {job_id}: {len(links)} links generated")
                    await self.upload_manager.send_progress_message(
                        update, context, job_id,
                        f"✅ Upload selesai!\n🔗 {len(links)} links generated"
                    )
            else:
                logger.info(f"⏭️  Auto-upload disabled untuk job {job_id}, skipping upload")
            
            # Auto-cleanup jika enabled
            if user_settings.get('auto_cleanup', True):
                try:
                    # Tunggu sebentar sebelum cleanup
                    await asyncio.sleep(2)
                    
                    # Cleanup folder yang berisi file-file yang didownload
                    if target_folder.exists() and target_folder != DOWNLOAD_BASE:
                        # Double check jika upload benar-benar completed
                        files_after_upload = list(target_folder.rglob('*'))
                        if files_after_upload:
                            logger.info(f"🧹 Starting auto-cleanup untuk job {job_id}, folder: {target_folder}")
                            shutil.rmtree(target_folder)
                            logger.info(f"✅ Auto-cleanup completed untuk job {job_id}")
                            await self.upload_manager.send_progress_message(
                                update, context, job_id, "🧹 Auto-cleanup selesai!"
                            )
                        else:
                            logger.info(f"📁 Folder sudah kosong untuk job {job_id}, skipping cleanup")
                    else:
                        logger.warning(f"⚠️  Folder tidak ditemukan selama cleanup untuk job {job_id}: {target_folder}")
                except Exception as e:
                    logger.error(f"💥 Cleanup error untuk {job_id}: {e}")
                    await self.upload_manager.send_progress_message(
                        update, context, job_id, f"⚠️ Cleanup error: {str(e)}"
                    )
            else:
                logger.info(f"⏭️  Auto-cleanup disabled untuk job {job_id}, skipping cleanup")
            
            # Mark as completed
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['progress'] = "Semua proses selesai"
            active_downloads[job_id]['completed_at'] = datetime.now().isoformat()
            
            # Pindah ke completed downloads
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            logger.info(f"🎉 Semua proses berhasil diselesaikan untuk job {job_id}")
            await self.upload_manager.send_progress_message(
                update, context, job_id, "✅ Semua proses selesai!"
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

# Telegram Bot Handlers - PERBAIKAN: Hapus format HTML/Markdown yang bermasalah
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when the command /start is issued."""
    user = update.effective_user
    welcome_text = f"""
🤖 Mega Downloader Bot

Halo {user.first_name}!

Saya adalah bot untuk mendownload folder dari Mega.nz dan menguploadnya ke berbagai platform.

Fitur:
📥 Download folder dari Mega.nz
🔄 Auto-rename file media  
📤 Upload ke Terabox/Doodstream
⚙️ Customizable settings

Commands:
/download <url> - Download folder Mega.nz
/upload <path> - Upload folder manual
/status - Lihat status download
/mysettings - Lihat pengaturan
/setprefix <prefix> - Set file prefix
/setplatform <terabox|doodstream> - Set platform upload
/autoupload <on|off> - Toggle auto upload
/autocleanup <on|off> - Toggle auto cleanup
/debug - Info debug system
/cleanup - Bersihkan file temporary

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

Pengaturan yang tersedia:
- prefix: Nama prefix untuk file setelah di-rename
- platform: Platform upload (terabox/doodstream)  
- auto_upload: Auto upload setelah download
- auto_cleanup: Hapus file lokal setelah upload

Contoh commands:
/download https://mega.nz/folder/abc123
/setprefix my_files
/setplatform terabox
/autoupload on
/status
    """
    await update.message.reply_text(help_text)

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /download command"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan URL Mega.nz\n"
                "Contoh: /download https://mega.nz/folder/abc123"
            )
            return
        
        mega_url = context.args[0]
        
        # Validate Mega.nz URL
        if not re.match(r'https://mega\.nz/folder/[a-zA-Z0-9_-]+', mega_url):
            await update.message.reply_text(
                "❌ URL Mega.nz tidak valid!\n"
                "Format yang benar: https://mega.nz/folder/ID_FOLDER"
            )
            return
        
        # Generate job ID
        job_id = f"job_{int(time.time())}_{update.effective_user.id}"
        
        # Get folder name from URL or use default
        folder_name = f"Folder_{int(time.time())}"
        if '#' in mega_url:
            folder_name = mega_url.split('#')[-1]
        
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
        platform = user_settings.get('platform', 'terabox')
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
        logger.info(f"📥 Added download job {job_id} untuk user {update.effective_user.id}")
        
    except Exception as e:
        logger.error(f"💥 Error in download_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manual upload command"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan path folder\n"
                "Contoh: /upload /path/to/folder"
            )
            return
        
        folder_path = Path(context.args[0])
        if not folder_path.exists() or not folder_path.is_dir():
            await update.message.reply_text("❌ Folder tidak ditemukan!")
            return
        
        user_id = update.effective_user.id
        user_settings = settings_manager.get_user_settings(user_id)
        platform = user_settings.get('platform', 'terabox')
        
        job_id = f"upload_{int(time.time())}_{user_id}"
        
        # Initialize active download
        active_downloads[job_id] = {
            'job_id': job_id,
            'folder_name': folder_path.name,
            'user_id': user_id,
            'chat_id': update.effective_chat.id,
            'status': DownloadStatus.UPLOADING,
            'progress': 'Memulai upload manual...',
            'created_at': datetime.now().isoformat()
        }
        
        await update.message.reply_text(f"📤 Memulai upload manual ke {platform}...")
        
        if platform == 'terabox':
            links = await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
        else:
            links = await upload_manager.upload_to_doodstream(folder_path, update, context, job_id)
        
        # Mark as completed
        active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
        active_downloads[job_id]['progress'] = "Upload manual selesai"
        
        if links:
            await update.message.reply_text(f"✅ Upload selesai! {len(links)} links generated")
        else:
            await update.message.reply_text("⚠️ Upload completed tetapi tidak ada links yang dihasilkan")
            
    except Exception as e:
        logger.error(f"💥 Error in upload_command: {e}")
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

Active Jobs:
"""
        
        if user_active_jobs:
            for job_id, job in list(user_active_jobs.items())[:5]:  # Show last 5
                status_text += f"\n📁 {job['folder_name']}\n"
                status_text += f"🆔 {job_id}\n"
                status_text += f"📊 {job['status'].value}\n"
                status_text += f"⏰ {job.get('progress', 'Processing...')}\n"
        else:
            status_text += "\nTidak ada active jobs"
        
        status_text += f"\nCompleted Jobs (last 3):"
        
        if user_completed_jobs:
            for job_id, job in list(user_completed_jobs.items())[-3:]:  # Show last 3
                status_text += f"\n📁 {job['folder_name']}\n"
                status_text += f"🆔 {job_id}\n"
                status_text += f"✅ {job['status'].value}\n"
                if job.get('completed_at'):
                    status_text += f"⏰ {job['completed_at'][:19]}\n"
        else:
            status_text += "\nTidak ada completed jobs"
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"💥 Error in status_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def counter_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Terabox job counter status"""
    try:
        counter_status = upload_manager.get_job_counter_status()
        system_status = SystemMonitor.get_system_status()
        
        status_text = f"""
🔢 Terabox Job Counter Status

Counter Info:
🔄 Current Job Counter: #{counter_status['current_job_counter']}
📂 Next Folder: {counter_status['next_folder']}
🔒 Counter Locked: {'✅' if counter_status['counter_locked'] else '❌'}

System Resources:
💾 Disk Free: {system_status.get('disk_free_gb', 0):.1f}GB
🧠 Memory Used: {system_status.get('memory_used_percent', 0):.1f}%
⚡ CPU Used: {system_status.get('cpu_used_percent', 0):.1f}%

Active Processes:
📥 Download Processes: {system_status.get('active_processes', 0)}
🔄 Active Downloads: {system_status.get('active_downloads', 0)}
📋 Queue Size: {system_status.get('queue_size', 0)}
        """
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"💥 Error in counter_status_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show debug information"""
    try:
        debug_info = mega_manager.debug_mega_session()
        system_status = SystemMonitor.get_system_status()
        
        debug_text = f"""
🐛 Debug Information

Mega.nz Status:
✅ mega-get Available: {debug_info.get('mega_get_exists', False)}
📂 Downloads Writable: {debug_info.get('downloads_writable', False)}
🔑 Accounts: {debug_info.get('total_accounts', 0)}
📧 Current Account: {debug_info.get('current_account', 'None')}

System Resources:
💾 Disk Free: {system_status.get('disk_free_gb', 0):.1f}GB / {system_status.get('disk_total_gb', 0):.1f}GB
💾 Disk Used: {system_status.get('disk_used_percent', 0):.1f}%
🧠 Memory Free: {system_status.get('memory_free_gb', 0):.1f}GB
🧠 Memory Used: {system_status.get('memory_used_percent', 0):.1f}%
⚡ CPU Used: {system_status.get('cpu_used_percent', 0):.1f}%

Bot Status:
🔄 Active Downloads: {system_status.get('active_downloads', 0)}
📋 Queue Size: {system_status.get('queue_size', 0)}
🔢 Active Processes: {system_status.get('active_processes', 0)}

Terabox Status:
🔢 Job Counter: {upload_manager.get_job_counter_status().get('current_job_counter', 0)}
📂 Next Folder: {upload_manager.get_job_counter_status().get('next_folder', 'Unknown')}
        """
        
        await update.message.reply_text(debug_text)
        
    except Exception as e:
        logger.error(f"💥 Error in debug_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set file prefix for user"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan prefix\n"
                "Contoh: /setprefix my_files"
            )
            return
        
        prefix = context.args[0]
        user_id = update.effective_user.id
        
        settings_manager.update_user_settings(user_id, {'prefix': prefix})
        
        await update.message.reply_text(f"✅ Prefix berhasil diubah menjadi: {prefix}")
        
    except Exception as e:
        logger.error(f"💥 Error in set_prefix: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform for user"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan platform\n"
                "Contoh: /setplatform terabox"
            )
            return
        
        platform = context.args[0].lower()
        if platform not in ['terabox', 'doodstream']:
            await update.message.reply_text(
                "❌ Platform tidak valid!\n"
                "Pilihan: terabox, doodstream"
            )
            return
        
        user_id = update.effective_user.id
        settings_manager.update_user_settings(user_id, {'platform': platform})
        
        await update.message.reply_text(f"✅ Platform upload berhasil diubah ke: {platform}")
        
    except Exception as e:
        logger.error(f"💥 Error in set_platform: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto upload setting"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan on/off\n"
                "Contoh: /autoupload on"
            )
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
        logger.error(f"💥 Error in auto_upload_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto cleanup setting"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan on/off\n"
                "Contoh: /autocleanup on"
            )
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
        logger.error(f"💥 Error in auto_cleanup_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings"""
    try:
        user_id = update.effective_user.id
        settings = settings_manager.get_user_settings(user_id)
        
        settings_text = f"""
⚙️ Pengaturan Anda

📝 Prefix: {settings.get('prefix', 'file_')}
📤 Platform: {settings.get('platform', 'terabox')}
🔄 Auto Upload: {'✅' if settings.get('auto_upload', True) else '❌'}
🧹 Auto Cleanup: {'✅' if settings.get('auto_cleanup', True) else '❌'}
🔄 Max Retries: {settings.get('max_retries', 3)}

Commands untuk mengubah:
/setprefix <prefix> - Ubah file prefix
/setplatform <terabox|doodstream> - Ubah platform
/autoupload <on|off> - Toggle auto upload  
/autocleanup <on|off> - Toggle auto cleanup
        """
        
        await update.message.reply_text(settings_text)
        
    except Exception as e:
        logger.error(f"💥 Error in my_settings: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup temporary files"""
    try:
        await update.message.reply_text("🧹 Memulai cleanup...")
        
        # Cleanup empty directories in downloads
        cleaned_count = 0
        for root, dirs, files in os.walk(DOWNLOAD_BASE, topdown=False):
            for dir_name in dirs:
                dir_path = Path(root) / dir_name
                try:
                    if not any(dir_path.iterdir()):  # Check if directory is empty
                        dir_path.rmdir()
                        cleaned_count += 1
                        logger.info(f"🧹 Cleaned empty directory: {dir_path}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not remove directory {dir_path}: {e}")
        
        # Clear old completed downloads (older than 1 hour)
        current_time = datetime.now()
        old_jobs = []
        for job_id, job in completed_downloads.items():
            if 'completed_at' in job:
                try:
                    completed_time = datetime.fromisoformat(job['completed_at'])
                    if (current_time - completed_time).total_seconds() > 3600:  # 1 hour
                        old_jobs.append(job_id)
                except:
                    pass
        
        for job_id in old_jobs:
            del completed_downloads[job_id]
        
        await update.message.reply_text(
            f"✅ Cleanup selesai!\n"
            f"📁 Directories dibersihkan: {cleaned_count}\n"
            f"🗑️ Old jobs dihapus: {len(old_jobs)}"
        )
        
    except Exception as e:
        logger.error(f"💥 Error in cleanup_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    """Start the bot"""
    logger.info("🚀 Starting Mega Downloader Bot...")
    
    # Create base download directory
    DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
    logger.info(f"📁 Base download directory: {DOWNLOAD_BASE}")
    
    # Check Mega.nz installation
    mega_get_exists = os.path.exists(mega_manager.mega_get_path)
    if not mega_get_exists:
        logger.error("❌ mega-get is not available! Please install mega-cmd: sudo snap install mega-cmd")
    else:
        logger.info("✅ mega-get executable found")
    
    # Check jika accounts are configured
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
    application.add_handler(CommandHandler("counterstatus", counter_status_command))
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
