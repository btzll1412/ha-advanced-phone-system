#!/usr/bin/with-contenv bashio
# ==============================================================================
# Generate Asterisk SIP and Extensions configuration
# ==============================================================================

bashio::log.info "Generating Asterisk configuration..."

SIP_CONF="/etc/asterisk/sip.conf"
EXT_CONF="/etc/asterisk/extensions.conf"

# Read SIP trunk settings
SIP_ENABLED=$(bashio::config 'sip_trunk.enabled')
SIP_HOST=$(bashio::config 'sip_trunk.host')
SIP_PORT=$(bashio::config 'sip_trunk.port')
SIP_USERNAME=$(bashio::config 'sip_trunk.username')
SIP_PASSWORD=$(bashio::config 'sip_trunk.password')
SIP_FROM_DOMAIN=$(bashio::config 'sip_trunk.from_domain')
CALLER_NUMBER=$(bashio::config 'sip_trunk.caller_number')

# ==============================================================================
# GENERATE SIP.CONF
# ==============================================================================

bashio::log.info "Generating sip.conf..."

{
    echo "[general]"
    echo "context=default"
    echo "bindport=5060"
    echo "bindaddr=0.0.0.0"
    echo "tcpenable=yes"
    echo "tcpbindaddr=0.0.0.0"
    echo "transport=udp,tcp"
    echo "srvlookup=yes"
    echo "allowguest=no"
    echo "alwaysauthreject=yes"
    echo "nat=force_rport,comedia"
    echo "externrefresh=10"
    echo ""
    echo "; CALL PROGRESS ANALYSIS - Voicemail Detection"
    echo "progressinband=yes"
    echo "callprogress=yes"
    echo ""
    echo "; Codecs"
    echo "disallow=all"
    echo "allow=ulaw"
    echo "allow=alaw"
    echo "allow=gsm"
    echo ""
    echo "; RTP Settings"
    echo "rtpstart=10000"
    echo "rtpend=10099"
    echo "rtptimeout=60"
    echo "rtpholdtimeout=300"
    echo ""
    echo "; Security"
    echo "requirecalltoken=no"
    echo ""
    echo "; Audio settings"
    echo "directmedia=no"
    echo "canreinvite=no"
    echo ""
} > ${SIP_CONF}

# Add SIP trunk if enabled
if bashio::var.true "${SIP_ENABLED}"; then
    bashio::log.info "Adding SIP trunk configuration..."
    
    {
        echo "; SIP Trunk Registration"
        echo "register => ${SIP_USERNAME}:${SIP_PASSWORD}@${SIP_HOST}:${SIP_PORT}/${SIP_USERNAME}"
        echo ""
        echo "[trunk_main]"
        echo "type=peer"
        echo "host=${SIP_HOST}"
        echo "port=${SIP_PORT}"
        echo "defaultuser=${SIP_USERNAME}"
        echo "secret=${SIP_PASSWORD}"
        echo "fromdomain=${SIP_FROM_DOMAIN}"
        echo "insecure=port,invite"
        echo "context=from-external"
        echo "dtmfmode=rfc2833"
        echo "canreinvite=no"
        echo "qualify=yes"
        echo "nat=force_rport,comedia"
        echo "progressinband=yes"
        echo ""
    } >> ${SIP_CONF}
fi

# Add extensions
bashio::log.info "Adding SIP extensions..."
EXT_COUNT=$(bashio::config 'extensions | length')
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
    
    {
        echo "[${EXT_NUMBER}]"
        echo "type=friend"
        echo "secret=${EXT_SECRET}"
        echo "context=default"
        echo "host=dynamic"
        echo "dtmfmode=rfc2833"
        echo "canreinvite=no"
        echo "nat=force_rport,comedia"
        echo "qualify=yes"
        echo "callgroup=1"
        echo "pickupgroup=1"
        echo "callerid=\"${EXT_NAME}\" <${EXT_NUMBER}>"
        echo "setvar=OUTBOUND_CID=${OUTBOUND_CID}"
        echo ""
    } >> ${SIP_CONF}
done

# ==============================================================================
# GENERATE EXTENSIONS.CONF
# ==============================================================================

bashio::log.info "Generating extensions.conf..."

{
    echo "[general]"
    echo "static=yes"
    echo "writeprotect=no"
    echo ""
    echo "; ====================================="
    echo "; INTERNAL CALLS (Extension to Extension)"
    echo "; ====================================="
    echo "[internal]"
    echo "exten => _XXX,1,NoOp(📞 Internal: \${CALLERID(num)} → \${EXTEN})"
    echo "    same => n,Set(CALL_DIRECTION=internal)"
    echo "    same => n,Dial(SIP/\${EXTEN},30,rtT)"
    echo "    same => n,Hangup()"
    echo ""
    echo "exten => _XXXX,1,NoOp(📞 Internal: \${CALLERID(num)} → \${EXTEN})"
    echo "    same => n,Set(CALL_DIRECTION=internal)"
    echo "    same => n,Dial(SIP/\${EXTEN},30,rtT)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; ====================================="
    echo "; OUTBOUND CALLS (External Numbers)"
    echo "; ====================================="
    echo "[outbound]"
    echo "; 10-digit US number"
    echo "exten => _NXXNXXXXXX,1,NoOp(📤 Outbound: \${EXTEN})"
    echo "    same => n,Set(CALL_DIRECTION=outbound)"
    echo "    same => n,Set(CALLERID(all)=${CALLER_NUMBER})"
    echo "    same => n,Dial(SIP/trunk_main/\${EXTEN},60,rtT)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; 11-digit with leading 1"
    echo "exten => _1NXXNXXXXXX,1,NoOp(📤 Outbound: \${EXTEN})"
    echo "    same => n,Set(CALL_DIRECTION=outbound)"
    echo "    same => n,Set(CALLERID(all)=${CALLER_NUMBER})"
    echo "    same => n,Dial(SIP/trunk_main/\${EXTEN:1},60,rtT)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; Emergency"
    echo "exten => 911,1,NoOp(🚨 Emergency)"
    echo "    same => n,Set(CALL_DIRECTION=outbound)"
    echo "    same => n,Dial(SIP/trunk_main/911,60)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; ====================================="
    echo "; INBOUND CALLS (From External)"
    echo "; ====================================="
    echo "[from-external]"
    echo "exten => _X.,1,NoOp(📥 Inbound: \${CALLERID(num)} → DID \${EXTEN})"
    echo "    same => n,Set(CALL_DIRECTION=inbound)"
    echo "    same => n,Answer()"
    echo "    same => n,Wait(1)"
    echo "    same => n,Dial(SIP/100,30,rtT)"
    echo "    same => n,Hangup()"
    echo ""
    echo "exten => s,1,NoOp(📥 Inbound - No DID)"
    echo "    same => n,Set(CALL_DIRECTION=inbound)"
    echo "    same => n,Answer()"
    echo "    same => n,Playback(hello-world)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; ====================================="
    echo "; OUTBOUND PLAYBACK (Broadcasts/TTS)"
    echo "; ====================================="
    echo "[outbound-playback]"
    echo "exten => s,1,NoOp(📻 Broadcast: \${CALL_ID})"
    echo "    same => n,Set(CALL_DIRECTION=\${CALL_DIRECTION})"
    echo "    same => n,Answer()"
    echo "    same => n,Wait(\${PRE_MESSAGE_DELAY})"
    echo "    same => n,Playback(\${AUDIO_FILE})"
    echo "    same => n,Wait(1)"
    echo "    same => n,Hangup()"
    echo ""
    echo "; ====================================="
    echo "; DEFAULT CONTEXT (Extensions use this)"
    echo "; ====================================="
    echo "[default]"
    echo "; Internal extensions"
    echo "exten => _XXX,1,Goto(internal,\${EXTEN},1)"
    echo "exten => _XXXX,1,Goto(internal,\${EXTEN},1)"
    echo ""
    echo "; External numbers"
    echo "exten => _NXXNXXXXXX,1,Goto(outbound,\${EXTEN},1)"
    echo "exten => _1NXXNXXXXXX,1,Goto(outbound,\${EXTEN},1)"
    echo ""
    echo "; Emergency"
    echo "exten => 911,1,Goto(outbound,911,1)"
    echo ""
} > ${EXT_CONF}

# Set permissions
chmod 644 ${SIP_CONF}
chmod 644 ${EXT_CONF}
chown asterisk:asterisk ${SIP_CONF}
chown asterisk:asterisk ${EXT_CONF}

bashio::log.info "✅ Asterisk configuration generated successfully!"
bashio::log.info "   - sip.conf: ${SIP_CONF}"
bashio::log.info "   - extensions.conf: ${EXT_CONF}"
