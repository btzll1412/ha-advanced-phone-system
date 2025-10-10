#!/usr/bin/with-contenv bashio

SOUNDS_DIR="/var/lib/asterisk/sounds"
SOUNDS_INSTALLED="/data/.sounds_installed"

# Check if sounds are already installed
if [ -f "$SOUNDS_INSTALLED" ]; then
    bashio::log.info "Asterisk sound files already installed"
    exit 0
fi

bashio::log.info "Installing Asterisk sound files..."

# Create sounds directory
mkdir -p "$SOUNDS_DIR"

# Download and install core English sounds (ulaw format for best compatibility)
bashio::log.info "Downloading core English sound files..."
cd /tmp
wget -q http://downloads.asterisk.org/pub/telephony/sounds/asterisk-core-sounds-en-ulaw-current.tar.gz

if [ $? -eq 0 ]; then
    bashio::log.info "Extracting sound files..."
    tar -xzf asterisk-core-sounds-en-ulaw-current.tar.gz -C "$SOUNDS_DIR/"
    rm asterisk-core-sounds-en-ulaw-current.tar.gz
    
    # Set proper permissions
    chown -R asterisk:asterisk "$SOUNDS_DIR"
    
    # Mark as installed
    touch "$SOUNDS_INSTALLED"
    
    bashio::log.info "✓ Sound files installed successfully"
else
    bashio::log.error "Failed to download sound files"
    exit 1
fi
