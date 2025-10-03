const TeraboxProBot = require('./terabox-pro-bot');
const readline = require('readline');
require('dotenv').config();

class CLIInterface {
    constructor() {
        this.bot = null;
        this.rl = readline.createInterface({
            input: process.stdin,
            output: process.stdout
        });
    }

    async initialize() {
        console.log('🚀 Starting Terabox Pro Bot CLI...\n');
        this.bot = new TeraboxProBot();
        await this.showMainMenu();
    }

    showMainMenu() {
        console.log('\n=== TERABOX PRO BOT ===');
        console.log('1. 📥 Download from MEGA');
        console.log('2. 📤 Upload folder to Terabox');
        console.log('3. ⚡ Automated Pipeline');
        console.log('4. 📊 System Status');
        console.log('5. 📋 Job Status');
        console.log('6. 🗑️  Cancel Job');
        console.log('7. ❌ Exit');
        console.log('=======================\n');
        
        this.askQuestion('Choose option (1-7): ', this.handleMainMenu.bind(this));
    }

    async handleMainMenu(choice) {
        switch (choice) {
            case '1':
                await this.downloadFromMega();
                break;
            case '2':
                await this.uploadToTerabox();
                break;
            case '3':
                await this.automatedPipeline();
                break;
            case '4':
                await this.showSystemStatus();
                break;
            case '5':
                await this.showJobStatus();
                break;
            case '6':
                await this.cancelJob();
                break;
            case '7':
                await this.exit();
                return;
            default:
                console.log('❌ Invalid choice');
        }
        
        this.showMainMenu();
    }

    async downloadFromMega() {
        this.askQuestion('Enter MEGA folder URL: ', async (url) => {
            if (!url.includes('mega.nz/folder/')) {
                console.log('❌ Invalid MEGA URL format');
                return;
            }
            
            console.log('📥 Starting download...');
            const jobId = await this.bot.downloadFromMega(url);
            console.log(`✅ Job started: ${jobId}`);
            console.log('💡 Use "5. Job Status" to monitor progress');
        });
    }

    async uploadToTerabox() {
        this.askQuestion('Enter folder path to upload: ', async (folderPath) => {
            if (!require('fs').existsSync(folderPath)) {
                console.log('❌ Folder does not exist');
                return;
            }
            
            console.log('📤 Starting upload...');
            const jobId = await this.bot.uploadToTerabox(folderPath);
            console.log(`✅ Upload job started: ${jobId}`);
        });
    }

    async automatedPipeline() {
        this.askQuestion('Enter MEGA URL for pipeline: ', async (megaUrl) => {
            const jobId = await this.bot.automatedPipeline(megaUrl, {
                autoRename: true,
                autoUpload: true,
                prefix: 'auto',
                cleanup: true
            });
            console.log(`✅ Pipeline started: ${jobId}`);
            console.log('🔄 This will: Download → Rename → Upload → Cleanup');
        });
    }

    async showSystemStatus() {
        const status = this.bot.getSystemStatus();
        console.log('\n📊 SYSTEM STATUS:');
        console.log(JSON.stringify(status, null, 2));
    }

    async showJobStatus() {
        const jobs = this.bot.getAllJobs();
        
        console.log('\n📋 JOB STATUS:');
        console.log(`Queued: ${jobs.queued}`);
        console.log(`Active: ${jobs.active.length}`);
        console.log(`Completed: ${jobs.statistics.totalProcessed}`);
        
        if (jobs.active.length > 0) {
            console.log('\n🔄 ACTIVE JOBS:');
            jobs.active.forEach(job => {
                console.log(`• ${job.id} (${job.type}): ${job.status} - ${job.progress}`);
            });
        }
    }

    async cancelJob() {
        this.askQuestion('Enter Job ID to cancel: ', (jobId) => {
            const success = this.bot.cancelJob(jobId);
            console.log(success ? '✅ Job cancelled' : '❌ Job not found');
        });
    }

    askQuestion(question, callback) {
        this.rl.question(question, callback);
    }

    async exit() {
        console.log('\n🔚 Shutting down...');
        this.rl.close();
        process.exit(0);
    }
}

// Start CLI if run directly
if (require.main === module) {
    const cli = new CLIInterface();
    cli.initialize().catch(console.error);
}

module.exports = CLIInterface;
