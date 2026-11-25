# Docker image for the `domain_to_copilot_agents` enricher (power-pwn's
# Copilot Studio Hunter). power-pwn pins Python <=3.11 and pydantic 1.10.7,
# which conflict with flowsint's runtime, so it ships as its own isolated image
# that flowsint runs Docker-in-Docker (same pattern as subfinder/trufflehog).
#
# Build & push, then set POWERPWN_IMAGE to the pushed repo:tag, e.g.:
#   docker build -t your-registry/powerpwn:latest docker/powerpwn
#   docker push your-registry/powerpwn:latest
#   # in flowsint .env:  POWERPWN_IMAGE=your-registry/powerpwn:latest
#
# Pin POWERPWN_REF to a tag/commit for reproducible builds.
FROM python:3.11-slim

ARG POWERPWN_REPO=https://github.com/mbrg/power-pwn.git
ARG POWERPWN_REF=main
ARG FFUF_VERSION=2.1.0
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Use the distro Chromium instead of letting Puppeteer download its own.
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PYTHONPATH=/home/powerpwn/power-pwn/src

# System deps: git (clone), chromium (Puppeteer), nodejs/npm (JS recon scripts),
# curl/tar (ffuf download), plus Chromium's shared-library dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tar \
        nodejs npm \
        chromium \
        fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install ffuf (Go binary) used by deep-scan for API fuzzing.
RUN curl -fsSL \
        "https://github.com/ffuf/ffuf/releases/download/v${FFUF_VERSION}/ffuf_${FFUF_VERSION}_linux_${TARGETARCH}.tar.gz" \
        -o /tmp/ffuf.tar.gz \
    && tar -xzf /tmp/ffuf.tar.gz -C /usr/local/bin ffuf \
    && chmod +x /usr/local/bin/ffuf \
    && rm /tmp/ffuf.tar.gz \
    && ffuf -V

# Non-root user: Puppeteer/Chromium runs reliably without --no-sandbox only
# outside root, and power-pwn's own image does the same.
RUN useradd -m -s /bin/bash powerpwn
USER powerpwn
WORKDIR /home/powerpwn

# Clone power-pwn, install the Python package (provides the `powerpwn` CLI) and
# the Node dependencies for the two Puppeteer recon scripts.
RUN git clone "${POWERPWN_REPO}" power-pwn \
    && cd power-pwn \
    && git checkout "${POWERPWN_REF}" \
    && pip install --no-cache-dir --user . \
    && bash src/powerpwn/copilot_studio/install_dependencies.sh

ENV PATH="/home/powerpwn/.local/bin:${PATH}"

WORKDIR /home/powerpwn/power-pwn/src/powerpwn/copilot_studio

# deep-scan writes its results here; pre-create the dirs it expects. flowsint
# copies the output back over the Docker API (no bind-mount), so this only needs
# to be writable by the container's own `powerpwn` user.
RUN mkdir -p final_results internal_results/url_results

# flowsint overrides the entrypoint at run time (entrypoint="powerpwn"); this
# default makes manual `docker run <img> copilot-studio-hunter deep-scan ...` work too.
ENTRYPOINT ["powerpwn"]
CMD ["--help"]
