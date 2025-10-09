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

# Constants - UPDATE PATH KE LOKASI BARU
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg'}
DOWNLOAD_BASE = Path('/home/ubuntu/bot-tele/downloads')  # PATH BARU YANG DIPERBAIKI
MAX_CONCURRENT_DOWNLOADS = 2
MAX_UPLOAD_RETRIES = 3
UPLOAD_BATCH_SIZE = 10  # Batasi jumlah file per batch

# Global state
download_queue = Queue()
active_downloads: Dict[str, Dict] = {}
completed_downloads: Dict[str, Dict] = {}
cancelled_downloads: Dict[str, Dict] = {}
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
    CANCELLED = "cancelled"

class UserSettingsManager:
    def __init__(self):
        self.settings_file = '/home/ubuntu/bot-tele/user_settings.json'  # PATH BARU
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
            # Pastikan directory exists
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
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
                'auto_cleanup': True,
                'auto_rename': True
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
        self.cred_file = '/home/ubuntu/bot-tele/mega_session.json'  # PATH BARU
        self.accounts = self.load_mega_accounts()
        self.current_account_index = 0
        self.mega_get_path = self._get_mega_get_path()
        self.active_processes: Dict[str, subprocess.Popen] = {}
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
        
        # Try to load from mega_accounts.json first - PATH BARU
        try:
            mega_accounts_file = '/home/ubuntu/bot-tele/mega_accounts.json'
            with open(mega_accounts_file, 'r') as f:
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

    def find_downloaded_folder(self, job_id: str) -> Optional[Path]:
        """Find the actual downloaded folder in DOWNLOAD_BASE"""
        try:
            logger.info(f"🔍 Searching for downloaded folder for job {job_id}")
            
            # List semua folder di DOWNLOAD_BASE
            all_items = list(DOWNLOAD_BASE.iterdir())
            folders = [item for item in all_items if item.is_dir()]
            
            logger.info(f"📁 Found {len(folders)} folders in download directory:")
            for folder in folders:
                # Hitung jumlah file dalam folder
                files = list(folder.rglob('*'))
                file_count = len([f for f in files if f.is_file()])
                logger.info(f"  - {folder.name}: {file_count} files")
                
                # Jika folder berisi file, anggap ini adalah folder hasil download
                if file_count > 0:
                    logger.info(f"✅ Selected folder for upload: {folder.name} with {file_count} files")
                    return folder
            
            logger.error("❌ No folders with files found for upload")
            return None
            
        except Exception as e:
            logger.error(f"💥 Error finding downloaded folder: {e}")
            return None

    def stop_download(self, job_id: str) -> bool:
        """Stop a running download process for the given job_id"""
        try:
            if job_id in self.active_processes:
                process = self.active_processes[job_id]
                logger.info(f"🛑 Attempting to stop download process for job {job_id}")
                
                # Terminate the process
                process.terminate()
                
                # Wait for process to terminate
                try:
                    process.wait(timeout=10)
                    logger.info(f"✅ Successfully stopped download process for job {job_id}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"⚠️ Process didn't terminate gracefully, killing for job {job_id}")
                    process.kill()
                    process.wait()
                
                # Remove from active processes
                del self.active_processes[job_id]
                return True
            else:
                logger.warning(f"⚠️ No active download process found for job {job_id}")
                return False
                
        except Exception as e:
            logger.error(f"💥 Error stopping download for job {job_id}: {e}")
            return False
    
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
                
                # Pastikan base download directory ada
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
                
                # Change to base download directory for mega-get
                original_cwd = os.getcwd()
                os.chdir(DOWNLOAD_BASE)
                logger.info(f"📂 Changed working directory to base: {DOWNLOAD_BASE}")
                
                try:
                    # Now download using mega-get dengan Popen agar bisa di-stop
                    download_cmd = [self.mega_get_path, folder_url]
                    logger.info(f"⚡ Executing download command: {' '.join(download_cmd)}")
                    
                    # Execute download dengan Popen untuk kontrol proses
                    start_time = time.time()
                    logger.info(f"⏰ Download started at: {datetime.now()}")
                    
                    # Gunakan Popen agar bisa dihentikan
                    process = subprocess.Popen(
                        download_cmd, 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    
                    # Simpan process reference untuk bisa di-stop
                    self.active_processes[job_id] = process
                    
                    # Tunggu proses selesai dengan timeout
                    try:
                        stdout, stderr = process.communicate(timeout=7200)  # 2 hours
                        return_code = process.returncode
                    except subprocess.TimeoutExpired:
                        # Jika timeout, terminate process
                        process.terminate()
                        stdout, stderr = process.communicate()
                        return_code = process.returncode
                        logger.error(f"⏰ Download timeout for {job_id} (2 hours)")
                    
                    # Hapus dari active processes setelah selesai
                    if job_id in self.active_processes:
                        del self.active_processes[job_id]
                    
                    end_time = time.time()
                    download_duration = end_time - start_time
                    logger.info(f"⏰ Download completed at: {datetime.now()}, duration: {download_duration:.2f}s")
                    
                    # Log command results
                    logger.info(f"📊 Download command return code: {return_code}")
                    logger.info(f"📤 Download stdout: {stdout}")
                    if stderr:
                        logger.warning(f"📥 Download stderr: {stderr}")
                    
                    # Return to original directory
                    os.chdir(original_cwd)
                    logger.info("📂 Returned to original working directory")
                    
                    if return_code == 0:
                        # Wait for files to stabilize
                        logger.info("⏳ Waiting for files to stabilize...")
                        time.sleep(5)
                        
                        # Cari folder yang berhasil di-download
                        downloaded_folder = self.find_downloaded_folder(job_id)
                        
                        if not downloaded_folder:
                            error_msg = "Download completed but no folder with files was found"
                            logger.error(f"❌ {error_msg}")
                            return False, error_msg
                        
                        # Update download path dengan folder yang sebenarnya
                        actual_download_path = downloaded_folder
                        logger.info(f"✅ Found downloaded folder: {actual_download_path}")
                        
                        # Check files in the actual folder
                        all_files = list(actual_download_path.rglob('*'))
                        files = [f for f in all_files if f.is_file()]
                        
                        total_files = len(files)
                        
                        if total_files == 0:
                            error_msg = "Download completed but no files were found in the folder"
                            logger.error(f"❌ {error_msg}")
                            return False, error_msg
                        
                        # Log all files for debugging
                        for f in files[:10]:  # Log first 10 files only
                            try:
                                file_size = f.stat().st_size
                                logger.info(f"📄 File: {f.relative_to(actual_download_path)} ({file_size} bytes)")
                            except Exception as e:
                                logger.warning(f"⚠️ Could not stat file {f}: {e}")
                        
                        if total_files > 10:
                            logger.info(f"📄 ... and {total_files - 10} more files")
                        
                        success_msg = f"Download successful! {total_files} files downloaded in {download_duration:.2f}s to {actual_download_path.name}"
                        logger.info(f"✅ {success_msg}")
                        
                        # Simpan path aktual ke active_downloads
                        if job_id in active_downloads:
                            active_downloads[job_id]['actual_download_path'] = str(actual_download_path)
                        
                        return True, success_msg
                    else:
                        error_msg = stderr if stderr else stdout
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
                            
                except Exception as e:
                    os.chdir(original_cwd)
                    # Hapus dari active processes jika ada error
                    if job_id in self.active_processes:
                        del self.active_processes[job_id]
                    logger.error(f"💥 Unexpected error during download: {e}")
                    return False, f"Unexpected error: {str(e)}"
                    
            except Exception as e:
                logger.error(f"💥 Error in download process: {e}")
                return False, f"Process error: {str(e)}"
        
        return False, f"Download failed after {max_retries} retries due to quota issues"

    def get_downloaded_folders(self) -> List[Dict]:
        """Get list of all downloaded folders in DOWNLOAD_BASE"""
        try:
            folders = []
            if not DOWNLOAD_BASE.exists():
                return folders
            
            for item in DOWNLOAD_BASE.iterdir():
                if item.is_dir():
                    # Count files in folder
                    files = list(item.rglob('*'))
                    file_count = len([f for f in files if f.is_file()])
                    
                    # Get folder size
                    total_size = sum(f.stat().st_size for f in files if f.is_file())
                    
                    folders.append({
                        'name': item.name,
                        'path': str(item),
                        'file_count': file_count,
                        'total_size': total_size,
                        'created_time': item.stat().st_ctime
                    })
            
            # Sort by creation time (newest first)
            folders.sort(key=lambda x: x['created_time'], reverse=True)
            return folders
            
        except Exception as e:
            logger.error(f"Error getting downloaded folders: {e}")
            return []

    def find_folder_by_name(self, folder_name: str) -> Optional[Path]:
        """Find folder by name in DOWNLOAD_BASE"""
        try:
            target_path = DOWNLOAD_BASE / folder_name
            if target_path.exists() and target_path.is_dir():
                return target_path
            
            # Jika tidak ditemukan dengan nama exact, cari partial match
            for item in DOWNLOAD_BASE.iterdir():
                if item.is_dir() and folder_name.lower() in item.name.lower():
                    return item
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding folder by name: {e}")
            return None

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

    @staticmethod
    def rename_folder(old_folder_name: str, new_folder_name: str) -> Tuple[bool, str]:
        """Rename folder inside DOWNLOAD_BASE"""
        try:
            old_path = DOWNLOAD_BASE / old_folder_name
            new_path = DOWNLOAD_BASE / new_folder_name

            if not old_path.exists():
                return False, f"Folder '{old_folder_name}' tidak ditemukan"
            
            if new_path.exists():
                return False, f"Folder '{new_folder_name}' sudah ada"
            
            old_path.rename(new_path)
            logger.info(f"✅ Folder renamed: {old_folder_name} -> {new_folder_name}")
            return True, f"Folder berhasil direname: {new_folder_name}"
        except Exception as e:
            logger.error(f"❌ Error renaming folder: {e}")
            return False, f"Error: {str(e)}"

class TeraboxPlaywrightUploader:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.terabox_email = os.getenv('TERABOX_EMAIL')
        self.terabox_password = os.getenv('TERABOX_PASSWORD')
        self.current_domain = None
        self.session_file = "/home/ubuntu/bot-tele/terabox_session.json"  # PATH BARU
        self.timeout = 45000  # 45 seconds in milliseconds
        self.uploaded_files_tracker = set()  # Track files yang sudah diupload
        logger.info("🌐 TeraboxPlaywrightUploader initialized dengan session persistence + anti-duplikasi")

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
            
            # Launch browser dengan headless mode dan opsi stabil
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
                    '--memory-pressure-off',
                    '--max-old-space-size=4096',
                ],
                timeout=60000
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
            # Pastikan directory exists
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            with open(self.session_file, 'w') as f:
                json.dump(storage_state, f)
            logger.info("💾 Session saved successfully")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to save session: {e}")
            return False

    async def wait_for_network_idle(self, timeout: int = 30000):
        """Wait for network to be idle"""
        try:
            await self.page.wait_for_load_state('networkidle', timeout=timeout)
        except Exception as e:
            logger.debug(f"Network idle wait timeout: {e}")

    async def safe_click(self, selector: str, description: str, timeout: int = 10000) -> bool:
        """Safe click dengan error handling yang lebih baik"""
        try:
            logger.info(f"🖱️ Attempting to click: {description} dengan selector: {selector}")
            
            # Tunggu element tersedia
            element = await self.page.wait_for_selector(selector, timeout=timeout)
            if not element:
                logger.error(f"❌ Element not found: {description}")
                return False
            
            # Scroll ke element
            await element.scroll_into_view_if_needed()
            
            # Tunggu sebentar sebelum klik
            await asyncio.sleep(1)
            
            # Click dengan error handling
            await element.click(delay=100)
            
            logger.info(f"✅ Successfully clicked: {description}")
            await asyncio.sleep(2)
            return True
            
        except Exception as e:
            logger.error(f"❌ Error clicking {description}: {e}")
            return False

    async def safe_upload_files(self, file_input, file_paths: List[str], description: str) -> bool:
        """Safe file upload dengan error handling dan anti-duplikasi"""
        try:
            logger.info(f"📤 Attempting to upload {len(file_paths)} files: {description}")
            
            # Filter files yang belum diupload dalam session ini
            files_to_upload = []
            for file_path in file_paths:
                file_id = f"{Path(file_path).name}_{Path(file_path).stat().st_size}"
                if file_id not in self.uploaded_files_tracker:
                    files_to_upload.append(file_path)
                else:
                    logger.info(f"⏭️ Skipping already uploaded file: {Path(file_path).name}")
            
            if not files_to_upload:
                logger.info("✅ All files already uploaded in this session")
                return True
            
            # Batasi jumlah file yang diupload sekaligus
            if len(files_to_upload) > UPLOAD_BATCH_SIZE:
                logger.info(f"📦 Splitting {len(files_to_upload)} files into batches of {UPLOAD_BATCH_SIZE}")
                
                for i in range(0, len(files_to_upload), UPLOAD_BATCH_SIZE):
                    batch = files_to_upload[i:i + UPLOAD_BATCH_SIZE]
                    logger.info(f"📦 Uploading batch {i//UPLOAD_BATCH_SIZE + 1}/{(len(files_to_upload)-1)//UPLOAD_BATCH_SIZE + 1}")
                    
                    await file_input.set_input_files(batch)
                    await asyncio.sleep(5)
                    
                    # Track uploaded files
                    for file_path in batch:
                        file_id = f"{Path(file_path).name}_{Path(file_path).stat().st_size}"
                        self.uploaded_files_tracker.add(file_id)
                    
                    # Cek jika browser masih responsive
                    try:
                        await self.page.title()
                    except Exception as e:
                        logger.error(f"❌ Browser crashed during batch upload: {e}")
                        return False
            else:
                await file_input.set_input_files(files_to_upload)
                # Track uploaded files
                for file_path in files_to_upload:
                    file_id = f"{Path(file_path).name}_{Path(file_path).stat().st_size}"
                    self.uploaded_files_tracker.add(file_id)
            
            logger.info(f"✅ Successfully uploaded {len(files_to_upload)} files")
            await asyncio.sleep(5)
            return True
            
        except Exception as e:
            logger.error(f"❌ Error uploading files {description}: {e}")
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
            login_success = await self.safe_click('div.referral-content span', "login button")
            
            if not login_success:
                logger.error("❌ Failed to click login button")
                return False
            
            await asyncio.sleep(3)
            
            # Step 3: Click email login method
            email_login_success = await self.safe_click('div.other-item > div:nth-of-type(2)', "email login method")
            
            if not email_login_success:
                logger.error("❌ Failed to click email login method")
                return False
            
            await asyncio.sleep(3)
            
            # Step 4: Fill email field
            email_input = await self.page.wait_for_selector('[aria-label="Enter your email"]', timeout=30000)
            if email_input:
                await email_input.click(click_count=3)
                await self.page.keyboard.press('Backspace')
                await email_input.fill(self.terabox_email)
                logger.info("✅ Email filled")
            else:
                logger.error("❌ Email input not found")
                return False
            
            await asyncio.sleep(2)
            
            # Step 5: Fill password field
            password_input = await self.page.wait_for_selector('[aria-label="Enter the password."]', timeout=30000)
            if password_input:
                await password_input.click(click_count=3)
                await self.page.keyboard.press('Backspace')
                await password_input.fill(self.terabox_password)
                logger.info("✅ Password filled")
            else:
                logger.error("❌ Password input not found")
                return False
            
            await asyncio.sleep(2)
            
            # Step 6: Click login submit button
            login_submit_success = await self.safe_click('div.btn-class-login', "login submit button")
            
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
                await self.page.screenshot(path="/home/ubuntu/bot-tele/login_error.png")  # PATH BARU
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
            folder_dialog_success = await self.safe_click("span.upload-tips-path", "folder path selector")
            
            if not folder_dialog_success:
                logger.error("❌ Gagal membuka dialog pilih folder")
                return False
            
            await asyncio.sleep(3)
            
            # Step 2: Klik tombol "New Folder" (sesuai recording)
            new_folder_success = await self.safe_click("div.create-dir", "new folder button")
            
            if not new_folder_success:
                logger.error("❌ Gagal klik tombol New Folder")
                return False
            
            await asyncio.sleep(2)
            
            # Step 3: Klik dan isi nama folder (sesuai recording)
            folder_input = await self.page.wait_for_selector("div.share-save input", timeout=30000)
            if folder_input:
                await folder_input.click(click_count=3)
                await self.page.keyboard.press('Backspace')
                await folder_input.fill(folder_name)
                logger.info("✅ Folder name filled")
            else:
                logger.error("❌ Folder name input not found")
                return False
            
            await asyncio.sleep(2)
            
            # Step 4: Klik tombol centang untuk konfirmasi nama folder (sesuai recording)
            folder_confirm_success = await self.safe_click("i.folder-name-commit", "folder name confirm button")
            
            if not folder_confirm_success:
                logger.error("❌ Gagal klik tombol konfirmasi nama folder")
                return False
            
            await asyncio.sleep(2)
            
            # Step 5: Klik tombol "Confirm" untuk membuat folder (sesuai recording)
            create_confirm_success = await self.safe_click("div.create-confirm", "create folder confirm button")
            
            if not create_confirm_success:
                logger.error("❌ Gagal klik tombol confirm pembuatan folder")
                return False
            
            await asyncio.sleep(3)
            
            logger.info(f"✅ Folder '{folder_name}' berhasil dibuat di Terabox")
            return True
            
        except Exception as e:
            logger.error(f"💥 Error creating folder {folder_name}: {e}")
            try:
                await self.page.screenshot(path=f"/home/ubuntu/bot-tele/create_folder_error_{folder_name}.png")  # PATH BARU
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
            
            upload_clicked = await self.safe_click("div.share-main > div:nth-of-type(1) div:nth-of-type(1) > img", "upload button")
            
            if not upload_clicked:
                logger.error("❌ Gagal menemukan tombol upload")
                return []
            
            await asyncio.sleep(2)

            # Step 3: Cari elemen input file yang mendukung multiple
            logger.info("🔍 Mencari elemen input file...")
            
            file_input = await self.page.query_selector("input[type='file'][multiple]")
            if not file_input:
                file_input = await self.page.query_selector("input[type='file']")
            
            if not file_input:
                logger.error("❌ Tidak menemukan elemen input file")
                await self.page.screenshot(path="/home/ubuntu/bot-tele/upload_input_error.png")  # PATH BARU
                return []

            # Step 4: Upload semua file sekaligus dengan anti-duplikasi
            try:
                logger.info(f"📤 Mengupload {total_files} file sekaligus...")
                
                # Konversi Path objects ke string paths
                file_paths = [str(f.absolute()) for f in all_files]
                
                # Upload semua file sekaligus dengan safe upload
                if not await self.safe_upload_files(file_input, file_paths, "batch upload"):
                    return []
                
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
            generate_success = await self.safe_click('div.share-way span', "generate link button", 60000)
            
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
                await self.page.screenshot(path="/home/ubuntu/bot-tele/upload_all_files_error.png", full_page=True)  # PATH BARU
                logger.info("📸 Saved upload error screenshot")
            except:
                pass
            return []

    async def upload_files_individual(self, folder_path: Path) -> List[str]:
        """Upload files individually dengan anti-duplikasi"""
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
            
            successful_uploads = 0
            uploaded_in_this_session = set()
            
            for i, file_path in enumerate(media_files, 1):
                file_identifier = f"{file_path.name}_{file_path.stat().st_size}"
                
                # 🛡️ CEK DUPLIKASI: Skip jika file sudah diupload di session ini
                if file_identifier in uploaded_in_this_session:
                    logger.info(f"⏭️ Skipping duplicate file: {file_path.name}")
                    continue
                
                logger.info(f"📤 Uploading file {i}/{len(media_files)}: {file_path.name}")
                
                # Retry mechanism untuk setiap file
                upload_success = False
                for retry in range(MAX_UPLOAD_RETRIES):
                    try:
                        # Navigate to upload page setiap beberapa file untuk refresh
                        if i % 10 == 1 or retry > 0:
                            await self.page.goto('https://dm.1024tera.com/webmaster/new/share', wait_until='networkidle')
                            await asyncio.sleep(3)
                        
                        # Click upload button
                        if await self.safe_click('div.share-main > div:nth-of-type(1) div:nth-of-type(1) > img', "upload button"):
                            
                            # Find file input
                            file_input = await self.page.query_selector("div.share-main > div:nth-of-type(1) input:nth-of-type(1)")
                            if file_input:
                                await file_input.set_input_files(str(file_path.absolute()))
                                logger.info(f"✅ File sent: {file_path.name}")
                                
                                # Wait for upload completion
                                await asyncio.sleep(8)
                                
                                # Mark as uploaded
                                uploaded_in_this_session.add(file_identifier)
                                self.uploaded_files_tracker.add(file_identifier)
                                successful_uploads += 1
                                upload_success = True
                                logger.info(f"✅ Upload verified: {file_path.name}")
                                break  # Break retry loop jika sukses
                            else:
                                logger.warning(f"⚠️ File input not found, retry {retry + 1}")
                        else:
                            logger.warning(f"⚠️ Upload button not found, retry {retry + 1}")
                            
                    except Exception as e:
                        logger.warning(f"⚠️ Upload error for {file_path.name}, retry {retry + 1}: {e}")
                    
                    # Tunggu sebelum retry
                    await asyncio.sleep(3)
                
                if not upload_success:
                    logger.error(f"❌ Failed to upload {file_path.name} after {MAX_UPLOAD_RETRIES} retries")
                
                # Tunggu antara file uploads
                if i < len(media_files):
                    await asyncio.sleep(2)
            
            # Click generate link setelah semua file diupload
            if successful_uploads > 0:
                if await self.safe_click('div.share-way span', "generate link button", 60000):
                    await asyncio.sleep(15)
                    links = await self.extract_share_links()
                    logger.info(f"📊 Individual upload completed: {successful_uploads}/{total_files} files, {len(links)} links")
            
            return links
            
        except Exception as e:
            logger.error(f"💥 Individual files upload error: {e}")
            return []

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
                await self.page.screenshot(path="/home/ubuntu/bot-tele/upload_result.png")  # PATH BARU
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
            # Reset uploaded files tracker untuk session baru
            self.uploaded_files_tracker.clear()
            
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
            links = await self.upload_all_files(folder_path)
            
            if not links:
                # Fallback ke individual upload
                logger.warning("⚠️ Batch upload failed, trying individual upload...")
                links = await self.upload_files_individual(folder_path)
            
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
                await self.page.screenshot(path=f"/home/ubuntu/bot-tele/error_{int(time.time())}.png")  # PATH BARU
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
        
        logger.info("📤 UploadManager initialized dengan Playwright uploader + buat folder + anti-duplikasi")

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
                f"🎯 Method: Upload Semua File Sekaligus + Buat Folder\n"
                f"🛡️ Anti-Duplikasi: AKTIF"
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

            # Cek jika folder berisi file
            all_files = [f for f in folder_path.rglob('*') if f.is_file()]
            if not all_files:
                await self.send_progress_message(
                    update, context, job_id,
                    f"❌ Folder is empty, nothing to upload!\n"
                    f"📁 Path: {folder_path}\n"
                    f"🔍 Checking folder contents..."
                )
                
                # Debug: list semua isi folder
                try:
                    all_items = list(folder_path.rglob('*'))
                    folders = [item for item in all_items if item.is_dir()]
                    files = [item for item in all_items if item.is_file()]
                    
                    debug_info = f"📊 Folder contents:\n- Folders: {len(folders)}\n- Files: {len(files)}"
                    for item in all_items[:10]:  # Tampilkan 10 item pertama
                        debug_info += f"\n- {item.name} ({'dir' if item.is_dir() else 'file'})"
                    
                    if len(all_items) > 10:
                        debug_info += f"\n- ... and {len(all_items) - 10} more items"
                    
                    await self.send_progress_message(update, context, job_id, debug_info)
                except Exception as e:
                    logger.error(f"Error checking folder contents: {e}")
                
                return []

            await self.send_progress_message(
                update, context, job_id,
                f"✅ Folder ready for upload!\n"
                f"📁 Files found: {len(all_files)}\n"
                f"🔄 Starting Terabox automation..."
            )

            # Coba automation dengan Playwright + buat folder
            await self.send_progress_message(
                update, context, job_id,
                "🔄 Mencoba login dan upload otomatis...\n"
                "📝 Alur: Buat folder → Upload semua file sekaligus → Generate Link\n"
                "🛡️ Anti-Duplikasi: File tidak akan terupload double"
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
                        f"🎯 Method: Upload Semua File + Buat Folder Otomatis\n"
                        f"🛡️ Anti-Duplikasi: File terproteksi dari duplikat"
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
            
            # Generate download path (hanya untuk tracking, tidak digunakan untuk download sebenarnya)
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
            
            # Check if job was cancelled during download
            if job_id not in active_downloads or active_downloads[job_id].get('status') == DownloadStatus.CANCELLED.value:
                logger.info(f"🛑 Job {job_id} was cancelled during download")
                if job_id in active_downloads:
                    # Move to cancelled downloads
                    cancelled_downloads[job_id] = active_downloads[job_id]
                    cancelled_downloads[job_id]['end_time'] = datetime.now()
                    del active_downloads[job_id]
                return
            
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
            
            # Dapatkan path aktual dari download
            actual_download_path = None
            if 'actual_download_path' in active_downloads[job_id]:
                actual_download_path = Path(active_downloads[job_id]['actual_download_path'])
            else:
                # Fallback: cari folder yang berisi file
                actual_download_path = self.mega_manager.find_downloaded_folder(job_id)
            
            if not actual_download_path:
                active_downloads[job_id].update({
                    'status': DownloadStatus.ERROR.value,
                    'error': 'Download completed but no folder found',
                    'end_time': datetime.now()
                })
                
                await self.upload_manager.send_progress_message(
                    update, context, job_id,
                    f"❌ Download completed but no folder found!\n"
                    f"🆔 Job ID: {job_id}\n"
                    f"🔍 Please check download directory manually"
                )
                return
            
            # Update status to download completed dengan path aktual
            active_downloads[job_id].update({
                'status': DownloadStatus.DOWNLOAD_COMPLETED.value,
                'download_path': str(actual_download_path),
                'actual_download_path': str(actual_download_path)
            })
            
            await self.upload_manager.send_progress_message(
                update, context, job_id,
                f"✅ Download completed!\n"
                f"🆔 Job ID: {job_id}\n"
                f"📁 Path: {actual_download_path.name}\n"
                f"🔄 Starting file processing..."
            )
            
            # Auto-rename files if enabled in settings
            if user_settings.get('auto_rename', True):
                active_downloads[job_id]['status'] = DownloadStatus.RENAMING.value
                
                prefix = user_settings.get('prefix', 'file_')
                rename_result = self.file_manager.auto_rename_media_files(actual_download_path, prefix)
                
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
                        f"📁 Folder: {actual_download_path.name}\n"
                        f"🎯 Platform: {platform}"
                    )
                    
                    links = await self.upload_manager.upload_to_terabox(actual_download_path, update, context, job_id)
                    
                    if links:
                        active_downloads[job_id].update({
                            'status': DownloadStatus.COMPLETED.value,
                            'upload_links': links,
                            'end_time': datetime.now()
                        })
                        
                        # Auto-cleanup jika berhasil upload
                        if user_settings.get('auto_cleanup', True):
                            try:
                                shutil.rmtree(actual_download_path)
                                logger.info(f"🧹 Cleaned up download folder: {actual_download_path}")
                                await self.upload_manager.send_progress_message(
                                    update, context, job_id,
                                    f"🧹 Auto-cleanup completed!\n"
                                    f"📁 Folder removed: {actual_download_path.name}"
                                )
                            except Exception as e:
                                logger.warning(f"⚠️ Could not cleanup folder {actual_download_path}: {e}")
                    else:
                        active_downloads[job_id].update({
                            'status': DownloadStatus.ERROR.value,
                            'error': 'Upload failed',
                            'end_time': datetime.now()
                        })
                        
                        # Jangan hapus folder jika upload gagal
                        await self.upload_manager.send_progress_message(
                            update, context, job_id,
                            f"❌ Upload failed! Folder preserved for manual upload.\n"
                            f"📁 Path: {actual_download_path}"
                        )
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
                        f"📁 Path: {actual_download_path}\n"
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
                    f"📁 Path: {actual_download_path}\n"
                    f"💡 Auto-upload is disabled in settings"
                )
            
            # Move to completed downloads
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
        except Exception as e:
            logger.error(f"💥 Error in async download job: {e}")
            if job_id in active_downloads:
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
✏️ Rename folder manual
📁 Buat folder otomatis di Terabox
🛡️ ANTI-DUPLIKASI file upload
📁 Upload by folder name
🧹 Auto-cleanup setelah selesai
🛑 Stop proses yang berjalan

**Perintah yang tersedia:**
/download [url] - Download folder Mega.nz
/upload [nama_folder] - Upload folder yang sudah didownload
/rename <old_name> <new_name> - Rename folder hasil download
/listfolders - Lihat daftar folder yang sudah didownload
/status - Lihat status download
/stop [job_id] - Hentikan proses download/upload
/setprefix [nama] - Set prefix untuk rename
/setplatform [terabox] - Set platform upload
/autoupload [on/off] - Toggle auto upload
/autorename [on/off] - Toggle auto rename
/autocleanup [on/off] - Toggle auto cleanup
/mysettings - Lihat pengaturan Anda
/cleanup - Bersihkan folder download
/help - Tampilkan bantuan ini

**Fitur Baru:**
🎯 Upload by folder name: /upload nama_folder
🛡️ Anti-duplikasi: File tidak akan terupload double
📋 List folders: /listfolders untuk melihat folder tersedia
✏️ Rename folder: /rename old_name new_name
    """
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message when the command /help is issued."""
    help_text = """
📖 **Bantuan Mega Downloader Bot**

**Cara Penggunaan:**
1. **Download**: `/download [mega_folder_url]`
   Contoh: `/download https://mega.nz/folder/abc123`

2. **Upload by Folder Name**: `/upload [nama_folder]`
   Contoh: `/upload my_downloaded_folder`
   Gunakan `/listfolders` untuk melihat folder tersedia

3. **Rename Folder**: `/rename <nama_folder_lama> <nama_folder_baru>`
   Contoh: `/rename download_abc123 my_new_folder`

4. **List Folders**: `/listfolders`

5. **Cek Status**: `/status`

6. **Stop Proses**: `/stop [job_id]`
   Contoh: `/stop abc12345`

**Pengaturan:**
- `/setprefix [nama]` - Set nama prefix untuk file
- `/setplatform [terabox]` - Set platform upload  
- `/autoupload [on/off]` - Enable/disable auto upload
- `/autorename [on/off]` - Enable/disable auto rename
- `/autocleanup [on/off]` - Enable/disable auto cleanup
- `/mysettings` - Lihat pengaturan Anda

**Fitur Terabox:**
✅ Buat folder otomatis di Terabox
✅ Upload semua file sekaligus
✅ Generate multiple share links
✅ Session persistence untuk login
🛡️ ANTI-DUPLIKASI file upload

**Catatan:**
- Bot akan otomatis membuat folder di Terabox dengan nama yang sama
- File akan di-rename dengan format: `prefix 01.ext`
- Download maksimal 2 folder bersamaan
- Gunakan `/stop <job_id>` untuk menghentikan proses yang berjalan
- Fitur anti-duplikasi mencegah file terupload double
- Gunakan `/rename` untuk merename folder jika download gagal sebagian
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
            f"⏳ Active downloads: {len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}\n"
            f"🛑 Gunakan `/stop {job_id}` untuk membatalkan"
        )
        
    except Exception as e:
        logger.error(f"Error in download command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /upload command for manual upload by folder name."""
    try:
        if not context.args:
            # Show available folders
            folders = mega_manager.get_downloaded_folders()
            if not folders:
                await update.message.reply_text(
                    "❌ No downloaded folders found!\n"
                    "📥 Use /download first to download folders from Mega.nz"
                )
                return
            
            folder_list = "📁 **Available Folders:**\n\n"
            for i, folder in enumerate(folders[:10], 1):  # Show first 10 folders
                size_mb = folder['total_size'] / (1024 * 1024)
                folder_list += f"{i}. `{folder['name']}`\n"
                folder_list += f"   📄 {folder['file_count']} files | 💾 {size_mb:.1f} MB\n"
            
            if len(folders) > 10:
                folder_list += f"\n... and {len(folders) - 10} more folders"
            
            folder_list += "\n\n**Usage:** `/upload folder_name`"
            await update.message.reply_text(folder_list)
            return
        
        folder_name = context.args[0]
        
        # Find folder by name
        folder_path = mega_manager.find_folder_by_name(folder_name)
        
        if not folder_path:
            await update.message.reply_text(
                f"❌ Folder '{folder_name}' not found!\n"
                f"📋 Use /listfolders to see available folders"
            )
            return
        
        # Generate job ID
        job_id = str(uuid.uuid4())[:8]
        
        # Initialize upload info
        active_downloads[job_id] = {
            'job_id': job_id,
            'folder_path': str(folder_path),
            'folder_name': folder_path.name,
            'status': DownloadStatus.UPLOADING.value,
            'chat_id': update.effective_chat.id,
            'user_id': update.effective_user.id,
            'start_time': datetime.now(),
            'is_manual_upload': True
        }
        
        # Count files in folder
        all_files = [f for f in folder_path.rglob('*') if f.is_file()]
        file_count = len(all_files)
        
        await update.message.reply_text(
            f"✅ Folder found!\n"
            f"📁 Name: {folder_path.name}\n"
            f"📄 Files: {file_count}\n"
            f"🆔 Job ID: {job_id}\n"
            f"🔄 Starting upload to Terabox..."
        )
        
        # Start upload
        await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
        
        # Mark as completed after upload
        if job_id in active_downloads:
            active_downloads[job_id].update({
                'status': DownloadStatus.COMPLETED.value,
                'end_time': datetime.now()
            })
            completed_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
        
    except Exception as e:
        logger.error(f"Error in upload command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def list_folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /listfolders command to show downloaded folders."""
    try:
        folders = mega_manager.get_downloaded_folders()
        
        if not folders:
            await update.message.reply_text(
                "📭 No downloaded folders found!\n"
                "📥 Use /download to download folders from Mega.nz"
            )
            return
        
        folder_list = "📁 **Downloaded Folders:**\n\n"
        
        for i, folder in enumerate(folders[:15], 1):  # Show first 15 folders
            size_mb = folder['total_size'] / (1024 * 1024)
            created_time = datetime.fromtimestamp(folder['created_time']).strftime('%Y-%m-%d %H:%M')
            
            folder_list += f"**{i}. {folder['name']}**\n"
            folder_list += f"   📄 {folder['file_count']} files | 💾 {size_mb:.1f} MB\n"
            folder_list += f"   🕒 {created_time}\n"
            folder_list += f"   📤 Upload: `/upload {folder['name']}`\n"
            folder_list += f"   ✏️ Rename: `/rename {folder['name']} new_name`\n\n"
        
        if len(folders) > 15:
            folder_list += f"📊 ... and {len(folders) - 15} more folders\n\n"
        
        folder_list += "💡 **Usage:**\n- `/upload folder_name` untuk upload\n- `/rename old_name new_name` untuk rename"
        
        await update.message.reply_text(folder_list)
        
    except Exception as e:
        logger.error(f"Error in list_folders command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /rename command to rename downloaded folders."""
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Format perintah: /rename <nama_folder_lama> <nama_folder_baru>\n"
                "Contoh: /rename download_abc123 my_new_folder\n\n"
                "💡 Gunakan /listfolders untuk melihat folder yang tersedia"
            )
            return

        old_name = context.args[0]
        new_name = context.args[1]

        success, message = FileManager.rename_folder(old_name, new_name)
        
        if success:
            await update.message.reply_text(
                f"✅ {message}\n\n"
                f"📁 Folder berhasil direname!\n"
                f"📤 Sekarang bisa diupload dengan: /upload {new_name}"
            )
        else:
            await update.message.reply_text(f"❌ {message}")

    except Exception as e:
        logger.error(f"Error in rename command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /status command."""
    try:
        if not active_downloads and not completed_downloads and not cancelled_downloads:
            await update.message.reply_text("📊 No active, completed, or cancelled downloads")
            return
        
        status_text = "📊 **Download Status**\n\n"
        
        # Active downloads
        if active_downloads:
            status_text += "**🟢 Active Downloads:**\n"
            for job_id, info in list(active_downloads.items())[:5]:  # Show last 5
                status_text += f"• `{job_id}`: {info['status']}"
                if 'folder_url' in info:
                    status_text += f" - {info['folder_url'][:30]}..."
                elif 'folder_name' in info:
                    status_text += f" - {info['folder_name']}"
                status_text += f" - /stop_{job_id}\n"
        else:
            status_text += "**🔴 No active downloads**\n"
        
        # Queue info
        status_text += f"\n**📥 Queue:** {download_queue.qsize()} waiting\n"
        status_text += f"**⚡ Active:** {len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}\n"
        
        # Downloaded folders info
        folders = mega_manager.get_downloaded_folders()
        status_text += f"**📁 Downloaded Folders:** {len(folders)}\n"
        
        # Recent completed
        if completed_downloads:
            completed_count = len(completed_downloads)
            status_text += f"\n**✅ Completed:** {completed_count} jobs"
            if completed_count > 0:
                latest_job = list(completed_downloads.keys())[-1]
                status_text += f" (Latest: `{latest_job}`)"
        
        # Recent cancelled
        if cancelled_downloads:
            cancelled_count = len(cancelled_downloads)
            status_text += f"\n**🟡 Cancelled:** {cancelled_count} jobs"
        
        status_text += f"\n\n**🛑 Usage:** `/stop job_id` to stop a process"
        status_text += f"\n**📁 Usage:** `/listfolders` to see downloaded folders"
        status_text += f"\n**✏️ Usage:** `/rename old_name new_name` to rename folders"
        
        await update.message.reply_text(status_text)
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /stop command to cancel a running job."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a job ID\n"
                "Contoh: /stop abc12345\n"
                "Gunakan /status untuk melihat job ID yang aktif"
            )
            return
        
        job_id = context.args[0]
        
        # Check if job exists in active downloads
        if job_id not in active_downloads:
            await update.message.reply_text(
                f"❌ Job ID `{job_id}` tidak ditemukan dalam proses aktif!\n"
                f"Gunakan /status untuk melihat job yang sedang berjalan"
            )
            return
        
        job_info = active_downloads[job_id]
        current_status = job_info['status']
        
        # Cancel the job based on its current status
        if current_status in [DownloadStatus.DOWNLOADING.value, DownloadStatus.PENDING.value]:
            # Stop download process
            success = mega_manager.stop_download(job_id)
            
            if success or current_status == DownloadStatus.PENDING.value:
                # Remove from queue if pending
                if current_status == DownloadStatus.PENDING.value:
                    # Create a temporary queue to filter out the cancelled job
                    temp_queue = Queue()
                    while not download_queue.empty():
                        q_job_id, q_folder_url, q_update, q_context = download_queue.get()
                        if q_job_id != job_id:
                            temp_queue.put((q_job_id, q_folder_url, q_update, q_context))
                    
                    # Replace the original queue
                    while not temp_queue.empty():
                        download_queue.put(temp_queue.get())
                
                # Update status to cancelled
                active_downloads[job_id]['status'] = DownloadStatus.CANCELLED.value
                active_downloads[job_id]['end_time'] = datetime.now()
                
                # Move to cancelled downloads
                cancelled_downloads[job_id] = active_downloads[job_id]
                del active_downloads[job_id]
                
                await update.message.reply_text(
                    f"✅ Job `{job_id}` berhasil dihentikan!\n"
                    f"📛 Status: {current_status} → cancelled\n"
                    f"⏰ Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                
                # Send progress message if exists
                if job_id in user_progress_messages:
                    try:
                        await context.bot.send_message(
                            chat_id=job_info['chat_id'],
                            text=f"🛑 Job `{job_id}` telah dihentikan oleh user!"
                        )
                    except Exception as e:
                        logger.debug(f"Could not send cancellation message: {e}")
            else:
                await update.message.reply_text(
                    f"⚠️ Gagal menghentikan download untuk job `{job_id}`\n"
                    f"Proses mungkin sudah selesai atau sedang dalam tahap lain"
                )
        
        elif current_status == DownloadStatus.UPLOADING.value:
            # For uploads, we can't easily stop Playwright, so we mark as cancelled
            # and let it finish but skip further processing
            active_downloads[job_id]['status'] = DownloadStatus.CANCELLED.value
            active_downloads[job_id]['end_time'] = datetime.now()
            
            # Move to cancelled downloads
            cancelled_downloads[job_id] = active_downloads[job_id]
            del active_downloads[job_id]
            
            await update.message.reply_text(
                f"✅ Upload job `{job_id}` ditandai untuk dibatalkan!\n"
                f"📛 Proses upload akan berhenti setelah tahap saat ini selesai\n"
                f"⏰ Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        
        else:
            await update.message.reply_text(
                f"⚠️ Job `{job_id}` sedang dalam status `{current_status}`\n"
                f"Tidak dapat dihentikan pada tahap ini"
            )
        
    except Exception as e:
        logger.error(f"Error in stop command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def counter_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /counterstatus command."""
    try:
        status_text = "📊 **Counter Status**\n\n"
        status_text += f"**📥 Download Queue:** {download_queue.qsize()}\n"
        status_text += f"**⚡ Active Downloads:** {len(active_downloads)}\n"
        status_text += f"**✅ Completed Downloads:** {len(completed_downloads)}\n"
        status_text += f"**🟡 Cancelled Downloads:** {len(cancelled_downloads)}\n"
        status_text += f"**🔢 Next Job Number:** #{upload_manager._job_counter}\n"
        status_text += f"**👥 User Settings:** {len(settings_manager.settings)} users"
        
        # Downloaded folders count
        folders = mega_manager.get_downloaded_folders()
        status_text += f"\n**📁 Downloaded Folders:** {len(folders)}"
        
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
        
        # Downloaded folders
        folders = mega_manager.get_downloaded_folders()
        debug_text += f"**Downloaded Folders:** {len(folders)}\n"
        
        # Active processes
        debug_text += f"**Active Processes:** {len(mega_manager.active_processes)}\n"
        
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

async def auto_rename_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-rename feature."""
    try:
        if not context.args:
            # Show current status
            user_id = update.effective_user.id
            user_settings = settings_manager.get_user_settings(user_id)
            auto_rename = user_settings.get('auto_rename', True)
            
            status = "ON" if auto_rename else "OFF"
            await update.message.reply_text(
                f"✏️ Auto-rename status: {status}\n"
                f"Gunakan: /autorename on atau /autorename off"
            )
            return
        
        toggle = context.args[0].lower()
        
        if toggle not in ['on', 'off']:
            await update.message.reply_text(
                "❌ Invalid option. Use: /autorename on atau /autorename off"
            )
            return
        
        user_id = update.effective_user.id
        auto_rename = toggle == 'on'
        settings_manager.update_user_settings(user_id, {'auto_rename': auto_rename})
        
        status = "ON" if auto_rename else "OFF"
        await update.message.reply_text(f"✅ Auto-rename: {status}")
        
    except Exception as e:
        logger.error(f"Error in auto_rename_toggle: {e}")
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
        settings_text += f"**✏️ Auto-rename:** {'ON' if user_settings.get('auto_rename', True) else 'OFF'}\n"
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
logger.info("🔄 Initializing managers dengan path baru /home/ubuntu/bot-tele...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

def main():
    """Start the bot dengan path baru"""
    logger.info("🚀 Starting Mega Downloader Bot dengan path baru /home/ubuntu/bot-tele...")
    
    # Create base download directory dengan path baru
    DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
    logger.info(f"📁 Base download directory: {DOWNLOAD_BASE}")
    
    # Check current working directory
    cwd = os.getcwd()
    logger.info(f"📂 Current working directory: {cwd}")
    
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
    session_exists = os.path.exists('/home/ubuntu/bot-tele/terabox_session.json')
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
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("listfolders", list_folders_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("counterstatus", counter_status_command))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setplatform", set_platform))
    application.add_handler(CommandHandler("autoupload", auto_upload_toggle))
    application.add_handler(CommandHandler("autorename", auto_rename_toggle))
    application.add_handler(CommandHandler("autocleanup", auto_cleanup_toggle))
    application.add_handler(CommandHandler("mysettings", my_settings))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    
    # Start bot
    logger.info("✅ Bot started successfully dengan path baru /home/ubuntu/bot-tele!")
    application.run_polling()

if __name__ == '__main__':
    main()
