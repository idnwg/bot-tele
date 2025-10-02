#!/bin/bash

echo "🔧 Setting up Mega Downloader Bot on VPS..."

# Update system
sudo apt update && sudo apt upgrade -y

# Install system dependencies
sudo apt install -y python3-pip python3-venv ffmpeg snapd wget curl

# Install Mega.nz via snap
echo "📦 Installing Mega.nz CMD..."
sudo snap install mega-cmd

# Setup Mega.nz (initial configuration)
echo "🔐 Setting up Mega.nz..."
mega-login --version  # This will initialize mega-cmd

# Create project directory
mkdir -p ~/bot-tele
cd ~/bot-tele

# Create Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install python-telegram-bot python-dotenv requests aiohttp pillow

# Create necessary directories
mkdir -p downloads teraboxcli

# Create environment file
cat > .env << 'EOF'
BOT_TOKEN=your_telegram_bot_token_here
TERABOX_CONNECT_KEY=your_terabox_connect_key_optional
DOODSTREAM_API_KEY=your_doodstream_api_key_optional

# Mega.nz accounts (add more as needed)
MEGA_EMAIL_1=your_mega_email_1
MEGA_PASSWORD_1=your_mega_password_1
MEGA_EMAIL_2=your_mega_email_2  
MEGA_PASSWORD_2=your_mega_password_2
EOF

# Create mega_accounts.json (alternative to environment variables)
cat > mega_accounts.json << 'EOF'
[
  {
    "email": "your_mega_email_1",
    "password": "your_mega_password_1"
  },
  {
    "email": "your_mega_email_2",
    "password": "your_mega_password_2"
  }
]
EOF

# Download and setup teraboxcli
echo "📥 Setting up Terabox CLI..."
cd teraboxcli
cat > main.py << 'EOF'
#!/usr/bin/env python3
"""
Terabox CLI Tool - Placeholder
Replace with actual teraboxuploadercli implementation
"""
import sys
import json
import requests
from pathlib import Path

def upload_file(file_path: str) -> str:
    """Upload file to Terabox and return download link"""
    # TODO: Implement actual Terabox upload logic
    # This is a placeholder - replace with real teraboxuploadercli
    
    print(f"Uploading {file_path} to Terabox...")
    
    # Simulate upload process
    # In production, use: teraboxuploadercli upload file_path
    filename = Path(file_path).name
    return f"https://terabox.com/sharing/link?surl=abc123_{filename}"

if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'upload':
        file_path = sys.argv[2]
        result = upload_file(file_path)
        print(f"Upload successful: {result}")
    else:
        print("Usage: python main.py upload <file_path>")
EOF

cd ..

# Create bot.py (will be created separately)
echo "📝 Creating bot.py..."
# The bot.py content will be added separately

# Create systemd service for auto-start
echo "🔧 Creating systemd service..."
sudo tee /etc/systemd/system/mega-bot.service > /dev/null << EOF
[Unit]
Description=Mega Downloader Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/bot-tele
Environment=PATH=/root/bot-tele/venv/bin
ExecStart=/root/bot-tele/venv/bin/python3 /root/bot-tele/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Setup instructions
echo "✅ Setup completed!"
echo ""
echo "📝 NEXT STEPS:"
echo "1. Edit .env file: nano /root/bot-tele/.env"
echo "   - Add your BOT_TOKEN from BotFather"
echo "   - Add your Mega.nz account credentials"
echo "   - Add Terabox/Doodstream API keys if available"
echo ""
echo "2. Configure Mega.nz accounts:"
echo "   mega-login your_email@example.com your_password"
echo ""
echo "3. Start the bot:"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable mega-bot"
echo "   sudo systemctl start mega-bot"
echo ""
echo "4. Check status:"
echo "   sudo systemctl status mega-bot"
echo "   tail -f /root/bot-tele/bot.log"
echo ""
echo "5. For manual testing:"
echo "   cd /root/bot-tele"
echo "   source venv/bin/activate"
echo "   python bot.py"
