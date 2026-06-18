# auto-pptx — Copilot CLI + skills pipeline image
#
# Base provides Node 22 (for Copilot CLI). Python runs in an isolated venv.
# LibreOffice (for visual QA / thumbnails) is optional and large — disable with:
#   docker build --build-arg INSTALL_OFFICE=false -t auto-pptx .
FROM node:22-bookworm-slim

ARG INSTALL_OFFICE=true
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# System deps. poppler-utils (pdftoppm) + libreoffice power visual QA in the review phase.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        git ca-certificates curl \
        poppler-utils fontconfig fonts-dejavu-core \
    && if [ "$INSTALL_OFFICE" = "true" ]; then \
         apt-get install -y --no-install-recommends libreoffice-impl libreoffice-core libreoffice-impress ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Copilot CLI.
RUN npm install -g @github/copilot && copilot --version

# Python venv + deps.
RUN python3 -m venv "$VIRTUAL_ENV"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

WORKDIR /work
# Copy the project (the build context). The original deck is included so a run can produce a copy.
COPY . /work

# Copilot config/state lives here; mount a volume to persist sessions/auth between runs.
ENV COPILOT_HOME=/work/.copilot-home
RUN mkdir -p "$COPILOT_HOME" output data

# Pipeline entrypoint. Override CMD to run a single phase if desired.
ENTRYPOINT ["bash", "run.sh"]
