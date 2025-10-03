ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    asterisk \
    asterisk-sample-config \
    curl \
    bash \
    espeak \
    sox \
    && rm -rf /var/cache/apk/*
# Install Python packages
RUN pip3 install --no-cache-dir \
    fastapi \
    uvicorn \
    aiofiles \
    pydantic \
    python-multipart \
    requests

# Create necessary directories
RUN mkdir -p /data/database \
    && mkdir -p /data/recordings \
    && mkdir -p /var/lib/asterisk/sounds/custom \
    && mkdir -p /var/spool/asterisk/outgoing \
    && chown -R asterisk:asterisk /var/lib/asterisk \
    && chown -R asterisk:asterisk /var/spool/asterisk

# Copy application files
COPY rootfs /

# Set execute permissions
RUN chmod +x /etc/services.d/phone-system/run

# Expose ports
EXPOSE 5060/tcp 5060/udp 8088/tcp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8088/health || exit 1

# Labels
LABEL io.hass.version="1.0.0" \
      io.hass.type="addon" \
      io.hass.arch="armhf|armv7|aarch64|amd64|i386"
