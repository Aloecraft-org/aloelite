# Must match the wasm-bindgen version in Cargo.lock. The bindgen schema is
# unstable and wasm-bindgen-test-runner refuses to run on a mismatch.
ARG WASM_BINDGEN_VERSION=0.2.114

# --- Stage 1: Protobuf Builder ---
# =================================
FROM debian:trixie-slim AS dc-proto-builder
RUN apt-get update && apt-get install -y curl unzip
RUN PROTOC_VERSION=$(curl -s "https://api.github.com/repos/protocolbuffers/protobuf/releases/latest" | grep -Po '"tag_name": "v\K[0-9.]+') \
    && curl -Lo protoc.zip "https://github.com/protocolbuffers/protobuf/releases/latest/download/protoc-${PROTOC_VERSION}-linux-x86_64.zip" \
    && unzip -q protoc.zip -d /usr/local \
    && chmod a+x /usr/local/bin/protoc

# --- Stage 2: Rust Tools Builder ---
# =================================
FROM rust:slim-trixie AS dc-rust-tools
ARG WASM_BINDGEN_VERSION
RUN cargo install trunk --locked
RUN cargo install geckodriver
RUN cargo install cargo-watch
RUN cargo install wasm-bindgen-cli --version ${WASM_BINDGEN_VERSION}
RUN cargo install cargo-edit jyt
RUN cargo install toml-cli

# --- Stage 3: Final Dev Container ---
# =================================
FROM rust:slim-trixie

RUN useradd -m vscode && echo "vscode ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
ENV RUSTUP_HOME=/usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo
ENV PATH="/usr/local/cargo/bin:${PATH}"

# 1. Add Targets (Fast, usually cached)
RUN rustup update
RUN rustup target add wasm32-unknown-unknown wasm32-wasip2 \
    x86_64-unknown-linux-musl aarch64-unknown-linux-gnu aarch64-unknown-linux-musl

# 2. Install System Deps (The "Apt Box")
RUN apt-get update && apt-get install -y \
    firefox-esr python3-pip python3-venv

RUN printf "[global]\nbreak-system-packages = true\nroot-user-action = ignore\n" > /etc/pip.conf
RUN python3 -m pip install --user --upgrade pip
ENV PATH="/root/.local/bin:${PATH}"
RUN pip install build

# 3. Copy from previous "Boxes" 
COPY --from=dc-proto-builder /usr/local/bin/protoc /usr/local/bin/
COPY --from=dc-proto-builder /usr/local/include/google /usr/local/include/google
COPY --from=dc-rust-tools /usr/local/cargo/bin/trunk /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/cargo-watch /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/wasm-bindgen /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/wasm-bindgen-test-runner /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/geckodriver /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/toml /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/cargo-add /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/cargo-rm /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/cargo-upgrade /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/cargo-set-version /usr/local/bin/
COPY --from=dc-rust-tools /usr/local/cargo/bin/jyt /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends wget curl
RUN curl https://wasmtime.dev/install.sh -sSf | WASMTIME_HOME=/usr/local bash
RUN wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq &&\
    chmod +x /usr/local/bin/yq

RUN chown -R vscode:vscode /usr/local
RUN chsh -s /bin/bash vscode

RUN rm /etc/sudoers
RUN apt install -y sudo
RUN echo "vscode ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
RUN mkdir -p /workspaces && chown -R vscode:vscode /workspaces
RUN chown -R vscode:vscode /usr/local
RUN mkdir -p /home/vscode/.vscode-server && chown -R vscode:vscode /home/vscode
RUN apt install -y libreadline-dev
USER vscode

RUN pip install xtrshow

RUN echo "alias xp=\"xtrpatch x.patch\"" >> /home/vscode/.bashrc \
&& echo "alias xs=\"xtrshow -o x.md\"" >> /home/vscode/.bashrc \
&& echo "alias cck=\"cargo check\"" >> /home/vscode/.bashrc
