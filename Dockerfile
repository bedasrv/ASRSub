# asrsub (Rust): remote-API subtitle pipeline — no local models, no GPU.
# Multi-stage: build static-ish release binary, ship ffmpeg + ca-certs only.
ARG RUST_VERSION=1.85
FROM rust:${RUST_VERSION}-slim-bookworm AS build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
COPY Cargo.toml Cargo.lock* ./
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release && cp target/release/asrsub /asrsub

FROM debian:bookworm-slim
ENV HOME=/home/user DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 asrsub && useradd -m -u 1000 -g 1000 -d /home/user asrsub
COPY --from=build /asrsub /usr/local/bin/asrsub
COPY asrsub_providers.json /app/asrsub_providers.json
COPY assets/dashboard.html /app/assets/dashboard.html
WORKDIR /app
USER asrsub
ENTRYPOINT ["asrsub"]
CMD ["daemon"]
