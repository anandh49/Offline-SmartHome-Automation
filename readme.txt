sudo apt update && sudo apt upgrade -y

sudo apt install -y python3-venv python3-dev build-essential ffmpeg mosquitto mosquitto-clients
sudo systemctl enable mosquitto
sudo systemctl start mosquito

sudo nano /etc/mosquitto/mosquitto.conf

	listener 1883
	allow_anonymous true



python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt



run as service:

chmod +x /home/xen4os/smarthome/start_smarthome.sh

sudo nano /etc/systemd/system/smarthome.service
………………………………………………………………………………………………………….
   [Unit]
Description=Smart Home Voice Control Service
# Wait for the network to be fully online before starting
Wants=network-online.target
After=network-online.target network.target sound.target

[Service]
# Run as your specific user
User=xen4os
Group=xen4os

# The working directory
WorkingDirectory=/home/xen4os/smarthome

# The command to start your app (pointing to your script)
ExecStart=/home/xen4os/smarthome/start_smarthome.sh

# Auto-restart if it crashes
Restart=always
RestartSec=5

# Log handling
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
……………………………………………………………………………….
sudo systemctl daemon-reload
sudo systemctl enable smarthome.service
sudo systemctl start smarthome.service

journalctl -u smarthome.service -f
…………………………………………………………………………………………………………
hotspot->
sudo nmcli connection delete Hotspot
sudo nmcli device wifi hotspot ssid "Smarthome-HUB" password "raspberry.connect" ifname wlan0 band bg
sudo nmcli connection modify Hotspot wifi-sec.pmf disable
sudo nmcli connection down Hotspot
sudo nmcli connection up Hotspot
…………………………………………………………………………………………………………………………………………….
sudo date -s "2026-01-14 06:15:00"
