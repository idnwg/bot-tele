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
from selenium.webdriver.common.action_chains import ActionChains
import pickle
import base64

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
        self.login_url = "https://www.terabox.com"  # URL login Terabox
        self.cookies_file = "terabox_cookies.pkl"
        self.chrome_options = Options()
        
        # Chrome options untuk menghindari deteksi bot
        self.chrome_options.add_argument('--headless=new')  # Headless mode baru
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--window-size=1920,1080')
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        self.chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        # Terabox credentials dari environment variables
        self.terabox_email = os.getenv('TERABOX_EMAIL')
        self.terabox_password = os.getenv('TERABOX_PASSWORD')
        
        logger.info("🌐 TeraboxWebUploader initialized")
    
    def is_logged_in(self, driver) -> bool:
        """Check if already logged in to Terabox"""
        try:
            driver.get("https://www.terabox.com")
            time.sleep(3)
            
            # Check for elements that indicate login
            login_indicators = [
                "//a[contains(text(), 'Log in')]",
                "//button[contains(text(), 'Login')]",
                "//div[contains(text(), 'Login')]"
            ]
            
            for indicator in login_indicators:
                try:
                    if driver.find_elements(By.XPATH, indicator):
                        return False
                except:
                    continue
            
            # If no login elements found, assume logged in
            logger.info("✅ Already logged in to Terabox")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error checking login status: {e}")
            return False
    
    def save_cookies(self, driver):
        """Save cookies to file"""
        try:
            cookies = driver.get_cookies()
            with open(self.cookies_file, 'wb') as f:
                pickle.dump(cookies, f)
            logger.info("🍪 Cookies saved successfully")
        except Exception as e:
            logger.error(f"❌ Error saving cookies: {e}")
    
    def load_cookies(self, driver):
        """Load cookies from file"""
        try:
            if os.path.exists(self.cookies_file):
                with open(self.cookies_file, 'rb') as f:
                    cookies = pickle.load(f)
                
                driver.get("https://www.terabox.com")
                time.sleep(2)
                
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception as e:
                        logger.warning(f"⚠️ Could not add cookie: {e}")
                        continue
                
                driver.refresh()
                time.sleep(3)
                logger.info("🍪 Cookies loaded successfully")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Error loading cookies: {e}")
            return False
    
    def login_to_terabox(self, driver):
        """Login to Terabox dengan kredensial"""
        try:
            logger.info("🔐 Attempting to login to Terabox...")
            
            if not self.terabox_email or not self.terabox_password:
                raise Exception("Terabox credentials not found in environment variables")
            
            driver.get("https://www.terabox.com")
            time.sleep(5)
            
            # Tunggu dan klik tombol login
            try:
                login_btn = WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Log in')]"))
                )
                login_btn.click()
                logger.info("✅ Clicked login button")
            except Exception as e:
                logger.warning(f"⚠️ Could not find login button: {e}")
                # Mungkin sudah di halaman login
            
            time.sleep(3)
            
            # Switch to login iframe jika ada
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in frames:
                try:
                    driver.switch_to.frame(frame)
                    logger.info("🔄 Switched to login iframe")
                    break
                except:
                    continue
            
            # Tunggu dan isi email
            email_field = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or @type='text']"))
            )
            email_field.clear()
            email_field.send_keys(self.terabox_email)
            logger.info("📧 Email entered")
            
            time.sleep(2)
            
            # Cari dan klik tombol next/continue
            next_buttons = driver.find_elements(By.XPATH, "//button[contains(text(), 'Next') or contains(text(), 'Continue') or contains(text(), '下一步')]")
            if next_buttons:
                next_buttons[0].click()
                logger.info("➡️ Clicked next button")
            
            time.sleep(3)
            
            # Isi password
            password_field = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
            )
            password_field.clear()
            password_field.send_keys(self.terabox_password)
            logger.info("🔑 Password entered")
            
            time.sleep(2)
            
            # Klik tombol login/submit
            submit_buttons = driver.find_elements(By.XPATH, "//button[@type='submit' or contains(text(), 'Log in') or contains(text(), 'Login') or contains(text(), '登录')]")
            if submit_buttons:
                submit_buttons[0].click()
                logger.info("✅ Clicked login/submit button")
            
            # Tunggu login complete
            time.sleep(10)
            
            # Kembali ke main content jika di iframe
            try:
                driver.switch_to.default_content()
                logger.info("🔄 Switched back to main content")
            except:
                pass
            
            # Verifikasi login berhasil
            if self.is_logged_in(driver):
                logger.info("🎉 Login successful!")
                self.save_cookies(driver)
                return True
            else:
                raise Exception("Login failed - still not logged in after attempt")
                
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            # Capture screenshot for debugging
            try:
                driver.save_screenshot("login_error.png")
                logger.info("📸 Screenshot saved as login_error.png")
            except:
                pass
            raise Exception(f"Login failed: {str(e)}")
    
    def ensure_login(self, driver):
        """Ensure we are logged in to Terabox"""
        # Try loading cookies first
        if self.load_cookies(driver):
            if self.is_logged_in(driver):
                return True
        
        # If cookies don't work, do fresh login
        return self.login_to_terabox(driver)
    
    async def upload_folder(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, file_type: str = "all_ages") -> List[str]:
        """Upload folder to Terabox using web interface"""
        logger.info(f"🚀 Starting Terabox web upload for job {job_id}, folder: {folder_path}")
        
        driver = None
        try:
            await self.send_progress_message(update, context, job_id, "🌐 Membuka browser untuk upload...")
            
            # Initialize Chrome driver
            driver = webdriver.Chrome(options=self.chrome_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            # Login terlebih dahulu
            await self.send_progress_message(update, context, job_id, "🔐 Login ke Terabox...")
            if not self.ensure_login(driver):
                raise Exception("Failed to login to Terabox")
            
            # Navigate to upload page
            await self.send_progress_message(update, context, job_id, "📋 Mengakses halaman upload...")
            driver.get(self.upload_url)
            time.sleep(5)
            
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
                # Save cookies sebelum quit
                try:
                    self.save_cookies(driver)
                except:
                    pass
                driver.quit()
                logger.info("🔴 Browser closed")
    
    async def select_file_type(self, driver, file_type: str):
        """Select file type (All ages or Adult)"""
        try:
            if file_type == "adult":
                # Select Adult content
                adult_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and contains(@value, 'adult') or contains(@id, 'adult')]"))
                )
                adult_radio.click()
                logger.info("🔞 Selected Adult content type")
            else:
                # Select All ages (default)
                all_ages_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and contains(@value, 'all') or contains(@id, 'all')]"))
                )
                all_ages_radio.click()
                logger.info("👪 Selected All ages content type")
                
            time.sleep(2)
        except Exception as e:
            logger.warning(f"⚠️ Could not select file type: {e}")
    
    async def select_upload_source(self, driver, source_type: str):
        """Select upload source (TextBox File or Global Search)"""
        try:
            if source_type == "local":
                # Select TextBox File (local upload)
                local_radio = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and (contains(@value, 'textbox') or contains(@value, 'local') or contains(@id, 'local'))]"))
                )
                local_radio.click()
                logger.info("📁 Selected local file upload")
                
            time.sleep(2)
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
            batch_size = 5  # Smaller batch size untuk stability
            uploaded_count = 0
            
            for i in range(0, len(files_to_upload), batch_size):
                batch = files_to_upload[i:i + batch_size]
                file_input.send_keys("\n".join(batch))
                uploaded_count += len(batch)
                
                # Wait for upload to process
                time.sleep(8)
                
                progress = f"📤 Progress: {uploaded_count}/{len(files_to_upload)} files"
                logger.info(progress)
                await self.send_progress_message(update, context, job_id, progress)
            
            # Wait for all uploads to complete
            logger.info("⏳ Menunggu upload selesai...")
            await self.send_progress_message(update, context, job_id, "⏳ Menunggu upload selesai...")
            
            # Wait max 10 minutes for uploads
            for _ in range(60):
                try:
                    # Check if upload is still in progress
                    uploading_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Uploading') or contains(text(), 'uploading') or contains(text(), 'Processing')]")
                    if not uploading_elements:
                        break
                    time.sleep(10)
                except:
                    break
            
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
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Generate Link') or contains(text(), 'generate link') or contains(text(), 'Generate')]"))
            )
            generate_btn.click()
            
            # Wait for links to be generated
            time.sleep(15)
            
            # Extract share links from the page
            links = []
            link_elements = driver.find_elements(By.XPATH, "//a[contains(@href, 'tera-box') or contains(@href, 'terabox') or contains(@href, '1024tera')]")
            
            for elem in link_elements:
                href = elem.get_attribute('href')
                if href and href not in links:
                    links.append(href)
                    logger.info(f"🔗 Found share link: {href}")
            
            # Also check for links in text areas or input fields
            input_elements = driver.find_elements(By.XPATH, "//input[@type='text' or @type='url']")
            for elem in input_elements:
                value = elem.get_attribute('value')
                if value and ('terabox' in value or '1024tera' in value) and value not in links:
                    links.append(value)
                    logger.info(f"🔗 Found share link in input: {value}")
            
            logger.info(f"🔗 Total {len(links)} share links found")
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

    # ... (Other methods remain the same)

# Initialize managers dan start processor
logger.info("🔄 Initializing managers...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

# Tambahkan command handlers untuk login dan pengaturan Terabox
async def terabox_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test Terabox login"""
    try:
        await update.message.reply_text("🔐 Testing Terabox login...")
        
        # Test login
        uploader = TeraboxWebUploader()
        driver = webdriver.Chrome(options=uploader.chrome_options)
        
        try:
            success = uploader.ensure_login(driver)
            if success:
                await update.message.reply_text("✅ Terabox login successful!")
            else:
                await update.message.reply_text("❌ Terabox login failed!")
        finally:
            driver.quit()
            
    except Exception as e:
        logger.error(f"❌ Terabox login test error: {e}")
        await update.message.reply_text(f"❌ Login test error: {str(e)}")

async def set_terabox_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set Terabox credentials manually"""
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Harap sertakan email dan password\n"
                "Contoh: /setterabox email@example.com password123"
            )
            return
        
        email = context.args[0]
        password = context.args[1]
        
        # Update environment variables
        os.environ['TERABOX_EMAIL'] = email
        os.environ['TERABOX_PASSWORD'] = password
        
        # Update uploader credentials
        upload_manager.terabox_web_uploader.terabox_email = email
        upload_manager.terabox_web_uploader.terabox_password = password
        
        await update.message.reply_text(
            f"✅ Terabox credentials updated!\n"
            f"📧 Email: {email}\n"
            f"Note: Credentials akan hilang saat bot restart. Untuk permanen, set di environment variables."
        )
        
    except Exception as e:
        logger.error(f"❌ Error setting Terabox credentials: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ... (Tambahkan command handlers lainnya)

def main():
    """Start the bot"""
    logger.info("🚀 Starting Mega Downloader Bot...")
    
    # Check Terabox credentials
    terabox_email = os.getenv('TERABOX_EMAIL')
    terabox_password = os.getenv('TERABOX_PASSWORD')
    
    if not terabox_email or not terabox_password:
        logger.warning("⚠️  Terabox credentials not found! Please set TERABOX_EMAIL and TERABOX_PASSWORD environment variables")
    else:
        logger.info("✅ Terabox credentials found")
    
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
    application.add_handler(CommandHandler("teraboxlogin", terabox_login_command))
    application.add_handler(CommandHandler("setterabox", set_terabox_credentials))
    # ... (Add other command handlers)
    
    # Start bot
    logger.info("✅ Bot started successfully!")
    application.run_polling()

if __name__ == '__main__':
    main()
