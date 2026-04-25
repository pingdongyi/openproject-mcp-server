# Stage 1: Install dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Production image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install dependencies from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src/ src/
COPY openproject-mcp-http.py .

# Run as non-root user
RUN useradd --create-home --shell /bin/bash mcp && \
    chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; s = socket.socket(); s.settimeout(3); s.connect(('localhost', 8000)); s.close()" || exit 1

CMD ["python", "openproject-mcp-http.py"]
