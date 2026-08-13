ARG BASE_IMAGE=node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/tmp \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/docker/speaker-identity.requirements /app/deploy/docker/speaker-identity.requirements
RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --requirement /app/deploy/docker/speaker-identity.requirements

COPY scripts/speaker_identity.py /app/scripts/speaker_identity.py
COPY deploy/docker/speaker_identity_healthcheck.py /app/deploy/docker/speaker_identity_healthcheck.py

RUN python3 -m compileall -q \
        /app/scripts/speaker_identity.py \
        /app/deploy/docker/speaker_identity_healthcheck.py \
    && chmod --recursive a+rX /app

USER node

ENTRYPOINT ["python3", "/app/scripts/speaker_identity.py"]
