# Analyzer Mock Server (Multi-Protocol) Docker Image
# For OpenELIS analyzer testing
# Reference: specs/004-astm-analyzer-mapping/plan.md, specs/011-madagascar-analyzer-integration

FROM python:3.11-slim

LABEL maintainer="OpenELIS Global <openelis@uw.edu>"
LABEL description="Multi-Protocol Analyzer Simulator for analyzer testing"

WORKDIR /app

# Copy server and M4 multi-protocol runtime (protocols, templates, loaders)
COPY server.py fields.json template_loader.py template_generator.py ./
COPY protocols/ ./protocols/
COPY templates/ ./templates/

# Server uses standard library; pyserial optional for serial mode

# Default port
EXPOSE 5000

# Environment variables (can be overridden)
ENV ANALYZER_PORT=5000
ENV ASTM_PORT=5000
ENV ANALYZER_TYPE=HEMATOLOGY
ENV RESPONSE_DELAY_MS=100

# Health check - verify server is listening
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('localhost', 5000)); s.close()" || exit 1

# Run server
CMD ["python", "-u", "server.py"]








