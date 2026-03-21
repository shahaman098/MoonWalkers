# =============================================================================
# SwarmOS Agent Runtime Container
# Multi-stage build: slim base with desktop environment for visual agents
# =============================================================================

FROM python:3.11-slim AS base

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies:
#   - xvfb: virtual X framebuffer (headless display)
#   - novnc + websockify: browser-based VNC client
#   - x11vnc: VNC server that captures an X display
#   - fluxbox: lightweight window manager
#   - chromium: browser for web-capable agents
#   - nodejs + npm: for JS/TS agent toolchains
#   - git, curl: essentials
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    novnc \
    websockify \
    x11vnc \
    fluxbox \
    chromium \
    nodejs \
    npm \
    git \
    curl \
    zip \
    unzip \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright system deps (for browser automation agents)
RUN pip install --no-cache-dir playwright \
    && playwright install-deps || true

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
COPY app/ /opt/swarm-os/app/
COPY scripts/ /opt/swarm-os/scripts/
RUN chmod +x /opt/swarm-os/scripts/*.sh

# ---------------------------------------------------------------------------
# Non-root user
# ---------------------------------------------------------------------------
RUN useradd -m -d /home/agent -s /bin/bash agent \
    && mkdir -p /home/agent/workspace \
    && chown -R agent:agent /home/agent

WORKDIR /home/agent/workspace

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV DISPLAY=:1
ENV PYTHONPATH=/opt/swarm-os
ENV WORKSPACE_ROOT=/home/agent/workspace

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
EXPOSE 8080 6080 5900

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
ENTRYPOINT ["/opt/swarm-os/scripts/entrypoint.sh"]
