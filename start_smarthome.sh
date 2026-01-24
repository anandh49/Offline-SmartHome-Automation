#!/bin/bash

# --- PART 1: WAIT FOR SYSTEM ---
# CRITICAL: Wait 15 seconds for NetworkManager to fully start
echo "Waiting for network services..."
sleep 15

# --- PART 2: NETWORK RESET ---
echo "Resetting Network..."

# Define the full path to nmcli to prevent 'command not found' errors
NMCLI=/usr/bin/nmcli

# 1. Delete old Hotspot (Hide errors if it doesn't exist)
sudo $NMCLI connection delete Hotspot 2>/dev/null
sudo $NMCLI connection delete Smarthome-HUB 2>/dev/null

# 2. Create the Hotspot
# We add 'con-name Hotspot' to FORCE the internal name to be 'Hotspot'
# This guarantees the next commands will find it.
sudo $NMCLI device wifi hotspot ssid "Smarthome-HUB" password "raspberry.connect" ifname wlan0 band bg con-name Hotspot

# 3. Apply your additions (Priority & Auto Connect)
sudo $NMCLI connection modify Hotspot connection.autoconnect-priority 1
sudo $NMCLI connection modify Hotspot connection.autoconnect yes

# 4. Disable PMF (Critical for ESP32)
sudo $NMCLI connection modify Hotspot wifi-sec.pmf disable

# 5. Fix IP Address (Critical for ESP32)
sudo $NMCLI connection modify Hotspot ipv4.addresses 10.42.0.1/24
sudo $NMCLI connection modify Hotspot ipv4.method shared

# 6. Restart the connection to apply everything
sudo $NMCLI connection down Hotspot
sudo $NMCLI connection up Hotspot

# 7. Restart Mosquitto to ensure it sees the new network
sleep 5
sudo systemctl restart mosquitto
sleep 2

# --- PART 3: START APP ---
echo "Starting Home Automation..."

# Navigate to project folder
cd /home/xen4os/smarthome

# Activate Virtual Environment
source venv/bin/activate

# Run the App
exec python -u app.py