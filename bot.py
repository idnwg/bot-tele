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

    async def upload_all_files_to_terabox(self, download_folder: Path, logger: logging.Logger) -> List[str]:
        """
        Upload semua file sekaligus dari folder download ke Terabox
        dengan membuat folder baru berdasarkan nama folder download
        """
        try:
            folder_name = download_folder.name
            logger.info(f"🚀 MEMULAI UPLOAD - Folder: {folder_name}")
            
            # Step 1: Dapatkan semua file dari folder
            all_files = [f for f in download_folder.rglob('*') if f.is_file()]
            total_files = len(all_files)
            
            logger.info(f"📁 MENEMUKAN FILE - {total_files} file di {download_folder}")
            
            if total_files == 0:
                logger.error("❌ TIDAK ADA FILE - Folder kosong, tidak ada yang bisa diupload")
                return []

            # Step 2: Setup browser dan login
            logger.info("🔄 SETUP BROWSER - Memulai browser Playwright...")
            if not await self.setup_browser(use_session=True):
                logger.error("❌ SETUP BROWSER GAGAL - Tidak bisa melanjutkan upload")
                return []

            # Step 3: Login ke Terabox
            logger.info("🔐 LOGIN - Memproses login Terabox...")
            if not await self.login_to_terabox():
                logger.error("❌ LOGIN GAGAL - Tidak bisa melanjutkan upload")
                return []

            # Step 4: Navigasi ke halaman upload
            logger.info("🧭 NAVIGASI - Menuju halaman upload...")
            if not await self.navigate_to_upload_page():
                logger.error("❌ NAVIGASI GAGAL - Tidak bisa mengakses halaman upload")
                return []

            # Step 5: Buat folder baru di Terabox
            logger.info(f"📁 MEMBUAT FOLDER - Membuat folder '{folder_name}' di Terabox...")
            if not await self.create_new_folder(folder_name):
                logger.warning("⚠️ BUAT FOLDER GAGAL - Melanjutkan upload ke root folder")

            # Step 6: Klik tombol upload file
            logger.info("🖱️ KLIK UPLOAD - Mencari dan mengklik tombol upload...")
            upload_clicked = await self.click_upload_button()
            
            if not upload_clicked:
                logger.error("❌ KLIK UPLOAD GAGAL - Tidak bisa menemukan tombol upload")
                return []

            await asyncio.sleep(2)

            # Step 7: Cari elemen input file
            logger.info("🔍 MENCARI INPUT FILE - Mencari elemen input file...")
            file_input, is_multiple = await self.find_file_input()
            
            if not file_input:
                logger.error("❌ INPUT FILE TIDAK DITEMUKAN - Tidak menemukan elemen input file")
                await self.page.screenshot(path="upload_input_error.png")
                return []

            # Step 8: Upload semua file sekaligus
            logger.info(f"📤 UPLOAD FILE - Mengupload {total_files} file sekaligus...")
            upload_success = await self.upload_files_batch(file_input, all_files, is_multiple, logger)
            
            if not upload_success:
                logger.error("❌ UPLOAD BATCH GAGAL - Fallback ke upload individual")
                return await self.upload_files_individual_fallback(download_folder, logger)

            # Step 9: Tunggu upload selesai
            logger.info("⏳ MENUNGGU UPLOAD - Menunggu proses upload selesai...")
            await asyncio.sleep(10)
            await self.wait_for_network_idle()

            # Step 10: Klik Generate Link
            logger.info("🔗 GENERATE LINK - Mengklik tombol Generate Link...")
            generate_success = await self.click_generate_link()
            
            if not generate_success:
                logger.error("❌ GENERATE LINK GAGAL - Tidak bisa mengklik tombol Generate Link")
                return []

            # Step 11: Tunggu generate link selesai
            logger.info("⏳ MENUNGGU LINK - Menunggu proses generate link selesai...")
            await asyncio.sleep(15)
            await self.wait_for_network_idle()

            # Step 12: Extract share links
            logger.info("🔗 EKSTRAK LINK - Mengekstrak link sharing...")
            links = await self.extract_share_links()
            
            if links:
                logger.info(f"✅ UPLOAD SELESAI - {len(links)} link berhasil dihasilkan")
                for i, link in enumerate(links, 1):
                    logger.info(f"🔗 Link {i}: {link}")
            else:
                logger.warning("⚠️ UPLOAD SELESAI TAPI TIDAK ADA LINK - Upload selesai tetapi tidak ada link yang ditemukan")

            # Step 13: Simpan session
            await self.save_session()
            logger.info("💾 SESSION DISIMPAN - Session berhasil disimpan")

            return links

        except Exception as e:
            logger.error(f"💥 UPLOAD ERROR - Error selama proses upload: {e}")
            try:
                await self.page.screenshot(path="upload_fatal_error.png", full_page=True)
                logger.info("📸 SCREENSHOT ERROR - Screenshot error disimpan")
            except Exception as screenshot_error:
                logger.error(f"❌ SCREENSHOT GAGAL - Tidak bisa menyimpan screenshot: {screenshot_error}")
            return []
        finally:
            await self.cleanup_browser()
            logger.info("🧹 BROWSER DIBERSIHKAN - Browser ditutup dengan aman")

    async def click_upload_button(self) -> bool:
        """Klik tombol upload file dengan berbagai selector"""
        upload_button_selectors = [
            "div.source-arr > div:nth-of-type(1) div:nth-of-type(2)",
            "div.share-main > div:nth-of-type(1) div:nth-of-type(1) > img",
            "text=Upload File",
            "text=Local File",
            "div.local-item",
        ]
        
        for selector in upload_button_selectors:
            try:
                await self.page.click(selector, timeout=10000)
                logger.info(f"✅ Tombol upload diklik dengan selector: {selector}")
                return True
            except Exception as e:
                logger.debug(f"❌ Selector {selector} gagal: {e}")
                continue
        
        return False

    async def find_file_input(self) -> Tuple[Optional[any], bool]:
        """Cari elemen input file dan cek apakah mendukung multiple"""
        input_selectors = [
            "input[type='file'][multiple]",
            "input[type='file']",
            "input[webkitdirectory]",
            "input[directory]",
            "input#fileElem",
            "div.source-arr input",
            "input[accept]",
            "input[name='file']"
        ]
        
        for selector in input_selectors:
            try:
                file_input = await self.page.query_selector(selector)
                if file_input:
                    is_multiple = await file_input.get_attribute("multiple")
                    logger.info(f"✅ Input file ditemukan: {selector}, multiple: {bool(is_multiple)}")
                    return file_input, bool(is_multiple)
            except Exception as e:
                logger.debug(f"❌ Selector {selector} gagal: {e}")
                continue
        
        return None, False

    async def upload_files_batch(self, file_input: any, all_files: List[Path], is_multiple: bool, logger: logging.Logger) -> bool:
        """Upload semua file sekaligus atau satu per satu berdasarkan kemampuan input"""
        try:
            file_paths = [str(f.absolute()) for f in all_files]
            
            if is_multiple and len(file_paths) > 0:
                # Upload semua file sekaligus
                await file_input.set_input_files(file_paths)
                logger.info(f"✅ UPLOAD BATCH BERHASIL - {len(file_paths)} file diupload sekaligus")
                return True
            else:
                # Fallback: upload satu per satu
                logger.info("🔄 MULTIPLE UPLOAD TIDAK DIDUKUNG - Fallback ke upload individual")
                for i, file_path in enumerate(file_paths, 1):
                    try:
                        await file_input.set_input_files([file_path])
                        logger.info(f"✅ Upload file {i}/{len(file_paths)}: {Path(file_path).name}")
                        if i < len(file_paths):
                            await asyncio.sleep(2)  # Jeda antara upload
                    except Exception as e:
                        logger.error(f"❌ Gagal upload file {i}: {e}")
                        continue
                return True
                
        except Exception as e:
            logger.error(f"❌ UPLOAD BATCH ERROR: {e}")
            return False

    async def upload_files_individual_fallback(self, download_folder: Path, logger: logging.Logger) -> List[str]:
        """Fallback method untuk upload file satu per satu"""
        try:
            logger.info("🔄 FALLBACK KE UPLOAD INDIVIDUAL")
            
            all_files = [f for f in download_folder.rglob('*') if f.is_file()]
            links = []
            successful_uploads = 0
            
            for i, file_path in enumerate(all_files, 1):
                logger.info(f"📤 Upload individual {i}/{len(all_files)}: {file_path.name}")
                
                # Klik upload button untuk setiap file
                if not await self.click_upload_button():
                    logger.error(f"❌ Gagal klik upload button untuk file {i}")
                    continue
                
                await asyncio.sleep(2)
                
                # Cari input file
                file_input, _ = await self.find_file_input()
                if not file_input:
                    logger.error(f"❌ Input file tidak ditemukan untuk file {i}")
                    continue
                
                # Upload file
                try:
                    await file_input.set_input_files([str(file_path.absolute())])
                    successful_uploads += 1
                    logger.info(f"✅ Berhasil upload file {i}")
                    
                    # Tunggu antara upload
                    if i < len(all_files):
                        await asyncio.sleep(3)
                        
                except Exception as e:
                    logger.error(f"❌ Gagal upload file {i}: {e}")
                    continue
            
            if successful_uploads > 0:
                # Klik Generate Link setelah semua file diupload
                await self.click_generate_link()
                await asyncio.sleep(15)
                links = await self.extract_share_links()
                logger.info(f"📊 UPLOAD INDIVIDUAL SELESAI: {successful_uploads}/{len(all_files)} file, {len(links)} link")
            
            return links
            
        except Exception as e:
            logger.error(f"💥 UPLOAD INDIVIDUAL ERROR: {e}")
            return []

    async def click_generate_link(self) -> bool:
        """Klik tombol Generate Link"""
        generate_selectors = [
            'div.share-way span',
            '//*[@id="app"]/div[1]/div[2]/div[2]/div/div[2]/div/div[1]/div[3]/div[1]/div[2]/div[2]/span',
            'text=Generate Link',
            'button:has-text("Generate Link")',
            '.generate-link-btn'
        ]
        
        for selector in generate_selectors:
            try:
                if selector.startswith('text='):
                    await self.page.click(selector, timeout=60000)
                else:
                    element = await self.page.wait_for_selector(selector, timeout=60000)
                    await element.click()
                logger.info(f"✅ Tombol Generate Link diklik dengan selector: {selector}")
                return True
            except Exception as e:
                logger.debug(f"❌ Selector Generate Link {selector} gagal: {e}")
                continue
        
        return False

    def get_current_domain(self, url: str) -> str:
        """Extract domain from URL"""
        try:
            domain = url.split('/')[2]
            logger.info(f"🌐 Domain terdeteksi: {domain}")
            return domain
        except Exception as e:
            logger.warning(f"⚠️ Tidak bisa extract domain dari {url}: {e}")
            return "dm.1024tera.com"

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
                    elif selector.startswith('text='):
                        # Text-based selector
                        text = selector.replace('text=', '')
                        element = await self.page.wait_for_selector(f"text={text}", timeout=timeout)
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
                        await element.click(delay=100)
                        
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
                    elif selector.startswith('text='):
                        text_sel = selector.replace('text=', '')
                        element = await self.page.wait_for_selector(f"text={text_sel}", timeout=timeout)
                    else:
                        element = await self.page.wait_for_selector(selector, timeout=timeout)
                    
                    if element:
                        logger.info(f"✅ Found {description} dengan selector: {selector}")
                        
                        # Scroll element into view
                        await element.scroll_into_view_if_needed()
                        
                        # Wait for element to be stable
                        await asyncio.sleep(1)
                        
                        # Clear dan fill field
                        await element.click(click_count=3)
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
                'text=Log in'
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
                "text=Path"
            ], "folder path selector")
            
            if not folder_dialog_success:
                logger.error("❌ Gagal membuka dialog pilih folder")
                return False
            
            await asyncio.sleep(3)
            
            # Step 2: Klik tombol "New Folder" (sesuai recording)
            new_folder_success = await self.find_and_click_element([
                "div.create-dir",
                "//html/body/div[8]/div/div[2]/div[3]/div[1]",
                "text=New Folder"
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

class UploadManager:
    def __init__(self):
        self.terabox_key = os.getenv('TERABOX_CONNECT_KEY')
        self.doodstream_key = os.getenv('DOODSTREAM_API_KEY')
        self.terabox_playwright_uploader = TeraboxPlaywrightUploader()
        self.terabox_lock = threading.Lock()
        
        # Counter global untuk urutan job upload
        self._job_counter = 1
        self._counter_lock = threading.Lock()
        
        logger.info("📤 UploadManager initialized dengan Playwright uploader + upload semua file")

    async def upload_to_terabox(self, folder_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str):
        """Upload files to Terabox menggunakan Playwright automation dengan upload semua file sekaligus"""
        logger.info(f"🚀 Starting Terabox upload dengan upload semua file untuk job {job_id}, folder: {folder_path}")
        
        try:
            # Dapatkan nomor job
            with self._counter_lock:
                job_number = self._job_counter
                self._job_counter += 1

            logger.info(f"🔢 Job number: {job_number}")
            
            await self.send_progress_message(
                update, context, job_id, 
                f"📤 MEMULAI UPLOAD KE TERABOX\n"
                f"🔢 Job Number: #{job_number}\n"
                f"📁 Folder: {folder_path.name}\n"
                f"🎯 Method: UPLOAD SEMUA FILE SEKALIGUS"
            )

            # Cek jika credential Terabox tersedia
            if not self.terabox_playwright_uploader.terabox_email or not self.terabox_playwright_uploader.terabox_password:
                await self.send_progress_message(
                    update, context, job_id,
                    "❌ TERABOX CREDENTIALS TIDAK DITEMUKAN!\n"
                    "📋 Silakan set environment variables:\n"
                    "- TERABOX_EMAIL\n" 
                    "- TERABOX_PASSWORD"
                )
                return []

            # Upload menggunakan metode baru - upload semua file sekaligus
            await self.send_progress_message(
                update, context, job_id,
                "🔄 PROSES UPLOAD OTOMATIS\n"
                "📝 Alur: Login → Buat folder → Upload semua file → Generate Link"
            )
            
            with self.terabox_lock:
                logger.info("🔒 Acquired Terabox upload lock")
                
                # Panggil fungsi upload semua file sekaligus
                links = await self.terabox_playwright_uploader.upload_all_files_to_terabox(folder_path, logger)
                
                if links:
                    success_msg = (
                        f"✅ UPLOAD KE TERABOX BERHASIL!\n"
                        f"🔢 Job Number: #{job_number}\n"
                        f"🔗 {len(links)} links generated\n"
                        f"📁 Folder: {folder_path.name}\n"
                        f"🎯 Method: UPLOAD SEMUA FILE SEKALIGUS"
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
                    error_msg = (
                        f"❌ UPLOAD OTOMATIS GAGAL\n"
                        f"🔢 Job Number: #{job_number}\n"
                        f"📁 Folder: {folder_path.name}\n"
                        f"💡 Silakan coba manual upload"
                    )
                    await self.send_progress_message(update, context, job_id, error_msg)
                    return []
                    
        except Exception as e:
            logger.error(f"💥 Terabox upload error untuk {job_id}: {e}")
            error_msg = f"❌ UPLOAD ERROR: {str(e)}"
            await self.send_progress_message(update, context, job_id, error_msg)
            return []

    async def send_progress_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: str, message: str):
        """Send progress message to user"""
        try:
            if job_id in active_downloads:
                chat_id = active_downloads[job_id]['chat_id']
                
                # Update existing message or send new one
                if job_id in user_progress_messages:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=user_progress_messages[job_id],
                            text=message
                        )
                        return
                    except Exception as e:
                        logger.debug(f"Could not edit message: {e}")
                
                # Send new message
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=message
                )
                user_progress_messages[job_id] = sent_message.message_id
                
        except Exception as e:
            logger.error(f"Error sending progress message: {e}")

class DownloadProcessor:
    def __init__(self, mega_manager: MegaManager, file_manager: FileManager, upload_manager: UploadManager, settings_manager: UserSettingsManager):
        self.mega_manager = mega_manager
        self.file_manager = file_manager
        self.upload_manager = upload_manager
        self.settings_manager = settings_manager
        self.processing = False
        logger.info("📥 DownloadProcessor initialized")

    def start_processing(self):
        """Start processing download queue"""
        if not self.processing:
            self.processing = True
            threading.Thread(target=self._process_queue, daemon=True).start()
            logger.info("🔄 Download queue processor started")

    def _process_queue(self):
        """Process download queue in separate thread"""
        while self.processing:
            try:
                if not download_queue.empty() and len(active_downloads) < MAX_CONCURRENT_DOWNLOADS:
                    job_id, folder_url, user_id, chat_id = download_queue.get()
                    
                    # Start download in separate thread
                    threading.Thread(
                        target=self._process_download,
                        args=(job_id, folder_url, user_id, chat_id),
                        daemon=True
                    ).start()
                    
                time.sleep(1)
            except Exception as e:
                logger.error(f"Queue processing error: {e}")
                time.sleep(5)

    def _process_download(self, job_id: str, folder_url: str, user_id: int, chat_id: int):
        """Process individual download"""
        try:
            # Get user settings
            user_settings = self.settings_manager.get_user_settings(user_id)
            
            # Update active download status
            active_downloads[job_id] = {
                'status': DownloadStatus.DOWNLOADING,
                'progress': 'Starting download...',
                'start_time': datetime.now(),
                'user_id': user_id,
                'chat_id': chat_id,
                'folder_url': folder_url,
                'user_settings': user_settings
            }
            
            # Generate download path
            download_path = DOWNLOAD_BASE / f"download_{job_id}"
            
            # Download from Mega.nz
            success, message = self.mega_manager.download_mega_folder(folder_url, download_path, job_id)
            
            if success:
                active_downloads[job_id]['status'] = DownloadStatus.DOWNLOAD_COMPLETED
                active_downloads[job_id]['progress'] = 'Download completed, starting upload...'
                
                # Auto rename if enabled
                if user_settings.get('auto_rename', True):
                    active_downloads[job_id]['status'] = DownloadStatus.RENAMING
                    prefix = user_settings.get('prefix', 'file_')
                    rename_result = self.file_manager.auto_rename_media_files(download_path, prefix)
                    active_downloads[job_id]['progress'] = f'Renamed {rename_result["renamed"]}/{rename_result["total"]} files'
                
                # Auto upload if enabled
                if user_settings.get('auto_upload', True):
                    active_downloads[job_id]['status'] = DownloadStatus.UPLOADING
                    
                    # Run upload in asyncio thread
                    asyncio.run(self._run_upload(job_id, download_path, user_id, chat_id))
                else:
                    active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
                    active_downloads[job_id]['progress'] = 'Download completed (upload disabled)'
                    
            else:
                active_downloads[job_id]['status'] = DownloadStatus.ERROR
                active_downloads[job_id]['progress'] = f'Download failed: {message}'
                
        except Exception as e:
            logger.error(f"Download processing error for {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['progress'] = f'Processing error: {str(e)}'

    async def _run_upload(self, job_id: str, download_path: Path, user_id: int, chat_id: int):
        """Run upload in asyncio context"""
        try:
            # Create mock update and context for upload
            class MockUpdate:
                def __init__(self, chat_id):
                    self.effective_chat = type('Chat', (), {'id': chat_id})()
            
            class MockContext:
                def __init__(self, chat_id):
                    self.bot = type('Bot', (), {'send_message': self.send_message})()
                    self.chat_id = chat_id
                
                async def send_message(self, chat_id, text):
                    # This is a mock, real implementation would use actual bot
                    logger.info(f"Mock message to {chat_id}: {text}")
            
            update = MockUpdate(chat_id)
            context = MockContext(chat_id)
            
            # Perform upload
            await self.upload_manager.upload_to_terabox(download_path, update, context, job_id)
            
            # Update status
            active_downloads[job_id]['status'] = DownloadStatus.COMPLETED
            active_downloads[job_id]['progress'] = 'Upload completed'
            
            # Auto cleanup if enabled
            user_settings = self.settings_manager.get_user_settings(user_id)
            if user_settings.get('auto_cleanup', True):
                try:
                    shutil.rmtree(download_path)
                    logger.info(f"Cleaned up download folder: {download_path}")
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
                    
        except Exception as e:
            logger.error(f"Upload processing error for {job_id}: {e}")
            active_downloads[job_id]['status'] = DownloadStatus.ERROR
            active_downloads[job_id]['progress'] = f'Upload error: {str(e)}'

# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when the command /start is issued."""
    user = update.effective_user
    welcome_text = f"""
🤖 **Mega Downloader Bot dengan Upload Semua File**

Halo {user.mention_html()}!

Saya dapat membantu Anda:
📥 Download folder dari Mega.nz
📤 Upload otomatis ke Terabox
🔄 Rename file secara otomatis

**FITUR BARU**: Upload semua file sekaligus ke Terabox!

**Perintah yang tersedia:**
/download <url> - Download folder Mega.nz
/upload <path> - Upload folder ke Terabox  
/status - Lihat status download
/setprefix <prefix> - Set prefix untuk rename
/autoupload - Toggle auto upload
/mysettings - Lihat pengaturan

**Contoh:**
/download https://mega.nz/folder/abc123
    """
    await update.message.reply_html(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message when the command /help is issued."""
    help_text = """
📖 **Bantuan Mega Downloader Bot**

**Perintah:**
/start - Memulai bot
/download <url> - Download folder dari Mega.nz
/upload <path> - Upload folder ke Terabox
/status - Status download aktif
/counterstatus - Status antrian
/debug - Informasi debug
/setprefix <prefix> - Set prefix rename file
/setplatform <terabox|doodstream> - Set platform upload
/autoupload <on|off> -Toggle auto upload
/autocleanup <on|off> - Toggle auto cleanup
/mysettings - Lihat pengaturan Anda
/cleanup - Bersihkan folder download

**Fitur Upload Baru:**
✅ Upload semua file sekaligus
✅ Buat folder otomatis di Terabox
✅ Generate link otomatis
✅ Fallback ke upload individual

**Contoh:**
/download https://mega.nz/folder/abc123
/setprefix vacation_
/autoupload on
    """
    await update.message.reply_text(help_text)

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /download command."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap berikan URL Mega.nz folder\n"
                "Contoh: /download https://mega.nz/folder/abc123"
            )
            return

        folder_url = context.args[0]
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Validate Mega.nz URL
        if not folder_url.startswith('https://mega.nz/'):
            await update.message.reply_text(
                "❌ URL Mega.nz tidak valid!\n"
                "Format yang benar: https://mega.nz/folder/..."
            )
            return

        # Generate job ID
        job_id = str(uuid.uuid4())[:8]
        
        # Add to download queue
        download_queue.put((job_id, folder_url, user_id, chat_id))
        
        # Get user settings
        user_settings = settings_manager.get_user_settings(user_id)
        
        response_text = (
            f"✅ **Download Ditambahkan ke Antrian**\n"
            f"🆔 Job ID: `{job_id}`\n"
            f"📁 URL: {folder_url}\n"
            f"👤 User: {update.effective_user.first_name}\n"
            f"⚙️ Auto Upload: {'✅' if user_settings.get('auto_upload', True) else '❌'}\n"
            f"🔄 Auto Rename: {'✅' if user_settings.get('auto_rename', True) else '❌'}\n"
            f"🧹 Auto Cleanup: {'✅' if user_settings.get('auto_cleanup', True) else '❌'}\n"
            f"📊 Posisi Antrian: {download_queue.qsize()}\n"
            f"\n**Fitur Baru**: Upload semua file sekaligus ke Terabox!"
        )
        
        await update.message.reply_text(response_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Download command error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /upload command for manual upload"""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap berikan path folder\n"
                "Contoh: /upload downloads/my_folder"
            )
            return

        folder_path = Path(context.args[0])
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not folder_path.exists():
            await update.message.reply_text(
                f"❌ Folder tidak ditemukan: {folder_path}"
            )
            return

        # Generate job ID
        job_id = f"upload_{str(uuid.uuid4())[:8]}"
        
        # Start upload
        await upload_manager.upload_to_terabox(folder_path, update, context, job_id)
        
    except Exception as e:
        logger.error(f"Upload command error: {e}")
        await update.message.reply_text(f"❌ Upload error: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    try:
        if not active_downloads:
            await update.message.reply_text("📊 Tidak ada download aktif")
            return

        status_text = "📊 **Status Download Aktif:**\n\n"
        
        for job_id, info in active_downloads.items():
            status_emoji = {
                DownloadStatus.DOWNLOADING: "⏬",
                DownloadStatus.DOWNLOAD_COMPLETED: "✅", 
                DownloadStatus.RENAMING: "📝",
                DownloadStatus.UPLOADING: "⏫",
                DownloadStatus.COMPLETED: "🎉",
                DownloadStatus.ERROR: "❌"
            }.get(info['status'], '❓')
            
            status_text += (
                f"{status_emoji} **Job {job_id}**\n"
                f"Status: {info['status'].value}\n"
                f"Progress: {info['progress']}\n"
                f"Start: {info['start_time'].strftime('%H:%M:%S')}\n"
                f"Platform: {info.get('user_settings', {}).get('platform', 'terabox')}\n"
                f"\n"
            )

        await update.message.reply_text(status_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Status command error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def counter_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /counterstatus command."""
    try:
        status_text = (
            f"📊 **Status Sistem:**\n"
            f"🔄 Download aktif: {len(active_downloads)}\n"
            f"⏳ Dalam antrian: {download_queue.qsize()}\n"
            f"✅ Selesai: {len(completed_downloads)}\n"
            f"🚀 Max concurrent: {MAX_CONCURRENT_DOWNLOADS}\n"
            f"📁 Base directory: {DOWNLOAD_BASE}\n"
            f"🔧 Mega-get: {'✅' if mega_manager.check_mega_get() else '❌'}\n"
            f"👤 Total users: {len(settings_manager.settings)}"
        )
        
        await update.message.reply_text(status_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Counter status error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /debug command."""
    try:
        debug_info = mega_manager.debug_mega_session()
        
        debug_text = "🐛 **Debug Information:**\n\n"
        
        for key, value in debug_info.items():
            if key == 'disk_space':
                debug_text += f"💾 **Disk Space:**\n```{value}```\n"
            elif key == 'error':
                debug_text += f"❌ **Error:** {value}\n"
            else:
                debug_text += f"**{key.replace('_', ' ').title()}:** {value}\n"

        await update.message.reply_text(debug_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Debug command error: {e}")
        await update.message.reply_text(f"❌ Debug error: {str(e)}")

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setprefix command."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap berikan prefix\n"
                "Contoh: /setprefix vacation_"
            )
            return

        prefix = context.args[0]
        user_id = update.effective_user.id
        
        settings_manager.update_user_settings(user_id, {'prefix': prefix})
        
        await update.message.reply_text(
            f"✅ Prefix diubah menjadi: `{prefix}`\n"
            f"File akan dinamai: `{prefix} 01.jpg`, `{prefix} 02.mp4`, dll."
        )
        
    except Exception as e:
        logger.error(f"Set prefix error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def set_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setplatform command."""
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Harap berikan platform\n"
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
        
        await update.message.reply_text(f"✅ Platform diubah menjadi: {platform}")
        
    except Exception as e:
        logger.error(f"Set platform error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_upload_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /autoupload command."""
    try:
        user_id = update.effective_user.id
        current_settings = settings_manager.get_user_settings(user_id)
        current_auto_upload = current_settings.get('auto_upload', True)
        
        if context.args:
            arg = context.args[0].lower()
            if arg in ['on', 'true', '1', 'enable']:
                new_setting = True
            elif arg in ['off', 'false', '0', 'disable']:
                new_setting = False
            else:
                await update.message.reply_text(
                    "❌ Argument tidak valid!\n"
                    "Gunakan: /autoupload on atau /autoupload off"
                )
                return
        else:
            # Toggle current setting
            new_setting = not current_auto_upload
        
        settings_manager.update_user_settings(user_id, {'auto_upload': new_setting})
        
        status = "AKTIF ✅" if new_setting else "NONAKTIF ❌"
        await update.message.reply_text(f"✅ Auto Upload: {status}")
        
    except Exception as e:
        logger.error(f"Auto upload toggle error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def auto_cleanup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /autocleanup command."""
    try:
        user_id = update.effective_user.id
        current_settings = settings_manager.get_user_settings(user_id)
        current_auto_cleanup = current_settings.get('auto_cleanup', True)
        
        if context.args:
            arg = context.args[0].lower()
            if arg in ['on', 'true', '1', 'enable']:
                new_setting = True
            elif arg in ['off', 'false', '0', 'disable']:
                new_setting = False
            else:
                await update.message.reply_text(
                    "❌ Argument tidak valid!\n"
                    "Gunakan: /autocleanup on atau /autocleanup off"
                )
                return
        else:
            # Toggle current setting
            new_setting = not current_auto_cleanup
        
        settings_manager.update_user_settings(user_id, {'auto_cleanup': new_setting})
        
        status = "AKTIF ✅" if new_setting else "NONAKTIF ❌"
        await update.message.reply_text(f"✅ Auto Cleanup: {status}")
        
    except Exception as e:
        logger.error(f"Auto cleanup toggle error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mysettings command."""
    try:
        user_id = update.effective_user.id
        user_settings = settings_manager.get_user_settings(user_id)
        
        settings_text = "⚙️ **Pengaturan Anda:**\n\n"
        
        for key, value in user_settings.items():
            if isinstance(value, bool):
                display_value = "✅ AKTIF" if value else "❌ NONAKTIF"
            else:
                display_value = value
            
            settings_text += f"**{key}:** {display_value}\n"
        
        settings_text += f"\n**Fitur Baru:** Upload semua file sekaligus ke Terabox!"

        await update.message.reply_text(settings_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"My settings error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cleanup command."""
    try:
        # Count files and folders before cleanup
        initial_count = len(list(DOWNLOAD_BASE.rglob('*')))
        
        # Remove all download folders
        for item in DOWNLOAD_BASE.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                logger.info(f"Cleaned up: {item}")
            elif item.is_file():
                item.unlink()
                logger.info(f"Removed file: {item}")
        
        # Count after cleanup
        final_count = len(list(DOWNLOAD_BASE.rglob('*')))
        
        await update.message.reply_text(
            f"🧹 **Cleanup Completed!**\n"
            f"📁 Files cleaned: {initial_count - final_count}\n"
            f"📊 Remaining: {final_count} files"
        )
        
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        await update.message.reply_text(f"❌ Cleanup error: {str(e)}")

# Initialize managers
logger.info("🔄 Initializing managers dengan fitur upload semua file...")
settings_manager = UserSettingsManager()
mega_manager = MegaManager()
file_manager = FileManager()
upload_manager = UploadManager()
download_processor = DownloadProcessor(mega_manager, file_manager, upload_manager, settings_manager)

# Start download processor
download_processor.start_processing()

def main():
    """Start the bot"""
    logger.info("🚀 Starting Mega Downloader Bot dengan Upload Semua File...")
    
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
    logger.info("✅ Bot started successfully dengan metode UPLOAD SEMUA FILE SEKALIGUS!")
    application.run_polling()

if __name__ == '__main__':
    main()
