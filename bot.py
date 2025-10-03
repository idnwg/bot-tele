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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import tempfile

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
                'max_retries': 3,
                'file_type': 'all_ages',  # all_ages or adult
                'share_type': 'permanent'  # permanent or temporary
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
            logger.info(f"📝 Rename process completed: {rename_result['renamed']}/{rename_result['total']} files renamed")
            return result
        except Exception as e:
            logger.error(f"💥 Error in auto_rename: {e}")
            return {'renamed': 0, 'total': 0}

class TeraboxWebUploader:
    def __init__(self):
        self.upload_url = "https://dm.1024tera.com/webmaster/new/share"
        self.chrome_options = Options()
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--window-size=1920,1080')
        logger.info("🌐 TeraboxWebUploader initialized")
    
    async def upload_folder(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, file_type: str = "all_ages") -> List[str]:
        """Upload folder to Terabox using web interface"""
        logger.info(f"🚀 Starting Terabox web upload for job {job_id}, folder: {folder_path}")
        
        driver = None
        try:
            await self.send_progress_message(update, context, job_id, "🌐 Membuka browser untuk upload...")
            
            # Initialize Chrome driver
            driver = webdriver.Chrome(options=self.chrome_options)
            driver.get(self.upload_url)
            
            await self.send_progress_message(update, context, job_id, "📋 Mengisi form upload...")
            
            # Wait for page to load
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Select file type (All ages or Adult)
            await self.select_file_type(driver, file_type)
            
            # Use local file upload
            await self.select_upload_source(driver, "local")
            
            # Upload the folder
            await self.upload_local_folder(driver, folder_path, update, context, job_id)
            
            # Generate share link
            share_links = await self.generate_share_links(driver, update, context, job_id)
            
            await self.send_progress_message(update, context, job_id, f"✅ Upload berhasil! {len(share_links)} link dihasilkan")
            
            return share_links
            
        except Exception as e:
            logger.error(f"❌ Terabox web upload error: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
            return []
        finally:
            if driver:
                driver.quit()
                logger.info("🔴 Browser closed")
    
    async def select_file_type(self, driver, file_type: str):
        """Select file type (All ages or Adult)"""
        try:
            if file_type == "adult":
                # Select Adult content
                adult_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and @value='adult']"))
                )
                adult_radio.click()
                logger.info("🔞 Selected Adult content type")
            else:
                # Select All ages (default)
                all_ages_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and @value='all_ages']"))
                )
                all_ages_radio.click()
                logger.info("👪 Selected All ages content type")
        except Exception as e:
            logger.warning(f"⚠️ Could not select file type: {e}")
    
    async def select_upload_source(self, driver, source_type: str):
        """Select upload source (TextBox File or Global Search)"""
        try:
            if source_type == "local":
                # Select TextBox File (local upload)
                local_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and contains(@value, 'textbox') or contains(@value, 'local')]"))
                )
                local_radio.click()
                logger.info("📁 Selected local file upload")
        except Exception as e:
            logger.warning(f"⚠️ Could not select upload source: {e}")
    
    async def upload_local_folder(self, driver, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload local folder using file input"""
        try:
            await self.send_progress_message(update, context, job_id, "📤 Mengupload folder...")
            
            # Find file input element
            file_input = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
            )
            
            # Get all files from the folder recursively
            all_files = list(folder_path.rglob('*'))
            files_to_upload = [str(f) for f in all_files if f.is_file()]
            
            logger.info(f"📁 Found {len(files_to_upload)} files to upload")
            await self.send_progress_message(update, context, job_id, f"📁 Mengupload {len(files_to_upload)} files...")
            
            # Upload files in batches to avoid timeouts
            batch_size = 10
            for i in range(0, len(files_to_upload), batch_size):
                batch = files_to_upload[i:i + batch_size]
                file_input.send_keys("\n".join(batch))
                
                # Wait for upload to process
                time.sleep(5)
                
                progress = f"📤 Progress: {min(i + batch_size, len(files_to_upload))}/{len(files_to_upload)} files"
                logger.info(progress)
                await self.send_progress_message(update, context, job_id, progress)
            
            # Wait for all uploads to complete
            WebDriverWait(driver, 300).until(  # 5 minute timeout for uploads
                EC.invisibility_of_element_located((By.XPATH, "//*[contains(text(), 'Uploading') or contains(text(), 'uploading')]"))
            )
            
            logger.info("✅ All files uploaded successfully")
            
        except Exception as e:
            logger.error(f"❌ Folder upload error: {e}")
            raise Exception(f"Folder upload failed: {str(e)}")
    
    async def generate_share_links(self, driver, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str) -> List[str]:
        """Generate share links after upload"""
        try:
            await self.send_progress_message(update, context, job_id, "🔗 Menghasilkan share link...")
            
            # Find and click generate link button
            generate_btn = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Generate Link') or contains(text(), 'generate link')]"))
            )
            generate_btn.click()
            
            # Wait for links to be generated
            time.sleep(10)
            
            # Extract share links from the page
            links = []
            link_elements = driver.find_elements(By.XPATH, "//a[contains(@href, 'tera-box') or contains(@href, 'terabox') or contains(@href, '1024tera')]")
            
            for elem in link_elements:
                href = elem.get_attribute('href')
                if href and href not in links:
                    links.append(href)
            
            logger.info(f"🔗 Found {len(links)} share links")
            return links
            
        except Exception as e:
            logger.error(f"❌ Generate links error: {e}")
            return []
    
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

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_web_uploader = TeraboxWebUploader()
        self.terabox_lock = threading.Lock()
        logger.info("📤 UploadManager initialized dengan Terabox web uploader")

    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox menggunakan web interface baru"""
        logger.info(f"🚀 Starting Terabox web upload untuk job {job_id}, folder: {folder_path}")
        
        try:
            user_id = active_downloads[job_id]['user_id']
            user_settings = settings_manager.get_user_settings(user_id)
            file_type = user_settings.get('file_type', 'all_ages')
            
            await self.send_progress_message(update, context, job_id, 
                f"🌐 Memulai upload ke Terabox...\n"
                f"📁 Folder: {folder_path.name}\n"
                f"🔞 Tipe Konten: {'Adult' if file_type == 'adult' else 'All Ages'}\n"
                f"🌐 URL: {self.terabox_web_uploader.upload_url}"
            )

            # Gunakan lock untuk mencegah multiple concurrent Terabox uploads
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                links = await self.terabox_web_uploader.upload_folder(
                    folder_path, update, context, job_id, file_type
                )
                
                if links:
                    success_msg = (
                        f"✅ Upload ke Terabox berhasil!\n"
                        f"🔗 {len(links)} link dihasilkan\n"
                        f"📁 Folder: {folder_path.name}"
                    )
                    logger.info(f"✅ {success_msg}")
                    
                    # Send links as separate messages
                    for i, link in enumerate(links, 1):
                        link_msg = f"🔗 Link {i}: {link}"
                        await context.bot.send_message(
                            chat_id=active_downloads[job_id]['chat_id'],
                            text=link_msg
                        )
                    
                    return links
                else:
                    error_msg = "Upload gagal: Tidak ada link yang dihasilkan"
                    logger.error(f"❌ {error_msg}")
                    raise Exception(error_msg)
                    
        except Exception as e:
            logger.error(f"💥 Terabox web upload error untuk {job_id}: {e}")
            await self.send_progress_message(update, context, job_id, f"❌ Upload error: {str(e)}")
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
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id, 
                    f"📤 Uploading ke {platform}..."
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

# Telegram Bot Handlers
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
/setfiletype <all_ages|adult> - Set tipe konten
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
- file_type: Tipe konten (all_ages/adult)
- auto_upload: Auto upload setelah download
- auto_cleanup: Hapus file lokal setelah upload

Contoh commands:
/download https://mega.nz/folder/abc123
/setprefix my_files
/setplatform terabox
/setfiletype all_ages
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
        file_type = user_settings.get('file_type', 'all_ages')
        
        response_text = (
            f"✅ Download Job Ditambahkan\n\n"
            f"📁 Folder: {folder_name}\n"
            f"🔗 URL: {mega_url}\n"
            f"🆔 Job ID: {job_id}\n"
            f"📊 Antrian: {download_queue.qsize() + 1}\n\n"
            f"⚙️ Pengaturan:\n"
            f"• Platform: {platform}\n"
            f"• Tipe Konten: {'Adult' if file_type == 'adult' else 'All Ages'}\n"
            f"• Auto Upload: {'✅' if auto_upload else '❌'}\n\n"
            f"Gunakan /status untuk memantau progress."
        )
        
        await update.message.reply_text(response_text)
        logger.info(f"📥 Added download job {job_id} untuk user {update.effective_user.id}")
        
    except Exception as e:
        logger.error(f"💥 Error in download_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_file_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set file type for user"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap sertakan tipe konten\n"
                "Contoh: /setfiletype all_ages"
            )
            return
        
        file_type = context.args[0].lower()
        if file_type not in ['all_ages', 'adult']:
            await update.message.reply_text(
                "❌ Tipe konten tidak valid!\n"
                "Pilihan: all_ages, adult"
            )
            return
        
        user_id = update.effective_user.id
        settings_manager.update_user_settings(user_id, {'file_type': file_type})
        
        type_name = "All Ages" if file_type == "all_ages" else "Adult"
        await update.message.reply_text(f"✅ Tipe konten berhasil diubah ke: {type_name}")
        
    except Exception as e:
        logger.error(f"💥 Error in set_file_type: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ... (Other command handlers remain the same as in the original code)

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
    application.add_handler(CommandHandler("setfiletype", set_file_type))
    # ... (Add other command handlers)
    
    # Start bot
    logger.info("✅ Bot started successfully!")
    application.run_polling()

if __name__ == '__main__':
    main()
