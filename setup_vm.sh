#!/bin/bash
# ==============================================================================
# Azure VM Initialization Script for India Alternatives DMS
# Run this script whenever the VM is re-allocated to restore the AI environment.
# ==============================================================================

# Exit immediately if a command exits with a non-zero status.
set -e

echo "======================================================"
echo "Initializing India Alternatives AI Node"
echo "======================================================"

# 1. Configure the high-speed ephemeral disk (/mnt) for Ollama
echo "[1/4] Configuring Ephemeral Storage (/mnt)..."
sudo mkdir -p /mnt/ollama_models
sudo chown -R ollama:ollama /mnt/ollama_models
# Ensure directory persists permissions across reboots if it already existed
sudo chmod 775 /mnt/ollama_models

# 2. Update Ollama systemd service to use the new path and allow remote access
echo "[2/4] Updating Ollama Service Configuration..."
# Create the override directory if it doesn't exist
sudo mkdir -p /etc/systemd/system/ollama.service.d

# Write the override configuration
cat <<EOF | sudo tee /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_MODELS=/mnt/ollama_models"
Environment="OLLAMA_HOST=0.0.0.0"
EOF

# Reload and restart the service
sudo systemctl daemon-reload
sudo systemctl restart ollama

# Wait a moment for the service to bind to the port
sleep 5

# Verify service is running
if curl -s -f -m 5 http://127.0.0.1:11434/api/tags > /dev/null; then
    echo "  -> Ollama service is active and responding."
else
    echo "  -> ERROR: Ollama service failed to start."
    exit 1
fi

# 3. Pull Required Models (Non-blocking / Background)
# We use nohup and background execution (&) so the script finishes quickly
# while the large models download over the Azure backbone network.
echo "[3/4] Initiating Background Model Pulls..."

# The log file where you can check download progress
PULL_LOG="/var/log/ollama_pulls.log"
sudo touch $PULL_LOG
sudo chmod 666 $PULL_LOG
echo "Starting model pulls at $(date)" > $PULL_LOG

# Define the required models
MODELS=(
    "nomic-embed-text"
    "mistral-nemo:12b-instruct-2407-q8_0"
    "qwen2.5vl:7b-q8_0"
)

# Function to pull models sequentially in the background
pull_models() {
    for model in "${MODELS[@]}"; do
        echo "Pulling $model..." >> $PULL_LOG
        curl -s -X POST http://127.0.0.1:11434/api/pull -d "{\"name\": \"$model\"}" >> $PULL_LOG 2>&1
        echo "\nFinished $model at $(date)" >> $PULL_LOG
    done
    echo "All models successfully pulled and verified." >> $PULL_LOG
}

# Execute the function in the background
pull_models &

echo "  -> Model pulls initiated in background."
echo "  -> Check progress with: tail -f /var/log/ollama_pulls.log"

# 4. Clean up old OS disk storage (Frees up space on /dev/root)
echo "[4/4] Cleaning up old OS disk storage..."
if [ -d "/usr/share/ollama/.ollama/models" ]; then
    # We only delete contents, not the directory itself to avoid permission issues if Ollama expects it
    sudo rm -rf /usr/share/ollama/.ollama/models/*
    echo "  -> Cleared legacy storage."
fi

echo "======================================================"
echo "Initialization Complete!"
echo "The system will be fully operational in ~5-10 minutes."
echo "======================================================"
