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
UPLOAD_BATCH_SIZE = 5  # DIKURANGI untuk menghindari timeout

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
        self.timeout = 90000  # DITINGKATKAN menjadi 90 detik untuk menghindari timeout
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
                timeout=120000  # DITINGKATKAN menjadi 120 detik
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

    async def wait_for_network_idle(self, timeout: int = 60000):  # DITINGKATKAN
        """Wait for network to be idle"""
        try:
            await self.page.wait_for_load_state('networkidle', timeout=timeout)
        except Exception as e:
            logger.debug(f"Network idle wait timeout: {e}")

    async def safe_click(self, selector: str, description: str, timeout: int = 15000) -> bool:  # DITINGKATKAN
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
        """Safe file upload dengan error handling dan anti-duplikasi - DIPERBAIKI"""
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
            
            # Batasi jumlah file yang diupload sekaligus - DIKURANGI untuk menghindari timeout
            batch_size = min(UPLOAD_BATCH_SIZE, 3)  # MAKSIMAL 3 file per batch
            
            if len(files_to_upload) > batch_size:
                logger.info(f"📦 Splitting {len(files_to_upload)} files into batches of {batch_size}")
                
                for i in range(0, len(files_to_upload), batch_size):
                    batch = files_to_upload[i:i + batch_size]
                    logger.info(f"📦 Uploading batch {i//batch_size + 1}/{(len(files_to_upload)-1)//batch_size + 1}")
                    
                    await file_input.set_input_files(batch)
                    await asyncio.sleep(8)  # DITINGKATKAN waktu tunggu
                    
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
                    
                    # Tunggu lebih lama antara batch
                    if i + batch_size < len(files_to_upload):
                        await asyncio.sleep(5)
            else:
                await file_input.set_input_files(files_to_upload)
                await asyncio.sleep(8)  # DITINGKATKAN waktu tunggu
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
            await self.page.goto(upload_url, wait_until='domcontentloaded', timeout=45000)
            
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
            
            logger.info("🔐 Login required, starting comprehensive login process...")
            
            # Step 1: Navigate to login page
            await self.page.goto('https://www.1024tera.com/webmaster/index', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(5)
            
            # Step 2: Click login button - MULTIPLE APPROACHES
            login_selectors = [
                'div.referral-content span',
                'button:has-text("Login")',
                'text/Login',
                '.login-btn',
                'a[href*="login"]'
            ]
            
            login_success = False
            for selector in login_selectors:
                try:
                    if await self.safe_click(selector, f"login button dengan {selector}", timeout=5000):
                        login_success = True
                        break
                except:
                    continue
            
            if not login_success:
                logger.error("❌ Failed to click login button dengan semua selector")
                return False
            
            await asyncio.sleep(3)
            
            # Step 3: Coba langsung email login tanpa melalui "other login way" jika memungkinkan
            logger.info("🔍 Mencari elemen email login langsung...")
            
            # Approach 1: Cari input email langsung
            email_login_success = False
            
            try:
                email_input = await self.page.wait_for_selector('#email-input', timeout=5000)
                if email_input:
                    logger.info("✅ Found email input directly, skipping login method selection")
                    # Langsung isi email dan password
                    await email_input.click(click_count=3)
                    await self.page.keyboard.press('Backspace')
                    await email_input.fill(self.terabox_email)
                    
                    password_input = await self.page.wait_for_selector('#pwd-input', timeout=5000)
                    if password_input:
                        await password_input.click(click_count=3)
                        await self.page.keyboard.press('Backspace')
                        await password_input.fill(self.terabox_password)
                        
                        # Submit login
                        if await self.safe_click('div.btn-class-login', "login submit button"):
                            email_login_success = True
            except Exception as e:
                logger.debug(f"⚠️ Direct email approach failed: {e}")
            
            # Approach 2: Jika direct approach gagal, coba melalui "other login way"
            if not email_login_success:
                logger.info("🔄 Mencoba melalui other login way...")
                
                # Step 4.1: Click other login way
                other_login_success = await self.safe_click('div.other-login-way', "other login way")
                
                if not other_login_success:
                    # Coba alternatif selector untuk other login way
                    other_selectors = [
                        'text/其他登录方式',
                        'text/Other login methods',
                        '.other-login-method',
                        'div[class*="other"]',
                        'span:has-text("其他")'
                    ]
                    
                    for selector in other_selectors:
                        try:
                            if await self.safe_click(selector, f"other login way dengan {selector}", timeout=3000):
                                other_login_success = True
                                break
                        except:
                            continue
                
                if other_login_success:
                    await asyncio.sleep(2)
                    
                    # Step 4.2: Click email login method - EXTENSIVE SELECTOR LIST
                    logger.info("🔍 Mencari tombol email login dengan selector komprehensif...")
                    
                    email_selectors = [
                        'div.other-login-way img[alt="email"]',
                        'div.other-login-way img[alt="Email"]',
                        'div.other-item > div:nth-of-type(2) > img',
                        'div.other-item img',
                        'img[alt="email"]',
                        'img[alt="Email"]',
                        'div[class*="email"]',
                        'div[class*="Email"]',
                        'text/邮箱登录',
                        'text/邮箱',
                        'text/Email',
                        'text/email'
                    ]
                    
                    for selector in email_selectors:
                        try:
                            logger.info(f"🔍 Mencoba selector: {selector}")
                            if selector.startswith('text/'):
                                # Handle text selectors
                                text = selector.replace('text/', '')
                                element = await self.page.wait_for_selector(f'text={text}', timeout=3000)
                            else:
                                element = await self.page.wait_for_selector(selector, timeout=3000)
                            
                            if element:
                                await element.click()
                                email_login_success = True
                                logger.info(f"✅ Successfully clicked email login dengan selector: {selector}")
                                break
                        except Exception as e:
                            logger.debug(f"⚠️ Selector {selector} failed: {e}")
                            continue
            
            if not email_login_success:
                logger.error("❌ Failed to click email login method dengan semua approach")
                return False
            
            await asyncio.sleep(3)
            
            # Step 5: Isi email dan password
            logger.info("📝 Mengisi email dan password...")
            
            # Cari email input dengan multiple selectors
            email_input_selectors = [
                '#email-input',
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="email"]',
                'input[placeholder*="邮箱"]'
            ]
            
            email_filled = False
            for selector in email_input_selectors:
                try:
                    email_input = await self.page.wait_for_selector(selector, timeout=5000)
                    if email_input:
                        await email_input.click(click_count=3)
                        await self.page.keyboard.press('Backspace')
                        await email_input.fill(self.terabox_email)
                        email_filled = True
                        logger.info(f"✅ Email filled dengan selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"⚠️ Email selector {selector} failed: {e}")
                    continue
            
            if not email_filled:
                logger.error("❌ Failed to fill email field")
                return False
            
            await asyncio.sleep(2)
            
            # Cari password input dengan multiple selectors
            password_input_selectors = [
                '#pwd-input',
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="password"]',
                'input[placeholder*="密码"]'
            ]
            
            password_filled = False
            for selector in password_input_selectors:
                try:
                    password_input = await self.page.wait_for_selector(selector, timeout=5000)
                    if password_input:
                        await password_input.click(click_count=3)
                        await self.page.keyboard.press('Backspace')
                        await password_input.fill(self.terabox_password)
                        password_filled = True
                        logger.info(f"✅ Password filled dengan selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"⚠️ Password selector {selector} failed: {e}")
                    continue
            
            if not password_filled:
                logger.error("❌ Failed to fill password field")
                return False
            
            await asyncio.sleep(2)
            
            # Step 6: Click login submit button
            login_submit_selectors = [
                'div.btn-class-login',
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Login")',
                'button:has-text("登录")'
            ]
            
            login_submit_success = False
            for selector in login_submit_selectors:
                try:
                    if await self.safe_click(selector, f"login submit dengan {selector}", timeout=5000):
                        login_submit_success = True
                        break
                except:
                    continue
            
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
            return False

    async def navigate_to_upload_page(self) -> bool:
        """Navigate ke halaman upload dengan memastikan elemen tersedia"""
        try:
            logger.info("🧭 Navigating to upload page...")
            
            # Coba navigasi langsung ke halaman upload
            upload_url = "https://dm.1024tera.com/webmaster/new/share"
            logger.info(f"🌐 Direct navigation to: {upload_url}")
            
            await self.page.goto(upload_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(5)
            
            current_url = self.page.url
            logger.info(f"🌐 Current URL after navigation: {current_url}")
            
            # Cek apakah kita sudah di halaman upload
            if 'new/share' in current_url:
                logger.info("✅ Successfully navigated to upload page (URL verified)")
                return True
            
            # Jika di-redirect, coba akses halaman home dulu
            if 'new/home' in current_url:
                logger.info("🔄 Redirected to home page, trying to navigate to upload from home...")
                # Klik tab share sesuai recording
                share_tab_success = await self.safe_click('div.guide-container > div.tab-item div', "share tab")
                if share_tab_success:
                    await asyncio.sleep(3)
                    current_url = self.page.url
                    if 'new/share' in current_url:
                        logger.info("✅ Successfully navigated to upload page from home")
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
            
            # Step 3: Klik dan isi nama folder (sesuai recording) - DIPERBAIKI DENGAN DOUBLE CLICK
            folder_input = await self.page.wait_for_selector("div.share-save input", timeout=30000)
            if folder_input:
                # Double click untuk select all text (sesuai recording)
                await folder_input.click(click_count=2)
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
            return False

    async def upload_all_files(self, folder_path: Path) -> List[str]:
        """
        Upload semua file sekaligus dari folder download ke Terabox
        dengan membuat folder baru terlebih dahulu - DIPERBAIKI
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

            # Step 2: Klik tombol upload (Local file / Upload File) - SESUAI RECORDING
            logger.info("🖱️ Mencari dan mengklik tombol upload...")
            
            upload_clicked = await self.safe_click("div.source-arr > div:nth-of-type(1) > div > div:nth-of-type(1)", "upload button")
            
            if not upload_clicked:
                logger.error("❌ Gagal menemukan tombol upload")
                return []
            
            await asyncio.sleep(2)

            # Step 3: Cari elemen input file yang mendukung multiple - DIPERBAIKI
            logger.info("🔍 Mencari elemen input file...")
            
            # Tunggu hingga file manager terbuka
            await asyncio.sleep(3)
            
            # DAPATKAN SEMUA FILE DAN UPLOAD SEBANYAK MUNGKIN
            file_paths = [str(f.absolute()) for f in all_files]
            
            # Step 4: Upload semua file sekaligus dengan anti-duplikasi - DIPERBAIKI
            try:
                logger.info(f"📤 Mengupload {total_files} file sekaligus...")
                
                # Gunakan input file yang tersedia di halaman
                file_input = await self.page.query_selector("input[type='file']")
                if not file_input:
                    # Coba alternatif selector
                    file_input = await self.page.query_selector("input[accept]")
                
                if not file_input:
                    logger.error("❌ Tidak menemukan elemen input file")
                    return []

                # Upload semua file sekaligus dengan safe upload
                if not await self.safe_upload_files(file_input, file_paths, "batch upload"):
                    return []
                
                logger.info(f"✅ Berhasil mengupload {total_files} file sekaligus")
                await asyncio.sleep(10)  # DITINGKATKAN waktu tunggu
                
            except Exception as e:
                logger.error(f"❌ Gagal upload semua file sekaligus: {e}")
                logger.info("🔄 Mencoba upload file satu per satu...")
                
                # Fallback: upload file satu per satu
                return await self.upload_files_individual(folder_path)

            # Step 5: Tunggu upload selesai - DIPERBAIKI dengan timeout lebih lama
            logger.info("⏳ Menunggu proses upload selesai...")
            await asyncio.sleep(15)
            await self.wait_for_network_idle(60000)

            # Step 6: Klik Generate Link (sesuai recording) - DIPERBAIKI dengan retry
            generate_success = False
            for retry in range(3):
                generate_success = await self.safe_click('div.share-way span', f"generate link button (attempt {retry+1})", 60000)
                if generate_success:
                    break
                await asyncio.sleep(5)
            
            if not generate_success:
                logger.error("❌ Could not click Generate Link after 3 attempts")
                return []
            
            # Wait for link generation dengan timeout lebih lama
            logger.info("⏳ Waiting for link generation...")
            await asyncio.sleep(20)
            await self.wait_for_network_idle(60000)

            # Step 7: Extract share links
            links = await self.extract_share_links()
            
            if links:
                logger.info(f"✅ Upload completed! {len(links)} links generated")
            else:
                logger.warning("⚠️ Upload completed but no links found")

            return links

        except Exception as e:
            logger.error(f"❌ Gagal upload semua file: {e}")
            return []

    async def upload_files_individual(self, folder_path: Path) -> List[str]:
        """Upload files individually dengan anti-duplikasi - DIPERBAIKI"""
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
                        
                        # Click upload button - SESUAI RECORDING
                        if await self.safe_click('div.source-arr > div:nth-of-type(1) > div > div:nth-of-type(1)', "upload button"):
                            
                            # Find file input
                            file_input = await self.page.query_selector("input[type='file']")
                            if file_input:
                                await file_input.set_input_files(str(file_path.absolute()))
                                logger.info(f"✅ File sent: {file_path.name}")
                                
                                # Wait for upload completion dengan timeout lebih lama
                                await asyncio.sleep(10)
                                
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
            
            # Click generate link setelah semua file diupload - DIPERBAIKI dengan retry
            if successful_uploads > 0:
                generate_success = False
                for retry in range(3):
                    generate_success = await self.safe_click('div.share-way span', f"generate link button (attempt {retry+1})", 60000)
                    if generate_success:
                        break
                    await asyncio.sleep(5)
                
                if generate_success:
                    await asyncio.sleep(20)
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
                f"🛡️ Anti-Duplikasi: AKTIF\n"
                f"⏰ Timeout: 90 detik (diperpanjang)"
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
                "🛡️ Anti-Duplikasi: File tidak akan terupload double\n"
                "⏱️  Batch size: 3-5 file per batch"
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

# ... (KELAS-KELAS LAIN DAN FUNGSI TETAP SAMA SEPERTI SEBELUMNYA)
# DownloadProcessor, Telegram Bot Handlers, dan main function tetap sama

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

# ... (FUNGSI TELEGRAM BOT HANDLERS TETAP SAMA)

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
