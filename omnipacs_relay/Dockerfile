# OmniPACS Relay — minimal production image.
#
# Build from the repo root so the build context can reach pyproject.toml:
#
#     docker build -f omnipacs_relay/Dockerfile -t omnipacs-relay:1.0.0 .
#
# Run with the spool mounted from a host directory so accepted DICOM
# survives container restarts:
#
#     docker run --rm \
#       -e OMNI_RELAY_TARGET_HOST=10.0.0.5 \
#       -e OMNI_RELAY_TARGET_PORT=11112 \
#       -e OMNI_RELAY_TARGET_AET=LOCAL_PACS \
#       -e OMNI_RELAY_TOKENS=$(uuidgen) \
#       -p 8443:8443 \
#       -e PORT=8443 \
#       -v /var/lib/omnirelay/spool:/spool \
#       -e OMNI_RELAY_SPOOL=/spool \
#       omnipacs-relay:1.0.0

FROM python:3.11-slim

# Don't cache pip wheels and fail fast.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal system deps for pydicom/pynetdicom: nothing extra needed at runtime,
# but ca-certificates is required for outbound TLS (e.g. for any future
# upstream calls) and tini gives us proper PID-1 signal handling.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The relay reads its dependency list from the repo's top-level
# pyproject.toml so the version pins stay in lockstep with OmniRouter.
COPY pyproject.toml ./pyproject.toml
RUN pip install --upgrade pip \
 && pip install \
        "fastapi>=0.136.1" \
        "httpx>=0.28.1" \
        "pydicom>=3.0.2" \
        "pynetdicom>=3.0.4" \
        "uvicorn[standard]>=0.46.0"

# Ship just the relay package — we don't need the rest of the monorepo
# in the image.
COPY omnipacs_relay/ ./omnipacs_relay/

# Default spool location inside the container; mount a host volume here.
ENV OMNI_RELAY_SPOOL=/spool \
    PORT=8443
RUN mkdir -p /spool && chmod 700 /spool
VOLUME ["/spool"]

EXPOSE 8443

# tini reaps the uvicorn worker cleanly on SIGTERM.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "omnipacs_relay.main"]
