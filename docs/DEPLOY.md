# ASRSub Deploy Instructions (Explicit, Immutable Release)

**Requirement (not executed evidence):** images are built by CI and published
as immutable OCI digests. The tracked `docker-compose.yml` is a template; the
sole renderer is `tools/compose_provenance.py`, which produces the exact
interpolation-free candidate installed at `/usr/local/libexec/asrsub/compose.yaml`.
The template hash and rendered Compose hash are separate approval-bound
identities. A mutable tag or registry lookup is never a production identity.

**Rollout-only evidence:** the final digest, release SHA, signed bundle,
approval generation, installed inventory, and target receipt must be read back
on the approved target before systemd may start the runtime. This document
does not claim that any rollout, secret activation, or host mutation has run.

## What's in the image

Single statically-built `asrsub` Rust binary + ffmpeg + ca-certs on
Debian slim. **No Python, no model weights, no GPU runtime** — all Whisper
and LLM inference is remote via `asrsub_providers.json`, of which the image
bakes only the **keyless template** (`asrsub_providers.json.example`):
images never carry keys. At runtime leave `api_key` empty and supply the
`key_env` vars in
`/home/user/.config/asr-pipeline/secrets/provider_keys.env` (`chmod 600`;
override the path with `PROVIDER_KEYS_FILE`), which the shipped compose loads
into the container environment through `env_file`. `resolve_key` prefers a
non-empty `api_key` and otherwise reads `$key_env` from the process
environment at call time, and Docker populates that environment when the
container starts — so after rotating a key, re-run `docker compose up -d`
(Compose recreates the container when the rendered environment changes).
The operator dashboard is server-rendered by the daemon at `/` and `/ui/*`
(JavaScript/CSS embedded in the binary — no runtime asset files). Read views
are open; settings and episode actions use ordinary POST/redirect/GET and
require the control key, entered once in the dashboard header (kept in
`sessionStorage` only). Successful mutations return `303 See Other` to a
complete page; failures remain complete HTML responses with their original
status. Settings writes go to `config.overrides.json` and apply on the next
daemon restart.

The settings form exposes only keys the config layers control. A few tuning
knobs are read straight from the process environment by their consumers
(`providers.rs`, `jimaku.rs`), so **neither** the dashboard's
`config.overrides.json` **nor** `pipeline.env` reaches them — `pipeline.env`
is parsed into the config map, never exported into the process environment.
Put these in the container environment (the compose `environment:` block or
the optional `env_file`): `LLM_PER_ENDPOINT_CONCURRENCY`, `LLM_TIMEOUT_S`,
`LLM_CONNECT_TIMEOUT_S`, `LLM_READ_TIMEOUT_S`, `TRANSLATION_TIMEOUT_S`,
`WHISPER_CONCURRENCY`, `WHISPER_TIMEOUT_S`, `JIMAKU_BASE_URL`,
`JIMAKU_CALL_SLEEP_MS`, `JIMAKU_TIMEOUT`, `ANILIST_TIMEOUT`,
`ANILIST_BASE_URL`, `RUST_LOG` (verbosity, `EnvFilter` syntax).
`ANILIST_CACHE` is **not** one of these: it is an ordinary settings field,
editable in the dashboard and settable from `pipeline.env`. Each provider entry's `key_env` is also read from the process
environment, never from a config file — that is how `provider_keys.env`
reaches the pipeline.

An **empty** variable is not a value: it pins nothing, it is skipped when the
layers merge, it is treated as unset by the consumers above, and a file value
survives it. `WEBHOOK_PORT=` in the compose `.env` therefore cannot blank a
setting. One exception: an empty `RUST_LOG` is a *valid empty filter* for
`EnvFilter`, so it silences the daemon instead of falling back to the default
level — unset the variable, or set a level. Note the other direction too: blanking a variable does **not** clear a
value that lives in `pipeline.env` or `config.overrides.json`. To clear one,
edit that file, `POST /api2/config {"KEY":""}`, or delete the key from
`config.overrides.json` — the settings form never submits an empty `Secret`.

Two settings are **pinned by the shipment**, because the bind mount and the
reverse proxy must agree with the daemon: `NAS_MEDIA_PREFIX` and
`WEBHOOK_PORT`. Compose exports both into the container, process env outranks
every config layer, so the dashboard renders them read-only — a value saved
from the UI could never take effect. Change them in the compose `.env`.

## Prerequisites

## Hardened boundary (requirements)

- Runtime secrets are staged by a root-owned, journaled deployment operation
  into `/var/lib/asrsub/runtime-secrets/`; the container sees only the fixed
  read-only projections `/run/secrets/discord_webhook` and
  `/run/secrets/control_api_key`. The operator source directory, provider-key
  source, approval files, rollback material, deployment journal, and Docker
  socket are never mounted.
- The application runs as UID/GID `1000:1000` with no capabilities and
  `no-new-privileges`. StateFs is separate from the pipeline JSONL ledgers and
  is admitted only after no-follow, ownership, mode, filesystem, and mount
  identity checks. Systemd owns recovery and restart; Compose does not.
- Deployment admission is blocked before quiesce, replacement, recovery, or
  journal mutation and is reopened only after terminal journal, receipt,
  evidence, and active-set witnesses read back. Pending notification state is
  preserved across deployment; no drain/reset side effect is defined.
- The daemon uses an in-process resolver snapshot for Discord. It does **not**
  claim a Discord-only firewall or cgroup allowlist because Discord shares the
  ASRSub process with other integrations.

**Local evidence:** disposable fixtures and localhost/fake transports verify
Compose projections, journal transitions, StateFs behavior, child policy,
provenance, and receipts. **Rollout-only evidence:** effective container
mounts, cgroup delegation, resolver peers, systemd ownership, running image
identity, and rollback timestamps require a target-VM receipt.

- GHCR read access on the host (one time):
  ```bash
  docker login ghcr.io   # PAT with read:packages
  ```

- Secret file present on host (not in repo, not in compose env):
  ```bash
  mkdir -p /home/user/.config/asr-pipeline/secrets
  chmod 700 /home/user/.config/asr-pipeline/secrets
  # Create secret file with raw key (no newline, chmod 600)
  printf '%s' '<CONTROL_API_KEY>' > /home/user/.config/asr-pipeline/secrets/control_api_key
  chmod 600 /home/user/.config/asr-pipeline/secrets/control_api_key
  # Verify compose picks it up (requires ASRSUB_IMAGE to be set for config rendering):
  ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:<full-40-char-sha> CONTROL_API_KEY_FILE_HOST=/home/user/.config/asr-pipeline/secrets/control_api_key docker compose config | grep -A2 control_api_key
  ```
  The file is mounted as a Docker Compose secret at `/run/secrets/control_api_key` inside containers. Environment `CONTROL_API_KEY` is only for hermetic tests.

- Provider API keys, if the providers file leaves `api_key` empty. One
  `KEY_ENV=value` line per `key_env` name the file references:
  ```bash
  printf '%s\n' 'OPENROUTER_API_KEY=...' 'OPENCODE_ZEN_API_KEY=...' \
    'COMMANDCODE_API_KEY=...' 'NOUS_API_KEY=...' \
    > /home/user/.config/asr-pipeline/secrets/provider_keys.env
  chmod 600 /home/user/.config/asr-pipeline/secrets/provider_keys.env
  # Verify the rendered container environment picks them up. `docker compose
  # config` interpolates env_file values, so print the names only — piping it
  # through a plain grep would print the live keys into your scrollback:
  ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:<full-40-char-sha> docker compose config \
    | grep -oE '^ *[A-Z_]+_API_KEY:'
  ```
  The compose `env_file` entry is `required: false`, so a deployment that
  keeps every key inside the mode-600 providers file still validates. (That
  mapping form, with `required:`, needs Docker Compose v2.24+; older plugins
  reject the file.) Values
  are readable by anyone who can run `docker inspect` on the container — on a
  single-admin host that is the same exposure as the providers file it
  replaces, and it keeps keys out of the config the daemon re-reads.

- Non-control settings remain in `/home/user/.config/asr-pipeline/pipeline.env` (mounted as a volume, not injected as env); copy `pipeline.env.example` as a starting point. Do not put `CONTROL_API_KEY` there, and do not put provider API keys in it — provider keys live in the providers file or, preferably, in `secrets/provider_keys.env` via each entry's `key_env` (see above).

- Media layout (see `HEALTH.md`). Two settings describe the same media from
  two vantage points and are both configurable:
  - `NAS_MEDIA_PREFIX` (default `/mnt/nas/share/media`) — where **this
    container** reads media; `/data/…` paths from Sonarr/Radarr map here.
    Set it in the compose `.env`, because the bind mount must match.
  - `JELLYFIN_MEDIA_ROOT` (default `/media`) — the path prefix the
    **Jellyfin server** reports; set it in `pipeline.env` to match Jellyfin.
    The compose file also uses the value from *its own* environment as the
    target of the second (read-only) media mount, and Compose cannot read
    `pipeline.env`: change it in both places, or leave it at `/media`.
  `MEDIA_HOST_PATH` is the host directory bind-mounted into the container
  (default `/mnt/nas/share/media`), mounted at both prefixes.

- Jellyfin has **no compiled-in URL**; set `JELLYFIN_URL` explicitly in
  `pipeline.env` (or leave it empty to disable refresh). A key without a URL
  logs a warning and `/ready` reports `jellyfin_misconfigured`.

- Check health prerequisites (see `HEALTH.md`):
  ```bash
  df -T /home/user/.config/asr-pipeline
  mountpoint -q /mnt/nas/share/media && echo "NFS mounted" || echo "NFS missing"
  ls -lh /home/user/.config/asr-pipeline/*.jsonl 2>/dev/null || echo "no state ledgers yet (first boot is fine)"
  ```

## Build (CI-owned, immutable, versioned)

Every push to `main` triggers `.github/workflows/release.yml`: it builds
and publishes a release identified by the full Git SHA and its registry content
digest. The SHA tag is a lookup label for CI and development; the production
approval records the immutable
`ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>` reference. A mutable tag or
registry lookup is never a production identity. The workflow emits the
non-secret release descriptor as an artifact (`release.json`:
`asrsub_image` / `git_sha` / `build_time` — no secrets).

`build.sh` remains for local/dev builds only: it tags the same
`ghcr.io/bedasrv/asrsub:<git-sha>` scheme (override the registry with
`ASRSUB_REGISTRY=` for mirrors), emits `.release.env` + `release.json`
locally, and **never pushes**. It never runs `docker compose down`,
`docker rmi`, or `docker builder prune`.

- Never deploy a mutable `latest` tag or a SHA tag alone. Production accepts only the approved full `@sha256:` image reference.
- Retains volumes: `/home/user/.config/asr-pipeline`, `/home/user/.cache/asr-pipeline`, and the host media directory.
- Single `orchestrator` service: the daemon serves the dashboard at `/` and the API at `/api2/*`; there is no separate dashboard replica (a read-only state mount used to restart-loop with `EROFS`).
- Release descriptor contains only `ASRSUB_IMAGE` / `GIT_SHA` / `BUILD_TIME` — no secrets.

## Deploy (systemd-owned, explicit, immutable)

Production deployment is owned by systemd and the checked-in fixed-path
adapters. The recovery unit must pass before Docker or runtime reconciliation
can start. The adapter reads the authenticated release transaction, verifies the
rendered Compose hash and `@sha256:` image identity, pulls only that digest,
and starts Compose with the fixed project and `/dev/null` environment file using
`up -d --no-build --pull=never`. It does not accept caller-selected Compose files, `.env` files, override files,
Docker contexts, proxies, or mutable tags.

```bash
# These are the fixed systemd entrypoints; systemd owns their invocation.
/usr/local/libexec/asrsub/asrsub-recover --preflight
/usr/local/libexec/asrsub/asrsub-runtime --reconcile
```

A missing approval, trust anchor, rendered Compose file, StateFs root, runtime
inventory member, or Docker executable blocks with a non-zero status. This
checkout has not executed a production rollout and does not fabricate a signed
bundle or target evidence.

For local or fixture-only checks, keep using the explicit `--test-seam` or
fixture harnesses. Do not run bare `docker compose config`: it can resolve
`env_file` values into output. A non-production development check may use
`docker compose pull`, but that command is not the production deployment path.

## Readiness verification

A ready deployment answers `200` on `/ready`; a `503` names the failing
local check (`checks.media_root`, `checks.providers`, `checks.state_dir`)
and reports integrations as diagnostics. A deployment smoke test is
provided:

```bash
scripts/deploy_smoke.sh                      # defaults to http://127.0.0.1:8085
BASE_URL=http://127.0.0.1:8085 scripts/deploy_smoke.sh
```


## Reverse proxy & firewall (port 8085)

The daemon listens on `0.0.0.0:${WEBHOOK_PORT}` (default `8085`) — both the
dashboard/API and the `/webhook` receiver share it. Putting it behind an
authenticated reverse proxy (Pomerium, oauth2-proxy, nginx, Traefik, …)
requires the network path to actually reach the upstream, not just a
working SSO page.

- **Upstream target**: `http://<asrsub-host>:8085` on the LAN/DMZ. With
  `network_mode: host` the proxy can target the host directly.
- **Firewall/ACL**: allow the **proxy host → asrsub host, TCP 8085**. A
  missing DMZ-to-LAN allowlist entry is the most common failure: the public
  route returns the SSO `302` (proxy healthy) while the authenticated
  upstream path is blocked.
- **Probes**: point the proxy health check at `/health` (liveness) or
  `/ready` (readiness). Both are unauthenticated GETs; do not require SSO
  for them.
- **WebSockets / long-lived connections**: enable upgrade/streaming
  timeouts for `/` and `/api2/*` if the dashboard holds a connection; keep
  `/webhook` reachable from the media server (it is authenticated by
  `X-API-Key`/`X-Control-Key`).
- **SSO vs upstream**: an unauthenticated request returning the IdP `302`
  proves only that the proxy works. Verify the upstream itself from the
  **proxy network** and from a trusted host:

  ```bash
  # From the proxy host/network: the origin must answer directly.
  curl -sf -o /dev/null -w '%{http_code}\n' http://<asrsub-host>:8085/health   # expect 200
  curl -sf -o /dev/null -w '%{http_code}\n' http://<asrsub-host>:8085/ready    # expect 200
  # Public, unauthenticated: expect a 302 to the IdP, NOT a 200.
  curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' https://asrsub.example.com/
  # Authenticated through the proxy: expect the dashboard HTML.
  curl -sf -H 'Cookie: <session>' https://asrsub.example.com/health | jq .
  ```

A public `200` on protected routes means auth is bypassed; a public `302`
with a dead authenticated path means the firewall/ACL above is missing.

## Upgrading from the two-service layout

Older deployments ran `orchestrator` **and** a second daemon as `dashboard`
on `WEBHOOK_PORT=8080` over a read-only state mount. That replica
restart-looped with `EROFS` and is gone: one service now serves `/` and
`/api2/*` on `WEBHOOK_PORT` (default 8085), so update anything that pointed
at `:8080` — proxies, bookmarks, uptime monitors.

`pctl` follows the same single-service API. Two subcommands changed:
`status2` is now `api-status`, and `config unset` was removed (delete the key
from `config.overrides.json` instead). A key file outranks the variable, and the
candidates are tried in this order: `CONTROL_API_KEY_FILE` (the environment
variable if set, else that config key, whose shipped default is
`/run/secrets/control_api_key`), then the literal
`/run/secrets/control_api_key`, and only then the `CONTROL_API_KEY`
**environment variable**, which exists as a test fallback. An empty
`CONTROL_API_KEY` therefore contributes no key, and with no key file present
every control request is refused — but blanking the variable alone does **not**
disable the control API while a key file is there; remove the secret file as
well. No value of `CONTROL_API_KEY_FILE` can prevent the daemon reading
`/run/secrets/control_api_key`: the literal path is an unconditional candidate,
and an empty variable is treated as unset, so the config key falls back to that
same default. The *token* is never read
from a config *value*: a key written into `pipeline.env` or
`config.overrides.json` does not authenticate, and the daemon no longer reads
`PIPE_TOKEN`. The *path* in `CONTROL_API_KEY_FILE` is an ordinary config key,
so any layer may point it somewhere else
(`CONTROL_API_KEY_FILE=/srv/keys/asrsub` in `pipeline.env` works and that
file's contents then authenticate). The bundled `pctl` client is separate and
reads the sources in the **opposite** order — `PIPE_TOKEN`, then
`CONTROL_API_KEY`, then `CONTROL_API_KEY_FILE`, `/run/secrets/control_api_key`
and `<config dir>/secrets/control_api_key` — so a client whose environment
holds a stale `PIPE_TOKEN` sends a token the daemon will not accept.

## Rollback (approved digest, systemd-owned)

Read the previous release descriptor and approval transaction. A previous SHA
tag such as `ghcr.io/bedasrv/asrsub:<previous-40-char-sha>` is only a lookup
label; it must resolve to a newly authenticated immutable digest before use.
Do not point production Compose at a tag.

```bash
# The recovery gate and runtime reconcile consume the approved previous digest.
/usr/local/libexec/asrsub/asrsub-recover --preflight
/usr/local/libexec/asrsub/asrsub-runtime --reconcile
```

Do NOT use `docker tag ... :latest` — the mutable latest tag is intentionally
absent from the production flow.

## Paused Startup

Not implemented at boot: the daemon always starts unpaused (no `paused`
file, no `PAUSED=1` handling — those belonged to the retired Python
daemon). The systemd-owned runtime starts only after the authenticated
reconciliation gate passes. Do not manually start a mutable image to hold the
backlog.

## Notes

- No secret values in code, docs, tests, or logs. Release descriptor (`.release.env` / `release.json`) is non-secret by construction.
- Source language fails closed: the audio track is picked by its real tag and that code is sent to Whisper; a tag that is absent **or unusable** (`und`, `unknown`, an empty/whitespace tag) is not sent at all — such a track is probed unforced and the detected code pins the rest. If the language cannot be established the episode errors (state row `error`) and nothing is uploaded or registered `done` — a deployment never commits a subtitle whose source language is unknown. Expect no `source_stream`/`source_lang` in the registry `extra` for old rows; new rows carry `source_lang` on **every** row and `source_stream` on **ASR rows only** (ladder rows have no chosen audio stream).
- Build and deploy logs must redact `CONTROL_API_KEY`.
- Verify after deploy: `HEALTH.md` probes, `docker compose ps`, and `pipeline.env` contains no control key.
