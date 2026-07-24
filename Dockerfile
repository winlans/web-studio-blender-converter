FROM ubuntu:24.04

ARG BLENDER_VERSION=5.0.1
ARG BLENDER_ARCHIVE=blender-${BLENDER_VERSION}-linux-x64.tar.xz

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:/opt/blender:$PATH \
    BLENDER_BIN=/opt/blender/blender \
    BLENDER_SCRIPT=/app/blender_standardize_glb.py \
    MODEL_CONVERT_WORK_ROOT=/tmp/model-convert \
    MODEL_CONVERT_OUTPUT_PREFIX=model-convert/output \
    MODEL_CONVERT_MAX_INPUT_BYTES=536870912 \
    MODEL_CONVERT_DOWNLOAD_TIMEOUT_SECONDS=120 \
    BLENDER_TIMEOUT_SECONDS=300 \
    BLENDER_TEXTURE_RESOLUTION=2048

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       libegl1 \
       libgl1 \
       libsm6 \
       libx11-6 \
       libxfixes3 \
       libxi6 \
       libxkbcommon0 \
       libxrender1 \
       libxxf86vm1 \
       python3 \
       python3-venv \
       xz-utils \
    && curl -fsSL "https://download.blender.org/release/Blender5.0/${BLENDER_ARCHIVE}" -o /tmp/blender.tar.xz \
    && mkdir -p /opt/blender \
    && tar -xJf /tmp/blender.tar.xz --strip-components=1 -C /opt/blender \
    && rm /tmp/blender.tar.xz \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/blender-converter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/blender-converter/handler.py \
     scripts/blender-converter/runpod_entrypoint.py \
     scripts/blender-converter/blender_standardize_glb.py \
     ./

RUN useradd --create-home --uid 10001 converter \
    && mkdir -p /tmp/model-convert \
    && chown -R converter:converter /app /tmp/model-convert

USER converter

CMD ["python", "-u", "/app/runpod_entrypoint.py"]
