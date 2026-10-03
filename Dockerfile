# Argus — reproducible runtime image.
# The base image is pinned by digest (not just by tag), so the build resolves to
# the exact same image bytes on every host until the digest is bumped on purpose.
# sha256:54c85f3c… is the digest of the `3.12-slim-bookworm` tag: an OCI image
# index (linux/amd64, linux/arm64, …), so the pin is multi-architecture.
# The bookworm variant is deliberate — the plain `3.12-slim` tag now tracks Debian
# trixie, where glibc's 64-bit-time transition renamed the runtime library package
# to `libglib2.0-0t64`; staying on bookworm keeps the apt list below valid.
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime

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

# Runtime dependencies only. requirements.txt also carries boto3, which the AWS
# features (Bedrock reasoner, S3 decision log, CloudWatch metrics) import lazily:
# the image stays runnable and the API still boots if no AWS credential is set.
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

COPY web ./web
COPY config ./config
# /data is ARGUS_OUT_DIR (mount point for EFS or a compose volume);
# /app/var/uploads is the default ARGUS_UPLOAD_DIR, pre-created and owned by the
# non-root user so the API can stage an upload even with a read-only root.
RUN useradd --create-home --uid 10001 argus \
 && mkdir -p /data /app/var/uploads \
 && chown -R argus:argus /data /app/var
USER argus

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["python", "-m", "uvicorn", "argus.api:app", "--host", "0.0.0.0", "--port", "8080"]