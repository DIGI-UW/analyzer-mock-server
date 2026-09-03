# Analyzer mock server image

FROM python:3.11-slim

LABEL maintainer="OpenELIS Global <openelis@uw.edu>"
LABEL description="ASTM LIS2-A2 Mock Server for analyzer testing"

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python modules + config
COPY *.py *.json ./
COPY protocols/ ./protocols/
COPY templates/ ./templates/
COPY fixtures/ ./fixtures/
COPY config/ ./config/

# Default port
EXPOSE 5000

# Listener defaults. A template or port map is still required at runtime.
ENV ASTM_PORT=5000
ENV RESPONSE_DELAY_MS=100

# Health check - verify server is listening
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('localhost', 5000)); s.close()" || exit 1

# Run server
CMD ["python", "-u", "server.py"]







