# asrsub (Rust): remote-API subtitle pipeline — no local models, no GPU.
# Multi-stage: build static-ish release binary, ship ffmpeg + ca-certs only.
# RUST_VERSION must satisfy Cargo.toml's rust-version; keep it in sync or
# the CI `docker` job fails.
# cmake is a build-time-only dep of aws-lc-rs (reqwest 0.13 rustls provider).
ARG RUST_VERSION=1.98
FROM rust:${RUST_VERSION}-slim-trixie AS build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gcc libc6-dev cmake \
    && rm -rf /var/lib/apt/lists/*
# Cargo.lock must be in the build context (see .dockerignore): --locked builds
# the release image from the committed dependency set that CI tested, and fails
# the build rather than silently resolving fresh versions.
COPY Cargo.toml Cargo.lock ./
COPY src ./src
# The dashboard (htmx/CSS) is embedded via include_str!, so it must be present
# at compile time even though it is not copied into the runtime stage.
COPY assets ./assets
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release --locked && cp target/release/asrsub /asrsub

FROM debian:trixie-slim
ENV HOME=/home/user DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 asrsub && useradd -m -u 1000 -g 1000 -d /home/user asrsub
COPY --from=build /asrsub /usr/local/bin/asrsub
# Provider template baked keyless: images never carry keys. At runtime leave
# api_key empty and export the key_env vars (see docs/DEPLOY.md).
# The dashboard (htmx + CSS) is embedded in the binary via include_str!.
COPY asrsub_providers.json.example /app/asrsub_providers.json
WORKDIR /app
USER asrsub
ENTRYPOINT ["asrsub"]
CMD ["daemon"]
