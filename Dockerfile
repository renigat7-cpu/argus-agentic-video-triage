# Argus — reproducible runtime image.
# Pinned to a digest-addressable base so the build is reproducible.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARGUS_OUT_DIR=/data \
    OPENCV_VIDEOIO_PRIORITY_AVFOUNDATION=0

# OpenCV 5 and its runtime deps. ffmpeg is required for robust video decode.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

COPY web ./web
COPY config ./config
RUN useradd --create-home --uid 10001 argus \
 && mkdir -p /data \
 && chown -R argus:argus /data
USER argus

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["python", "-m", "uvicorn", "argus.api:app", "--host", "0.0.0.0", "--port", "8080"]