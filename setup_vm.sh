#!/bin/bash
# ==============================================================================
# Azure VM Setup Script for GPU Deployment (Django + Ollama)
# ==============================================================================
# TARGET: Ubuntu 22.04 LTS or 24.04 LTS
# GPU: NCas_T3_v4 (NVIDIA T4)
# ==============================================================================

set -e  # Exit on error
set -o pipefail # Fail if any part of a pipeline fails

echo "🚀 Starting VM initialization..."

# 1. Update & Upgrade
echo "📦 Updating system packages..."
sudo apt-get update && sudo apt-get upgrade -y

# 2. Install Essential Tools
echo "🛠️ Installing essential tools..."
sudo apt-get install -y \
    curl \
    git \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    unattended-upgrades \
    fail2ban

# 3. Configure Automatic Security Updates
echo "🛡️ Configuring automatic security updates..."
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

# 4. Install Docker
if ! command -v docker &> /dev/null; then
    echo "🐳 Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    echo "Docker installed successfully."
else
    echo "🐳 Docker already installed."
fi

# 5. Install NVIDIA Container Toolkit (GPU Support for Docker)
echo "🎮 Installing NVIDIA Container Toolkit..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

# Configure Docker to use NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 6. Configure Firewall (UFW)
echo "🧱 Configuring UFW firewall..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh        # Port 22
sudo ufw allow http       # Port 80
sudo ufw allow https      # Port 443
sudo ufw --force enable
echo "UFW enabled and configured (Ports 22, 80, 443 only)."

# 7. Create App Directory
echo "📁 Preparing application directory..."
mkdir -p ~/indian-alt
cd ~/indian-alt

echo "✅ VM Initialization Complete!"
echo "----------------------------------------------------------------"
echo "Next Steps:"
echo "1. Log out and log back in (to apply docker group membership)."
echo "2. Clone your repository into ~/indian-alt."
echo "3. Create a .env file with your production secrets."
echo "4. Run 'docker compose up -d'."
echo "----------------------------------------------------------------"
