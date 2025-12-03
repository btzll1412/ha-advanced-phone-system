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

# Run the prompt generator
cd /app
python3 generate_ivr_prompts.py

if [ $? -eq 0 ]; then
    bashio::log.info "✓ IVR prompts generated successfully"
else
    bashio::log.warning "Some IVR prompts failed to generate - IVR may have limited functionality"
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
