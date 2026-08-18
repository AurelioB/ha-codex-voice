ARG NODE_IMAGE=node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436
FROM ${NODE_IMAGE}

ARG CODEX_CLI_VERSION=0.147.0

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

RUN npm install --global --omit=dev "@openai/codex@${CODEX_CLI_VERSION}" \
    && codex --version

WORKDIR /app

COPY bridge/requirements.txt /app/bridge/requirements.txt
COPY deploy/docker/requirements.lock /app/deploy/docker/requirements.lock
RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --constraint /app/deploy/docker/requirements.lock \
        --requirement /app/bridge/requirements.txt

COPY bridge /app/bridge
COPY deploy/docker/healthcheck.py /app/deploy/docker/healthcheck.py

RUN install --directory --owner=root --group=root --mode=0711 /run/codex-auth \
    && python3 -m compileall -q /app/bridge /app/deploy/docker/healthcheck.py \
    && chmod --recursive a+rX /app

USER node

CMD ["python3", "-m", "bridge"]
