# Reverse-engineering lab used for the podmicusb-doctor investigation (see docker/README.md).
# Build from the docker/ folder:  docker build -t podmic-re docker/   (ghidra.zip must be in docker/)
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# 1. System utilities, Neovim, Node.js 26, Python, OpenJDK 21, build tools, TShark
#    (build-essential + cmake + meson are needed for Rizin and rz-ghidra)
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    curl \
    git \
    neovim \
    openjdk-21-jdk-headless \
    python3 \
    python3-pip \
    meson \
    ninja-build \
    pkg-config \
    libzip-dev \
    libssl-dev \
    unzip \
    tshark \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 2. Rizin, pinned so rz-ghidra can be built against the exact same version.
#    Default = the commit the lab was running (rizin 0.10.0 @ 46565f6).
#    For a release build, set both refs to matching tags, e.g.
#    --build-arg RIZIN_REF=v0.x.y --build-arg RZ_GHIDRA_REF=v0.x.y
ARG RIZIN_REF=46565f6b5f09d00e9889bae8ad2962cab6a94de7
RUN git clone https://github.com/rizinorg/rizin.git /tmp/rizin && \
    cd /tmp/rizin && git checkout "${RIZIN_REF}" && \
    meson setup build --prefix=/usr/local && \
    meson compile -C build && \
    meson install -C build && \
    ldconfig && \
    rm -rf /tmp/rizin

# 3. rz-ghidra decompiler plugin (provides `pdg`), installed system-wide so it
#    is not hidden by a volume mounted over /root.
#    "dev" tracks Rizin's development branch; use a release tag matching RIZIN_REF otherwise.
ARG RZ_GHIDRA_REF=dev
RUN git clone --recurse-submodules https://github.com/rizinorg/rz-ghidra.git /tmp/rz-ghidra && \
    cd /tmp/rz-ghidra && git checkout "${RZ_GHIDRA_REF}" && \
    git submodule update --init --recursive && \
    cmake -S . -B build -G Ninja \
        -DCMAKE_PREFIX_PATH=/usr/local \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DRIZIN_INSTALL_PLUGDIR="$(rizin -H RZ_LIBR_PLUGINS)" && \
    ninja -C build && ninja -C build install && \
    rm -rf /tmp/rz-ghidra && \
    rizin -qc 'Ll' -- | grep -qi ghidra

# 4. AI tooling (optional for this repo): REA and Claude Code
RUN npm install -g rea-agents @anthropic-ai/claude-code

# 5. Ghidra (from https://github.com/NationalSecurityAgency/ghidra/releases, saved as docker/ghidra.zip).
#    Lives in /opt so a bind mount over /workspace does not hide it.
COPY ghidra.zip /tmp/ghidra.zip
RUN cd /tmp && unzip -q ghidra.zip && rm ghidra.zip && \
    mv /tmp/ghidra_*_PUBLIC /opt/ghidra && \
    chmod +x /opt/ghidra/support/analyzeHeadless

ENV JAVA_HOME="/usr/lib/jvm/java-21-openjdk-amd64"
ENV GHIDRA_INSTALL_DIR="/opt/ghidra"

# Claude Code: always use the fullscreen terminal UI (same as `/tui fullscreen`)
ENV CLAUDE_CODE_NO_FLICKER=1

# No secrets are baked into the image; pass them at run time if needed (docker run --env-file .env ...).

WORKDIR /workspace
CMD ["/bin/bash"]
