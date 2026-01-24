document.addEventListener('DOMContentLoaded', () => {
    
    // --- 1. OFFLINE ANIMATIONS ---
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('active');
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.reveal').forEach(el => observer.observe(el));


    // --- 2. DARK MODE LOGIC ---
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


    // --- 3. CUSTOM DEVICE NAME TOGGLE ---
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


    // --- 4. DASHBOARD LOGIC (Index only) ---
    if(document.getElementById('control-panel')) {
        
        // A. Send Commands
        const sendCommand = (payload) => {
            fetch('/control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).catch(console.error);
        };

        // B. Relay Toggles
        document.querySelectorAll('.relay-toggle').forEach(toggle => {
            toggle.addEventListener('change', function() {
                sendCommand({ 
                    room: this.dataset.room, 
                    relay: this.dataset.relay, 
                    action: this.checked ? 'ON' : 'OFF' 
                });
            });
        });

        // C. Motion Toggles
        document.querySelectorAll('.motion-icon').forEach(icon => {
            icon.addEventListener('click', function() {
                const room = this.dataset.room;
                const relay = this.dataset.relay;
                const isActive = this.classList.contains('active');
                
                this.classList.toggle('active');
                sendCommand({ room, relay, motion_control: !isActive });
            });
        });

        // D. Voice Command (Multi-Room Support)
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
                            method: 'POST', 
                            body: blob 
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
                    alert("Microphone access denied or not available."); 
                    console.error(err); 
                }
            });
        });

        // --- E. ADVANCED VOICE & STATUS UPDATES ---
        
        // 1. Voice Setup
        let availableVoices = [];

        function loadVoices() {
            availableVoices = window.speechSynthesis.getVoices();
        }
        
        loadVoices();
        if (window.speechSynthesis.onvoiceschanged !== undefined) {
            window.speechSynthesis.onvoiceschanged = loadVoices;
        }

        function speakText(text) {
            if ('speechSynthesis' in window) {
                // Clear any pending speech
                window.speechSynthesis.cancel(); 
                
                const utterance = new SpeechSynthesisUtterance(text);
                
                // Priority for Google voices (Android)
                const preferredVoice = availableVoices.find(voice => 
                    voice.name === "Google US English" || 
                    voice.name === "Google UK English Female" ||
                    (voice.name.includes("Google") && voice.lang.includes("en")) ||
                    voice.lang === "en-US" || 
                    voice.lang === "en-GB"
                );

                if (preferredVoice) {
                    utterance.voice = preferredVoice;
                }

                utterance.rate = 1.0; 
                utterance.pitch = 1.0; 
                utterance.volume = 1.0;

                // Timeout to prevent chopped start
                setTimeout(() => {
                    window.speechSynthesis.speak(utterance);
                }, 50); 
            }
        }

        // 2. SSE Event Source
        function setupEventSource() {
            const eventSource = new EventSource("/status-stream");

            eventSource.onmessage = (e) => {
                const data = JSON.parse(e.data);
                
                // Status Update
                if (data.type === 'status_update') {
                    const elementId = `switch-${data.room}-${data.relay}`;
                    const toggle = document.getElementById(elementId);
                    if (toggle) toggle.checked = (data.status === 'ON');
                }
                
                // Motion Update
                if (data.type === 'motion_update') {
                    const elementId = `motion-${data.room}-${data.relay}`;
                    const icon = document.getElementById(elementId);
                    if (icon) {
                        if(data.motion_control) icon.classList.add('active');
                        else icon.classList.remove('active');
                    }
                }

                // Voice Feedback
                if (data.type === 'voice_feedback') {
                    speakText(data.text);
                }
            };

            eventSource.onerror = (e) => {
                eventSource.close();
                setTimeout(setupEventSource, 3000); 
            };
        }
        
        setupEventSource();
    }
});