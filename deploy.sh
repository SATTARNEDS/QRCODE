#!/bin/bash
# =============================================================
#  Oracle Cloud Always Free — QR Tracker Setup Script
#  รัน script นี้บน Oracle Cloud Ubuntu VM ครั้งแรกครั้งเดียว
#  คำสั่ง: bash deploy.sh
# =============================================================

set -e

REPO_URL="https://github.com/SATTARNEDS/QRCODE.git"
APP_DIR="/opt/qrcode"
DATA_DIR="/opt/qrcode-data"
SERVICE_NAME="qrcode"
APP_USER="ubuntu"   # Oracle Cloud ใช้ user ชื่อ ubuntu

echo "========================================"
echo " QR Tracker — Oracle Cloud Deploy"
echo "========================================"

# 1. อัปเดต package และติดตั้งสิ่งที่จำเป็น
echo "[1/8] Installing packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv nginx git iptables-persistent

# 2. สร้าง data directory สำหรับเก็บ database (persistent)
echo "[2/8] Creating persistent data directory at $DATA_DIR..."
sudo mkdir -p "$DATA_DIR"
sudo chown "$APP_USER":"$APP_USER" "$DATA_DIR"

# 3. Clone หรืออัปเดต repo
echo "[3/8] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    sudo git pull
else
    sudo git clone "$REPO_URL" "$APP_DIR"
fi
sudo chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# 4. ตั้งค่า Python virtual environment
echo "[4/8] Setting up Python venv..."
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 5. สร้าง SECRET_KEY แบบสุ่ม (ถ้ายังไม่มี)
echo "[5/8] Generating secret key..."
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "SECRET_KEY generated (จะถูกใส่ใน systemd service)"

# 6. สร้าง systemd service
echo "[6/8] Creating systemd service..."
sudo tee /etc/systemd/system/"$SERVICE_NAME".service > /dev/null <<EOF
[Unit]
Description=QR Code Tracker (Flask/Gunicorn)
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment="DATA_DIR=$DATA_DIR"
Environment="SECRET_KEY=$SECRET_KEY"
ExecStart=$APP_DIR/.venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 --timeout 60 app:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 7. ตั้งค่า Nginx reverse proxy
echo "[7/8] Configuring Nginx..."
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me || echo "_")
sudo tee /etc/nginx/sites-available/"$SERVICE_NAME" > /dev/null <<EOF
server {
    listen 80;
    server_name $PUBLIC_IP _;

    # ขนาดสูงสุดของ request
    client_max_body_size 5M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/"$SERVICE_NAME" /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

# 8. เปิด port 80 ใน firewall (Oracle Cloud ใช้ iptables)
echo "[8/8] Opening firewall ports..."
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

# เริ่ม service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl restart nginx

echo ""
echo "========================================"
echo " ✅ Deploy สำเร็จ!"
echo "========================================"
echo " URL : http://$PUBLIC_IP"
echo " DB  : $DATA_DIR/qrtrack.db"
echo " Log : sudo journalctl -u $SERVICE_NAME -f"
echo "========================================"
echo ""
echo " ⚠️  อย่าลืมไปเปิด port 80 ที่"
echo "    Oracle Cloud Console → VCN → Security List"
