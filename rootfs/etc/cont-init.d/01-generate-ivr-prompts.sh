#!/usr/bin/with-contenv bashio
# ==============================================================================
# Generate IVR voice prompts using TTS
# ==============================================================================

bashio::log.info "Generating IVR voice prompts..."

IVR_DIR="/var/lib/asterisk/sounds/ivr"
CUSTOM_DIR="/var/lib/asterisk/sounds/custom"

# Create directories
mkdir -p "$IVR_DIR"
mkdir -p "$CUSTOM_DIR"

# Clear old prompts to regenerate with updated tempo (faster speech)
# Only clear IVR system prompts, not user recordings
rm -f "$IVR_DIR"/*.wav "$IVR_DIR"/*.sln "$IVR_DIR"/*.mp3 2>/dev/null || true
bashio::log.info "Cleared old IVR prompts for regeneration"

# Run the prompt generator
cd /app
python3 generate_ivr_prompts.py

if [ $? -eq 0 ]; then
    bashio::log.info "✓ IVR prompts generated successfully"
else
    bashio::log.warning "Some IVR prompts failed to generate - IVR may have limited functionality"
fi

# Generate beep sound if it doesn't exist (fallback if asterisk-sounds-en not available)
SOUNDS_DIR="/var/lib/asterisk/sounds"
if [ ! -f "$SOUNDS_DIR/beep.gsm" ] && [ ! -f "$SOUNDS_DIR/beep.wav" ] && [ ! -f "$SOUNDS_DIR/beep.ulaw" ]; then
    bashio::log.info "Generating beep sound..."
    # Generate a 0.2 second 1000Hz beep tone
    sox -n -r 8000 -c 1 "$SOUNDS_DIR/beep.wav" synth 0.2 sine 1000 vol 0.5
    chown asterisk:asterisk "$SOUNDS_DIR/beep.wav"
fi

# Set proper permissions
chown -R asterisk:asterisk "$IVR_DIR"
chown -R asterisk:asterisk "$CUSTOM_DIR"
chmod -R 755 "$IVR_DIR"
chmod -R 755 "$CUSTOM_DIR"

# Ensure AGI scripts are executable
AGI_DIR="/var/lib/asterisk/agi-bin"
if [ -d "$AGI_DIR" ]; then
    chmod +x "$AGI_DIR"/*.py 2>/dev/null || true
    chown -R asterisk:asterisk "$AGI_DIR"
fi

bashio::log.info "✓ IVR system ready"
