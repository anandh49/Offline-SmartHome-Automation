document.addEventListener('DOMContentLoaded', () => {
    
    // --- 1. AUTOMATIC TIME SYNC (NEW) ---
    function syncDeviceTime() {
        // Only sync if not already done in this session (to save bandwidth)
        if (sessionStorage.getItem('time_synced')) return;

        const now = new Date();
        // Format to: YYYY-MM-DD HH:MM:SS
        const pad = (n) => String(n).padStart(2, '0');
        const timeString = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
        
        console.log("Syncing time to:", timeString);
        fetch('/sync_time', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ time: timeString })
        }).then(res => {
            if(res.ok) {
                console.log("Time synced successfully.");
                sessionStorage.setItem('time_synced', 'true');
            }
        }).catch(console.error);
    }
    syncDeviceTime();

    // --- 2. OFFLINE ANIMATIONS ---
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('active');
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

    // --- 3. DARK MODE LOGIC ---
    const themeBtn = document.getElementById('theme-toggle');
    const body = document.body;
    
    if(localStorage.getItem('theme') === 'dark'){
        body.classList.add('dark-mode');
    }

    if(themeBtn){
        themeBtn.addEventListener('click', (e) => {
            e.preventDefault();
            body.classList.toggle('dark-mode');
            localStorage.setItem('theme', body.classList.contains('dark-mode') ? 'dark' : 'light');
        });
    }

    // --- 4. CUSTOM DEVICE NAME TOGGLE ---
    window.toggleCustom = function(select, id) {
        const input = document.getElementById(id);
        if(input) {
            if(select.value === 'Other') {
                input.classList.remove('hidden');
                input.focus();
            } else {
                input.classList.add('hidden');
            }
        }
    };

    // --- 5. DASHBOARD LOGIC (Index only) ---
    if(document.getElementById('control-panel')) {
        const sendCommand = (payload) => {
            fetch('/control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).catch(console.error);
        };

        document.querySelectorAll('.relay-toggle').forEach(toggle => {
            toggle.addEventListener('change', function() {
                sendCommand({ 
                    room: this.dataset.room, 
                    relay: this.dataset.relay, 
                    action: this.checked ? 'ON' : 'OFF' 
                });
            });
        });

        document.querySelectorAll('.motion-icon').forEach(icon => {
            icon.addEventListener('click', function() {
                const room = this.dataset.room;
                const relay = this.dataset.relay;
                const isActive = this.classList.contains('active');
                this.classList.toggle('active');
                sendCommand({ room, relay, motion_control: !isActive });
            });
        });

        const micButtons = document.querySelectorAll('.push-to-talk');
        micButtons.forEach(btn => {
            let mediaRecorder;
            let audioChunks = [];
            let isRecording = false;
            const micWrapper = btn.closest('.mic-wrapper');
            const micStatus = micWrapper.querySelector('.mic-status');

            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (isRecording) return; 

                const roomName = btn.dataset.room;

                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    mediaRecorder = new MediaRecorder(stream);
                    audioChunks = [];
                    
                    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
                    
                    mediaRecorder.onstop = () => {
                        const blob = new Blob(audioChunks);
                        fetch(`/process_browser_audio?room=${encodeURIComponent(roomName)}`, { 
                            method: 'POST', body: blob 
                        });
                        btn.classList.remove('listening');
                        if(micStatus) micStatus.innerHTML = `Tap to Speak`;
                        isRecording = false;
                        stream.getTracks().forEach(track => track.stop());
                    };

                    mediaRecorder.start();
                    isRecording = true;
                    btn.classList.add('listening');
                    if(micStatus) micStatus.innerHTML = 'Listening...';

                    setTimeout(() => { 
                        if (mediaRecorder.state !== 'inactive') mediaRecorder.stop(); 
                    }, 4000); 

                } catch (err) { 
                    alert("Microphone access denied."); 
                }
            });
        });

        let availableVoices = [];
        function loadVoices() { availableVoices = window.speechSynthesis.getVoices(); }
        loadVoices();
        if (window.speechSynthesis.onvoiceschanged !== undefined) window.speechSynthesis.onvoiceschanged = loadVoices;

        function speakText(text) {
            if ('speechSynthesis' in window) {
                window.speechSynthesis.cancel(); 
                const utterance = new SpeechSynthesisUtterance(text);
                const preferredVoice = availableVoices.find(voice => 
                    voice.name.includes("Google") && voice.lang.includes("en") || voice.lang === "en-US"
                );
                if (preferredVoice) utterance.voice = preferredVoice;
                setTimeout(() => { window.speechSynthesis.speak(utterance); }, 50); 
            }
        }

        function setupEventSource() {
            const eventSource = new EventSource("/status-stream");
            eventSource.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if (data.type === 'status_update') {
                    const el = document.getElementById(`switch-${data.room}-${data.relay}`);
                    if (el) el.checked = (data.status === 'ON');
                }
                if (data.type === 'voice_feedback') speakText(data.text);
            };
            eventSource.onerror = (e) => { eventSource.close(); setTimeout(setupEventSource, 3000); };
        }
        setupEventSource();
    }
});