#!/bin/bash
# =============================================================
#  อัปเดตแอปบน Oracle Cloud (ใช้หลังจาก git push ทุกครั้ง)
#  คำสั่ง: bash update.sh
# =============================================================

set -e

APP_DIR="/opt/qrcode"
SERVICE_NAME="qrcode"

echo "Pulling latest code..."
cd "$APP_DIR"
sudo git pull

echo "Installing/updating dependencies..."
.venv/bin/pip install -r requirements.txt

echo "Restarting service..."
sudo systemctl restart "$SERVICE_NAME"

echo "✅ Updated! Status:"
sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -20
