FROM python:3.11-slim

# Install FFmpeg + curl (for healthcheck) BEFORE Python dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg curl gcc python3-dev \
        libgl1 libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=120 --retries=3 -r requirements.txt

# Copy application code
COPY . .

# Create temp directory for video processing
RUN mkdir -p /tmp/video-cutter

# Port (configurable via env for panels like Easypanel, Coolify)
ENV PORT=8000

EXPOSE ${PORT}

# Healthcheck for Docker / panels / load balancers
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT}/ || exit 1

# Single worker: BackgroundTasks runs in the same process
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
