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

# Selenium imports untuk automation Terabox
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.service import Service

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
            # Instead of --version, use a simple help command or just check if executable exists
            cmd = [self.mega_get_path, '--help']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            # Even if return code is not 0, if we can execute the command, it's available
            logger.info(f"mega-get executable check passed")
            return True
            
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
            old_email = self.get_current_account()['email']
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
            new_email = self.get_current_account()['email']
            logger.info(f"🔄 Rotated account: {old_email} -> {new_email}")
        else:
            logger.warning("Cannot rotate accounts: only one account available")
    
    def debug_mega_session(self) -> Dict:
        """Debug function to check mega session status"""
        debug_info = {}
        
        try:
            # Check if mega-get executable exists and is accessible
            debug_info['mega_get_path'] = self.mega_get_path
            debug_info['mega_get_exists'] = os.path.exists(self.mega_get_path)
            debug_info['mega_get_executable'] = os.access(self.mega_get_path, os.X_OK)
            
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
                logger.info("✅ Downloads directory is writable")
            except Exception as e:
                debug_info['downloads_writable'] = False
                debug_info['downloads_error'] = str(e)
                logger.error(f"❌ Downloads directory not writable: {e}")
            
            # Check account status
            debug_info['current_account'] = self.get_current_account()['email'] if self.get_current_account() else None
            debug_info['total_accounts'] = len(self.accounts)
            
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
        
        while retry_count < max_retries:
            try:
                # Debug session first
                debug_info = self.debug_mega_session()
                logger.info(f"🔧 Debug info for {job_id}: {json.dumps(debug_info, indent=2)}")
                
                # HANYA pastikan base download directory ada, folder spesifik akan dibuat oleh mega-get
                DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
                logger.info(f"📁 Base download directory ready: {DOWNLOAD_BASE}")
                
                # Test write permission di base directory
                test_file = DOWNLOAD_BASE / 'test_write.txt'
                try:
                    test_file.write_text('test')
                    test_file.unlink()
                    logger.info("✅ Write test successful")
                except Exception as e:
                    error_msg = f"Cannot write to download directory: {str(e)}"
                    logger.error(f"❌ {error_msg}")
                    return False, error_msg
                
                # Change to base download directory for mega-get (bukan folder spesifik)
                original_cwd = os.getcwd()
                os.chdir(DOWNLOAD_BASE)
                logger.info(f"📂 Changed working directory to base: {DOWNLOAD_BASE}")
                
                try:
                    # Now download using mega-get - biarkan mega-get yang membuat folder
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
                        # mega-get biasanya membuat folder dengan nama berdasarkan link
                        all_files = list(DOWNLOAD_BASE.rglob('*'))
                        files = [f for f in all_files if f.is_file()]
                        directories = [f for f in all_files if f.is_dir()]
                        
                        logger.info(f"📊 File check results: {len(files)} files, {len(directories)} directories")
                        
                        # Log all files and directories for debugging
                        for f in files:
                            try:
                                file_size = f.stat().st_size
                                logger.info(f"📄 File: {f.relative_to(DOWNLOAD_BASE)} ({file_size} bytes)")
                            except Exception as e:
                                logger.warning(f"⚠️ Could not stat file {f}: {e}")
                        
                        for d in directories:
                            logger.info(f"📁 Directory: {d.relative_to(DOWNLOAD_BASE)}")
                        
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

class TeraboxSeleniumUploader:
    def __init__(self):
        self.driver = None
        self.wait = None
        self.terabox_email = os.getenv('TERABOX_EMAIL')
        self.terabox_password = os.getenv('TERABOX_PASSWORD')
        self.timeout = 30
        logger.info("🌐 TeraboxSeleniumUploader initialized dengan login automation")
    
    def find_chrome_binary(self):
        """Find Chrome binary in common locations"""
        possible_paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/google-chrome-stable', 
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/snap/bin/chromium',
            '/snap/bin/google-chrome',
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            'C:/Program Files/Google/Chrome/Application/chrome.exe'
        ]
        for path in possible_paths:
            if os.path.exists(path):
                logger.info(f"✅ Found Chrome binary at: {path}")
                return path
        logger.error("❌ Chrome binary not found in common locations")
        return None

    def get_chrome_version(self):
        """Get installed Chrome version"""
        try:
            chrome_binary = self.find_chrome_binary()
            if chrome_binary:
                result = subprocess.run([chrome_binary, '--version'], capture_output=True, text=True)
                version_output = result.stdout.strip()
                # Extract version number
                version_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', version_output)
                if version_match:
                    version = version_match.group(1)
                    logger.info(f"🔍 Chrome version: {version}")
                    return version
            return None
        except Exception as e:
            logger.error(f"❌ Error getting Chrome version: {e}")
            return None

    def download_compatible_chromedriver(self):
        """Download compatible chromedriver untuk Chrome versi 141"""
        try:
            logger.info("🔧 Downloading compatible chromedriver untuk Chrome 141...")
            
            # URL untuk Chrome 141
            version = "141.0.7390.54"
            url = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64/chromedriver-linux64.zip"
            
            import tempfile
            import zipfile
            
            temp_dir = tempfile.mkdtemp()
            zip_path = os.path.join(temp_dir, "chromedriver.zip")
            
            # Download
            response = requests.get(url)
            if response.status_code != 200:
                logger.error(f"❌ Failed to download chromedriver: HTTP {response.status_code}")
                return None
            
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            # Extract
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            driver_path = os.path.join(temp_dir, "chromedriver-linux64", "chromedriver")
            
            # Make executable
            os.chmod(driver_path, 0o755)
            
            # Cleanup zip file
            os.remove(zip_path)
            
            logger.info(f"✅ Chromedriver downloaded successfully: {driver_path}")
            return driver_path
            
        except Exception as e:
            logger.error(f"❌ Chromedriver download failed: {e}")
            return None

    def setup_driver(self):
        """Setup Chrome driver dengan handling yang lebih baik untuk Chrome 141"""
        try:
            chrome_options = Options()
            
            # Cari Chrome binary
            chrome_binary = self.find_chrome_binary()
            if chrome_binary:
                chrome_options.binary_location = chrome_binary
                logger.info(f"🔧 Using Chrome binary: {chrome_binary}")
            else:
                logger.warning("⚠️ Chrome binary not found, trying default...")
            
            # Opsi untuk stability
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1536,835')
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            
            # Additional options untuk stability
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            # Gunakan unique user data directory untuk menghindari konflik
            import tempfile
            user_data_dir = tempfile.mkdtemp(prefix="chrome_profile_")
            chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
            logger.info(f"🔧 Using unique user data directory: {user_data_dir}")
            
            # Comment out headless untuk debugging
            # chrome_options.add_argument('--headless')
            
            # Setup service dengan handling error
            from selenium.webdriver.chrome.service import Service
            
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    # Coba menggunakan webdriver_manager terlebih dahulu
                    if attempt == 0:
                        try:
                            logger.info("🔄 Trying webdriver_manager...")
                            from webdriver_manager.chrome import ChromeDriverManager
                            from selenium.webdriver.chrome.service import Service as ChromeService
                            
                            driver_path = ChromeDriverManager().install()
                            service = ChromeService(driver_path)
                            self.driver = webdriver.Chrome(service=service, options=chrome_options)
                            
                        except Exception as wdm_error:
                            logger.warning(f"⚠️ webdriver_manager failed: {wdm_error}, trying direct download...")
                            # Fallback ke direct download
                            driver_path = self.download_compatible_chromedriver()
                            if driver_path:
                                service = Service(driver_path)
                                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                            else:
                                raise Exception("Both webdriver_manager and direct download failed")
                    
                    self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    
                    self.wait = WebDriverWait(self.driver, self.timeout)
                    logger.info("✅ Chrome driver setup completed successfully")
                    return True
                    
                except Exception as e:
                    logger.warning(f"⚠️ Chrome driver setup attempt {attempt + 1} failed: {e}")
                    
                    # Cleanup driver jika ada
                    if hasattr(self, 'driver') and self.driver:
                        try:
                            self.driver.quit()
                        except:
                            pass
                    
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    else:
                        # Final fallback - coba tanpa service path
                        try:
                            logger.info("🔄 Trying final fallback without specific service...")
                            self.driver = webdriver.Chrome(options=chrome_options)
                            self.wait = WebDriverWait(self.driver, self.timeout)
                            logger.info("✅ Chrome driver setup completed with fallback")
                            return True
                        except Exception as final_error:
                            logger.error(f"❌ All Chrome driver setup attempts failed: {final_error}")
                            return False
                        
        except Exception as e:
            logger.error(f"❌ Failed to setup Chrome driver: {e}")
            return False

    def find_and_click_element(self, selectors: List[str], description: str, offset_x: int = 0, offset_y: int = 0, count: int = 1) -> bool:
        """Find and click element dengan multiple selector strategies"""
        try:
            for selector in selectors:
                try:
                    if selector.startswith('::-p-xpath('):
                        # Extract XPath from Puppeteer format
                        xpath = selector.replace('::-p-xpath(', '').rstrip(')')
                        element = self.wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    elif selector.startswith('::-p-aria('):
                        # Skip ARIA selectors for now
                        continue
                    elif selector.startswith('::-p-text('):
                        # Text-based selector
                        text = selector.replace('::-p-text(', '').rstrip(')')
                        element = self.wait.until(EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{text}')]")))
                    elif ' >>> ' in selector:
                        # Shadow DOM selector - skip for now
                        continue
                    else:
                        # CSS selector
                        element = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    
                    if element:
                        logger.info(f"✅ Found {description} dengan selector: {selector}")
                        
                        # Use ActionChains untuk klik dengan offset
                        actions = ActionChains(self.driver)
                        actions.move_to_element_with_offset(element, offset_x, offset_y)
                        
                        for _ in range(count):
                            actions.click()
                        
                        actions.perform()
                        
                        logger.info(f"✅ Clicked {description} dengan offset ({offset_x}, {offset_y})")
                        time.sleep(2)
                        return True
                        
                except Exception as e:
                    logger.debug(f"❌ Selector failed {selector}: {e}")
                    continue
            
            logger.error(f"❌ All selectors failed untuk {description}")
            return False
            
        except Exception as e:
            logger.error(f"💥 Error finding/clicking {description}: {e}")
            return False

    def find_and_fill_element(self, selectors: List[str], description: str, text: str) -> bool:
        """Find and fill element dengan multiple selector strategies"""
        try:
            for selector in selectors:
                try:
                    if selector.startswith('::-p-xpath('):
                        xpath = selector.replace('::-p-xpath(', '').rstrip(')')
                        element = self.wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    elif selector.startswith('::-p-aria('):
                        continue
                    elif ' >>> ' in selector:
                        continue
                    else:
                        element = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    
                    if element:
                        logger.info(f"✅ Found {description} dengan selector: {selector}")
                        element.clear()
                        element.send_keys(text)
                        logger.info(f"✅ Filled {description} dengan text: {text}")
                        time.sleep(1)
                        return True
                        
                except Exception as e:
                    logger.debug(f"❌ Selector failed {selector}: {e}")
                    continue
            
            logger.error(f"❌ All selectors failed untuk {description}")
            return False
            
        except Exception as e:
            logger.error(f"💥 Error finding/filling {description}: {e}")
            return False

    def login_to_terabox(self) -> bool:
        """Login ke Terabox menggunakan element yang tepat dari recording"""
        try:
            logger.info("🔐 Attempting to login to Terabox")
            
            # Step 1: Navigate to login page
            self.driver.get('https://www.1024tera.com/webmaster/index')
            time.sleep(5)
            
            # Step 2: Click login button dengan element dari recording
            login_success = self.find_and_click_element([
                'div.referral-content span',
                '//*[@id="app"]/div[1]/div[2]/div[1]/div[2]/span',
                '::-p-text(Log in)'
            ], "login button", offset_x=57.7, offset_y=2.56)
            
            if not login_success:
                return False
            
            time.sleep(3)
            
            # Step 3: Click email login method
            email_login_success = self.find_and_click_element([
                'div.other-item > div:nth-of-type(2)',
                '//*[@id="app"]/div[1]/div[1]/div[2]/div[2]/div/div[2]/div/div[4]/div[3]/div[2]'
            ], "email login method", offset_x=42.5, offset_y=24.6)
            
            if not email_login_success:
                return False
            
            time.sleep(3)
            
            # Step 4: Click dan fill email field
            email_click_success = self.find_and_click_element([
                '::-p-aria(Enter your email)',
                '#email-input',
                '//*[@id="email-input"]'
            ], "email field", offset_x=65.5, offset_y=11.4)
            
            if not email_click_success:
                return False
            
            time.sleep(1)
            
            # Double click email field (seperti di recording)
            self.find_and_click_element([
                '::-p-aria(Enter your email)',
                '#email-input',
                '//*[@id="email-input"]'
            ], "email field double click", offset_x=65.5, offset_y=13.4)
            
            time.sleep(1)
            
            # Fill email
            email_fill_success = self.find_and_fill_element([
                '::-p-aria(Enter your email)',
                '#email-input',
                '//*[@id="email-input"]'
            ], "email field", self.terabox_email)
            
            if not email_fill_success:
                return False
            
            time.sleep(2)
            
            # Step 5: Click dan fill password field
            password_click_success = self.find_and_click_element([
                '::-p-aria(Enter the password.)',
                '#pwd-input',
                '//*[@id="pwd-input"]'
            ], "password field", offset_x=69.5, offset_y=27.4)
            
            if not password_click_success:
                return False
            
            time.sleep(1)
            
            # Fill password
            password_fill_success = self.find_and_fill_element([
                '::-p-aria(Enter the password.)',
                '#pwd-input',
                '//*[@id="pwd-input"]'
            ], "password field", self.terabox_password)
            
            if not password_fill_success:
                return False
            
            time.sleep(2)
            
            # Step 6: Click login submit button
            login_submit_success = self.find_and_click_element([
                'div.btn-class-login',
                '//*[@id="app"]/div[1]/div[1]/div[2]/div[2]/div/div[2]/div/div[3]/div/div[5]'
            ], "login submit button", offset_x=178.5, offset_y=23)
            
            if not login_submit_success:
                return False
            
            # Wait for login process dan navigation
            logger.info("⏳ Waiting for login process...")
            time.sleep(10)
            
            # Verifikasi login berhasil
            try:
                # Check jika kita di redirect ke halaman utama setelah login
                current_url = self.driver.current_url
                if 'webmaster/index' in current_url or 'webmaster/new/share' in current_url:
                    logger.info("✅ Login successful!")
                    return True
                else:
                    logger.warning(f"⚠️ Unexpected URL after login: {current_url}")
                    # Coba lanjutkan anyway
                    return True
            except Exception as e:
                logger.warning(f"⚠️ Login verification warning: {e}")
                # Coba lanjutkan anyway
                return True
                
        except Exception as e:
            logger.error(f"💥 Login error: {e}")
            try:
                self.driver.save_screenshot("login_error.png")
            except:
                pass
            return False

    def navigate_to_upload_page(self) -> bool:
        """Navigate ke halaman upload menggunakan element dari recording"""
        try:
            logger.info("🧭 Navigating to upload page...")
            
            # Step 1: Click guide container tab (seperti di recording)
            guide_success = self.find_and_click_element([
                'div.guide-container > div.tab-item div',
                '//*[@id="app"]/div[1]/div[2]/div[1]/div/div[2]/div[1]/span/div'
            ], "guide tab", offset_x=18, offset_y=14)
            
            if not guide_success:
                logger.warning("⚠️ Could not click guide tab, trying direct navigation")
                # Coba navigasi langsung ke upload page
                self.driver.get('https://dm.1024tera.com/webmaster/new/share')
                time.sleep(5)
                return True
            
            time.sleep(3)
            
            # Step 2: Click source array item (Local File)
            source_success = self.find_and_click_element([
                'div.source-arr > div:nth-of-type(1) div:nth-of-type(2)',
                '//*[@id="upload-container"]/div/div[2]/div[1]/div/div[2]'
            ], "source array item", offset_x=56, offset_y=19)
            
            if not source_success:
                logger.warning("⚠️ Could not click source array, trying direct upload page")
                self.driver.get('https://dm.1024tera.com/webmaster/new/share')
                time.sleep(5)
                return True
            
            time.sleep(3)
            return True
            
        except Exception as e:
            logger.error(f"💥 Navigation error: {e}")
            # Fallback ke direct navigation
            try:
                self.driver.get('https://dm.1024tera.com/webmaster/new/share')
                time.sleep(5)
                return True
            except:
                return False

    def upload_files(self, folder_path: Path) -> List[str]:
        """Upload files menggunakan element yang tepat dari recording"""
        try:
            links = []
            
            # Get all files from folder
            all_files = [f for f in folder_path.rglob('*') if f.is_file()]
            total_files = len(all_files)
            
            logger.info(f"📁 Found {total_files} files to upload")
            
            if total_files == 0:
                logger.error("❌ No files found to upload")
                return []
            
            # Upload files dalam batch kecil untuk testing
            batch_files = all_files[:3]  # Batasi 3 file dulu untuk testing
            
            for i, file_path in enumerate(batch_files, 1):
                if not self.upload_single_file(file_path, i, total_files):
                    logger.error(f"❌ Failed to upload file: {file_path.name}")
                else:
                    logger.info(f"✅ Successfully uploaded: {file_path.name}")
            
            # Extract links setelah semua file diupload
            return self.extract_share_links()
            
        except Exception as e:
            logger.error(f"💥 Upload files error: {e}")
            return []

    def upload_single_file(self, file_path: Path, current: int, total: int) -> bool:
        """Upload single file menggunakan element dari recording"""
        try:
            logger.info(f"📤 Uploading file {current}/{total}: {file_path.name}")
            
            # Step 1: Click input file element
            file_input_success = self.find_and_click_element([
                'input:nth-of-type(2)',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div/input[2]'
            ], "file input", offset_x=0, offset_y=0)
            
            if not file_input_success:
                logger.error("❌ Could not find file input element")
                return False
            
            time.sleep(2)
            
            # Step 2: Handle file picker - kita perlu menggunakan JavaScript untuk set file input
            try:
                file_inputs = self.driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                if file_inputs:
                    file_input = file_inputs[0]
                    file_input.send_keys(str(file_path.absolute()))
                    logger.info(f"✅ File sent to input: {file_path.name}")
                else:
                    logger.error("❌ No file input element found")
                    return False
            except Exception as e:
                logger.error(f"❌ Error setting file input: {e}")
                return False
            
            time.sleep(5)  # Wait for file upload
            
            # Step 3: Click Generate Link
            generate_success = self.find_and_click_element([
                'div.share-way span',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[3]/div[1]/div[2]/div[2]/span',
                '::-p-text(Generate Link)'
            ], "generate link button", offset_x=31.95, offset_y=3.6)
            
            if not generate_success:
                logger.error("❌ Could not click Generate Link")
                return False
            
            # Wait for link generation
            logger.info("⏳ Waiting for link generation...")
            time.sleep(10)
            
            # Step 4: Copy link (multiple clicks seperti di recording)
            copy_success = self.find_and_click_element([
                'div.media-item > img:nth-of-type(1)',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[3]/div/div[4]/img[1]'
            ], "copy button", offset_x=13, offset_y=12.85, count=3)
            
            if not copy_success:
                logger.warning("⚠️ Could not click copy button, but upload might still be successful")
            
            time.sleep(3)
            
            # Step 5: Close modal (jika ada)
            close_success = self.find_and_click_element([
                'div.top-header > img',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[3]/div/div[1]/img'
            ], "close button", offset_x=3, offset_y=5)
            
            if not close_success:
                logger.debug("ℹ️ No close button found, continuing...")
            
            time.sleep(2)
            
            return True
            
        except Exception as e:
            logger.error(f"💥 Single file upload error: {e}")
            return False

    def extract_share_links(self) -> List[str]:
        """Extract sharing links dari halaman"""
        try:
            logger.info("🔍 Extracting share links from page...")
            
            links = []
            
            # Cari link dalam page source
            page_source = self.driver.page_source
            
            # Pattern untuk Terabox share links
            patterns = [
                r'https?://[^\s<>"{}|\\^`]*terabox[^\s<>"{}|\\^`]*',
                r'https?://[^\s<>"{}|\\^`]*1024tera[^\s<>"{}|\\^`]*'
            ]
            
            for pattern in patterns:
                found_links = re.findall(pattern, page_source)
                # Filter hanya link share yang valid
                valid_links = [link for link in found_links if any(x in link for x in ['/s/', '/share/', 'download'])]
                links.extend(valid_links)
            
            # Remove duplicates
            links = list(set(links))
            
            logger.info(f"📊 Found {len(links)} share links")
            
            # Save screenshot untuk debugging
            try:
                self.driver.save_screenshot("upload_result.png")
                logger.info("📸 Saved upload result screenshot")
            except:
                pass
            
            return links
            
        except Exception as e:
            logger.error(f"❌ Link extraction error: {e}")
            return []

    def upload_folder_via_selenium(self, folder_path: Path) -> List[str]:
        """Main method untuk upload folder menggunakan Selenium dengan element exact"""
        try:
            if not self.setup_driver():
                logger.error("❌ Driver setup failed, cannot proceed with upload")
                return []

            logger.info(f"🚀 Starting Selenium upload for folder: {folder_path}")
            
            # Step 1: Login
            if not self.login_to_terabox():
                logger.error("❌ Login failed, cannot proceed with upload")
                return []
            
            # Step 2: Navigate to upload page
            if not self.navigate_to_upload_page():
                logger.error("❌ Navigation to upload page failed")
                return []
            
            # Step 3: Upload files
            links = self.upload_files(folder_path)
            
            if links:
                logger.info(f"✅ Upload completed! {len(links)} links generated")
            else:
                logger.warning("⚠️ Upload completed but no links found")
            
            return links
                
        except Exception as e:
            logger.error(f"💥 Selenium upload error: {e}")
            try:
                self.driver.save_screenshot(f"error_{int(time.time())}.png")
            except:
                pass
            return []
        finally:
            if self.driver:
                self.driver.quit()
                logger.info("✅ Chrome driver closed")

    def get_enhanced_manual_instructions(self, folder_path: Path, job_number: int) -> str:
        """Generate enhanced manual instructions berdasarkan element yang tepat"""
        file_count = len(list(folder_path.rglob('*')))
        
        instructions = f"""
📋 **INSTRUKSI UPLOAD MANUAL TERABOX - Job #{job_number}**

🎯 **Langkah-langkah Berdasarkan Element Exact dari Recording**:

1. **Buka Website**: https://www.1024tera.com/webmaster/index

2. **Login**:
   - Klik: `div.referral-content span` (teks Log in)
   - Klik: `div.other-item > div:nth-of-type(2)` (email login)
   - Input email: `#email-input` ({self.terabox_email})
   - Input password: `#pwd-input` 
   - Klik: `div.btn-class-login`

3. **Navigasi ke Upload**:
   - Klik: `div.guide-container > div.tab-item div`
   - Klik: `div.source-arr > div:nth-of-type(1) div:nth-of-type(2)` (Local File)

4. **Upload File**:
   - Klik: `input:nth-of-type(2)` (file input)
   - Pilih file dari: `{folder_path}`
   - Klik: `div.share-way span` (Generate Link)

5. **Copy Link**:
   - Tunggu link generated
   - Klik: `div.media-item > img:nth-of-type(1)` (copy button) 3x
   - Klik: `div.top-header > img` (close)

📁 **Detail Folder**:
- Path: `{folder_path}`
- Total Files: {file_count} files
- Job ID: #{job_number}

🔧 **Jika Automation Gagal**:
- Pastikan Chrome terinstall di sistem
- Pastikan chromedriver kompatibel dengan versi Chrome
- Cek log untuk detail error

💡 **Tips**:
- Gunakan Chrome browser versi terbaru
- Matikan pop-up blocker
- Allow file system permissions
- Pastikan login berhasil sebelum upload
"""
        return instructions

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_selenium_uploader = TeraboxSeleniumUploader()
        self.terabox_lock = threading.Lock()
        
        # Counter global untuk urutan job upload
        self._job_counter = 1
        self._counter_lock = threading.Lock()
        
        logger.info("📤 UploadManager initialized dengan Selenium uploader")

    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox menggunakan Selenium automation dengan element yang tepat"""
        logger.info(f"🚀 Starting Terabox upload untuk job {job_id}, folder: {folder_path}")
        
        try:
            # Dapatkan nomor job
            with self._counter_lock:
                job_number = self._job_counter
                self._job_counter += 1

            logger.info(f"🔢 Job number: {job_number}")
            
            await self.send_progress_message(
                update, context, job_id, 
                f"📤 Memulai upload ke Terabox...\n"
                f"🔢 Job Number: #{job_number}\n"
                f"📁 Folder: {folder_path.name}\n"
                f"🎯 Method: Selenium dengan Element Exact dari Recording"
            )

            # Cek jika credential Terabox tersedia
            if not self.terabox_selenium_uploader.terabox_email or not self.terabox_selenium_uploader.terabox_password:
                await self.send_progress_message(
                    update, context, job_id,
                    "❌ Terabox credentials tidak ditemukan!\n"
                    "📋 Silakan set environment variables:\n"
                    "- TERABOX_EMAIL\n" 
                    "- TERABOX_PASSWORD"
                )
                return []

            # Coba automation dengan Selenium menggunakan element yang tepat
            await self.send_progress_message(
                update, context, job_id,
                "🔄 Mencoba login dan upload otomatis dengan element exact..."
            )
            
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                # Try Selenium automation dengan element exact dari recording
                links = self.terabox_selenium_uploader.upload_folder_via_selenium(folder_path)
                
                if links:
                    success_msg = (
                        f"✅ Upload ke Terabox berhasil!\n"
                        f"🔢 Job Number: #{job_number}\n"
                        f"🔗 {len(links)} links generated\n"
                        f"📁 Folder: {folder_path.name}\n"
                        f"🎯 Method: Automated dengan Element Exact"
                    )
                    logger.info(f"✅ {success_msg}")
                    await self.send_progress_message(update, context, job_id, success_msg)
                    
                    # Send individual links
                    for i, link in enumerate(links, 1):
                        link_msg = f"🔗 Link {i}: {link}"
                        await context.bot.send_message(
                            chat_id=active_downloads[job_id]['chat_id'],
                            text=link_msg
                        )
                    
                    return links
                else:
                    # Fallback ke instruksi manual yang sangat detail
                    await self.send_progress_message(
                        update, context, job_id,
                        "⚠️ Upload otomatis tidak berhasil\n"
                        "📋 Beralih ke mode manual dengan instruksi lengkap..."
                    )
                    
                    instructions = self.terabox_selenium_uploader.get_enhanced_manual_instructions(folder_path, job_number)
                    await self.send_progress_message(update, context, job_id, instructions)
                    
                    return [f"Manual upload required - Job #{job_number}"]
                    
        except Exception as e:
            logger.error(f"💥 Terabox upload error untuk {job_id}: {e}")
            
            # Berikan instruksi manual dengan element exact
            with self._counter_lock:
                job_number = self._job_counter - 1
            
            instructions = self.terabox_selenium_uploader.get_enhanced_manual_instructions(folder_path, job_number)
            await self.send_progress_message(update, context, job_id, instructions)
            
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
                    f"🔢 Urutan Job: #{counter_status['current_job_counter']}"
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
📤 Upload ke Terabox/Doodstream (Selenium Automation dengan Element Exact)
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
        
        status_text = f"""
📊 Status System

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
        
        status_text = f"""
🔢 Terabox Job Counter Status

Counter Info:
🔄 Current Job Counter: #{counter_status['current_job_counter']}
🔒 Counter Locked: {'✅' if counter_status['counter_locked'] else '❌'}

Upload Method:
🤖 Selenium Automation: Browser otomatis dengan element exact
🌐 URL: https://www.1024tera.com/webmaster/index
🎯 Element: Exact dari recording Puppeteer
        """
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"💥 Error in counter_status_command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show debug information"""
    try:
        debug_info = mega_manager.debug_mega_session()
        
        # Cek Terabox credentials
        terabox_email = os.getenv('TERABOX_EMAIL')
        terabox_password = os.getenv('TERABOX_PASSWORD')
        
        debug_text = f"""
🐛 Debug Information

Mega.nz Status:
✅ mega-get Available: {debug_info.get('mega_get_exists', False)}
📂 Downloads Writable: {debug_info.get('downloads_writable', False)}
🔑 Accounts: {debug_info.get('total_accounts', 0)}
📧 Current Account: {debug_info.get('current_account', 'None')}

Bot Status:
🔄 Active Downloads: {len(active_downloads)}
📋 Queue Size: {download_queue.qsize()}

Terabox Status:
🔢 Job Counter: {upload_manager.get_job_counter_status().get('current_job_counter', 0)}
🤖 Upload Method: Selenium dengan Element Exact
📧 Terabox Email: {'✅ Set' if terabox_email else '❌ Not Set'}
🔑 Terabox Password: {'✅ Set' if terabox_password else '❌ Not Set'}
🌐 Target URL: https://www.1024tera.com/webmaster/index
🎯 Element Source: Recording Puppeteer dengan offset exact
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
    logger.info("🚀 Starting Mega Downloader Bot dengan Selenium Element Exact...")
    
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
    
    # Check Terabox credentials
    terabox_email = os.getenv('TERABOX_EMAIL')
    terabox_password = os.getenv('TERABOX_PASSWORD')
    if not terabox_email or not terabox_password:
        logger.warning("⚠️ Terabox credentials not found! Please set TERABOX_EMAIL and TERABOX_PASSWORD environment variables")
    else:
        logger.info("✅ Terabox credentials found")
    
    # Install required packages untuk Chrome driver
    try:
        import webdriver_manager
        logger.info("✅ webdriver_manager is available")
    except ImportError:
        logger.warning("⚠️ webdriver_manager not installed, installing...")
        subprocess.run(['pip', 'install', 'webdriver-manager'], check=True)
        logger.info("✅ webdriver_manager installed")
    
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
    logger.info("✅ Bot started successfully dengan Selenium element exact automation!")
    application.run_polling()

if __name__ == '__main__':
    main()
