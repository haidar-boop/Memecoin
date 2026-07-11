#!/usr/bin/env bash
# One-time bootstrap for a fresh DigitalOcean droplet (Ubuntu 22.04/24.04).
# Run as the deploy user (not root) from the repo root after cloning:
#   git clone <your-repo-url> ~/meme-intelligence
#   cd ~/meme-intelligence
#   bash deploy/setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Installing system packages (python3, venv, git)"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Creating virtualenv at $REPO_DIR/.venv"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "==> No .env found. Copying .env.example -> .env"
  echo "    EDIT .env NOW and fill in your real secrets before starting the service."
  cp .env.example .env
fi

mkdir -p logs data

echo "==> Installing systemd service"
sudo cp deploy/meme-intelligence.service /etc/systemd/system/meme-intelligence.service
sudo sed -i "s#__REPO_DIR__#$REPO_DIR#g" /etc/systemd/system/meme-intelligence.service
sudo sed -i "s#__USER__#$(whoami)#g" /etc/systemd/system/meme-intelligence.service
sudo systemctl daemon-reload
sudo systemctl enable meme-intelligence

echo ""
echo "==> Done. Next steps:"
echo "    1. Edit $REPO_DIR/.env with your real API keys/tokens (nano .env)"
echo "    2. Start it:   sudo systemctl start meme-intelligence"
echo "    3. Watch logs: journalctl -u meme-intelligence -f"
