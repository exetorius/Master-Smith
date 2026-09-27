# Master Smith API + worker in one image: Python 3.11, headless Blender 5.2 (Cycles CPU), FastAPI.
# Build:  docker build -t mastersmith .
# Run:    docker run -p 127.0.0.1:8080:8080 --env-file .env -v mastersmith-data:/data mastersmith
# Or, with the chat interface as well: docker compose up --build
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    BLENDER_VERSION=5.2.0 \
    BLENDER_BIN=/opt/blender/blender \
    MASTERSMITH_DATA=/data \
    PYTHONUNBUFFERED=1

# Blender's headless build still links against X11/GL client libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 \
        libgl1 libegl1 libgomp1 libsm6 libice6 && \
    rm -rf /var/lib/apt/lists/*

# download.blender.org answers 403 to plain curl; the nluug mirror carries the same release files.
ARG BLENDER_URL=https://ftp.nluug.nl/pub/graphics/blender/release/Blender5.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz
RUN curl -fsSL -A "mastersmith/0.1 (+https://github.com/kevinpbuckley/Master-Smith)" "${BLENDER_URL}" -o /tmp/blender.tar.xz && \
    mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 && rm /tmp/blender.tar.xz && \
    /opt/blender/blender --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mastersmith ./mastersmith
COPY tests ./tests
COPY README.md .

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://localhost:8080/healthz || exit 1
CMD ["python", "-m", "mastersmith", "serve", "--host", "0.0.0.0", "--port", "8080"]
