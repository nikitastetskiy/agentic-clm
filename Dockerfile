# auto-pptx — pipeline image: Python (fetch / plan / build / verify), headless Chromium for the
# bot-protected hashicorp.com pages, and the opencode CLI for the two LLM steps.
#
# LibreOffice (PDF preview attached to each published deck) is optional and large — leave it out with:
#   docker build --build-arg INSTALL_OFFICE=false -t auto-pptx .
FROM node:22-bookworm-slim

ARG INSTALL_OFFICE=true
# Pinned: this is the opencode version the agents in .opencode/agents/ were written and tested against.
ARG OPENCODE_VERSION=1.18.35
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PATH=/opt/venv/bin:$PATH

# System deps. fonts-inter is the deck's typeface: with it the LibreOffice preview wraps text the way
# PowerPoint does. poppler-utils provides pdftoppm for per-slide thumbnails.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        git ca-certificates curl \
        poppler-utils fontconfig fonts-dejavu-core fonts-inter \
    && if [ "$INSTALL_OFFICE" = "true" ]; then \
         apt-get install -y --no-install-recommends libreoffice-impress libreoffice-core ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# opencode CLI (enrichment + self-heal agents).
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && opencode --version

# Python venv + deps.
RUN python3 -m venv "$VIRTUAL_ENV"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# Playwright browser + OS libraries, in a fixed path so it is found whatever user the pipeline runs as.
RUN playwright install --with-deps chromium && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

WORKDIR /work
# The project (the build context), so the image can also run a build on its own. In a pipeline the
# job runs from the pipeline's own checkout and only uses this image for its tools.
COPY . /work
RUN mkdir -p output data

# One weekly build. Override the command for the pipeline entry point: `bash ci/run.sh`.
ENTRYPOINT ["bash", "build.sh"]
