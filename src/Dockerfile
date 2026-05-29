FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for cryptg (fast Telegram decryption)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel.py auth.py ./

# Data volume: session file, media, logs
VOLUME ["/data"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')"

CMD ["python", "sentinel.py"]
