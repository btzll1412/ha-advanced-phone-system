ARG BUILD_FROM
FROM $BUILD_FROM

# Install Asterisk and dependencies
RUN apk add --no-cache \
    asterisk \
    asterisk-sounds-en \
    asterisk-sounds-moh \
    asterisk-curl \
    asterisk-srtp \
    python3 \
    py3-pip \
    sox \
    ffmpeg \
    curl \
    bash \
    sqlite

# Install Python packages
RUN pip3 install --break-system-packages \
    fastapi==0.104.1 \
    uvicorn==0.24.0 \
    pydantic==2.5.0 \
    aiofiles==23.2.1 \
    requests==2.31.0 \
    jinja2==3.1.2 \
    python-multipart==0.0.6

# Create directories
RUN mkdir -p \
    /var/lib/asterisk/sounds/custom \
    /var/spool/asterisk/outgoing \
    /app \
    /data/recordings \
    /data/database

# Copy application files
COPY rootfs /

# Set permissions
RUN chmod +x /etc/services.d/phone-system/run && \
    chown -R asterisk:asterisk /var/lib/asterisk && \
    chown -R asterisk:asterisk /var/spool/asterisk

# Expose ports
EXPOSE 5060/tcp 5060/udp 10000-10099/udp 8088/tcp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
    CMD curl -f http://localhost:8088/health || exit 1

WORKDIR /app
