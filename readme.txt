sudo apt update && sudo apt upgrade -y

sudo apt install -y python3-venv python3-dev build-essential ffmpeg mosquitto mosquitto-clients
sudo systemctl enable mosquitto
sudo systemctl start mosquito

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

…………………………………………………………………………………………………………
if execstack error:

sudo apt install patchelf
sudo patchelf --clear-execstack /home/xen4os/smarthome/venv/lib/python3.13/site-packages/vosk/libvosk.so