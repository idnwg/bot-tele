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
import uuid
import tempfile
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

# Playwright imports untuk automation Terabox
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

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

class TeraboxPlaywrightUploader:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.terabox_email = os.getenv('TERABOX_EMAIL')
        self.terabox_password = os.getenv('TERABOX_PASSWORD')
        self.current_domain = None
        self.session_file = "terabox_session.json"
        self.timeout = 45000  # 45 seconds in milliseconds
        logger.info("🌐 TeraboxPlaywrightUploader initialized dengan session persistence")

    def get_current_domain(self, url: str) -> str:
        """Extract domain from URL"""
        try:
            domain = url.split('/')[2]  # ambil domain dari URL
            logger.info(f"🌐 Extracted domain: {domain}")
            return domain
        except Exception as e:
            logger.warning(f"⚠️ Could not extract domain from {url}, using fallback: {e}")
            return "dm.1024tera.com"  # fallback domain

    async def setup_browser(self, use_session: bool = True) -> bool:
        """Setup Playwright browser dengan session persistence"""
        try:
            logger.info("🔄 Setting up Playwright browser dengan session persistence...")
            
            self.playwright = await async_playwright().start()
            
            # Launch browser dengan headless mode
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--window-size=1920,1080',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-translate',
                    '--disable-sync',
                    '--metrics-recording-only',
                    '--mute-audio',
                    '--no-first-run',
                    '--disable-default-apps',
                    '--disable-component-extensions-with-background-pages',
                ]
            )
            
            # Load session jika ada dan diminta
            storage_state = None
            if use_session and os.path.exists(self.session_file):
                try:
                    with open(self.session_file, 'r') as f:
                        storage_state = json.load(f)
                    logger.info("✅ Loaded existing session state")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to load session state: {e}")
            
            # Create context dengan atau tanpa session
            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                ignore_https_errors=True,
                java_script_enabled=True,
                bypass_csp=True,
                storage_state=storage_state
            )
            
            # Create page
            self.page = await self.context.new_page()
            
            # Set default timeout
            self.page.set_default_timeout(self.timeout)
            
            # Enable request interception untuk monitoring
            await self.page.route("**/*", self.route_handler)
            
            logger.info("✅ Playwright browser setup completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Playwright browser setup failed: {e}")
            await self.cleanup_browser()
            return False

    async def save_session(self):
        """Save session cookies untuk penggunaan berikutnya"""
        try:
            storage_state = await self.context.storage_state()
            with open(self.session_file, 'w') as f:
                json.dump(storage_state, f)
            logger.info("💾 Session saved successfully")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to save session: {e}")
            return False

    async def route_handler(self, route):
        """Handler untuk monitoring requests"""
        try:
            # Continue semua requests tanpa modifikasi
            await route.continue_()
        except Exception as e:
            logger.debug(f"Route handler error: {e}")

    async def wait_for_network_idle(self, timeout: int = 30000):
        """Wait for network to be idle"""
        try:
            await self.page.wait_for_load_state('networkidle', timeout=timeout)
        except Exception as e:
            logger.debug(f"Network idle wait timeout: {e}")

    async def find_and_click_element(self, selectors: List[str], description: str, timeout: int = None) -> bool:
        """Find and click element dengan multiple selector strategies"""
        if timeout is None:
            timeout = self.timeout
            
        try:
            for selector in selectors:
                try:
                    logger.debug(f"🔍 Trying selector: {selector} untuk {description}")
                    
                    # Handle different selector types
                    if selector.startswith('//'):
                        # XPath selector
                        element = await self.page.wait_for_selector(f"xpath={selector}", timeout=timeout)
                    elif selector.startswith('::-p-text('):
                        # Text-based selector
                        text = selector.replace('::-p-text(', '').rstrip(')')
                        element = await self.page.wait_for_selector(f"text={text}", timeout=timeout)
                    elif selector.startswith('::-p-aria('):
                        # ARIA selector
                        aria_label = selector.replace('::-p-aria(', '').rstrip(')')
                        element = await self.page.wait_for_selector(f'[aria-label="{aria_label}"]', timeout=timeout)
                    else:
                        # CSS selector
                        element = await self.page.wait_for_selector(selector, timeout=timeout)
                    
                    if element:
                        logger.info(f"✅ Found {description} dengan selector: {selector}")
                        
                        # Scroll element into view
                        await element.scroll_into_view_if_needed()
                        
                        # Wait for element to be stable
                        await asyncio.sleep(1)
                        
                        # Click element
                        await element.click(delay=100)  # 100ms delay untuk realism
                        
                        logger.info(f"✅ Clicked {description}")
                        await asyncio.sleep(2)
                        return True
                        
                except Exception as e:
                    logger.debug(f"❌ Selector failed {selector}: {e}")
                    continue
            
            logger.error(f"❌ All selectors failed untuk {description}")
            return False
            
        except Exception as e:
            logger.error(f"💥 Error finding/clicking {description}: {e}")
            return False

    async def find_and_fill_element(self, selectors: List[str], description: str, text: str, timeout: int = None) -> bool:
        """Find and fill element dengan multiple selector strategies"""
        if timeout is None:
            timeout = self.timeout
            
        try:
            for selector in selectors:
                try:
                    logger.debug(f"🔍 Trying selector: {selector} untuk {description}")
                    
                    # Handle different selector types
                    if selector.startswith('//'):
                        element = await self.page.wait_for_selector(f"xpath={selector}", timeout=timeout)
                    elif selector.startswith('::-p-text('):
                        text_sel = selector.replace('::-p-text(', '').rstrip(')')
                        element = await self.page.wait_for_selector(f"text={text_sel}", timeout=timeout)
                    elif selector.startswith('::-p-aria('):
                        aria_label = selector.replace('::-p-aria(', '').rstrip(')')
                        element = await self.page.wait_for_selector(f'[aria-label="{aria_label}"]', timeout=timeout)
                    else:
                        element = await self.page.wait_for_selector(selector, timeout=timeout)
                    
                    if element:
                        logger.info(f"✅ Found {description} dengan selector: {selector}")
                        
                        # Scroll element into view
                        await element.scroll_into_view_if_needed()
                        
                        # Wait for element to be stable
                        await asyncio.sleep(1)
                        
                        # Clear dan fill field
                        await element.click(click_count=3)  # Triple click untuk select all
                        await self.page.keyboard.press('Backspace')
                        await element.fill(text)
                        
                        logger.info(f"✅ Filled {description} dengan text: {text}")
                        await asyncio.sleep(1)
                        return True
                        
                except Exception as e:
                    logger.debug(f"❌ Selector failed {selector}: {e}")
                    continue
            
            logger.error(f"❌ All selectors failed untuk {description}")
            return False
            
        except Exception as e:
            logger.error(f"💥 Error finding/filling {description}: {e}")
            return False

    async def check_if_logged_in(self) -> bool:
        """Check jika user sudah login dengan mencoba akses halaman upload"""
        try:
            logger.info("🔍 Checking login status...")
            
            # Coba akses halaman upload langsung
            upload_url = "https://dm.1024tera.com/webmaster/new/share"
            await self.page.goto(upload_url, wait_until='domcontentloaded', timeout=30000)
            
            # Tunggu sebentar untuk melihat redirect atau perubahan
            await asyncio.sleep(3)
            
            current_url = self.page.url
            logger.info(f"🌐 Current URL after navigation: {current_url}")
            
            # Jika berhasil di halaman upload, berarti sudah login
            if 'new/share' in current_url:
                logger.info("✅ Already logged in (detected upload page)")
                self.current_domain = self.get_current_domain(current_url)
                return True
            
            # Jika di-redirect ke halaman login, berarti belum login
            if 'login' in current_url or 'index' in current_url:
                logger.info("❌ Not logged in (redirected to login page)")
                return False
            
            # Default: anggap sudah login jika tidak di-redirect
            logger.info("✅ Assuming logged in (no redirect detected)")
            return True
            
        except Exception as e:
            logger.error(f"💥 Error checking login status: {e}")
            return False

    async def login_to_terabox(self) -> bool:
        """Login ke Terabox hanya jika diperlukan"""
        try:
            # Cek dulu apakah sudah login
            if await self.check_if_logged_in():
                logger.info("✅ Already logged in, skipping login process")
                return True
            
            logger.info("🔐 Login required, starting login process...")
            
            # Step 1: Navigate to login page
            await self.page.goto('https://www.1024tera.com/webmaster/index', wait_until='domcontentloaded')
            await asyncio.sleep(5)
            
            # Step 2: Click login button
            login_success = await self.find_and_click_element([
                'div.referral-content span',
                '//*[@id="app"]/div[1]/div[2]/div[1]/div[2]/span',
                '::-p-text(Log in)'
            ], "login button")
            
            if not login_success:
                logger.error("❌ Failed to click login button")
                return False
            
            await asyncio.sleep(3)
            
            # Step 3: Click email login method
            email_login_success = await self.find_and_click_element([
                'div.other-item > div:nth-of-type(2)',
                '//*[@id="app"]/div[1]/div[1]/div[2]/div[2]/div/div[2]/div/div[4]/div[3]/div[2]'
            ], "email login method")
            
            if not email_login_success:
                logger.error("❌ Failed to click email login method")
                return False
            
            await asyncio.sleep(3)
            
            # Step 4: Fill email field
            email_fill_success = await self.find_and_fill_element([
                '[aria-label="Enter your email"]',
                '#email-input',
                '//*[@id="email-input"]',
                'input[type="email"]'
            ], "email field", self.terabox_email)
            
            if not email_fill_success:
                logger.error("❌ Failed to fill email field")
                return False
            
            await asyncio.sleep(2)
            
            # Step 5: Fill password field
            password_fill_success = await self.find_and_fill_element([
                '[aria-label="Enter the password."]',
                '#pwd-input',
                '//*[@id="pwd-input"]',
                'input[type="password"]'
            ], "password field", self.terabox_password)
            
            if not password_fill_success:
                logger.error("❌ Failed to fill password field")
                return False
            
            await asyncio.sleep(2)
            
            # Step 6: Click login submit button
            login_submit_success = await self.find_and_click_element([
                'div.btn-class-login',
                '//*[@id="app"]/div[1]/div[1]/div[2]/div[2]/div/div[2]/div/div[3]/div/div[5]',
                'button[type="submit"]'
            ], "login submit button")
            
            if not login_submit_success:
                logger.error("❌ Failed to click login submit button")
                return False
            
            # Wait for login process
            logger.info("⏳ Waiting for login process...")
            await asyncio.sleep(10)
            
            # Verifikasi login berhasil
            current_url = self.page.url
            logger.info(f"🌐 Current URL after login: {current_url}")
            
            # Simpan domain untuk navigasi selanjutnya
            self.current_domain = self.get_current_domain(current_url)
            logger.info(f"💾 Saved domain for navigation: {self.current_domain}")
            
            # Save session setelah login berhasil
            await self.save_session()
            logger.info("💾 Session saved after successful login")
            
            if any(x in current_url for x in ['webmaster/index', 'webmaster/new/share', 'webmaster/new/home']):
                logger.info("✅ Login successful!")
                return True
            else:
                logger.warning(f"⚠️ Unexpected URL after login: {current_url}")
                # Coba lanjutkan anyway
                return True
                
        except Exception as e:
            logger.error(f"💥 Login error: {e}")
            try:
                await self.page.screenshot(path="login_error.png")
                logger.info("📸 Saved login error screenshot")
            except:
                pass
            return False

    async def navigate_to_upload_page(self) -> bool:
        """Navigate ke halaman upload dengan memastikan elemen tersedia"""
        try:
            logger.info("🧭 Navigating to upload page...")
            
            upload_url = "https://dm.1024tera.com/webmaster/new/share"
            logger.info(f"🌐 Direct navigation to: {upload_url}")
            
            # Approach: Direct navigation dengan verifikasi elemen
            await self.page.goto(upload_url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(5)
            
            current_url = self.page.url
            logger.info(f"🌐 Current URL after navigation: {current_url}")
            
            # Verifikasi kita di halaman upload dengan mengecek elemen kunci
            try:
                # Cek apakah elemen upload area ada
                upload_area = await self.page.query_selector("div.source-arr")
                if upload_area:
                    logger.info("✅ Successfully navigated to upload page (upload area found)")
                    return True
                else:
                    logger.warning("⚠️ Upload area not found, might not be on upload page")
            except Exception as e:
                logger.warning(f"⚠️ Could not verify upload area: {e}")
        
            # Fallback: cek URL
            if 'new/share' in current_url:
                logger.info("✅ Successfully navigated to upload page (URL verified)")
                return True
            
            logger.error("❌ Navigation to upload page failed")
            return False
            
        except Exception as e:
            logger.error(f"💥 Navigation process error: {e}")
            return False

    async def create_new_folder(self, folder_name: str) -> bool:
        """Buat folder baru di Terabox berdasarkan recording devtools"""
        try:
            logger.info(f"📁 Membuat folder baru: {folder_name}")
            
            # Step 1: Klik elemen untuk memunculkan dialog pilih folder (sesuai recording)
            folder_dialog_success = await self.find_and_click_element([
                "span.upload-tips-path",
                "//*[@id=\"upload-container\"]/div/div[2]/div/span[2]",
                "::-p-text(Path)"
            ], "folder path selector")
            
            if not folder_dialog_success:
                logger.error("❌ Gagal membuka dialog pilih folder")
                return False
            
            await asyncio.sleep(3)
            
            # Step 2: Klik tombol "New Folder" (sesuai recording)
            new_folder_success = await self.find_and_click_element([
                "div.create-dir",
                "//html/body/div[8]/div/div[2]/div[3]/div[1]",
                "::-p-text(New Folder)"
            ], "new folder button")
            
            if not new_folder_success:
                logger.error("❌ Gagal klik tombol New Folder")
                return False
            
            await asyncio.sleep(2)
            
            # Step 3: Klik dan isi nama folder (sesuai recording)
            folder_input_success = await self.find_and_click_element([
                "div.share-save input",
                "//html/body/div[8]/div/div[2]/div[2]/div/div/div[1]/div/div[2]/div[8]/div/div/input"
            ], "folder name input")
            
            if not folder_input_success:
                logger.error("❌ Gagal klik input nama folder")
                return False
            
            await asyncio.sleep(1)
            
            # Step 4: Isi nama folder
            folder_fill_success = await self.find_and_fill_element([
                "div.share-save input",
                "//html/body/div[8]/div/div[2]/div[2]/div/div/div[1]/div/div[2]/div[8]/div/div/input"
            ], "folder name input", folder_name)
            
            if not folder_fill_success:
                logger.error("❌ Gagal mengisi nama folder")
                return False
            
            await asyncio.sleep(2)
            
            # Step 5: Klik tombol centang untuk konfirmasi nama folder (sesuai recording)
            folder_confirm_success = await self.find_and_click_element([
                "i.folder-name-commit",
                "//html/body/div[8]/div/div[2]/div[2]/div/div/div[1]/div/div[2]/div[8]/div/div/i[1]"
            ], "folder name confirm button")
            
            if not folder_confirm_success:
                logger.error("❌ Gagal klik tombol konfirmasi nama folder")
                return False
            
            await asyncio.sleep(2)
            
            # Step 6: Klik tombol "Confirm" untuk membuat folder (sesuai recording)
            create_confirm_success = await self.find_and_click_element([
                "div.create-confirm",
                "//html/body/div[8]/div/div[2]/div[3]/div[2]"
            ], "create folder confirm button")
            
            if not create_confirm_success:
                logger.error("❌ Gagal klik tombol confirm pembuatan folder")
                return False
            
            await asyncio.sleep(3)
            
            logger.info(f"✅ Folder '{folder_name}' berhasil dibuat di Terabox")
            return True
            
        except Exception as e:
            logger.error(f"💥 Error creating folder {folder_name}: {e}")
            try:
                await self.page.screenshot(path=f"create_folder_error_{folder_name}.png")
                logger.info("📸 Saved create folder error screenshot")
            except:
                pass
            return False

    async def upload_all_files(self, folder_path: Path) -> List[str]:
        """
        Upload semua file sekaligus dari folder download ke Terabox
        dengan membuat folder baru terlebih dahulu
        """
        try:
            folder_name = folder_path.name
            logger.info(f"📁 Memulai upload ke folder: {folder_name}")
            
            # Step 1: Buat folder baru di Terabox
            if not await self.create_new_folder(folder_name):
                logger.error("❌ Gagal membuat folder, melanjutkan upload ke root")
                # Lanjutkan tanpa membuat folder
            
            # Dapatkan semua file dari folder
            all_files = [f for f in folder_path.rglob('*') if f.is_file()]
            total_files = len(all_files)
            
            logger.info(f"📁 Menemukan {total_files} file di {folder_path}")
            
            if total_files == 0:
                logger.error("❌ Tidak ada file yang ditemukan untuk diupload")
                return []

            # Step 2: Klik tombol upload (Local file / Upload File)
            logger.info("🖱️ Mencari dan mengklik tombol upload...")
            
            # Coba berbagai selector untuk tombol upload
            upload_button_selectors = [
                "div.source-arr > div:nth-of-type(1) div:nth-of-type(2)",  # Local file area
                "div.share-main > div:nth-of-type(1) div:nth-of-type(1) > img",  # Upload icon
                "::-p-text(Upload File)",
                "::-p-text(Local File)",
                "div.local-item",  # Local file item
            ]
            
            upload_clicked = False
            for selector in upload_button_selectors:
                try:
                    await self.page.click(selector, timeout=10000)
                    logger.info(f"✅ Berhasil klik tombol upload dengan selector: {selector}")
                    upload_clicked = True
                    break
                except Exception as e:
                    logger.debug(f"❌ Gagal klik dengan selector {selector}: {e}")
                    continue
            
            if not upload_clicked:
                logger.error("❌ Gagal menemukan tombol upload")
                return []
            
            await asyncio.sleep(2)

            # Step 3: Cari elemen input file yang mendukung multiple
            logger.info("🔍 Mencari elemen input file...")
            
            input_selectors = [
                "input[type='file'][multiple]",  # Prioritaskan input dengan multiple
                "input[type='file']",
                "input[webkitdirectory]",  # Untuk folder upload
                "input[directory]",
                "input#fileElem",
                "div.source-arr input",
                "input[accept]",
                "input[name='file']"
            ]
            
            file_input = None
            for selector in input_selectors:
                try:
                    file_input = await self.page.query_selector(selector)
                    if file_input:
                        logger.info(f"✅ Found file input dengan selector: {selector}")
                        
                        # Cek apakah mendukung multiple
                        is_multiple = await file_input.get_attribute("multiple")
                        if is_multiple:
                            logger.info("🎯 Input file mendukung multiple selection")
                        else:
                            logger.info("ℹ️ Input file tidak mendukung multiple selection")
                        
                        break
                except Exception as e:
                    logger.debug(f"❌ Selector {selector} gagal: {e}")
                    continue
            
            if not file_input:
                logger.error("❌ Tidak menemukan elemen input file")
                await self.page.screenshot(path="upload_input_error.png")
                return []

            # Step 4: Upload semua file sekaligus
            try:
                logger.info(f"📤 Mengupload {total_files} file sekaligus...")
                
                # Konversi Path objects ke string paths
                file_paths = [str(f.absolute()) for f in all_files]
                
                # Upload semua file sekaligus
                await file_input.set_input_files(file_paths)
                
                logger.info(f"✅ Berhasil mengupload {total_files} file sekaligus")
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"❌ Gagal upload semua file sekaligus: {e}")
                logger.info("🔄 Mencoba upload file satu per satu...")
                
                # Fallback: upload file satu per satu
                return await self.upload_files_individual(folder_path)

            # Step 5: Tunggu upload selesai
            logger.info("⏳ Menunggu proses upload selesai...")
            await asyncio.sleep(10)
            await self.wait_for_network_idle()

            # Step 6: Klik Generate Link (sesuai recording)
            generate_success = await self.find_and_click_element([
                'div.share-way span',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[3]/div[1]/div[2]/div[2]/span',
                '//*[contains(text(), "Generate Link")]',
                'button:has-text("Generate Link")',
                '.generate-link-btn'
            ], "generate link button", timeout=60000)
            
            if not generate_success:
                logger.error("❌ Could not click Generate Link")
                return []
            
            # Wait for link generation
            logger.info("⏳ Waiting for link generation...")
            await asyncio.sleep(15)
            await self.wait_for_network_idle()

            # Step 7: Extract share links
            links = await self.extract_share_links()
            
            if links:
                logger.info(f"✅ Upload completed! {len(links)} links generated")
            else:
                logger.warning("⚠️ Upload completed but no links found")

            return links

        except Exception as e:
            logger.error(f"❌ Gagal upload semua file: {e}")
            try:
                await self.page.screenshot(path="upload_all_files_error.png", full_page=True)
                logger.info("📸 Saved upload error screenshot")
            except:
                pass
            return []

    async def upload_folder(self, folder_path: Path) -> List[str]:
        """Upload entire folder to Terabox - prioritaskan upload semua file sekaligus dengan buat folder"""
        try:
            logger.info(f"📁 Starting folder upload dengan metode all files + buat folder: {folder_path}")
            
            # Coba metode upload semua file sekaligus dengan buat folder terlebih dahulu
            links = await self.upload_all_files(folder_path)
            
            if links:
                logger.info("✅ Metode upload semua file + buat folder berhasil!")
                return links
            
            # Jika metode all files gagal, fallback ke metode individual
            logger.warning("⚠️ Metode semua file gagal, fallback ke upload individual...")
            return await self.upload_files_individual(folder_path)
            
        except Exception as e:
            logger.error(f"💥 Folder upload error: {e}")
            return await self.upload_files_individual(folder_path)

    async def upload_files(self, folder_path: Path) -> List[str]:
        """Upload files - prioritaskan upload semua file sekaligus dengan buat folder, lalu fallback ke individual"""
        try:
            logger.info(f"🔄 Starting upload process for folder: {folder_path}")
            
            # Cek jika folder berisi file yang valid
            all_files = [f for f in folder_path.rglob('*') if f.is_file()]
            if not all_files:
                logger.error("❌ Folder is empty, nothing to upload")
                return []
            
            # First try upload all files at once dengan buat folder
            logger.info("📁 Attempting upload semua file sekaligus dengan buat folder...")
            links = await self.upload_folder(folder_path)
            
            if links:
                logger.info("✅ Upload semua file + buat folder berhasil!")
                return links
            
            # Jika upload semua file gagal, try individual files
            logger.info("📄 Upload semua file gagal, trying individual file upload...")
            return await self.upload_files_individual(folder_path)
            
        except Exception as e:
            logger.error(f"💥 Upload files error: {e}")
            return []

    async def upload_files_individual(self, folder_path: Path) -> List[str]:
        """Upload files individually as fallback"""
        try:
            links = []
            
            # Get all files from folder
            all_files = [f for f in folder_path.rglob('*') if f.is_file()]
            
            # Filter hanya file media (foto dan video)
            media_files = [f for f in all_files if f.suffix.lower() in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS)]
            
            # Jika tidak ada file media, gunakan semua file
            if not media_files:
                media_files = all_files
            
            total_files = len(media_files)
            
            logger.info(f"📄 Found {total_files} media files for individual upload")
            
            if total_files == 0:
                logger.error("❌ No media files found to upload")
                return []
            
            # Buat folder baru terlebih dahulu (fallback method) - HANYA UNTUK TERABOX
            folder_name = folder_path.name
            if not await self.create_new_folder(folder_name):
                logger.warning("⚠️ Gagal membuat folder, melanjutkan upload ke root")
            
            # Upload semua file tanpa batasan
            batch_files = media_files
            
            successful_uploads = 0
            
            for i, file_path in enumerate(batch_files, 1):
                logger.info(f"📤 Uploading file {i}/{len(batch_files)}: {file_path.name}")
                
                if await self.upload_single_file(file_path, i, len(batch_files)):
                    successful_uploads += 1
                    logger.info(f"✅ Successfully uploaded: {file_path.name}")
                    
                    # Tunggu sebentar antara upload file
                    if i < len(batch_files):
                        await asyncio.sleep(3)
                else:
                    logger.error(f"❌ Failed to upload file: {file_path.name}")
            
            # Extract links setelah semua file diupload
            if successful_uploads > 0:
                links = await self.extract_share_links()
                logger.info(f"📊 Individual upload completed: {successful_uploads}/{len(batch_files)} files uploaded, {len(links)} links generated")
                
                # Jika berhasil upload beberapa file, simpan session
                await self.save_session()
            else:
                logger.error("❌ No files were successfully uploaded")
            
            return links
            
        except Exception as e:
            logger.error(f"💥 Individual files upload error: {e}")
            return []

    async def upload_single_file(self, file_path: Path, current: int, total: int) -> bool:
        """Upload single file dengan pendekatan yang lebih spesifik untuk Terabox"""
        try:
            logger.info(f"📤 Uploading file {current}/{total}: {file_path.name}")
            
            # Step 1: Klik tombol upload utama (sesuai recording)
            await self.page.click("div.share-main > div:nth-of-type(1) div:nth-of-type(1) > img", timeout=10000)
            logger.info("🖱️ Klik tombol upload utama")
            await asyncio.sleep(2)
            
            # Step 2: Cari input file (sesuai recording)
            file_input = None
            selectors = [
                "div.share-main > div:nth-of-type(1) input:nth-of-type(1)",
                "//*[@id=\"app\"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[1]/div[1]/div/input[1]",
                "input[type='file']",
                "input:nth-of-type(2)",
                "input#fileElem"
            ]
            
            for selector in selectors:
                try:
                    file_input = await self.page.query_selector(selector)
                    if file_input:
                        logger.info(f"✅ Found file input dengan selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"❌ Selector {selector} gagal: {e}")
        
            if not file_input:
                logger.error("❌ Could not find file input element")
                await self.page.screenshot(path="file_input_error.png")
                return False
            
            # Step 3: Handle file upload
            try:
                await file_input.set_input_files(str(file_path.absolute()))
                logger.info(f"✅ File sent to input: {file_path.name}")
            except Exception as e:
                logger.error(f"❌ Error setting file input: {e}")
                return False
            
            # Wait for file upload to complete
            logger.info("⏳ Waiting for file upload...")
            await asyncio.sleep(10)
            await self.wait_for_network_idle()
            
            # Step 4: Click Generate Link (sesuai recording)
            generate_success = await self.find_and_click_element([
                'div.share-way span',
                '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[3]/div[1]/div[2]/div[2]/span',
                '//*[contains(text(), "Generate Link")]',
                'button:has-text("Generate Link")',
                '.generate-link-btn'
            ], "generate link button", timeout=60000)
            
            if not generate_success:
                logger.error("❌ Could not click Generate Link")
                return False
            
            # Wait for link generation
            logger.info("⏳ Waiting for link generation...")
            await asyncio.sleep(12)
            await self.wait_for_network_idle()
            
            return True
            
        except Exception as e:
            logger.error(f"💥 Single file upload error: {e}")
            await self.page.screenshot(path=f"upload_error_{current}.png")
            return False

    async def extract_share_links(self) -> List[str]:
        """Extract sharing links dari halaman"""
        try:
            logger.info("🔍 Extracting share links from page...")
            
            links = []
            
            # Cari link dalam page content
            page_content = await self.page.content()
            
            # Pattern untuk Terabox share links
            patterns = [
                r'https?://[^\s<>"{}|\\^`]*terabox[^\s<>"{}|\\^`]*',
                r'https?://[^\s<>"{}|\\^`]*1024tera[^\s<>"{}|\\^`]*',
                r'https?://www\.terabox\.com/[^\s<>"{}|\\^`]*',
                r'https?://terabox\.com/[^\s<>"{}|\\^`]*'
            ]
            
            for pattern in patterns:
                found_links = re.findall(pattern, page_content)
                # Filter hanya link share yang valid
                valid_links = [link for link in found_links if any(x in link for x in ['/s/', '/share/', 'download', 'sharing'])]
                links.extend(valid_links)
            
            # Remove duplicates
            links = list(set(links))
            
            logger.info(f"📊 Found {len(links)} share links")
            
            # Save screenshot untuk debugging
            try:
                await self.page.screenshot(path="upload_result.png")
                logger.info("📸 Saved upload result screenshot")
            except:
                pass
            
            return links
            
        except Exception as e:
            logger.error(f"❌ Link extraction error: {e}")
            return []

    async def upload_folder_via_playwright(self, folder_path: Path) -> List[str]:
        """Main method untuk upload folder menggunakan Playwright dengan session persistence dan buat folder"""
        try:
            # Setup browser dengan session
            if not await self.setup_browser(use_session=True):
                logger.error("❌ Browser setup failed, cannot proceed with upload")
                return []

            logger.info(f"🚀 Starting Playwright upload for folder: {folder_path}")
            
            # Step 1: Check login status dan login jika diperlukan
            if not await self.login_to_terabox():
                logger.error("❌ Login failed, cannot proceed with upload")
                return []
            
            # Step 2: Navigate to upload page
            if not await self.navigate_to_upload_page():
                logger.error("❌ Navigation to upload page failed")
                return []
            
            # Step 3: Upload files (upload semua file sekaligus dengan buat folder first, then fallback to individual)
            links = await self.upload_files(folder_path)
            
            if links:
                logger.info(f"✅ Upload completed! {len(links)} links generated")
                for i, link in enumerate(links, 1):
                    logger.info(f"🔗 Link {i}: {link}")
            else:
                logger.warning("⚠️ Upload completed but no links found")
            
            return links
                
        except Exception as e:
            logger.error(f"💥 Playwright upload error: {e}")
            try:
                await self.page.screenshot(path=f"error_{int(time.time())}.png")
                logger.info("📸 Saved error screenshot")
            except:
                pass
            return []
        finally:
            await self.cleanup_browser()

    async def cleanup_browser(self):
        """Cleanup browser dan resources"""
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            logger.info("✅ Playwright browser closed")
        except Exception as e:
            logger.warning(f"⚠️ Error closing browser: {e}")

    def get_enhanced_manual_instructions(self, folder_path: Path, job_number: int) -> str:
        """Generate enhanced manual instructions dengan fitur buat folder"""
        file_count = len(list(folder_path.rglob('*')))
        
        instructions = f"""
📋 **INSTRUKSI UPLOAD MANUAL TERABOX - Job #{job_number}**

🎯 **Langkah-langkah Upload**:

1. **Buka Website**: https://dm.1024tera.com/webmaster/index

2. **Login**:
   - Email: {self.terabox_email}
   - Password: [tersembunyi]

3. **Navigasi ke Upload**:
   - Buka: https://dm.1024tera.com/webmaster/new/share

4. **Buat Folder Baru**:
   - Klik pada teks "Path" (di sebelah kanan atas area upload)
   - Klik tombol "New Folder"
   - Isi nama folder: `{folder_path.name}`
   - Klik tombol centang (✓)
   - Klik tombol "Confirm"

5. **Upload File**:
   - Klik tombol "Upload File" atau area upload
   - Pilih semua file dari folder: `{folder_path}`
   - Klik "Generate Link"

6. **Copy Link**:
   - Tunggu link generated
   - Klik tombol copy
   - Simpan link yang dihasilkan

📁 **Detail Folder**:
- Path: `{folder_path}`
- Total Files: {file_count} files
- Job ID: #{job_number}
- Folder Terabox: {folder_path.name}

🔧 **Jika Automation Gagal**:
- Pastikan login berhasil manual terlebih dahulu
- Cek koneksi internet
- Verifikasi folder berisi file yang valid

💡 **Tips**:
- Gunakan Chrome browser versi terbaru
- Matikan pop-up blocker
- Allow file system permissions
- Fitur buat folder otomatis sudah tersedia di automation
"""
        return instructions

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_playwright_uploader = TeraboxPlaywrightUploader()
        self.terabox_lock = threading.Lock()
        
        # Counter global untuk urutan job upload
        self._job_counter = 1
        self._counter_lock = threading.Lock()
        
        logger.info("📤 UploadManager initialized dengan Playwright uploader + buat folder")

    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, message: str):
        """Send progress message dan update user progress"""
        try:
            chat_id = active_downloads[job_id]['chat_id']
            
            # Hapus pesan progress sebelumnya jika ada
            if job_id in user_progress_messages:
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=user_progress_messages[job_id]
                    )
                except Exception as e:
                    logger.debug(f"Could not delete previous progress message: {e}")
            
            # Kirim pesan progress baru
            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=message
            )
            
            # Simpan message_id untuk penghapusan nanti
            user_progress_messages[job_id] = sent_message.message_id
            
        except Exception as e:
            logger.error(f"Error sending progress message: {e}")

    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox menggunakan Playwright automation dengan buat folder"""
        logger.info(f"🚀 Starting Terabox upload dengan buat folder untuk job {job_id}, folder: {folder_path}")
        
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
                f"🎯 Method: Upload Semua File Sekaligus + Buat Folder"
            )

            # Cek jika credential Terabox tersedia
            if not self.terabox_playwright_uploader.terabox_email or not self.terabox_playwright_uploader.terabox_password:
                await self.send_progress_message(
                    update, context, job_id,
                    "❌ Terabox credentials tidak ditemukan!\n"
                    "📋 Silakan set environment variables:\n"
                    "- TERABOX_EMAIL\n" 
                    "- TERABOX_PASSWORD"
                )
                return []

            # Coba automation dengan Playwright + buat folder
            await self.send_progress_message(
                update, context, job_id,
                "🔄 Mencoba login dan upload otomatis...\n"
                "📝 Alur: Buat folder → Upload semua file sekaligus → Generate Link"
            )
            
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                # Try Playwright automation dengan metode baru + buat folder
                links = await self.terabox_playwright_uploader.upload_folder_via_playwright(folder_path)
                
                if links:
                    success_msg = (
                        f"✅ Upload ke Terabox berhasil!\n"
                        f"🔢 Job Number: #{job_number}\n"
                        f"🔗 {len(links)} links generated\n"
                        f"📁 Folder: {folder_path.name}\n"
                        f"🎯 Method: Upload Semua File + Buat Folder Otomatis"
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
                    # Fallback ke instruksi manual
                    await self.send_progress_message(
                        update, context, job_id,
                        "⚠️ Upload otomatis tidak berhasil\n"
                        "📋 Beralih ke mode manual dengan instruksi lengkap..."
                    )
                    
                    instructions = self.terabox_playwright_uploader.get_enhanced_manual_instructions(folder_path, job_number)
                    await self.send_progress_message(update, context, job_id, instructions)
                    
                    return [f"Manual upload required - Job #{job_number}"]
                    
        except Exception as e:
            logger.error(f"💥 Terabox upload error untuk {job_id}: {e}")
            
            # Berikan instruksi manual
            with self._counter_lock:
                job_number = self._job_counter - 1
            
            instructions = self.terabox_playwright_uploader.get_enhanced_manual_instructions(folder_path, job_number)
            await self.send_progress_message(update, context, job_id, instructions)
            
            return []

class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, file_manager: FileManager, upload_manager: UploadManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.settings_manager = settings_manager
        self.processing = False
        self.processing_thread = None
        logger.info("🔄 DownloadProcessor initialized")

    def start_processing(self):
        """Start the download processing thread"""
        if not self.processing:
            self.processing = True
            self.processing_thread = threading.Thread(target=self._process_queue, daemon=True)
            self.processing_thread.start()
            logger.info("🚀 Download processor started")

    def stop_processing(self):
        """Stop the download processing thread"""
        self.processing = False
        if self.processing_thread:
            self.processing_thread.join(timeout=10)
        logger.info("🛑 Download processor stopped")

    def _process_queue(self):
        """Process download queue in a separate thread"""
        while self.processing:
            try:
                if not download_queue.empty() and len(active_downloads) < MAX_CONCURRENT_DOWNLOADS:
                    job_id, folder_url, update, context = download_queue.get()
                    
                    # Start download in a separate thread to avoid blocking
                    download_thread = threading.Thread(
                        target=self._process_download_job,
                        args=(job_id, folder_url, update, context),
                        daemon=True
                    )
                    download_thread.start()
                    
                time.sleep(1)
            except Exception as e:
                logger.error(f"💥 Error in queue processing: {e}")
                time.sleep(5)

    def _process_download_job(self, job_id: str, folder_url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process a single download job"""
        try:
            asyncio.run(self._async_process_download_job(job_id, folder_url, update, context))
        except Exception as e:
            logger.error(f"💥 Error in download job processing: {e}")

    async def _async_process_download_job(self, job_id: str, folder_url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Async process a download job"""
        try:
            user_id = update.effective_user.id
            user_settings = self.settings_manager.get_user_settings(user_id)
            
            # Update job status
            active_downloads[job_id].update({
                'status': DownloadStatus.DOWNLOADING.value,
                'start_time': datetime.now(),
                'user_settings': user_settings
            })
            
            # Generate download path
            download_folder_name = f"download_{job_id}_{int(time.time())}"
            download_path = DOWNLOAD_BASE / download_folder_name
            
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"📥 Starting download...\n"
                f"🆔 Job ID: {job_id}\n"
                f"📁 Folder: {download_folder_name}\n"
                f"🔗 URL: {folder_url[:50]}..."
            )
            
            # Download from Mega.nz
            success, message = self.mega_manager.download_mega_folder(folder_url, download_path, job_id)
            
            if not success:
                active_downloads[job_id].update({
                    'status': DownloadStatus.ERROR.value,
                    'error': message,
                    'end_time': datetime.now()
                })
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id,
                    f"❌ Download failed!\n"
                    f"🆔 Job ID: {job_id}\n"
                    f"📛 Error: {message}"
                )
                return
            
            # Update status to download completed
            active_downloads[job_id].update({
                'status': DownloadStatus.DOWNLOAD_COMPLETED.value,
                'download_path': str(download_path)
            })
            
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"✅ Download completed!\n"
                f"🆔 Job ID: {job_id}\n"
                f"📁 Path: {download_path.name}\n"
                f"🔄 Starting file processing..."
            )
            
            # Auto-rename files if enabled in settings
            if user_settings.get('auto_rename', True):
                active_downloads[job_id]['status'] = DownloadStatus.RENAMING.value
                
                prefix = user_settings.get('prefix', 'file_')
                rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id,
                    f"📝 File renaming completed!\n"
                    f"🆔 Job ID: {job_id}\n"
                    f"📊 Result: {rename_result['renamed']}/{rename_result['total']} files renamed"
                )
            
            # Auto-upload if enabled in settings
            if user_settings.get('auto_upload', True):
                active_downloads[job_id]['status'] = DownloadStatus.UPLOADING.value
                
                platform = user_settings.get('platform', 'terabox')
                
                if platform == 'terabox':
                    await self.upload_manager.send_progress_message(
                        update, context, job_id,
                        f"📤 Starting upload to Terabox...\n"
                        f"🆔 Job ID: {job_id}\n"
                        f"📁 Folder: {download_path.name}\n"
                        f"🎯 Platform: {platform}"
                    )
                    
                    links = await self.upload_manager.upload_to_terabox(download_path, update, context, job_id)
                    
                    if links:
                        active_downloads[job_id].update({
                            'status': DownloadStatus.COMPLETED.value,
                            'upload_links': links,
                            'end_time': datetime.now()
                        })
                    else:
                        active_downloads[job_id].update({
                            'status': DownloadStatus.ERROR.value,
                            'error': 'Upload failed',
                            'end_time': datetime.now()
                        })
                else:
                    # Other platforms can be added here
                    active_downloads[job_id].update({
                        'status': DownloadStatus.COMPLETED.value,
                        'end_time': datetime.now()
                    })
                    
                    await self.upload_manager.send_progress_message(
                        update, context, job_id,
                        f"✅ Download completed without upload!\n"
                        f"🆔 Job ID: {job_id}\n"
                        f"📁 Path: {download_path}\n"
                        f"💡 Platform {platform} not configured for auto-upload"
                    )
            else:
                # Mark as completed without upload
                active_downloads[job_id].update({
                    'status': DownloadStatus.COMPLETED.value,
                    'end_time': datetime.now()
                })
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id,
                    f"✅ Download completed!\n"
                    f"🆔 Job ID: {job_id}\n"
                    f"📁 Path: {download_path}\n"
                    f"💡 Auto-upload is disabled in settings"
                )
            
            # Auto-cleanup if enabled
            if user_settings.get('auto_cleanup', True) and active_downloads[job_id]['status'] == DownloadStatus.COMPLETED.value:
                try:
                    shutil.rmtree(download_path)
                    logger.info(f"🧹 Cleaned up download folder: {download_path}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not cleanup folder {download_path}: {e}")
            
            # Move to completed downloads
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
        except Exception as e:
            logger.error(f"💥 Error in async download job: {e}")
            active_downloads[job_id].update({
                'status': DownloadStatus.ERROR.value,
                'error': str(e),
                'end_time': datetime.now()
            })

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when the command /start is issued."""
    welcome_text = """
🤖 **Mega Downloader Bot dengan Upload Terabox**

**Fitur Utama:**
📥 Download folder dari Mega.nz
📤 Upload otomatis ke Terabox  
📝 Auto-rename file numbering
📁 Buat folder otomatis di Terabox
🧹 Auto-cleanup setelah selesai

**Perintah yang tersedia:**
/download [url] - Download folder Mega.nz
/upload [path] - Upload manual ke Terabox
/status - Lihat status download
/setprefix [nama] - Set prefix untuk rename
/setplatform [terabox] - Set platform upload
/autoupload [on/off] -Toggle auto upload
/autocleanup [on/off] - Toggle auto cleanup
/mysettings - Lihat pengaturan Anda
/cleanup - Bersihkan folder download
/help - Tampilkan bantuan ini

**Fitur Baru:**
🎯 Buat folder otomatis di Terabox untuk setiap upload!
    """
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message when the command /help is issued."""
    help_text = """
📖 **Bantuan Mega Downloader Bot**

**Cara Penggunaan:**
1. **Download**: `/download [mega_folder_url]`
   Contoh: `/download https://mega.nz/folder/abc123`

2. **Upload Manual**: `/upload [folder_path]`
   Contoh: `/upload downloads/my_folder`

3. **Cek Status**: `/status`

**Pengaturan:**
- `/setprefix [nama]` - Set nama prefix untuk file
- `/setplatform [terabox]` - Set platform upload  
- `/autoupload [on/off]` - Enable/disable auto upload
- `/autocleanup [on/off]` - Enable/disable auto cleanup
- `/mysettings` - Lihat pengaturan Anda

**Fitur Terabox:**
✅ Buat folder otomatis di Terabox
✅ Upload semua file sekaligus
✅ Generate multiple share links
✅ Session persistence untuk login

**Catatan:**
- Bot akan otomatis membuat folder di Terabox dengan nama yang sama
- File akan di-rename dengan format: `prefix 01.ext`
- Download maksimal 2 folder bersamaan
    """
    await update.message.reply_text(help_text)

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /download command."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a Mega.nz folder URL\n"
                "Contoh: /download https://mega.nz/folder/abc123"
            )
            return
        
        folder_url = context.args[0]
        
        # Validate Mega.nz URL
        if not folder_url.startswith('https://mega.nz/'):
            await update.message.reply_text(
                "❌ Invalid Mega.nz URL\n"
                "URL harus dimulai dengan: https://mega.nz/"
            )
            return
        
        # Generate job ID
        job_id = str(uuid.uuid4())[:8]
        
        # Add to download queue
        download_queue.put((job_id, folder_url, update, context))
        
        # Initialize download info
        active_downloads[job_id] = {
            'job_id': job_id,
            'folder_url': folder_url,
            'status': DownloadStatus.PENDING.value,
            'chat_id': update.effective_chat.id,
            'user_id': update.effective_user.id,
            'queue_time': datetime.now()
        }
        
        await update.message.reply_text(
            f"✅ Download job added to queue!\n"
            f"🆔 Job ID: {job_id}\n"
            f"📥 URL: {folder_url[:50]}...\n"
            f"📊 Queue position: {download_queue.qsize()}\n"
            f"⏳ Active downloads: {len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}"
        )
        
    except Exception as e:
        logger.error(f"Error in download command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /upload command for manual upload."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a folder path\n"
                "Contoh: /upload downloads/my_folder"
            )
            return
        
        folder_path = Path(context.args[0])
        
        if not folder_path.exists():
            await update.message.reply_text(
                f"❌ Folder not found: {folder_path}"
            )
            return
        
        # Generate job ID
        job_id = str(uuid.uuid4())[:8]
        
        # Initialize upload info
        active_downloads[job_id] = {
            'job_id': job_id,
            'folder_path': str(folder_path),
            'status': DownloadStatus.UPLOADING.value,
            'chat_id': update.effective_chat.id,
            'user_id': update.effective_user.id,
            'start_time': datetime.now()
        }
        
        # Start upload
        await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
        
    except Exception as e:
        logger.error(f"Error in upload command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /status command."""
    try:
        if not active_downloads and not completed_downloads:
            await update.message.reply_text("📊 No active or completed downloads")
            return
        
        status_text = "📊 **Download Status**\n\n"
        
        # Active downloads
        if active_downloads:
            status_text += "**🟢 Active Downloads:**\n"
            for job_id, info in list(active_downloads.items())[:5]:  # Show last 5
                status_text += f"• {job_id}: {info['status']}\n"
        else:
            status_text += "**🔴 No active downloads**\n"
        
        # Queue info
        status_text += f"\n**📥 Queue:** {download_queue.qsize()} waiting\n"
        status_text += f"**⚡ Active:** {len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}\n"
        
        # Recent completed
        if completed_downloads:
            status_text += f"\n**✅ Completed:** {len(completed_downloads)} jobs"
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def counter_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /counterstatus command."""
    try:
        status_text = "📊 **Counter Status**\n\n"
        status_text += f"**📥 Download Queue:** {download_queue.qsize()}\n"
        status_text += f"**⚡ Active Downloads:** {len(active_downloads)}\n"
        status_text += f"**✅ Completed Downloads:** {len(completed_downloads)}\n"
        status_text += f"**🔢 Next Job Number:** #{upload_manager._job_counter}\n"
        status_text += f"**👥 User Settings:** {len(settings_manager.settings)} users"
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"Error in counter status command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /debug command for system diagnostics."""
    try:
        debug_info = mega_manager.debug_mega_session()
        
        debug_text = "🐛 **Debug Information**\n\n"
        
        # Mega-get status
        debug_text += f"**Mega-get Path:** {debug_info.get('mega_get_path', 'N/A')}\n"
        debug_text += f"**Mega-get Exists:** {debug_info.get('mega_get_exists', False)}\n"
        debug_text += f"**Mega-get Executable:** {debug_info.get('mega_get_executable', False)}\n"
        
        # Accounts
        debug_text += f"**Mega Accounts:** {len(mega_manager.accounts)}\n"
        if mega_manager.accounts:
            debug_text += f"**Current Account:** {debug_info.get('current_account', 'N/A')}\n"
        
        # Disk space
        if 'disk_space' in debug_info:
            debug_text += f"**Disk Space:**\n{debug_info['disk_space']}\n"
        
        # Downloads directory
        debug_text += f"**Downloads Writable:** {debug_info.get('downloads_writable', False)}\n"
        
        await update.message.reply_text(debug_text)
        
    except Exception as e:
        logger.error(f"Error in debug command: {e}")
        await update.message.reply_text(f"❌ Debug error: {str(e)}")

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set file prefix for auto-rename."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a prefix\n"
                "Contoh: /setprefix myfiles"
            )
            return
        
        prefix = context.args[0]
        user_id = update.effective_user.id
        
        settings_manager.update_user_settings(user_id, {'prefix': prefix})
        
        await update.message.reply_text(
            f"✅ Prefix updated to: {prefix}\n"
            f"File akan di-rename sebagai: {prefix} 01.ext, {prefix} 02.ext, dst."
        )
        
    except Exception as e:
        logger.error(f"Error in set_prefix: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set upload platform."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a platform\n"
                "Contoh: /setplatform terabox"
            )
            return
        
        platform = context.args[0].lower()
        
        if platform not in ['terabox']:
            await update.message.reply_text(
                f"❌ Platform tidak didukung: {platform}\n"
                f"Platform yang tersedia: terabox"
            )
            return
        
        user_id = update.effective_user.id
        settings_manager.update_user_settings(user_id, {'platform': platform})
        
        await update.message.reply_text(
            f"✅ Platform updated to: {platform}\n"
            f"File akan diupload ke: {platform}"
        )
        
    except Exception as e:
        logger.error(f"Error in set_platform: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload feature."""
    try:
        if not context.args:
            # Show current status
            user_id = update.effective_user.id
            user_settings = settings_manager.get_user_settings(user_id)
            auto_upload = user_settings.get('auto_upload', True)
            
            status = "ON" if auto_upload else "OFF"
            await update.message.reply_text(
                f"🔄 Auto-upload status: {status}\n"
                f"Gunakan: /autoupload on atau /autoupload off"
            )
            return
        
        toggle = context.args[0].lower()
        
        if toggle not in ['on', 'off']:
            await update.message.reply_text(
                "❌ Invalid option. Use: /autoupload on atau /autoupload off"
            )
            return
        
        user_id = update.effective_user.id
        auto_upload = toggle == 'on'
        settings_manager.update_user_settings(user_id, {'auto_upload': auto_upload})
        
        status = "ON" if auto_upload else "OFF"
        await update.message.reply_text(f"✅ Auto-upload: {status}")
        
    except Exception as e:
        logger.error(f"Error in auto_upload_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-cleanup feature."""
    try:
        if not context.args:
            # Show current status
            user_id = update.effective_user.id
            user_settings = settings_manager.get_user_settings(user_id)
            auto_cleanup = user_settings.get('auto_cleanup', True)
            
            status = "ON" if auto_cleanup else "OFF"
            await update.message.reply_text(
                f"🧹 Auto-cleanup status: {status}\n"
                f"Gunakan: /autocleanup on atau /autocleanup off"
            )
            return
        
        toggle = context.args[0].lower()
        
        if toggle not in ['on', 'off']:
            await update.message.reply_text(
                "❌ Invalid option. Use: /autocleanup on atau /autocleanup off"
            )
            return
        
        user_id = update.effective_user.id
        auto_cleanup = toggle == 'on'
        settings_manager.update_user_settings(user_id, {'auto_cleanup': auto_cleanup})
        
        status = "ON" if auto_cleanup else "OFF"
        await update.message.reply_text(f"✅ Auto-cleanup: {status}")
        
    except Exception as e:
        logger.error(f"Error in auto_cleanup_toggle: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user settings."""
    try:
        user_id = update.effective_user.id
        user_settings = settings_manager.get_user_settings(user_id)
        
        settings_text = "⚙️ **Your Settings**\n\n"
        settings_text += f"**📝 Prefix:** {user_settings.get('prefix', 'file_')}\n"
        settings_text += f"**📤 Platform:** {user_settings.get('platform', 'terabox')}\n"
        settings_text += f"**🔄 Auto-upload:** {'ON' if user_settings.get('auto_upload', True) else 'OFF'}\n"
        settings_text += f"**🧹 Auto-cleanup:** {'ON' if user_settings.get('auto_cleanup', True) else 'OFF'}\n"
        
        await update.message.reply_text(settings_text)
        
    except Exception as e:
        logger.error(f"Error in my_settings: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup download directories."""
    try:
        # Count files and size before cleanup
        total_size = 0
        total_files = 0
        total_folders = 0
        
        for path in DOWNLOAD_BASE.rglob('*'):
            if path.is_file():
                total_files += 1
                total_size += path.stat().st_size
            elif path.is_dir():
                total_folders += 1
        
        # Perform cleanup
        for item in DOWNLOAD_BASE.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink()
        
        # Format size
        size_mb = total_size / (1024 * 1024)
        
        await update.message.reply_text(
            f"🧹 Cleanup completed!\n"
            f"📁 Folders removed: {total_folders}\n"
            f"📄 Files removed: {total_files}\n"
            f"💾 Space freed: {size_mb:.2f} MB"
        )
        
    except Exception as e:
        logger.error(f"Error in cleanup_command: {e}")
        await update.message.reply_text(f"❌ Cleanup error: {str(e)}")

# Initialize managers
logger.info("🔄 Initializing managers dengan fitur buat folder Terabox...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

def main():
    """Start the bot"""
    logger.info("🚀 Starting Mega Downloader Bot dengan Upload Semua File + Buat Folder Terabox...")
    
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
    
    # Check session file
    session_exists = os.path.exists('terabox_session.json')
    if session_exists:
        logger.info("✅ Terabox session file found - will use existing session")
    else:
        logger.info("ℹ️ No Terabox session file found - will create new session on first login")
    
    # Install required packages untuk Playwright
    try:
        import playwright
        logger.info("✅ Playwright is available")
    except ImportError:
        logger.warning("⚠️ Playwright not installed, installing...")
        subprocess.run(['pip', 'install', 'playwright'], check=True)
        subprocess.run(['playwright', 'install', 'chromium'], check=True)
        logger.info("✅ Playwright installed")
    
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
    logger.info("✅ Bot started successfully dengan metode upload semua file + buat folder Terabox!")
    application.run_polling()

if __name__ == '__main__':
    main()
