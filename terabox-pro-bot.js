const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const RecaptchaPlugin = require('puppeteer-extra-plugin-recaptcha');
const fs = require('fs');
const path = require('path');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
const axios = require('axios');

// Gunakan plugin stealth untuk menghindari deteksi
puppeteer.use(StealthPlugin());
puppeteer.use(RecaptchaPlugin({
  provider: {
    id: '2captcha',
    token: process.env.TWOCAPTCHA_API_KEY || 'YOUR_2CAPTCHA_API_KEY'
  },
  visualFeedback: true
}));

/**
 * Bot Terabox Pro - Versi JavaScript dari Python Bot
 * Fitur Lengkap: Download MEGA, Upload Terabox, Auto Rename, Telegram Bot
 */

class TeraboxProBot {
  constructor() {
    this.browser = null;
    this.page = null;
    this.timeout = 30000;
    this.isLoggedIn = false;
    this.downloadBase = path.join(__dirname, 'downloads');
    this.photoExtensions = new Set(['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heic']);
    this.videoExtensions = new Set(['.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp', '.mpeg']);
    this.activeDownloads = new Map();
    this.completedDownloads = new Map();
    this.downloadQueue = [];
    this.maxConcurrentDownloads = 2;
    this.currentProcesses = 0;
    
    // Inisialisasi directory
    this.ensureDirectories();
    
    console.log('🚀 Terabox Pro Bot initialized');
  }

  ensureDirectories() {
    if (!fs.existsSync(this.downloadBase)) {
      fs.mkdirSync(this.downloadBase, { recursive: true });
    }
  }

  async initialize() {
    console.log('🌐 Initializing browser dengan stealth mode...');
    
    try {
      this.browser = await puppeteer.launch({
        headless: false,
        defaultViewport: null,
        args: [
          '--start-maximized',
          '--no-sandbox',
          '--disable-setuid-sandbox',
          '--disable-blink-features=AutomationControlled',
          '--disable-web-security',
          '--disable-features=IsolateOrigins,site-per-process',
          '--disable-site-isolation-trials'
        ],
        ignoreHTTPSErrors: true
      });

      this.page = await this.browser.newPage();
      
      // Setup stealth
      await this.page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
      await this.page.setExtraHTTPHeaders({
        'Accept-Language': 'en-US,en;q=0.9',
      });

      // Bypass automation detection
      await this.page.evaluateOnNewDocument(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
      });

      console.log('✅ Browser initialized dengan stealth mode');
      return this;
    } catch (error) {
      console.error('❌ Browser initialization error:', error);
      throw error;
    }
  }

  /**
   * MEGA Download Manager
   */
  class MegaManager {
    constructor() {
      this.megaGetPath = this.findMegaGet();
      this.accounts = this.loadMegaAccounts();
      this.currentAccountIndex = 0;
    }

    findMegaGet() {
      const possiblePaths = [
        '/snap/bin/mega-get',
        '/usr/bin/mega-get',
        '/usr/local/bin/mega-get',
        'mega-get'
      ];

      for (const p of possiblePaths) {
        try {
          execSync(`which ${p}`, { stdio: 'ignore' });
          console.log(`✅ Found mega-get at: ${p}`);
          return p;
        } catch {
          continue;
        }
      }
      throw new Error('mega-get not found. Please install mega-cmd: sudo snap install mega-cmd');
    }

    loadMegaAccounts() {
      const accounts = [];
      let i = 1;
      
      while (process.env[`MEGA_EMAIL_${i}`] && process.env[`MEGA_PASSWORD_${i}`]) {
        accounts.push({
          email: process.env[`MEGA_EMAIL_${i}`],
          password: process.env[`MEGA_PASSWORD_${i}`]
        });
        i++;
      }

      if (accounts.length === 0) {
        throw new Error('No MEGA accounts found in environment variables');
      }

      console.log(`✅ Loaded ${accounts.length} MEGA accounts`);
      return accounts;
    }

    getCurrentAccount() {
      return this.accounts[this.currentAccountIndex];
    }

    rotateAccount() {
      if (this.accounts.length > 1) {
        this.currentAccountIndex = (this.currentAccountIndex + 1) % this.accounts.length;
        console.log(`🔄 Rotated to account: ${this.getCurrentAccount().email}`);
      }
    }

    async downloadMegaFolder(megaUrl, downloadPath, jobId) {
      console.log(`🚀 Starting MEGA download for job ${jobId}`);
      console.log(`📥 URL: ${megaUrl}`);
      console.log(`📁 Path: ${downloadPath}`);

      const maxRetries = 3;
      let retryCount = 0;

      while (retryCount < maxRetries) {
        try {
          // Ensure base directory exists
          if (!fs.existsSync(this.downloadBase)) {
            fs.mkdirSync(this.downloadBase, { recursive: true });
          }

          // Change to download directory
          const originalCwd = process.cwd();
          process.chdir(this.downloadBase);

          try {
            const downloadCmd = [this.megaGetPath, megaUrl];
            console.log(`⚡ Executing: ${downloadCmd.join(' ')}`);

            const result = await this.execCommand(downloadCmd.join(' '), 7200000); // 2 hours timeout

            process.chdir(originalCwd);

            if (result.success) {
              // Check if files were downloaded
              const files = this.getAllFiles(this.downloadBase);
              const fileCount = files.length;

              if (fileCount === 0) {
                throw new Error('Download completed but no files found');
              }

              console.log(`✅ Download successful! ${fileCount} files downloaded`);
              return { success: true, message: `Downloaded ${fileCount} files` };
            } else {
              const error = result.error.toLowerCase();
              if (error.includes('quota') || error.includes('storage')) {
                console.log('🔄 Quota exceeded, rotating account...');
                this.rotateAccount();
                retryCount++;
                continue;
              }
              throw new Error(result.error);
            }
          } catch (error) {
            process.chdir(originalCwd);
            throw error;
          }
        } catch (error) {
          console.error(`❌ Download error: ${error.message}`);
          retryCount++;
          if (retryCount < maxRetries) {
            console.log(`🔄 Retrying... (${retryCount}/${maxRetries})`);
            await this.sleep(5000);
          }
        }
      }

      throw new Error(`Download failed after ${maxRetries} retries`);
    }

    execCommand(command, timeout = 300000) {
      return new Promise((resolve) => {
        const child = exec(command, { timeout }, (error, stdout, stderr) => {
          if (error) {
            resolve({ success: false, error: stderr || stdout || error.message });
          } else {
            resolve({ success: true, stdout, stderr });
          }
        });
      });
    }

    getAllFiles(dir) {
      let results = [];
      const list = fs.readdirSync(dir);
      
      for (const file of list) {
        const filePath = path.join(dir, file);
        const stat = fs.statSync(filePath);
        
        if (stat && stat.isDirectory()) {
          results = results.concat(this.getAllFiles(filePath));
        } else {
          results.push(filePath);
        }
      }
      
      return results;
    }

    sleep(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }
  }

  /**
   * File Management System
   */
  class FileManager {
    autoRenameMediaFiles(folderPath, prefix) {
      console.log(`🔄 Starting auto-rename in ${folderPath} with prefix: ${prefix}`);
      
      try {
        const allFiles = this.getAllFiles(folderPath);
        const mediaFiles = allFiles.filter(file => {
          const ext = path.extname(file).toLowerCase();
          return this.photoExtensions.has(ext) || this.videoExtensions.has(ext);
        });

        mediaFiles.sort();

        let renamedCount = 0;
        const totalFiles = mediaFiles.length;

        console.log(`📊 Found ${totalFiles} media files to rename`);

        for (let i = 0; i < totalFiles; i++) {
          const filePath = mediaFiles[i];
          const numberStr = (i + 1).toString().padStart(2, '0');
          const newName = `${prefix} ${numberStr}${path.extname(filePath)}`;
          const newPath = path.join(path.dirname(filePath), newName);

          try {
            if (filePath !== newPath) {
              fs.renameSync(filePath, newPath);
              renamedCount++;
              console.log(`✅ Renamed: ${path.basename(filePath)} -> ${newName}`);
            }
          } catch (error) {
            console.error(`❌ Error renaming ${filePath}:`, error);
          }
        }

        console.log(`📝 Rename completed: ${renamedCount}/${totalFiles} files renamed`);
        return { renamed: renamedCount, total: totalFiles };
      } catch (error) {
        console.error('💥 Auto-rename error:', error);
        return { renamed: 0, total: 0 };
      }
    }

    getAllFiles(dir) {
      let results = [];
      
      try {
        const list = fs.readdirSync(dir);
        
        for (const file of list) {
          const filePath = path.join(dir, file);
          const stat = fs.statSync(filePath);
          
          if (stat.isDirectory()) {
            results = results.concat(this.getAllFiles(filePath));
          } else {
            results.push(filePath);
          }
        }
      } catch (error) {
        console.error(`❌ Error reading directory ${dir}:`, error);
      }
      
      return results;
    }

    cleanupFolder(folderPath) {
      try {
        if (fs.existsSync(folderPath)) {
          fs.rmSync(folderPath, { recursive: true, force: true });
          console.log(`🧹 Cleaned up folder: ${folderPath}`);
          return true;
        }
        return false;
      } catch (error) {
        console.error(`❌ Cleanup error for ${folderPath}:`, error);
        return false;
      }
    }
  }

  /**
   * Terabox Upload System dengan Selenium-like Approach
   */
  class TeraboxUploader {
    constructor() {
      this.email = process.env.TERABOX_EMAIL;
      this.password = process.env.TERABOX_PASSWORD;
      this.browser = null;
      this.page = null;
      this.isLoggedIn = false;
    }

    async initialize() {
      console.log('🌐 Initializing Terabox uploader...');
      
      try {
        this.browser = await puppeteer.launch({
          headless: false,
          defaultViewport: null,
          args: [
            '--start-maximized',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled'
          ]
        });

        this.page = await this.browser.newPage();
        
        // Set user agent dan headers
        await this.page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        
        console.log('✅ Terabox uploader initialized');
        return true;
      } catch (error) {
        console.error('❌ Terabox uploader initialization error:', error);
        return false;
      }
    }

    async login() {
      if (this.isLoggedIn) {
        console.log('✅ Already logged in to Terabox');
        return true;
      }

      console.log('🔐 Logging in to Terabox...');
      
      try {
        await this.page.goto('https://www.terabox.com', { 
          waitUntil: 'networkidle2',
          timeout: 30000
        });

        // Tunggu dan klik tombol login
        await this.clickElement('.login-btn, .sign-in-btn, [data-testid="login-button"]');
        await this.page.waitForTimeout(3000);

        // Isi email
        await this.fillField('input[type="email"], input[type="text"], #email, #username', this.email);
        
        // Isi password
        await this.fillField('input[type="password"], #password', this.password);
        
        // Submit login
        await this.clickElement('button[type="submit"], .login-submit, .submit-btn');
        
        // Tunggu login berhasil
        await this.page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 15000 });
        
        this.isLoggedIn = true;
        console.log('✅ Login successful!');
        return true;
      } catch (error) {
        console.error('❌ Login failed:', error);
        return false;
      }
    }

    async uploadFolder(folderPath) {
      console.log(`📤 Starting folder upload: ${folderPath}`);
      
      try {
        if (!await this.login()) {
          throw new Error('Login failed');
        }

        // Navigate to upload page
        await this.page.goto('https://www.terabox.com/main?category=all', {
          waitUntil: 'networkidle2',
          timeout: 30000
        });

        // Click upload button
        await this.clickElement('.upload-btn, .upload-button, [aria-label="Upload"]');
        await this.page.waitForTimeout(2000);

        // Handle file input
        const fileInput = await this.page.$('input[type="file"]');
        if (!fileInput) {
          throw new Error('File input not found');
        }

        // Get all files from folder
        const allFiles = this.getAllFiles(folderPath);
        if (allFiles.length === 0) {
          throw new Error('No files found in folder');
        }

        // Upload files (batch processing untuk menghindari timeout)
        const batchSize = 5;
        for (let i = 0; i < allFiles.length; i += batchSize) {
          const batch = allFiles.slice(i, i + batchSize);
          await fileInput.uploadFile(...batch);
          console.log(`📦 Uploaded batch ${Math.floor(i/batchSize) + 1}`);
          await this.page.waitForTimeout(3000);
        }

        // Wait for upload to complete
        await this.waitForUploadCompletion();
        
        console.log('✅ Folder upload completed!');
        return this.generateShareLinks();

      } catch (error) {
        console.error('❌ Upload error:', error);
        throw error;
      }
    }

    async waitForUploadCompletion() {
      console.log('⏳ Waiting for upload completion...');
      
      const maxWaitTime = 300000; // 5 minutes
      const startTime = Date.now();
      
      while (Date.now() - startTime < maxWaitTime) {
        const isUploading = await this.page.evaluate(() => {
          const progressElements = document.querySelectorAll('.progress-bar, .upload-progress, [aria-valuenow]');
          return Array.from(progressElements).some(el => {
            const value = el.getAttribute('aria-valuenow') || el.style.width;
            return value !== '100%' && value !== '100';
          });
        });

        if (!isUploading) {
          console.log('✅ Upload completed!');
          return true;
        }

        await this.page.waitForTimeout(5000);
      }
      
      throw new Error('Upload timeout');
    }

    async generateShareLinks() {
      console.log('🔗 Generating share links...');
      
      try {
        // Select all uploaded files
        await this.clickElement('.file-select-all, [aria-label="Select all"]');
        await this.page.waitForTimeout(2000);

        // Click share button
        await this.clickElement('.share-btn, [aria-label="Share"], .action-share');
        await this.page.waitForTimeout(3000);

        // Generate share link
        await this.clickElement('.generate-link, .create-link-btn, [data-action="generate"]');
        await this.page.waitForTimeout(5000);

        // Extract share links
        const links = await this.page.evaluate(() => {
          const linkElements = document.querySelectorAll('.share-link, [data-share-url], input[readonly]');
          const links = [];
          
          linkElements.forEach(el => {
            const url = el.value || el.getAttribute('data-share-url') || el.href;
            if (url && url.includes('terabox.com')) {
              links.push(url);
            }
          });
          
          return links;
        });

        console.log(`📎 Generated ${links.length} share links`);
        return links;
      } catch (error) {
        console.error('❌ Share link generation error:', error);
        return [];
      }
    }

    getAllFiles(dir) {
      let results = [];
      
      try {
        const list = fs.readdirSync(dir);
        
        for (const file of list) {
          const filePath = path.join(dir, file);
          const stat = fs.statSync(filePath);
          
          if (stat.isDirectory()) {
            results = results.concat(this.getAllFiles(filePath));
          } else {
            results.push(filePath);
          }
        }
      } catch (error) {
        console.error(`❌ Error reading directory ${dir}:`, error);
      }
      
      return results;
    }

    async clickElement(selectors) {
      const selectorArray = selectors.split(', ');
      
      for (const selector of selectorArray) {
        try {
          await this.page.waitForSelector(selector, { timeout: 5000 });
          await this.page.click(selector);
          return true;
        } catch (error) {
          continue;
        }
      }
      
      throw new Error(`No clickable element found for selectors: ${selectors}`);
    }

    async fillField(selectors, value) {
      const selectorArray = selectors.split(', ');
      
      for (const selector of selectorArray) {
        try {
          await this.page.waitForSelector(selector, { timeout: 5000 });
          await this.page.click(selector);
          await this.page.evaluate((sel) => {
            document.querySelector(sel).value = '';
          }, selector);
          await this.page.type(selector, value, { delay: 50 });
          return true;
        } catch (error) {
          continue;
        }
      }
      
      throw new Error(`No fillable field found for selectors: ${selectors}`);
    }

    async close() {
      if (this.browser) {
        await this.browser.close();
        console.log('🔚 Terabox uploader closed');
      }
    }
  }

  /**
   * Download Processor - Queue Management
   */
  class DownloadProcessor {
    constructor(megaManager, fileManager, teraboxUploader) {
      this.megaManager = megaManager;
      this.fileManager = fileManager;
      this.teraboxUploader = teraboxUploader;
      this.processing = false;
    }

    startProcessing() {
      if (!this.processing) {
        this.processing = true;
        this.processQueue();
        console.log('🚀 Download processor started');
      }
    }

    async processQueue() {
      while (this.processing) {
        if (this.currentProcesses < this.maxConcurrentDownloads && this.downloadQueue.length > 0) {
          const jobData = this.downloadQueue.shift();
          this.currentProcesses++;
          
          this.processDownloadJob(jobData).finally(() => {
            this.currentProcesses--;
          });
        }
        
        await this.sleep(5000);
      }
    }

    async processDownloadJob(jobData) {
      const { jobId, megaUrl, userId, folderName, callback } = jobData;
      
      console.log(`🔄 Processing download job ${jobId}`);
      
      try {
        // Update status
        this.updateJobStatus(jobId, 'downloading', 'Starting download from MEGA...');
        
        // Download from MEGA
        const downloadResult = await this.megaManager.downloadMegaFolder(megaUrl, this.downloadBase, jobId);
        
        if (!downloadResult.success) {
          throw new Error(downloadResult.message);
        }

        this.updateJobStatus(jobId, 'download_completed', 'Download completed, starting file processing...');
        
        // Find downloaded folder
        const downloadedFolder = this.findDownloadedFolder();
        if (!downloadedFolder) {
          throw new Error('Downloaded folder not found');
        }

        // Auto-rename files
        const userSettings = this.getUserSettings(userId);
        const renameResult = this.fileManager.autoRenameMediaFiles(downloadedFolder, userSettings.prefix);
        
        this.updateJobStatus(jobId, 'renaming', `Renamed ${renameResult.renamed}/${renameResult.total} files`);

        // Auto-upload to Terabox
        if (userSettings.autoUpload) {
          this.updateJobStatus(jobId, 'uploading', 'Starting upload to Terabox...');
          
          const shareLinks = await this.teraboxUploader.uploadFolder(downloadedFolder);
          
          this.updateJobStatus(jobId, 'uploading', `Generated ${shareLinks.length} share links`);
          
          // Auto-cleanup
          if (userSettings.autoCleanup) {
            this.fileManager.cleanupFolder(downloadedFolder);
            this.updateJobStatus(jobId, 'cleaning', 'Cleaned up local files');
          }

          this.updateJobStatus(jobId, 'completed', 'All processes completed successfully');
          
          // Call callback dengan results
          if (callback) {
            callback({
              success: true,
              jobId,
              shareLinks,
              renamedCount: renameResult.renamed,
              totalFiles: renameResult.total
            });
          }
        } else {
          this.updateJobStatus(jobId, 'completed', 'Download completed (upload skipped)');
          
          if (callback) {
            callback({
              success: true,
              jobId,
              message: 'Download completed, upload skipped',
              renamedCount: renameResult.renamed,
              totalFiles: renameResult.total
            });
          }
        }

      } catch (error) {
        console.error(`❌ Job ${jobId} error:`, error);
        this.updateJobStatus(jobId, 'error', error.message);
        
        if (callback) {
          callback({
            success: false,
            jobId,
            error: error.message
          });
        }
      }
    }

    findDownloadedFolder() {
      const items = fs.readdirSync(this.downloadBase)
        .map(item => path.join(this.downloadBase, item))
        .filter(item => fs.statSync(item).isDirectory())
        .sort((a, b) => fs.statSync(b).mtime.getTime() - fs.statSync(a).mtime.getTime());

      return items.length > 0 ? items[0] : null;
    }

    updateJobStatus(jobId, status, progress) {
      if (this.activeDownloads.has(jobId)) {
        const job = this.activeDownloads.get(jobId);
        job.status = status;
        job.progress = progress;
        job.updatedAt = new Date().toISOString();
        
        console.log(`📊 Job ${jobId} status: ${status} - ${progress}`);
      }
    }

    getUserSettings(userId) {
      // Default settings
      return {
        prefix: 'file_',
        platform: 'terabox',
        autoUpload: true,
        autoCleanup: true
      };
    }

    sleep(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }

    addToQueue(jobData) {
      this.downloadQueue.push(jobData);
      this.activeDownloads.set(jobData.jobId, {
        ...jobData,
        status: 'pending',
        progress: 'Waiting in queue',
        createdAt: new Date().toISOString()
      });
      
      console.log(`📥 Added job ${jobData.jobId} to queue. Queue size: ${this.downloadQueue.length}`);
    }

    getQueueStatus() {
      return {
        queueSize: this.downloadQueue.length,
        activeProcesses: this.currentProcesses,
        maxConcurrent: this.maxConcurrentDownloads,
        activeJobs: Array.from(this.activeDownloads.entries()).map(([id, job]) => ({
          id,
          status: job.status,
          progress: job.progress
        }))
      };
    }
  }

  /**
   * Telegram Bot Integration
   */
  class TelegramBot {
    constructor(token) {
      this.token = token;
      this.bot = null;
      this.downloadProcessor = null;
    }

    initialize(downloadProcessor) {
      this.downloadProcessor = downloadProcessor;
      console.log('🤖 Telegram Bot initialized');
      
      // Simulate bot commands (in real implementation, use telegram-bot-api)
      this.setupCommandHandlers();
    }

    setupCommandHandlers() {
      console.log('⌨️  Bot commands available:');
      console.log('/start - Start bot');
      console.log('/download <mega_url> - Download from MEGA');
      console.log('/status - Check download status');
      console.log('/queue - Check queue status');
      console.log('/help - Show help');
    }

    async handleDownloadCommand(userId, megaUrl) {
      const jobId = `job_${Date.now()}_${userId}`;
      
      const jobData = {
        jobId,
        megaUrl,
        userId,
        folderName: this.extractFolderName(megaUrl),
        callback: (result) => this.sendJobResult(userId, result)
      };

      this.downloadProcessor.addToQueue(jobData);
      
      return {
        success: true,
        jobId,
        message: `📥 Download job added to queue\n🆔 Job ID: ${jobId}\n📊 Queue Position: ${this.downloadProcessor.downloadQueue.length}`
      };
    }

    extractFolderName(megaUrl) {
      const match = megaUrl.match(/folder\/([^#]+)#?(.*)$/);
      return match ? `MEGA_${match[1]}` : `Folder_${Date.now()}`;
    }

    sendJobResult(userId, result) {
      // In real implementation, send message to user
      console.log(`📨 Sending result to user ${userId}:`, result);
    }

    getStatus() {
      return this.downloadProcessor.getQueueStatus();
    }
  }

  // Initialize all components
  async initializeAll() {
    try {
      await this.initialize();
      
      // Initialize managers
      this.megaManager = new this.MegaManager();
      this.fileManager = new this.FileManager();
      this.teraboxUploader = new this.TeraboxUploader();
      await this.teraboxUploader.initialize();
      
      // Initialize download processor
      this.downloadProcessor = new this.DownloadProcessor(
        this.megaManager,
        this.fileManager,
        this.teraboxUploader
      );
      this.downloadProcessor.startProcessing();
      
      // Initialize Telegram bot
      this.telegramBot = new this.TelegramBot(process.env.TELEGRAM_BOT_TOKEN);
      this.telegramBot.initialize(this.downloadProcessor);
      
      console.log('🎉 All systems initialized successfully!');
      return this;
    } catch (error) {
      console.error('❌ Initialization error:', error);
      throw error;
    }
  }

  /**
   * Public Methods untuk external use
   */
  async downloadAndUpload(megaUrl, options = {}) {
    const jobId = `direct_${Date.now()}`;
    
    return new Promise((resolve) => {
      const jobData = {
        jobId,
        megaUrl,
        userId: 'direct_user',
        folderName: this.extractFolderName(megaUrl),
        callback: resolve
      };

      this.downloadProcessor.addToQueue(jobData);
    });
  }

  async directUpload(folderPath) {
    try {
      console.log(`📤 Direct upload: ${folderPath}`);
      return await this.teraboxUploader.uploadFolder(folderPath);
    } catch (error) {
      console.error('❌ Direct upload error:', error);
      throw error;
    }
  }

  getSystemStatus() {
    return {
      mega: {
        accounts: this.megaManager.accounts.length,
        currentAccount: this.megaManager.getCurrentAccount().email
      },
      terabox: {
        loggedIn: this.teraboxUploader.isLoggedIn
      },
      queue: this.downloadProcessor.getQueueStatus(),
      storage: {
        downloadBase: this.downloadBase,
        freeSpace: this.getFreeSpace()
      }
    };
  }

  getFreeSpace() {
    try {
      const stats = fs.statSync(this.downloadBase);
      // Simple implementation - in production, use proper disk space check
      return 'Available';
    } catch {
      return 'Unknown';
    }
  }

  extractFolderName(megaUrl) {
    const match = megaUrl.match(/folder\/([^#]+)#?(.*)$/);
    return match ? `MEGA_${match[1]}` : `Folder_${Date.now()}`;
  }

  async close() {
    console.log('🔚 Shutting down Terabox Pro Bot...');
    
    if (this.teraboxUploader) {
      await this.teraboxUploader.close();
    }
    
    if (this.browser) {
      await this.browser.close();
    }
    
    if (this.downloadProcessor) {
      this.downloadProcessor.processing = false;
    }
    
    console.log('✅ Terabox Pro Bot shutdown completed');
  }
}

// Helper function untuk exec dengan timeout
function execSync(cmd, options) {
  try {
    const result = require('child_process').execSync(cmd, options);
    return result.toString();
  } catch (error) {
    throw error;
  }
}

// Export class
module.exports = TeraboxProBot;

/**
 * Contoh Penggunaan:
 * 
 * const TeraboxProBot = require('./terabox-pro-bot');
 * 
 * // Initialize bot
 * const bot = new TeraboxProBot();
 * await bot.initializeAll();
 * 
 * // Download dan upload dari MEGA
 * const result = await bot.downloadAndUpload('https://mega.nz/folder/abc123#def456');
 * console.log('Result:', result);
 * 
 * // Upload folder langsung
 * const links = await bot.directUpload('/path/to/folder');
 * console.log('Share links:', links);
 * 
 * // Dapatkan status system
 * const status = bot.getSystemStatus();
 * console.log('System status:', status);
 * 
 * // Tutup bot
 * await bot.close();
 */

// Jika dijalankan langsung
if (require.main === module) {
  const bot = new TeraboxProBot();
  
  bot.initializeAll().then(() => {
    console.log('🤖 Terabox Pro Bot is ready!');
    console.log('💡 Use the exported methods to interact with the bot');
  }).catch(console.error);
  
  // Handle graceful shutdown
  process.on('SIGINT', async () => {
    console.log('\n🔚 Received SIGINT, shutting down...');
    await bot.close();
    process.exit(0);
  });
  
  process.on('SIGTERM', async () => {
    console.log('\n🔚 Received SIGTERM, shutting down...');
    await bot.close();
    process.exit(0);
  });
}
