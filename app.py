from flask import Flask, render_template, request, redirect, url_for, Response, session, flash
import paho.mqtt.client as mqtt
import datetime
import json
import os
import threading
import queue
from vosk import Model, KaldiRecognizer
from thefuzz import fuzz
import subprocess
from functools import wraps
import time
import audioop

# --- VOSK Configuration ---
VOSK_MODEL_PATH = "vosk-model-small-en-in-0.4"
if not os.path.exists(VOSK_MODEL_PATH):
    print(f"Vosk model not found at '{VOSK_MODEL_PATH}'.")
    exit(1)
model = Model(VOSK_MODEL_PATH)
SAMPLE_RATE = 16000

app = Flask(__name__)
app.secret_key = "super_secret_session_key"

# --- CREDENTIALS ---
USERNAME = "test1"
PASSWORD = "t1"

# --- MQTT Configuration ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_PUB_TOPIC = "home/control"
MQTT_DISCOVERY_TOPIC = "home/device_discovery"
MQTT_ASSIGNMENT_TOPIC = "home/room_assignment"
MQTT_CONFIG_TOPIC_PREFIX = "home/config/"
MQTT_VOICE_AUDIO_TOPIC = "home/voice/audio/"
MQTT_VOICE_COMMAND_TOPIC = "home/voice/command/"
MQTT_TRIGGER_TOPIC = "home/motion_trigger"
MQTT_STATUS_TOPIC = "home/status"

# --- Global Variables ---
device_states = {}
command_log = []
unassigned_devices = {}
device_room_map = {}
sse_subscribers = [] 
PRESET_DEVICES = ["Television","Air Conditioner","Internet","Home Theater","Cofee Maker","Main Light", "Fan","Speaker"]
CONFIG_PATH = os.path.dirname(os.path.realpath(__file__))
MOTION_TIMEOUT = datetime.timedelta(seconds=25)
last_motion_time = {}
vad_processors = {}

# --- Decorator for Login Protection ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Helper: Broadcast ---
def broadcast_update(data):
    dead_subscribers = []
    for q in sse_subscribers:
        try: q.put_nowait(data)
        except queue.Full: dead_subscribers.append(q)
    for q in dead_subscribers:
        if q in sse_subscribers: sse_subscribers.remove(q)

def load_config(filename):
    filepath = os.path.join(CONFIG_PATH, filename)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_config(data, filename):
    filepath = os.path.join(CONFIG_PATH, filename)
    with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)

def log_command(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} - {message}"
    command_log.insert(0, log_entry)
    print(log_entry)
    if len(command_log) > 100: command_log.pop()
    broadcast_update({"type": "log", "log": log_entry})

# --- TIME SYNC ROUTE ---
@app.route('/sync_time', methods=['POST'])
def sync_time():
    data = request.get_json()
    if data and 'time' in data:
        time_str = data['time'] 
        try:
            subprocess.run(["sudo", "date", "-s", time_str], check=True)
            subprocess.run(["sudo", "hwclock", "-w"], check=False)
            print(f"[TIME] Synced to {time_str}")
            return "Synced", 200
        except Exception as e:
            print(f"[TIME] Sync Error: {e}")
            return "Error", 500
    return "Invalid", 400

# --- SCHEDULER & MODE LOGIC ---
def execute_mode(mode_name, mode_data, turn_off_mode=False):
    action_text = "Deactivating" if turn_off_mode else "Activating"
    log_command(f"[MODE] {action_text} '{mode_name}'...")
    
    # 1. EXECUTE RELAY ACTIONS
    actions = mode_data.get("actions", {})
    device_switched = False
    
    for room, relays in actions.items():
        if room in device_states:
            for relay, state in relays.items():
                target_state = "OFF" if turn_off_mode else state
                current = device_states[room].get(relay, {}).get('status', 'OFF')
                
                if current != target_state:
                    device_states[room][relay]['status'] = target_state
                    client.publish(MQTT_PUB_TOPIC, f"{room}:{relay}:{target_state}")
                    broadcast_update({"type": "status_update", "room": room, "relay": relay, "status": target_state})
                    device_switched = True
                    time.sleep(0.1) 
    
    if device_switched:
        save_config(device_states, "device_config.json")

    # 2. PLAY MUSIC (Only if activating)
    if not turn_off_mode:
        song_id = mode_data.get("audio_id")
        if song_id:
            if device_switched:
                log_command(f"[MODE] Waiting for system voice...")
                time.sleep(3.5) 
            
            first_room = next(iter(mode_data.get("actions", {}))) if mode_data.get("actions") else None
            if not first_room and device_room_map:
                first_room = next(iter(device_room_map))

            if first_room:
                client.publish(MQTT_PUB_TOPIC, f"{first_room}:AUDIO:{song_id}")
                log_command(f"[MODE] Playing Song #{song_id} in {first_room}")
    
    feedback = f"Okay, {mode_name} mode deactivated" if turn_off_mode else f"Okay, switching to {mode_name} mode"
    broadcast_update({"type": "voice_feedback", "text": feedback})

def scheduler_loop():
    last_checked_minute = -1
    while True:
        now = datetime.datetime.now()
        current_time_str = now.strftime("%H:%M") 
        
        if now.minute != last_checked_minute:
            last_checked_minute = now.minute
            current_modes = load_config("modes.json")
            
            for name, data in current_modes.items():
                start_time = data.get("start_time")
                if start_time == current_time_str:
                    today_short = now.strftime("%a") 
                    if today_short in data.get("days", []):
                        execute_mode(name, data)
        time.sleep(1)

# --- Voice Logic ---
VOICE_COMMAND_STOP_WORDS = ['turn', 'set', 'the', 'to', 'a', 'is', 'in', 'on', 'off', 'open', 'close','start', 'stop', 'activate', 'deactivate', 'enable', 'disable', 'please','would', 'you', 'can', 'jarvis', 'and', 'of', 'it', 'for','yeah', "i'm", 'i', 'kill', 'my', 'device', 'switch']
FUZZY_MATCH_CONFIDENCE_THRESHOLD = 70 

class VadAudio:
    def __init__(self, room_id, aggressiveness=1):
        self.room_id = room_id
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(aggressiveness)
        except ImportError: self.vad = None
        self.buffer = bytearray()
        self.speech_buffer = bytearray()
        self.is_speaking = False
        self.frame_duration_ms = 30 
        self.frame_size = int(SAMPLE_RATE * (self.frame_duration_ms / 1000.0) * 2)

    def process_chunk(self, chunk):
        try:
            chunk = audioop.mul(chunk, 2, 4) 
        except Exception: pass

        if not self.vad: return
        self.buffer.extend(chunk)
        while len(self.buffer) >= self.frame_size:
            frame = self.buffer[:self.frame_size]
            del self.buffer[:self.frame_size]
            try:
                if self.vad.is_speech(frame, SAMPLE_RATE):
                    if not self.is_speaking:
                        self.is_speaking = True
                        log_command(f"[VAD] Speech detected in {self.room_id}.")
                    self.speech_buffer.extend(frame)
                elif self.is_speaking:
                    if len(self.speech_buffer) > 3200:
                        self.is_speaking = False
                        log_command(f"[VAD] Processing command from {self.room_id}...")
                        threading.Thread(target=process_voice_command, args=(self.room_id, self.speech_buffer)).start()
                    else:
                        self.is_speaking = False
                    self.speech_buffer = bytearray()
            except: pass

def find_matching_devices_fuzzy(text, room_devices):
    text_lower = text.lower()
    cleaned_words = [word for word in text_lower.split() if word not in VOICE_COMMAND_STOP_WORDS]
    cleaned_text = " ".join(cleaned_words)
    if not cleaned_text: return []
    matching_devices = []
    for relay, info in room_devices.items():
        if relay.startswith("relay") and "label" in info:
            label_lower = info["label"].lower()
            current_score = 0
            is_short = len(cleaned_text) <= 3
            if cleaned_text.replace(" ", "") == label_lower.replace(" ", ""): current_score = 100
            elif not is_short:
                current_score = max(fuzz.token_set_ratio(cleaned_text, label_lower), fuzz.token_sort_ratio(cleaned_text, label_lower))
            elif is_short:
                 if cleaned_text in label_lower.split(): current_score = 100
                 else: current_score = fuzz.token_sort_ratio(cleaned_text, label_lower)
            if current_score >= FUZZY_MATCH_CONFIDENCE_THRESHOLD: matching_devices.append((relay, current_score))
    if matching_devices: matching_devices.sort(key=lambda x: x[1], reverse=True)
    return matching_devices

def process_voice_command(room_id, audio_data):
    if not audio_data or room_id not in device_states: return
    
    current_modes = load_config("modes.json")
    mode_names = [m.lower() for m in current_modes.keys()]

    vocabulary = set(["turn", "switch", "on", "off", "party", "mode", "shutdown", "stop", "activate", "start", "execute", "set", "enable", "disable", "[unk]"])
    vocabulary.update(VOICE_COMMAND_STOP_WORDS)
    vocabulary.update(mode_names) 
    
    for r_id, devices in device_states.items():
        vocabulary.update(r_id.lower().replace("_", " ").split())
        for dev_data in devices.values():
            if isinstance(dev_data, dict) and "label" in dev_data:
                vocabulary.update(dev_data["label"].lower().split())

    grammar = json.dumps(list(vocabulary))
    rec = KaldiRecognizer(model, SAMPLE_RATE, grammar)
    rec.AcceptWaveform(bytes(audio_data))
    result = json.loads(rec.FinalResult())
    text = result.get('text', '')
    
    if not text: return
    log_command(f'[VOSK] Heard in {room_id}: "{text}"')
    
    command_text = text.lower()

    # --- MODE MATCHING ---
    best_mode = None
    best_score = 0
    for name in current_modes:
        score = fuzz.partial_ratio(name.lower(), command_text)
        if score > 85: 
            if score > best_score:
                best_score = score
                best_mode = name
    
    if best_mode:
        negatives = ["off", "stop", "deactivate", "disable", "kill", "end", "shutdown"]
        is_negative = any(word in command_text.split() for word in negatives)
        log_command(f"[VOICE] Matched Mode: '{best_mode}' (Negative: {is_negative})")
        execute_mode(best_mode, current_modes[best_mode], turn_off_mode=is_negative)
        return 

    # --- DEVICE MATCHING (FIXED TOGGLING) ---
    action = None
    if any(x in command_text.split() for x in ["on", "open", "start", "enable", "activate"]): action = "ON"
    elif any(x in command_text.split() for x in ["off", "close", "stop", "kill", "shutdown", "disable"]): action = "OFF"
    
    matching_devices = find_matching_devices_fuzzy(command_text, device_states[room_id])
    if matching_devices:
        top_score = matching_devices[0][1]
        cutoff_score = 99 if top_score == 100 else 70
        final_targets = [d for d in matching_devices if d[1] >= cutoff_score]
        
        executed_relays = set()
        switched_labels = [] 
        
        for relay, score in final_targets:
            if relay in executed_relays: continue 
            target_relay = relay
            
            # --- INTELLIGENT STATE CHECK ---
            current_status = device_states[room_id][target_relay].get('status', 'OFF')
            
            # If explicit command ("Turn ON") used, respect it. If ambiguous ("Fan"), toggle it.
            final_action = action
            if not final_action:
                final_action = "OFF" if current_status == "ON" else "ON"
            
            # SKIP if already in requested state
            if final_action == current_status:
                log_command(f"[SKIPPED] {device_states[room_id][target_relay]['label']} is already {final_action}")
                continue 
            # -------------------------------

            device_states[room_id][target_relay]['status'] = final_action
            label = device_states[room_id][target_relay]['label']
            
            log_command(f"[EXECUTE] {label} -> {final_action}")
            client.publish(MQTT_PUB_TOPIC, f"{room_id}:{target_relay}:{final_action}")
            broadcast_update({"type": "status_update", "room": room_id, "relay": target_relay, "status": final_action})
            
            executed_relays.add(relay)
            switched_labels.append(label)
            time.sleep(0.05) 
        
        if executed_relays:
            save_config(device_states, "device_config.json")
            if len(executed_relays) > 1:
                 first_label = switched_labels[0]
                 if all(l == first_label for l in switched_labels): feedback_text = f"Okay, {first_label}s turned {final_action.lower()}"
                 else: feedback_text = f"Okay, {len(executed_relays)} devices turned {final_action.lower()}"
            else:
                feedback_text = f"Okay, {switched_labels[0]} turned {final_action.lower()}"
            broadcast_update({"type": "voice_feedback", "text": feedback_text})
    else: log_command(f"[IGNORED] No matching device for '{command_text}'.")

def check_motion_timeouts():
    while True:
        now = datetime.datetime.now()
        updated = False
        for room_id, last_seen in list(last_motion_time.items()):
            if now - last_seen > MOTION_TIMEOUT:
                log_command(f"[MOTION] No motion in '{room_id}' for 25s.")
                if room_id in device_states:
                    for relay, data in device_states[room_id].items():
                        if relay.startswith('relay') and data.get('motion_control') and data.get('status') == 'ON':
                            device_states[room_id][relay]['status'] = 'OFF'
                            client.publish(MQTT_PUB_TOPIC, f"{room_id}:{relay}:OFF")
                            broadcast_update({"type": "status_update", "room": room_id, "relay": relay, "status": "OFF"})
                            updated = True
                            time.sleep(0.05)
                del last_motion_time[room_id]
        if updated: save_config(device_states, "device_config.json")
        threading.Event().wait(1.0)

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"Connected to MQTT Broker with code {rc}")
    client.subscribe(f"{MQTT_VOICE_AUDIO_TOPIC}#")
    client.subscribe(f"{MQTT_VOICE_COMMAND_TOPIC}#")
    client.subscribe(MQTT_DISCOVERY_TOPIC)
    client.subscribe(MQTT_ASSIGNMENT_TOPIC)
    client.subscribe(MQTT_TRIGGER_TOPIC)
    client.subscribe(MQTT_STATUS_TOPIC)

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        if topic.startswith(MQTT_VOICE_AUDIO_TOPIC):
            room_id = topic.split('/')[-1].strip()
            if room_id in vad_processors: vad_processors[room_id].process_chunk(msg.payload)
            return
        try: payload_str = msg.payload.decode('utf-8')
        except: return

        if topic == MQTT_STATUS_TOPIC:
            room, relay, status = payload_str.split(':')
            if room in device_states and relay in device_states[room]:
                device_states[room][relay]['status'] = status
                broadcast_update({"type": "status_update", "room": room, "relay": relay, "status": status})
                save_config(device_states, "device_config.json")
            return
        if topic.startswith(MQTT_VOICE_COMMAND_TOPIC):
            room_id = topic.split('/')[-1].strip()
            if payload_str == "START": vad_processors[room_id] = VadAudio(room_id, aggressiveness=1)
            elif payload_str == "END": 
                if room_id in vad_processors: del vad_processors[room_id]
            return
        if topic == MQTT_TRIGGER_TOPIC:
            last_motion_time[payload_str.strip()] = datetime.datetime.now()
            return
        if topic == MQTT_DISCOVERY_TOPIC:
            device_info = json.loads(payload_str)
            device_id = device_info["device_id"]
            if device_id not in device_room_map.values():
                unassigned_devices[device_id] = {"device_id": device_id, "type": "esp32_relay", "last_seen": datetime.datetime.now().isoformat()}
    except Exception as e: print(f"Error: {e}")

# --- Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pw = request.form['password']
        if user == USERNAME and pw == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash('Invalid Credentials. Please try again.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route("/")
@login_required
def index():
    return render_template("index.html", device_states=device_states, device_room_map=device_room_map)

# --- MODE MANAGEMENT ROUTES ---
@app.route("/modes")
@login_required
def modes():
    current_modes = load_config("modes.json")
    return render_template("modes.html", modes=current_modes, device_states=device_states)

@app.route("/save_mode", methods=["POST"])
@login_required
def save_mode():
    mode_name = request.form.get("mode_name")
    if not mode_name: return redirect(url_for('modes'))
    
    current_modes = load_config("modes.json")
    
    actions = {}
    for room, devices in device_states.items():
        actions[room] = {}
        for relay_key in devices:
            if relay_key.startswith('relay'):
                form_key = f"action_{room}_{relay_key}"
                if request.form.get(form_key):
                    actions[room][relay_key] = "ON"
                else:
                    actions[room][relay_key] = "OFF"
    
    new_mode = {
        "start_time": request.form.get("start_time"),
        "days": request.form.getlist("days"), 
        "audio_id": int(request.form.get("audio_id")) if request.form.get("audio_id") else None,
        "actions": actions
    }
    
    current_modes[mode_name] = new_mode
    save_config(current_modes, "modes.json")
    return redirect(url_for('modes'))

@app.route("/delete_mode", methods=["POST"])
@login_required
def delete_mode():
    mode_name = request.form.get("mode_name")
    current_modes = load_config("modes.json")
    if mode_name in current_modes:
        del current_modes[mode_name]
        save_config(current_modes, "modes.json")
    return redirect(url_for('modes'))

@app.route("/control", methods=["POST"])
@login_required
def control():
    data = request.get_json()
    if not data: return "Invalid JSON", 400
    room, relay, action = data.get("room"), data.get("relay"), data.get("action")
    motion_ctrl = data.get("motion_control")
    
    if room in device_states and relay in device_states[room]:
        if motion_ctrl is not None:
            device_states[room][relay]['motion_control'] = motion_ctrl
            broadcast_update({"type": "motion_update", "room": room, "relay": relay, "motion_control": motion_ctrl})
        
        if action:
            device_states[room][relay]['status'] = action
            client.publish(MQTT_PUB_TOPIC, f"{room}:{relay}:{action}")
            
        save_config(device_states, "device_config.json")
    return "OK", 200

# Management routes
@app.route("/devices")
@login_required
def device_management(): return render_template("devices.html", unassigned_devices=unassigned_devices, device_states=device_states, device_room_map=device_room_map)

@app.route("/assign_device", methods=["POST"])
@login_required
def assign_device():
    device_id, room_name = request.form["device_id"], request.form["room_name"]
    client.publish(MQTT_ASSIGNMENT_TOPIC, json.dumps({"device_id": device_id, "room_name": room_name}))
    device_room_map[room_name] = device_id
    save_config(device_room_map, "device_room_map.json")
    if device_id in unassigned_devices: unassigned_devices.pop(device_id)
    client.publish(f"{MQTT_CONFIG_TOPIC_PREFIX}{device_id}", json.dumps(device_states.get(room_name, {})))
    return redirect(url_for("device_management"))

@app.route("/unbind_device")
@login_required
def unbind_device_form(): return render_template("unbind_device.html", room_name=request.args.get('room'))

@app.route("/unassign_device", methods=["POST"])
@login_required
def unassign_device():
    room_name = request.form["room_name"]
    if room_name in device_room_map:
        device_id = device_room_map.pop(room_name)
        save_config(device_room_map, "device_room_map.json")
        client.publish(MQTT_ASSIGNMENT_TOPIC, json.dumps({"device_id": device_id, "room_name": "unassigned"}))
        client.publish(f"{MQTT_CONFIG_TOPIC_PREFIX}{device_id}", json.dumps({"action": "reset"}))
    return redirect(url_for("device_management"))

@app.route("/add_room")
@login_required
def add_room_form():
    if "Other" not in PRESET_DEVICES: PRESET_DEVICES.append("Other")
    return render_template("add_room.html", preset_devices=PRESET_DEVICES)

@app.route("/add_room", methods=["POST"])
@login_required
def add_room():
    new_room = request.form.get("new_room", "").lower().strip().replace(" ", "_")
    if new_room and new_room not in device_states:
        device_states[new_room] = {'wake_word': request.form.get("wake_word", "jarvis")}
        for i in range(1, 9):
            relay_key = f"relay{i}"
            selection = request.form.get(f"relay{i}_select")
            label = request.form.get(f"relay{i}_custom") if selection == "Other" else selection
            motion = request.form.get(f'relay{i}_motion') == 'on'
            device_states[new_room][relay_key] = {"label": label or f"Device {i}", "status": "OFF", "motion_control": motion}
        save_config(device_states, "device_config.json")
    return redirect(url_for("device_management"))

@app.route("/edit_room")
@login_required
def edit_room_form():
    selected_room = request.args.get('room')
    if "Other" not in PRESET_DEVICES: PRESET_DEVICES.append("Other")
    return render_template("edit_room.html", device_states=device_states, selected_room=selected_room, preset_devices=PRESET_DEVICES)

@app.route("/edit_room", methods=["POST"])
@login_required
def edit_room():
    original_room_name = request.form.get("original_room_name")
    if original_room_name not in device_states: return "Original room not found", 404
    new_room_name = request.form.get("new_room_name", original_room_name).lower().strip().replace(" ", "_")
    room_data = device_states.get(original_room_name, {})
    room_data['wake_word'] = request.form.get('wake_word', room_data.get('wake_word', 'jarvis'))
    for i in range(1, 9):
        relay_key = f'relay{i}'
        selection = request.form.get(f"relay{i}_select")
        label = request.form.get(f"relay{i}_custom") if selection == "Other" else selection
        motion = request.form.get(f'relay{i}_motion') == 'on'
        if relay_key in room_data:
            room_data[relay_key]["label"] = label or f"Device {i}"
            room_data[relay_key]["motion_control"] = motion
    if original_room_name != new_room_name:
        if new_room_name in device_states: return f"Room name '{new_room_name}' already exists.", 400
        device_states[new_room_name] = device_states.pop(original_room_name)
        if original_room_name in device_room_map:
            device_id = device_room_map.pop(original_room_name)
            device_room_map[new_room_name] = device_id
            save_config(device_room_map, "device_room_map.json")
            client.publish(MQTT_ASSIGNMENT_TOPIC, json.dumps({"device_id": device_id, "room_name": new_room_name}))
    save_config(device_states, "device_config.json")
    current_room_name = new_room_name or original_room_name
    if current_room_name in device_room_map:
        device_id = device_room_map[current_room_name]
        client.publish(f"{MQTT_CONFIG_TOPIC_PREFIX}{device_id}", json.dumps(device_states[current_room_name]))
    return redirect(url_for("device_management"))

@app.route("/remove_room")
@login_required
def remove_room_form():
    return render_template("remove_room.html", rooms=list(device_states.keys()), selected_room=request.args.get('room'))

@app.route("/remove_room", methods=["POST"])
@login_required
def remove_room():
    room_to_remove = request.form.get("room_to_remove")
    if room_to_remove in device_states:
        if room_to_remove in device_room_map:
            device_id = device_room_map.pop(room_to_remove)
            save_config(device_room_map, "device_room_map.json")
            client.publish(MQTT_ASSIGNMENT_TOPIC, json.dumps({"device_id": device_id, "room_name": "unassigned"}))
            client.publish(f"{MQTT_CONFIG_TOPIC_PREFIX}{device_id}", json.dumps({"action": "reset"}))
        del device_states[room_to_remove]
        save_config(device_states, "device_config.json")
    return redirect(url_for("device_management"))

@app.route("/process_browser_audio", methods=['POST'])
@login_required
def process_browser_audio():
    target_room = request.args.get('room') or next(iter(device_states), None)
    if not target_room or target_room not in device_states: return "Invalid room", 400
    try:
        # --- ADDED: Volume Boost (3x) for GUI Mic ---
        ffmpeg_command = ['ffmpeg', '-i', 'pipe:0', '-af', 'volume=3.0', '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', '1', 'pipe:1']
        
        process = subprocess.run(ffmpeg_command, input=request.data, capture_output=True, check=True)
        threading.Thread(target=process_voice_command, args=(target_room, process.stdout)).start()
        return "OK", 200
    except Exception as e: return "Error", 500

@app.route("/status-stream")
def status_stream():
    q = queue.Queue(maxsize=100) 
    sse_subscribers.append(q)
    def event_stream():
        try:
            while True:
                data = q.get()
                yield f"data: {json.dumps(data)}\n\n"
        except GeneratorExit:
            if q in sse_subscribers: sse_subscribers.remove(q)
        except Exception:
            if q in sse_subscribers: sse_subscribers.remove(q)
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    device_states = load_config("device_config.json")
    device_room_map = load_config("device_room_map.json")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    motion_thread = threading.Thread(target=check_motion_timeouts, daemon=True)
    motion_thread.start()
    client.loop_start()

    ssl_context = None
    if os.path.exists('cert.pem') and os.path.exists('key.pem'):
        ssl_context = ('cert.pem', 'key.pem')
        print("Starting Flask server with HTTPS...")
    else:
        print("Starting Flask server with HTTP...")
    
    app.run(host="0.0.0.0", port=5000, debug=True, ssl_context=ssl_context)