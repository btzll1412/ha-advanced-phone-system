#!/usr/bin/with-contenv bashio
# ==============================================================================
# Generate Asterisk SIP configuration from Home Assistant options
# ==============================================================================

bashio::log.info "Generating Asterisk SIP configuration..."

SIP_CONF="/etc/asterisk/sip.conf"

# Read SIP trunk settings
SIP_ENABLED=$(bashio::config 'sip_trunk.enabled')
SIP_HOST=$(bashio::config 'sip_trunk.host')
SIP_PORT=$(bashio::config 'sip_trunk.port')
SIP_USERNAME=$(bashio::config 'sip_trunk.username')
SIP_PASSWORD=$(bashio::config 'sip_trunk.password')
SIP_FROM_DOMAIN=$(bashio::config 'sip_trunk.from_domain')

# Start with [general] section
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
    echo "; RTP Media Path"
    echo "directmedia=no"
    echo "directrtpsetup=no"
    echo "rtcpinterval=5"
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
        echo "context=inbound"
        echo "dtmfmode=rfc2833"
        echo "canreinvite=no"
        echo "qualify=yes"
        echo "nat=force_rport,comedia"
        echo "progressinband=yes"
        echo "directmedia=no"
        echo "directrtpsetup=no"
        echo "rtcpmux=no"
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
        echo "context=internal"
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

bashio::log.info "✓ Asterisk SIP configuration generated"
chmod 644 ${SIP_CONF}
chown asterisk:asterisk ${SIP_CONF}
