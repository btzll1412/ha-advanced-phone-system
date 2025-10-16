#!/usr/bin/with-contenv bashio
# ==============================================================================
# Generate Asterisk SIP configuration from Home Assistant options
# ==============================================================================

bashio::log.info "Generating Asterisk SIP configuration..."

CONFIG_PATH="/data/options.json"
SIP_CONF="/etc/asterisk/sip.conf"

# Read SIP trunk settings
SIP_ENABLED=$(bashio::config 'sip_trunk.enabled')
SIP_HOST=$(bashio::config 'sip_trunk.host')
SIP_PORT=$(bashio::config 'sip_trunk.port')
SIP_USERNAME=$(bashio::config 'sip_trunk.username')
SIP_PASSWORD=$(bashio::config 'sip_trunk.password')
SIP_FROM_DOMAIN=$(bashio::config 'sip_trunk.from_domain')
SIP_CALLER_NUMBER=$(bashio::config 'sip_trunk.caller_number')

# Generate sip.conf
cat > ${SIP_CONF} << EOF
[general]
context=default
bindport=5060
bindaddr=0.0.0.0
tcpenable=yes
tcpbindaddr=0.0.0.0
transport=udp,tcp
srvlookup=yes
allowguest=no
alwaysauthreject=yes
nat=force_rport,comedia
externrefresh=10

; ⭐ ENABLE CALL PROGRESS ANALYSIS (Voicemail Detection)
progressinband=yes
callprogress=yes

; Codecs - Order matters!
disallow=all
allow=ulaw
allow=alaw
allow=gsm

; RTP Settings
rtpstart=10000
rtpend=10099
rtptimeout=60
rtpholdtimeout=300

; Security
requirecalltoken=no

; Audio settings
directmedia=no
canreinvite=no

EOF

# Add SIP trunk if enabled
if bashio::var.true "${SIP_ENABLED}"; then
    bashio::log.info "Adding SIP trunk configuration..."
    
    cat >> ${SIP_CONF} << EOF
; SIP Trunk Registration
register => ${SIP_USERNAME}:${SIP_PASSWORD}@${SIP_HOST}:${SIP_PORT}/${SIP_USERNAME}

[trunk_main]
type=peer
host=${SIP_HOST}
port=${SIP_PORT}
defaultuser=${SIP_USERNAME}
secret=${SIP_PASSWORD}
fromdomain=${SIP_FROM_DOMAIN}
insecure=port,invite
context=inbound
dtmfmode=rfc2833
canreinvite=no
qualify=yes
nat=force_rport,comedia
progressinband=yes

EOF
fi

# Add extensions - FIXED parsing
bashio::log.info "Adding SIP extensions..."

# Get number of extensions
EXT_COUNT=$(bashio::config 'extensions | length')

# Loop through each extension by index
for (( i=0; i<${EXT_COUNT}; i++ )); do
    EXT_NUMBER=$(bashio::config "extensions[${i}].number")
    EXT_NAME=$(bashio::config "extensions[${i}].name")
    EXT_SECRET=$(bashio::config "extensions[${i}].secret")
    EXT_CALLER_ID=$(bashio::config "extensions[${i}].caller_id" || echo "")
    
    if [ -z "${EXT_CALLER_ID}" ] || [ "${EXT_CALLER_ID}" == "null" ]; then
        OUTBOUND_CID="null"
    else
        OUTBOUND_CID="${EXT_CALLER_ID}"
    fi
    
    cat >> ${SIP_CONF} << EOF
[${EXT_NUMBER}]
type=friend
secret=${EXT_SECRET}
context=internal
host=dynamic
dtmfmode=rfc2833
canreinvite=no
nat=force_rport,comedia
qualify=yes
callgroup=1
pickupgroup=1
callerid="${EXT_NAME}" <${EXT_NUMBER}>
setvar=OUTBOUND_CID=${OUTBOUND_CID}

EOF
done

bashio::log.info "✓ Asterisk SIP configuration generated successfully"
chmod 644 ${SIP_CONF}
chown asterisk:asterisk ${SIP_CONF}
