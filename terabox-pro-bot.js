const { Telegraf, Markup, session } = require('telegraf');
const fs = require('fs');
const path = require('path');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
require('dotenv').config();

const execAsync = promisify(exec);

/**
 * 🤖 TERABOX TELEGRAM BOT - Full Features with Advanced Set Prefix
 * Bot Telegram lengkap dengan fitur set prefix custom seperti "TELEGRAM @missyhot22"
 */

class TelegramTeraboxBot {
    constructor() {
        if (!process.env.TELEGRAM_BOT_TOKEN) {
            throw new Error('TELEGRAM_BOT_TOKEN is required in .env file');
        }
        
        this.bot = new Telegraf(process.env.TELEGRAM_BOT_TOKEN);
        this.downloadBase = path.join(__dirname, 'downloads');
        this.userSessions = new Map();
        this.activeDownloads = new Map();
        this.userSettings = new Map();
        this.presetPrefixes = new Map([ // Preset prefix templates
            ['telegram', 'TELEGRAM @missyhot22'],
            ['instagram', 'INSTAGRAM @username'],
            ['twitter', 'TWITTER @username'],
            ['tiktok', 'TIKTOK @username'],
            ['onlyfans', 'ONLYFANS @username'],
            ['custom', 'CUSTOM PREFIX']
        ]);
        
        this.setupDirectories();
        this.setupMiddlewares();
        this.setupBotHandlers();
        
        console.log('🤖 Telegram Terabox Bot with Advanced Prefix Initialized');
    }

    setupDirectories() {
        if (!fs.existsSync(this.downloadBase)) {
            fs.mkdirSync(this.downloadBase, { recursive: true });
        }
        if (!fs.existsSync(path.join(this.downloadBase, 'users'))) {
            fs.mkdirSync(path.join(this.downloadBase, 'users'), { recursive: true });
        }
        if (!fs.existsSync(path.join(__dirname, 'user_data'))) {
            fs.mkdirSync(path.join(__dirname, 'user_data'), { recursive: true });
        }
        
        this.loadUserSettings();
    }

    setupMiddlewares() {
        this.bot.use((ctx, next) => {
            const userId = ctx.from?.id;
            if (userId && !this.userSessions.has(userId)) {
                this.userSessions.set(userId, {
                    userId: userId,
                    username: ctx.from.username,
                    firstName: ctx.from.first_name,
                    lastCommand: null,
                    lastMessageId: null,
                    downloadHistory: [],
                    createdAt: new Date(),
                    waitingForPrefix: false,
                    prefixMode: null // 'preset' or 'custom'
                });
                
                if (!this.userSettings.has(userId)) {
                    this.userSettings.set(userId, {
                        prefix: 'file',
                        prefixType: 'simple', // 'simple', 'preset', 'custom'
                        autoRename: true,
                        autoUpload: true,
                        autoCleanup: false,
                        notifications: true
                    });
                }
            }
            return next();
        });
    }

    loadUserSettings() {
        try {
            const settingsFile = path.join(__dirname, 'user_data', 'user_settings.json');
            if (fs.existsSync(settingsFile)) {
                const data = fs.readFileSync(settingsFile, 'utf8');
                const settings = JSON.parse(data);
                this.userSettings = new Map(Object.entries(settings));
                console.log(`✅ Loaded settings for ${this.userSettings.size} users`);
            }
        } catch (error) {
            console.error('❌ Error loading user settings:', error.message);
        }
    }

    saveUserSettings() {
        try {
            const settingsFile = path.join(__dirname, 'user_data', 'user_settings.json');
            const settingsObj = Object.fromEntries(this.userSettings);
            fs.writeFileSync(settingsFile, JSON.stringify(settingsObj, null, 2));
            console.log('💾 User settings saved');
        } catch (error) {
            console.error('❌ Error saving user settings:', error.message);
        }
    }

    setupBotHandlers() {
        // ==================== COMMAND HANDLERS ====================

        // Start command
        this.bot.start((ctx) => {
            const welcomeText = `🤖 *TERABOX PRO BOT*

Halo ${ctx.from.first_name}! Saya adalah bot untuk mendownload folder dari MEGA.nz dan menguploadnya ke Terabox.

*Fitur Utama:*
📥 Download folder dari MEGA.nz
🔄 Auto-rename file media dengan custom prefix
📤 Upload otomatis ke Terabox
📊 Progress tracking real-time
⚡ Multiple concurrent downloads
🎯 *Advanced Set Prefix* (Template: TELEGRAM @missyhot22)

*Commands Available:*
/download - Download dari MEGA.nz
/upload - Upload folder manual
/quick - Automated pipeline
/setprefix - Atur custom prefix file
/myprefix - Lihat prefix saat ini
/status - Status download
/mystats - Statistik Anda
/settings - Pengaturan
/help - Bantuan lengkap

*Contoh penggunaan:*
\`/download https://mega.nz/folder/abc123#def456\`
\`/setprefix\` - Pilih template prefix

Bot siap melayani! 🚀`;

            const keyboard = Markup.inlineKeyboard([
                [Markup.button.callback('📥 Download MEGA', 'download_btn')],
                [Markup.button.callback('⚡ Quick Pipeline', 'quick_btn')],
                [Markup.button.callback('🎯 Set Prefix', 'setprefix_btn')],
                [Markup.button.callback('📊 My Stats', 'stats_btn')],
                [Markup.button.callback('⚙️ Settings', 'settings_btn')]
            ]);

            return ctx.replyWithMarkdown(welcomeText, keyboard);
        });

        // Help command
        this.bot.help((ctx) => {
            const helpText = `📖 *BOT HELP GUIDE*

*DOWNLOAD FROM MEGA:*
\`/download <mega_url>\`
Contoh: /download https://mega.nz/folder/abc123#def456

*SET CUSTOM PREFIX:*
\`/setprefix\` - Pilih template prefix
\`/setprefix <custom_prefix>\` - Set custom langsung

*PREFIX TEMPLATES:*
• TELEGRAM @username
• INSTAGRAM @username  
• TWITTER @username
• TIKTOK @username
• ONLYFANS @username
• Custom text bebas

*QUICK PIPELINE:*
\`/quick <mega_url>\`
Download + Rename + Upload otomatis

*MANUAL UPLOAD:*
\`/upload\`
Upload folder manual (reply dengan folder)

*STATUS & MANAGEMENT:*
\`/status\` - Lihat status download
\`/mystats\` - Statistik Anda
\`/settings\` - Pengaturan user
\`/cancel\` - Batalkan download
\`/myprefix\` - Lihat prefix saat ini

*FORMAT URL MEGA:*
https://mega.nz/folder/FOLDER_ID#FOLDER_KEY

*CONTOH HASIL PREFIX:*
• TELEGRAM @missyhot22_001.jpg
• INSTAGRAM @model_001.mp4  
• MY TRIP 2024_001.jpg
• CUSTOM TEXT_001.mp4

*NOTE:*
• Max concurrent downloads: 2
• Auto rename files dengan numbering
• Progress update real-time`;

            return ctx.replyWithMarkdown(helpText);
        });

        // Download command
        this.bot.command('download', async (ctx) => {
            const megaUrl = ctx.message.text.split(' ')[1];
            
            if (!megaUrl) {
                return ctx.replyWithMarkdown('❌ *Format salah!*\n\nGunakan: `/download <mega_url>`\n\nContoh: `/download https://mega.nz/folder/abc123#def456`');
            }

            if (!megaUrl.includes('mega.nz/folder/')) {
                return ctx.reply('❌ URL MEGA tidak valid! Pastikan format: https://mega.nz/folder/...');
            }

            await this.handleMegaDownload(ctx, megaUrl);
        });

        // Set Prefix command - VERSI ADVANCED
        this.bot.command('setprefix', async (ctx) => {
            const args = ctx.message.text.split(' ').slice(1);
            const prefixInput = args.join(' ');
            
            if (!prefixInput) {
                // Tampilkan menu template prefix
                await this.showPrefixTemplateMenu(ctx);
                return;
            }

            await this.handleSetPrefix(ctx, prefixInput, 'custom');
        });

        // My Prefix command
        this.bot.command('myprefix', (ctx) => {
            const userId = ctx.from.id;
            const userSettings = this.userSettings.get(userId);
            const currentPrefix = userSettings?.prefix || 'file';
            const prefixType = userSettings?.prefixType || 'simple';
            
            let typeInfo = '';
            if (prefixType === 'preset') {
                typeInfo = ' (Template Preset)';
            } else if (prefixType === 'custom') {
                typeInfo = ' (Custom)';
            }

            const exampleFiles = [
                `${currentPrefix}_001.jpg`,
                `${currentPrefix}_002.mp4`, 
                `${currentPrefix}_003.png`
            ];

            const prefixText = `🎯 *YOUR CURRENT PREFIX*\n\n` +
                              `**Prefix:** ${currentPrefix}${typeInfo}\n` +
                              `**Type:** ${prefixType}\n` +
                              `**Contoh file:**\n` +
                              `• ${exampleFiles[0]}\n` +
                              `• ${exampleFiles[1]}\n` +
                              `• ${exampleFiles[2]}\n\n` +
                              `Gunakan \`/setprefix\` untuk mengubah prefix`;

            ctx.replyWithMarkdown(prefixText);
        });

        // Quick pipeline command
        this.bot.command('quick', async (ctx) => {
            const megaUrl = ctx.message.text.split(' ')[1];
            
            if (!megaUrl) {
                return ctx.replyWithMarkdown('❌ *Format salah!*\n\nGunakan: `/quick <mega_url>`\n\nContoh: `/quick https://mega.nz/folder/abc123#def456`');
            }

            await this.handleQuickPipeline(ctx, megaUrl);
        });

        // Upload command
        this.bot.command('upload', (ctx) => {
            ctx.replyWithMarkdown(
                '📤 *Manual Upload*\n\n' +
                'Silakan reply pesan ini dengan folder atau file yang ingin diupload ke Terabox.\n\n' +
                '📎 *Supported formats:* ZIP, RAR, atau multiple files',
                Markup.forceReply()
            );
        });

        // Status command
        this.bot.command('status', (ctx) => {
            this.showUserStatus(ctx);
        });

        // MyStats command
        this.bot.command('mystats', (ctx) => {
            this.showUserStats(ctx);
        });

        // Settings command
        this.bot.command('settings', (ctx) => {
            this.showSettingsMenu(ctx);
        });

        // Cancel command
        this.bot.command('cancel', (ctx) => {
            this.handleCancelDownload(ctx);
        });

        // ==================== MESSAGE HANDLERS ====================

        // Handle document messages (file uploads)
        this.bot.on('document', async (ctx) => {
            await this.handleFileUpload(ctx);
        });

        // Handle text messages (for manual URL input and prefix input)
        this.bot.on('text', async (ctx) => {
            const userId = ctx.from.id;
            const userSession = this.userSessions.get(userId);
            const text = ctx.message.text;

            // Check if it's a reply to upload command
            if (ctx.message.reply_to_message?.text?.includes('Manual Upload')) {
                if (text.includes('mega.nz/folder/')) {
                    await this.handleMegaDownload(ctx, text);
                } else {
                    ctx.reply('❌ URL tidak valid. Silakan masukkan URL MEGA.nz yang valid.');
                }
            }
            // Check if it's a reply to custom prefix command
            else if (userSession?.waitingForPrefix && userSession.prefixMode === 'custom') {
                userSession.waitingForPrefix = false;
                userSession.prefixMode = null;
                await this.handleSetPrefix(ctx, text, 'custom');
            }
            // Check if it's a standalone MEGA URL
            else if (text.includes('mega.nz/folder/') && text.includes('#')) {
                await this.handleMegaDownload(ctx, text);
            }
        });

        // ==================== CALLBACK QUERY HANDLERS ====================

        // Handle button clicks
        this.bot.action('download_btn', (ctx) => {
            ctx.replyWithMarkdown(
                '📥 *Download dari MEGA*\n\n' +
                'Kirim command: `/download <mega_url>`\n\n' +
                'Contoh: `/download https://mega.nz/folder/abc123#def456`\n\n' +
                'Atau langsung reply pesan ini dengan URL MEGA:',
                Markup.forceReply()
            );
            ctx.answerCbQuery();
        });

        this.bot.action('quick_btn', (ctx) => {
            ctx.replyWithMarkdown(
                '⚡ *Quick Pipeline*\n\n' +
                'Kirim command: `/quick <mega_url>`\n\n' +
                'Bot akan otomatis:\n' +
                '1. 📥 Download dari MEGA\n' +
                '2. 🔄 Rename files dengan prefix Anda\n' +
                '3. 📤 Upload ke Terabox\n' +
                '4. 🧹 Cleanup files\n\n' +
                'Contoh: `/quick https://mega.nz/folder/abc123#def456`'
            );
            ctx.answerCbQuery();
        });

        // Set Prefix Button - MENU TEMPLATE
        this.bot.action('setprefix_btn', (ctx) => {
            this.showPrefixTemplateMenu(ctx);
            ctx.answerCbQuery();
        });

        this.bot.action('stats_btn', (ctx) => {
            this.showUserStats(ctx);
            ctx.answerCbQuery();
        });

        this.bot.action('settings_btn', (ctx) => {
            this.showSettingsMenu(ctx);
            ctx.answerCbQuery();
        });

        // Prefix template selection
        this.bot.action(/prefix_(.+)/, (ctx) => {
            const template = ctx.match[1];
            this.handlePrefixTemplateSelection(ctx, template);
        });

        // Settings actions
        this.bot.action(/setting_(.+)/, (ctx) => {
            const action = ctx.match[1];
            this.handleSettingsAction(ctx, action);
        });

        // Cancel download action
        this.bot.action(/cancel_(.+)/, (ctx) => {
            const jobId = ctx.match[1];
            this.cancelUserJob(ctx, jobId);
        });

        // ==================== ERROR HANDLING ====================

        this.bot.catch((err, ctx) => {
            console.error(`Error for ${ctx.updateType}:`, err);
            ctx.reply('❌ Terjadi error. Silakan coba lagi atau hubungi admin.');
        });
    }

    // ==================== ADVANCED PREFIX HANDLING METHODS ====================

    async showPrefixTemplateMenu(ctx) {
        const prefixMenuText = `🎯 *PILIH TEMPLATE PREFIX*\n\n` +
                              `Pilih template prefix yang ingin digunakan:\n\n` +
                              `📱 *Social Media Templates:*\n` +
                              `• TELEGRAM @username\n` +
                              `• INSTAGRAM @username\n` +
                              `• TWITTER @username\n` +
                              `• TIKTOK @username\n` +
                              `• ONLYFANS @username\n\n` +
                              `✏️ *Custom Options:*\n` +
                              `• Custom Text (bebas)\n` +
                              `• Simple Prefix (file_001.jpg)`;

        const keyboard = Markup.inlineKeyboard([
            [Markup.button.callback('📱 TELEGRAM Template', 'prefix_telegram')],
            [Markup.button.callback('📷 INSTAGRAM Template', 'prefix_instagram')],
            [Markup.button.callback('🐦 TWITTER Template', 'prefix_twitter')],
            [Markup.button.callback('🎵 TIKTOK Template', 'prefix_tiktok')],
            [Markup.button.callback('💎 ONLYFANS Template', 'prefix_onlyfans')],
            [Markup.button.callback('✏️ Custom Text', 'prefix_custom')],
            [Markup.button.callback('📄 Simple Prefix', 'prefix_simple')]
        ]);

        await ctx.replyWithMarkdown(prefixMenuText, keyboard);
    }

    async handlePrefixTemplateSelection(ctx, template) {
        const userId = ctx.from.id;
        const userSession = this.userSessions.get(userId);

        if (template === 'custom') {
            userSession.waitingForPrefix = true;
            userSession.prefixMode = 'custom';
            
            await ctx.replyWithMarkdown(
                `✏️ *CUSTOM PREFIX*\n\n` +
                `Silakan ketik custom prefix yang diinginkan:\n\n` +
                `*Contoh:*\n` +
                `• TELEGRAM @missyhot22\n` +
                `• MY TRIP 2024\n` +
                `• PROJECT X\n` +
                `• NAMA ANDA\n\n` +
                `Kirim custom prefix Anda:`,
                Markup.forceReply()
            );
            ctx.answerCbQuery();
            return;
        }

        if (template === 'simple') {
            await this.handleSetPrefix(ctx, 'file', 'simple');
            ctx.answerCbQuery();
            return;
        }

        // Handle preset templates
        const presetPrefix = this.presetPrefixes.get(template);
        if (presetPrefix) {
            await this.handlePresetPrefix(ctx, template, presetPrefix);
        }

        ctx.answerCbQuery();
    }

    async handlePresetPrefix(ctx, templateType, presetPrefix) {
        const userId = ctx.from.id;
        const userSession = this.userSessions.get(userId);

        if (templateType === 'telegram') {
            userSession.waitingForPrefix = true;
            userSession.prefixMode = 'preset';
            
            await ctx.replyWithMarkdown(
                `📱 *TELEGRAM PREFIX TEMPLATE*\n\n` +
                `Template: \`TELEGRAM @username\`\n\n` +
                `Silakan ketik username Telegram Anda (tanpa @):\n` +
                `Contoh: missyhot22\n\n` +
                `Hasil: TELEGRAM @missyhot22_001.jpg`,
                Markup.forceReply()
            );
        } else {
            userSession.waitingForPrefix = true;
            userSession.prefixMode = 'preset';
            const platformName = templateType.toUpperCase();
            
            await ctx.replyWithMarkdown(
                `📱 *${platformName} PREFIX TEMPLATE*\n\n` +
                `Template: \`${presetPrefix}\`\n\n` +
                `Silakan ketik username ${platformName} Anda (tanpa @):\n` +
                `Contoh: username\n\n` +
                `Hasil: ${presetPrefix.replace('@username', '@')}[username]_001.jpg`,
                Markup.forceReply()
            );
        }
    }

    async handleSetPrefix(ctx, prefixInput, prefixType = 'custom') {
        const userId = ctx.from.id;
        const userSession = this.userSessions.get(userId);
        
        // Validasi prefix
        if (!prefixInput || prefixInput.trim().length === 0) {
            return ctx.reply('❌ Prefix tidak boleh kosong!');
        }

        let finalPrefix = prefixInput.trim();
        
        // Handle preset template completion
        if (userSession?.prefixMode === 'preset' && userSession.waitingForPrefix) {
            const lastMessage = ctx.message?.reply_to_message?.text;
            if (lastMessage?.includes('TELEGRAM')) {
                finalPrefix = `TELEGRAM @${prefixInput}`;
                prefixType = 'preset';
            } else if (lastMessage?.includes('INSTAGRAM')) {
                finalPrefix = `INSTAGRAM @${prefixInput}`;
                prefixType = 'preset';
            } else if (lastMessage?.includes('TWITTER')) {
                finalPrefix = `TWITTER @${prefixInput}`;
                prefixType = 'preset';
            } else if (lastMessage?.includes('TIKTOK')) {
                finalPrefix = `TIKTOK @${prefixInput}`;
                prefixType = 'preset';
            } else if (lastMessage?.includes('ONLYFANS')) {
                finalPrefix = `ONLYFANS @${prefixInput}`;
                prefixType = 'preset';
            }
            
            userSession.waitingForPrefix = false;
            userSession.prefixMode = null;
        }

        // Clean prefix (replace spaces with underscores for filename safety)
        finalPrefix = finalPrefix.replace(/[<>:"/\\|?*]/g, '').substring(0, 50);
        
        if (finalPrefix.length === 0) {
            return ctx.reply('❌ Prefix tidak valid! Hindari karakter khusus: < > : " / \\ | ? *');
        }

        // Get user settings atau buat baru
        let userSettings = this.userSettings.get(userId);
        if (!userSettings) {
            userSettings = {
                prefix: 'file',
                prefixType: 'simple',
                autoRename: true,
                autoUpload: true,
                autoCleanup: false,
                notifications: true
            };
        }

        // Simpan prefix baru
        const oldPrefix = userSettings.prefix;
        userSettings.prefix = finalPrefix;
        userSettings.prefixType = prefixType;
        this.userSettings.set(userId, userSettings);
        
        // Save to file
        this.saveUserSettings();

        let typeInfo = '';
        if (prefixType === 'preset') {
            typeInfo = ' (Template Preset)';
        } else if (prefixType === 'custom') {
            typeInfo = ' (Custom)';
        }

        const successText = `✅ *PREFIX BERHASIL DIUBAH!*${typeInfo}\n\n` +
                           `**Dari:** ${oldPrefix}\n` +
                           `**Menjadi:** ${finalPrefix}\n\n` +
                           `*Contoh file:*\n` +
                           `• ${finalPrefix}_001.jpg\n` +
                           `• ${finalPrefix}_002.mp4\n` +
                           `• ${finalPrefix}_003.png\n\n` +
                           `Prefix ini akan digunakan untuk semua download selanjutnya.`;

        await ctx.replyWithMarkdown(successText);
        
        // Update user session
        if (userSession) {
            userSession.waitingForPrefix = false;
            userSession.prefixMode = null;
        }
    }

    getUserPrefix(userId) {
        const userSettings = this.userSettings.get(userId);
        return userSettings?.prefix || 'file';
    }

    getPrefixType(userId) {
        const userSettings = this.userSettings.get(userId);
        return userSettings?.prefixType || 'simple';
    }

    // ==================== CORE FUNCTIONALITY ====================

    async handleMegaDownload(ctx, megaUrl) {
        const userId = ctx.from.id;
        const userPrefix = this.getUserPrefix(userId);
        const prefixType = this.getPrefixType(userId);
        const jobId = `job_${Date.now()}_${userId}`;

        // Validasi URL MEGA
        if (!this.isValidMegaUrl(megaUrl)) {
            return ctx.reply('❌ URL MEGA tidak valid! Format: https://mega.nz/folder/FOLDER_ID#FOLDER_KEY');
        }

        // Update user session
        const userSession = this.userSessions.get(userId);
        userSession.lastCommand = 'download';
        userSession.lastMegaUrl = megaUrl;

        // Create download job
        const downloadJob = {
            id: jobId,
            userId: userId,
            chatId: ctx.chat.id,
            type: 'mega_download',
            megaUrl: megaUrl,
            prefix: userPrefix,
            prefixType: prefixType,
            status: 'starting',
            progress: 'Memulai download...',
            createdAt: new Date(),
            messageId: null
        };

        this.activeDownloads.set(jobId, downloadJob);

        // Send initial status message
        let typeBadge = '';
        if (prefixType === 'preset') {
            typeBadge = ' 🎯';
        } else if (prefixType === 'custom') {
            typeBadge = ' ✏️';
        }

        const statusMsg = await ctx.replyWithMarkdown(
            `📥 *DOWNLOAD STARTED*${typeBadge}\n\n` +
            `🆔 *Job ID:* ${jobId}\n` +
            `👤 *User:* ${ctx.from.first_name}\n` +
            `🎯 *Prefix:* ${userPrefix}\n` +
            `📁 *URL:* ${megaUrl}\n` +
            `📊 *Status:* ${downloadJob.status}\n` +
            `⏳ *Progress:* ${downloadJob.progress}\n\n` +
            `_Menyiapkan download..._`
        );

        downloadJob.messageId = statusMsg.message_id;

        // Add cancel button
        await ctx.telegram.editMessageReplyMarkup(
            ctx.chat.id,
            statusMsg.message_id,
            undefined,
            Markup.inlineKeyboard([
                [Markup.button.callback('❌ Cancel Download', `cancel_${jobId}`)]
            ]).reply_markup
        );

        // Start download process
        this.processMegaDownload(downloadJob);
    }

    async processMegaDownload(job) {
        try {
            const userId = job.userId;
            const userPrefix = job.prefix || this.getUserPrefix(userId);
            const userSession = this.userSessions.get(userId);
            const userDir = path.join(this.downloadBase, 'users', userId.toString());
            
            if (!fs.existsSync(userDir)) {
                fs.mkdirSync(userDir, { recursive: true });
            }

            const folderName = `mega_${Date.now()}`;
            const downloadPath = path.join(userDir, folderName);

            // Update status
            job.status = 'downloading';
            job.progress = 'Connecting to MEGA...';
            await this.updateProgressMessage(job);

            // Create download directory
            if (!fs.existsSync(downloadPath)) {
                fs.mkdirSync(downloadPath, { recursive: true });
            }

            // Download using mega-get
            job.progress = 'Starting download...';
            await this.updateProgressMessage(job);

            const megaProcess = spawn('mega-get', [job.megaUrl], {
                cwd: downloadPath,
                stdio: ['pipe', 'pipe', 'pipe']
            });

            let downloadOutput = '';

            megaProcess.stdout.on('data', async (data) => {
                const message = data.toString().trim();
                downloadOutput += message + '\n';
                
                if (message.includes('%') || message.includes('Downloading')) {
                    job.progress = `Downloading: ${message}`;
                    await this.updateProgressMessage(job);
                }
            });

            megaProcess.stderr.on('data', async (data) => {
                const error = data.toString().trim();
                console.log(`MEGA Error for ${job.id}:`, error);
                
                if (error.includes('ERROR') || error.includes('Failed')) {
                    job.progress = `Error: ${error}`;
                    await this.updateProgressMessage(job);
                }
            });

            megaProcess.on('close', async (code) => {
                if (code === 0) {
                    job.status = 'processing';
                    job.progress = 'Download completed, renaming files...';
                    await this.updateProgressMessage(job);

                    // Count files
                    const fileCount = await this.countFiles(downloadPath);
                    
                    // Auto rename files dengan prefix user
                    const renameResult = await this.autoRenameFiles(downloadPath, userPrefix);
                    
                    job.status = 'completed';
                    job.progress = `Completed! ${fileCount} files downloaded, ${renameResult.renamed} renamed with prefix "${userPrefix}"`;
                    job.result = {
                        downloadPath,
                        fileCount,
                        renamedCount: renameResult.renamed,
                        totalFiles: renameResult.total,
                        prefixUsed: userPrefix,
                        prefixType: job.prefixType
                    };

                    await this.updateProgressMessage(job);
                    
                    // Add to user history
                    userSession.downloadHistory.push({
                        jobId: job.id,
                        type: 'mega_download',
                        fileCount: fileCount,
                        prefix: userPrefix,
                        prefixType: job.prefixType,
                        success: true,
                        completedAt: new Date()
                    });

                    // Send completion message
                    await this.sendCompletionMessage(job);

                } else {
                    job.status = 'failed';
                    job.error = `Download failed with code ${code}`;
                    await this.updateProgressMessage(job);
                }
            });

            megaProcess.on('error', async (error) => {
                job.status = 'failed';
                job.error = `Process error: ${error.message}`;
                await this.updateProgressMessage(job);
            });

            // Timeout protection
            setTimeout(() => {
                if (job.status === 'downloading') {
                    megaProcess.kill();
                    job.status = 'failed';
                    job.error = 'Download timeout (30 minutes)';
                    this.updateProgressMessage(job);
                }
            }, 30 * 60 * 1000);

        } catch (error) {
            job.status = 'failed';
            job.error = error.message;
            await this.updateProgressMessage(job);
        }
    }

    // ==================== UTILITY METHODS ====================

    isValidMegaUrl(url) {
        return url.includes('mega.nz/folder/') && url.includes('#');
    }

    async downloadMegaFolder(megaUrl, downloadPath, job) {
        return new Promise((resolve, reject) => {
            if (!fs.existsSync(downloadPath)) {
                fs.mkdirSync(downloadPath, { recursive: true });
            }

            const megaProcess = spawn('mega-get', [megaUrl], {
                cwd: downloadPath
            });

            megaProcess.on('close', (code) => {
                if (code === 0) {
                    resolve();
                } else {
                    reject(new Error(`Download failed with code ${code}`));
                }
            });

            megaProcess.on('error', reject);
        });
    }

    async autoRenameFiles(folderPath, prefix) {
        try {
            const files = this.getAllFiles(folderPath)
                .filter(file => {
                    const ext = path.extname(file).toLowerCase();
                    return ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.avi', '.mov', '.mkv', '.webp'].includes(ext);
                })
                .sort();

            let renamedCount = 0;

            files.forEach((filePath, index) => {
                const ext = path.extname(filePath);
                const dir = path.dirname(filePath);
                const newName = `${prefix}_${(index + 1).toString().padStart(3, '0')}${ext}`;
                const newPath = path.join(dir, newName);

                try {
                    if (filePath !== newPath) {
                        fs.renameSync(filePath, newPath);
                        renamedCount++;
                    }
                } catch (error) {
                    console.log(`Failed to rename: ${filePath}`);
                }
            });

            return { renamed: renamedCount, total: files.length };
        } catch (error) {
            return { renamed: 0, total: 0, error: error.message };
        }
    }

    getAllFiles(dir) {
        let results = [];
        try {
            const items = fs.readdirSync(dir);
            for (const item of items) {
                const fullPath = path.join(dir, item);
                const stat = fs.statSync(fullPath);
                if (stat.isDirectory()) {
                    results = results.concat(this.getAllFiles(fullPath));
                } else {
                    results.push(fullPath);
                }
            }
        } catch (error) {
            console.error('Error reading directory:', error.message);
        }
        return results;
    }

    async countFiles(dir) {
        const files = this.getAllFiles(dir);
        return files.length;
    }

    async cleanupFolder(folderPath) {
        try {
            if (fs.existsSync(folderPath)) {
                fs.rmSync(folderPath, { recursive: true, force: true });
                return true;
            }
            return false;
        } catch (error) {
            console.error('Cleanup error:', error.message);
            return false;
        }
    }

    delay(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    // ==================== TELEGRAM MESSAGE METHODS ====================

    async updateProgressMessage(job) {
        try {
            const statusText = this.getStatusText(job);
            await this.bot.telegram.editMessageText(
                job.chatId,
                job.messageId,
                undefined,
                statusText,
                { 
                    parse_mode: 'Markdown',
                    reply_markup: job.status === 'completed' || job.status === 'failed' ? 
                        { inline_keyboard: [] } : 
                        Markup.inlineKeyboard([
                            [Markup.button.callback('❌ Cancel', `cancel_${job.id}`)]
                        ]).reply_markup
                }
            );
        } catch (error) {
            console.error('Error updating progress message:', error.message);
        }
    }

    getStatusText(job) {
        let typeBadge = '';
        if (job.prefixType === 'preset') {
            typeBadge = ' 🎯';
        } else if (job.prefixType === 'custom') {
            typeBadge = ' ✏️';
        }

        const baseText = `🆔 *Job ID:* ${job.id}\n` +
                        `👤 *User:* ${this.getUserName(job.userId)}\n` +
                        `🎯 *Prefix:* ${job.prefix || this.getUserPrefix(job.userId)}${typeBadge}\n` +
                        `📊 *Status:* ${this.getStatusEmoji(job.status)} ${job.status}\n` +
                        `⏳ *Progress:* ${job.progress}\n` +
                        `🕐 *Started:* ${job.createdAt.toLocaleTimeString()}`;

        if (job.type === 'quick_pipeline') {
            return `⚡ *QUICK PIPELINE*\n\n${baseText}`;
        } else {
            return `📥 *DOWNLOAD STATUS*\n\n${baseText}`;
        }
    }

    getStatusEmoji(status) {
        const emojis = {
            'starting': '🟡',
            'downloading': '📥',
            'processing': '🔄',
            'renaming': '📝',
            'uploading': '📤',
            'cleaning': '🧹',
            'completed': '✅',
            'failed': '❌'
        };
        return emojis[status] || '⚪';
    }

    getUserName(userId) {
        const session = this.userSessions.get(userId);
        return session?.firstName || `User_${userId}`;
    }

    async sendCompletionMessage(job) {
        const userPrefix = job.prefix || this.getUserPrefix(job.userId);
        let typeInfo = '';
        if (job.prefixType === 'preset') {
            typeInfo = ' 🎯';
        } else if (job.prefixType === 'custom') {
            typeInfo = ' ✏️';
        }

        const completionText = `🎉 *DOWNLOAD COMPLETED!*${typeInfo}\n\n` +
                              `🆔 *Job ID:* ${job.id}\n` +
                              `🎯 *Prefix Used:* ${userPrefix}\n` +
                              `📁 *Files Downloaded:* ${job.result.fileCount}\n` +
                              `🔄 *Files Renamed:* ${job.result.renamedCount}\n` +
                              `⏱️ *Duration:* ${this.getDuration(job.createdAt)}\n\n` +
                              `✅ Proses selesai dengan sukses!`;

        await this.bot.telegram.sendMessage(job.chatId, completionText, { parse_mode: 'Markdown' });
    }

    // ==================== USER MANAGEMENT ====================

    async showUserStatus(ctx) {
        const userId = ctx.from.id;
        const userJobs = Array.from(this.activeDownloads.values())
            .filter(job => job.userId === userId);

        if (userJobs.length === 0) {
            return ctx.reply('📊 Tidak ada download aktif saat ini.');
        }

        let statusText = `📊 *YOUR ACTIVE DOWNLOADS*\n\n`;
        
        userJobs.forEach(job => {
            let typeBadge = '';
            if (job.prefixType === 'preset') {
                typeBadge = ' 🎯';
            } else if (job.prefixType === 'custom') {
                typeBadge = ' ✏️';
            }

            statusText += `🆔 *${job.id}*\n`;
            statusText += `📁 *Type:* ${job.type}\n`;
            statusText += `🎯 *Prefix:* ${job.prefix || this.getUserPrefix(userId)}${typeBadge}\n`;
            statusText += `📊 *Status:* ${this.getStatusEmoji(job.status)} ${job.status}\n`;
            statusText += `⏳ *Progress:* ${job.progress}\n`;
            statusText += `🕐 *Started:* ${job.createdAt.toLocaleTimeString()}\n\n`;
        });

        await ctx.replyWithMarkdown(statusText);
    }

    async showUserStats(ctx) {
        const userId = ctx.from.id;
        const userSession = this.userSessions.get(userId);
        const userSettings = this.userSettings.get(userId);
        const currentPrefix = userSettings?.prefix || 'file';
        const prefixType = userSettings?.prefixType || 'simple';
        const userJobs = Array.from(this.activeDownloads.values())
            .filter(job => job.userId === userId);

        let typeInfo = '';
        if (prefixType === 'preset') {
            typeInfo = ' (Template Preset)';
        } else if (prefixType === 'custom') {
            typeInfo = ' (Custom)';
        }

        const statsText = `📈 *YOUR STATISTICS*\n\n` +
                         `👤 *User:* ${ctx.from.first_name}\n` +
                         `🎯 *Current Prefix:* ${currentPrefix}${typeInfo}\n` +
                         `🆔 *User ID:* ${userId}\n` +
                         `📅 *Member Since:* ${userSession.createdAt.toLocaleDateString()}\n` +
                         `📥 *Total Downloads:* ${userSession.downloadHistory.length}\n` +
                         `⚡ *Active Jobs:* ${userJobs.length}\n` +
                         `✅ *Successful:* ${userSession.downloadHistory.filter(d => d.success).length}\n` +
                         `❌ *Failed:* ${userSession.downloadHistory.filter(d => !d.success).length}\n\n` +
                         `💪 Terus gunakan bot ini!`;

        await ctx.replyWithMarkdown(statsText);
    }

    async showSettingsMenu(ctx) {
        const userId = ctx.from.id;
        const userSettings = this.userSettings.get(userId);
        const currentPrefix = userSettings?.prefix || 'file';
        const prefixType = userSettings?.prefixType || 'simple';

        let typeInfo = '';
        if (prefixType === 'preset') {
            typeInfo = ' 🎯';
        } else if (prefixType === 'custom') {
            typeInfo = ' ✏️';
        }

        const settingsText = `⚙️ *USER SETTINGS*\n\n` +
                            `**Current Prefix:** ${currentPrefix}${typeInfo}\n` +
                            `**Prefix Type:** ${prefixType}\n\n` +
                            `Atur preferensi Anda:\n\n` +
                            `1. 🎯 Set Custom Prefix\n` +
                            `2. 🔄 Auto-rename files\n` +
                            `3. 📤 Auto-upload to Terabox\n` +
                            `4. 🧹 Auto-cleanup files\n` +
                            `5. 🔔 Notifications`;

        const keyboard = Markup.inlineKeyboard([
            [Markup.button.callback('🎯 Set Prefix', 'setting_set_prefix')],
            [Markup.button.callback('🔄 Auto-rename', 'setting_auto_rename')],
            [Markup.button.callback('📤 Auto-upload', 'setting_auto_upload')],
            [Markup.button.callback('🧹 Auto-cleanup', 'setting_auto_cleanup')],
            [Markup.button.callback('🔔 Notifications', 'setting_notifications')]
        ]);

        await ctx.replyWithMarkdown(settingsText, keyboard);
    }

    handleSettingsAction(ctx, action) {
        const userId = ctx.from.id;
        
        switch (action) {
            case 'set_prefix':
                this.showPrefixTemplateMenu(ctx);
                break;
            default:
                ctx.answerCbQuery(`Setting ${action} clicked!`);
                ctx.reply(`⚙️ Setting "${action}" akan segera diimplementasi!`);
        }
    }

    async handleCancelDownload(ctx) {
        const userId = ctx.from.id;
        const userJobs = Array.from(this.activeDownloads.values())
            .filter(job => job.userId === userId && 
                      ['starting', 'downloading', 'processing'].includes(job.status));

        if (userJobs.length === 0) {
            return ctx.reply('❌ Tidak ada download yang bisa dibatalkan.');
        }

        if (userJobs.length === 1) {
            await this.cancelUserJob(ctx, userJobs[0].id);
        } else {
            let cancelText = `❌ *PILIH DOWNLOAD UNTUK DIBATALKAN*\n\n`;
            const keyboard = [];

            userJobs.forEach(job => {
                cancelText += `🆔 ${job.id}\n📁 ${job.type}\n⏳ ${job.progress}\n\n`;
                keyboard.push([Markup.button.callback(`Cancel ${job.id}`, `cancel_${job.id}`)]);
            });

            await ctx.replyWithMarkdown(cancelText, Markup.inlineKeyboard(keyboard));
        }
    }

    async cancelUserJob(ctx, jobId) {
        const job = this.activeDownloads.get(jobId);
        
        if (!job) {
            await ctx.answerCbQuery('Job tidak ditemukan!');
            return;
        }

        if (job.userId !== ctx.from.id) {
            await ctx.answerCbQuery('Anda tidak memiliki akses ke job ini!');
            return;
        }

        job.status = 'cancelled';
        job.progress = 'Dibatalkan oleh user';
        
        await this.updateProgressMessage(job);
        await ctx.answerCbQuery('✅ Download dibatalkan!');
        
        setTimeout(() => {
            this.activeDownloads.delete(jobId);
        }, 5000);
    }

    async handleFileUpload(ctx) {
        await ctx.reply('📤 Fitur upload file manual akan segera tersedia!');
    }

    getDuration(startTime) {
        const diff = Date.now() - startTime.getTime();
        const minutes = Math.floor(diff / 60000);
        const seconds = Math.floor((diff % 60000) / 1000);
        return `${minutes}m ${seconds}s`;
    }

    // ==================== BOT CONTROLS ====================

    start() {
        console.log('🚀 Starting Telegram Bot with Advanced Prefix Features...');
        this.bot.launch()
            .then(() => {
                console.log('✅ Telegram Bot is running!');
            })
            .catch(err => {
                console.error('❌ Failed to start bot:', err);
            });

        process.once('SIGINT', () => this.stop());
        process.once('SIGTERM', () => this.stop());
    }

    stop() {
        console.log('🛑 Stopping Telegram Bot...');
        this.bot.stop();
    }
}

// ==================== MAIN EXECUTION ====================

if (require.main === module) {
    if (!process.env.TELEGRAM_BOT_TOKEN) {
        console.error('❌ ERROR: TELEGRAM_BOT_TOKEN is required in .env file');
        console.log('💡 Get token from @BotFather on Telegram');
        process.exit(1);
    }

    if (!process.env.MEGA_EMAIL_1 || process.env.MEGA_EMAIL_1.includes('your_')) {
        console.warn('⚠️  WARNING: MEGA_EMAIL_1 not configured in .env');
    }

    if (!process.env.TERABOX_EMAIL || process.env.TERABOX_EMAIL.includes('your_')) {
        console.warn('⚠️  WARNING: TERABOX_EMAIL not configured in .env');
    }

    const telegramBot = new TelegramTeraboxBot();
    telegramBot.start();
}

module.exports = TelegramTeraboxBot;
