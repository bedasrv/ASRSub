# ASRSub Health / Readiness Contract

This document defines the local health and readiness story for the ASRSub
pipeline: a single Rust `asrsub` binary running as one compose service (the
daemon serves both the dashboard and the API on `WEBHOOK_PORT`, default
`:8085`). It is the authoritative contract for deployment and monitoring.

## Endpoints (actually served)

- `GET /health` — **Liveness** (unauthenticated, always open). Returns `200
  {"ok": true}` if the process is running. Does **not** check any
  dependency; use it for container liveness probes. (Also `/api2/health`.)
- `GET /ready` — **Readiness** (unauthenticated). Evaluates the local
  prerequisites below and returns `200` when ready, `503` otherwise. It
  never makes remote calls and never gates on Sonarr/Bazarr/Jellyfin, so a
  slow or down integration cannot flap readiness. (Also `/api2/ready`.)
- `GET /status` — Daemon state (unauthenticated): `paused`, `uptime_s`,
  `last_pass{at,scanned,done,failed}`, `current`, `started_at`,
  `run_once_requested`, `media_ok` (the configured media root is present — a
  dead mount no longer looks like "idle").
- `GET /config` — Merged config with secrets masked (by key name, and any `user:pass@` userinfo inside a value, including a scheme-less `user:pass@host`, and in whatever spelling the separating colon arrives: `:`, `%3A` up to eight escape layers deep, `&#58;`/`&#x3a;`/`&colon;` with or without the trailing semicolon, or the full-width colon — and a numeric entity counts whatever follows it, so the credential-free `&#580` is masked rather than risk publishing `svc&#x3aabc@inner`). Masking fails closed: a credential is masked whatever the host looks like — a trailing space, a non-ASCII or percent-encoded host, an alphabetic port, a password that starts with digits and contains a `/`, a user name that itself contains an `@`, a name that contains an `@` *and* a colon written as an escape (`a@b&#58Zk1P@host.lan`, `a@b%3AZk3P/ss@host.lan`, `a@b&colonZk3P/ss@host.lan` — a `#` inside an entity is an entity introducer, not a fragment boundary, and a candidate "host" that carries a colon in any other spelling is not read as one), a value with no scheme whose own text embeds a URL (`nominal@host/redir?url=http://svc:pw@inner`) — and a password containing `/`, `?` or `#` cannot hide behind a path separator: a colon-free name does not end the check, because the text after the `@` is scanned for its own credential. Every credential-shaped run in a value is masked, not only the authority's: a value embedding a second credentialed URL in its own path or query (`https://user:pw@gw.lan/redirect?url=http://a:b@c`) becomes `https://***:***@gw.lan/redirect?url=http://***:***@c`, so the URL stays legible instead of the inner pair staying visible. Deliberate trade-offs, all documented at the function and pinned by tests: a `@` that sits past the authority when neither the text in front of the separator nor the text in front of the `@` carries a colon (a base URL like `https://bazarr.lan/api?x=a@b`, or `file:///path@x`) is left alone — the price is that `https://host:6767/path@x` is masked though it holds no credential, and that a scheme-less tail credential is masked at its innermost run, so a query key can be swallowed (`...?to=svc:pw@inner` becomes `...?***:***@inner`); the port colon of a `host:port` following an already-masked user name is not treated as credential evidence, so `https://user@host:6767/x@y` keeps its readable host and port (`https://***@host:6767/x@y`) while a digit-only password sitting in that host slot is published with it (`https://u@svc:1234/x@y`) — the price of a readable host; a bracketed IPv6 literal counts as a host (`user@[::1]:8080/x@y` stays intact) but an empty host or port falls back to masking; and a semicolon-less named entity is read as a colon wherever it appears, so a credential-free `&colony` is masked too; a *colon-free* authority-less user name is published (`https://///pw@host`, `https:///path@x`); a `user:pass` pair with no `@` anywhere after it is indistinguishable from `host:port` and is left as it stands (`https://gw.lan/r?url=https://u:p`); a bracketed host slot counts as a host only when it parses as an IPv6 literal, so `user@[::1]:8080/x@y` stays readable while `user@[root:s3cr3t]/x@y` is masked (a bracket is not a licence to hide a password); a scheme-qualified bare user name is masked to `***` though it holds no password (`ssh://git@github.com/owner/repo`), while the scheme-less `user@host` spelling is untouched; and a scheme-less value whose first word is a non-hierarchical scheme with no port after the host (`mailto:admin@example.com`, `urn:isbn:…@x`) is read as that URI — which would also publish a credential whose user name is literally `mailto`/`data`/`tel`/…, the price of not masking every email address. `/config` is not the only sink for these values, and every sink goes through the same masker (`mask_for_log`): a log line that echoes a configured value, the settings form and the overview stat (both served without authentication, so a form field shows its masked value and posts that masked value back — an untouched field is skipped, never written), the `media_root.path` field of `/ready` and `/api2/ready`, the providers loader's "tried …" and read/parse messages, the whisper endpoint and transport error text, the Jimaku error chains, and the derived single-instance lock filename — a `.daemon.lock` name published a credential on disk, so the lock now sits **beside** the state file under its masked name plus a short digest of the real path (a credential-free path keeps its exact name, and two state files can never share one lock). The CLI prints one masked cause chain instead of each site printing its own: `reqwest` puts the request URL into its error text, so masking the rendered chain closes the class rather than chasing the sites.
- `POST /pause /resume /run-once /wake /webhook` — Control (require
  `X-API-Key` (or the `X-Control-Key` alias that media-server notification
  plugins send): `<control key>` from a key file (`CONTROL_API_KEY_FILE` if set,
  else the shipped `/run/secrets/control_api_key`); the `CONTROL_API_KEY`
  variable is tried last, so a key file outranks it).
- `/api2/*` — Telemetry + episode actions (same auth rule for POSTs).

### `/ready` contract

```jsonc
{
  "ready": true,                       // 200 when true, 503 when false
  "checks": {
    "media_root": {"ok": true, "path": "/mnt/nas/share/media"},
    "providers":  {"ok": true, "llm": 14, "whisper": 1,
                    "llm_keyed": 14, "whisper_keyed": 1},   // resolvable keys
    "state_dir":  {"ok": true, "path": "/home/user/.config/asr-pipeline"}
  },
  "integrations": {                    // diagnostics ONLY — never gate ready
    "sonarr": true,
    "bazarr": true,
    "jellyfin": false,
    "jellyfin_misconfigured": false    // API key set but JELLYFIN_URL empty
  }
}
```

Readiness requires all three gating checks:

1. **`media_root`** — the directory named by `NAS_MEDIA_PREFIX` is a mount
   point (checked in `/proc/self/mountinfo`) or a non-empty tree. `is_dir()`
   alone was not enough: when a bind mount's source goes away, Docker creates
   an empty directory at the target, which used to report the media as
   present. This is where `/data/…` paths from Sonarr/Radarr are mapped and
   what the daemon reads; it must be the mounted media tree.
2. **`providers`** — at least one LLM model **and** at least one Whisper
   endpoint have a *resolvable key* (a non-empty `api_key`, or a `$key_env`
   that is set). The daemon refuses to start without an LLM, and the pipeline
   skips keyless endpoints, so counting configured entries hid a container
   whose provider keys were never injected: it reported ready and then did no
   work at all. The payload reports totals plus `llm_keyed`/`whisper_keyed`.
3. **`state_dir`** — the directory containing `STATE_FILE` is writable. A
   short-lived probe file (per-process name) is created and removed, and the
   result is memoized for 15 s: `/ready` is unauthenticated and polled by the
   compose healthcheck every 30 s and by the dashboard every 5 s, so a GET
   must not mutate the state directory on every call. State lives here.

`integrations` reports whether the optional `SONARR_URL`, `BAZARR_URL` and
`JELLYFIN_URL`/key pairs are configured. `jellyfin_misconfigured` is `true`
whenever a Jellyfin key is set without a URL (no site-specific default
ships in code, so refresh is silently off until the URL is set).

Use `/health` for liveness and `/ready` for readiness:

```bash
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/ready  | jq .   # non-zero exit when 503
```

## Operational prerequisites

Checked by `/ready` (media root, state dir, providers) or by the operator:

### 1. Local State Directory

- **What**: `/home/user/.config/asr-pipeline` (or `$STATE_FILE` dirname)
  should be local, writable storage with headroom. State files
  (`state.jsonl`, `subtitle_registry.jsonl`, `actions.jsonl`) are
  append-or-atomic-replace under `flock` sidecars — never on NFS/tmpfs.
- **How**:
  ```bash
  df -T /home/user/.config/asr-pipeline
  stat -f -c %T /home/user/.config/asr-pipeline  # expected: btrfs
  test -w /home/user/.config/asr-pipeline
  ```

### 2. Media Mount

- **What**: the directory named by `NAS_MEDIA_PREFIX` (default
  `/mnt/nas/share/media`) must be mounted and contain the media tree. The
  compose file mounts the host media directory (`MEDIA_HOST_PATH`) there.
- **Jellyfin mapping**: `JELLYFIN_MEDIA_ROOT` (default `/media`) is the path
  prefix the **Jellyfin server** reports for the same files. It is a string
  mapping used only for the refresh lookup — the daemon does not read it
  from disk. Keep it aligned with the Jellyfin server's container path.
- **Why**: Missing media must never trigger destructive cleanup — episodes
  without files on disk error the pass item (`failed`, state row `error`)
  and are retried next pass; nothing is pruned.
- **How**:
  ```bash
  mountpoint -q "${NAS_MEDIA_PREFIX:-/mnt/nas/share/media}" || \
    grep -q " ${NAS_MEDIA_PREFIX:-/mnt/nas/share/media} " /proc/mounts
  curl -sf http://127.0.0.1:8085/ready | jq .checks.media_root
  ```

### 3. Webhooks (no inbox ledger)

The retired Python daemon used a SQLite webhook inbox
(`webhook_inbox.db` + integrity checks). The Rust daemon has **no inbox
DB**: `POST /webhook` (authenticated like every other control POST — send
`X-API-Key` or `X-Control-Key: <CONTROL_API_KEY>`, otherwise 401) wakes the
pass loop and extracts embedded subtitles on a spawned task, with
concurrent duplicates for the same file collapsed to one extraction.
A POST without a `file`/`filePath`/`path` field is a no-op.
There is nothing to `PRAGMA
integrity_check` — if you migrated from the Python deployment, the stale
`.db` files under the state dir are inert and can be archived away.

> OPS: the Tdarr/Sonarr notification that POSTs `/webhook` MUST carry the
> key header — without it webhooks 401 and new episodes wait for the next
> periodic pass (30–120s) instead of starting immediately.

### 4. Runtime Pause (no paused boot)

The daemon always starts **unpaused** (no `paused` file, no `PAUSED=1` —
those belonged to the retired Python daemon). Hold/resume at runtime:

```bash
curl -H "X-API-Key: $(cat /run/secrets/control_api_key)" -X POST http://127.0.0.1:8085/pause
curl -H "X-API-Key: $(cat /run/secrets/control_api_key)" http://127.0.0.1:8085/status | jq .paused
# To resume: POST /resume (or /api2/resume)
```

## Operational Notes

- **No secret values** appear in health/readiness responses, logs, or dashboards. `CONTROL_API_KEY` is loaded from `/run/secrets/control_api_key` (or `CONTROL_API_KEY_FILE`); `pipeline.env` at `/home/user/.config/asr-pipeline/pipeline.env` holds non-control settings (BAZARR_URL, SONARR_URL, JELLYFIN_URL, …) and is mounted as a volume, not injected as environment variables.
- **One daemon serves the dashboard**: exactly one `orchestrator` service runs the daemon, which serves the UI at `/` and the API at `/api2/*` on `WEBHOOK_PORT` (default 8085). There is no separate dashboard replica: running a second full daemon on a read-only state mount caused an `EROFS` restart loop and risked competing state writers. Dashboard mutations (`POST /ui/…`) answer `400` with a rendered fragment when nothing was saved (a deployment-pinned key, an out-of-range value, a failed write) and mark every error body **from a dashboard route** with `X-Asrsub-Fragment: 1`, so a script can tell a refusal from a save and the shell can swap app errors without pasting proxy pages into a pane. An unmatched path (a typo'd `/ui/…`, an unknown `/assets/…`) is deliberately *not* marked and returns the plain 404, so the fallback cannot inject a fragment into whatever pane htmx was targeting. `200` means the write landed (or there was nothing to change). The same rule guards both write paths, so `POST /api2/config` refuses an out-of-range value too — a value the loader would only warn about and discard is never persisted.
- **Empty means unset, in every layer**: a variable that is set but empty (or whitespace-only) pins nothing, does not shadow a file value during the merge, is not validated, and is treated as unset by the consumers that read the environment directly — `PROVIDERS_FILE`, `ASRSUB_CONFIG_DIR`, `HOME`, `JIMAKU_BASE_URL`, `JIMAKU_CALL_SLEEP_MS`, `JIMAKU_TIMEOUT`, `ANILIST_BASE_URL`, `ANILIST_CACHE`, `ANILIST_TIMEOUT`, `LLM_TIMEOUT_S`, `LLM_PER_ENDPOINT_CONCURRENCY`, `WHISPER_TIMEOUT_S`, `WHISPER_CONCURRENCY`, `CONTROL_API_KEY` (empty denies control access) and every provider entry's `key_env`. Because of that, **blanking a variable no longer clears a value that lives in `pipeline.env` or `config.overrides.json`** — the file value survives. To clear such a value, edit `pipeline.env`, `POST /api2/config {"KEY":""}`, or delete the key from `config.overrides.json`; note the settings form never submits an empty `Secret`, so a password is cleared through one of those, not through the UI.
- **Build vs deploy**: images are built by CI and pushed to GHCR as immutable `ghcr.io/bedasrv/asrsub:<full-40-char-git-sha>` (`build.sh` is local/dev builds only and never pushes); deploy pulls an explicit tag (`docker compose pull`, then `up -d --no-build`). The compose file fails closed if `ASRSUB_IMAGE` is unset. Deployment detail lives in `DEPLOY.md`.
- **Probes** (compose ships this healthcheck; `/health` for liveness, `/ready` for readiness). The probe is `CMD-SHELL` so it follows `WEBHOOK_PORT` — the same variable the daemon binds:
  ```yaml
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://127.0.0.1:${WEBHOOK_PORT:-8085}/ready"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 20s
  ```
  Release gating: `/ready` 200. Rollout gating (e.g. `depends_on:
  condition: service_healthy`) should also use `/ready`.

## Verification (local)

```bash
cargo test            # Rust suite (offline; includes full-program simulation)
python3 -m unittest tests.test_immutable_release_contract tests.test_release_descriptor_execution
curl -sf http://127.0.0.1:8085/health | jq .
curl -sf http://127.0.0.1:8085/ready  | jq .
curl -sf http://127.0.0.1:8085/status | jq '{paused, media_ok, last_pass}'
# Authenticated config (secrets masked):
curl -H "X-API-Key: $(cat /run/secrets/control_api_key 2>/dev/null || echo $CONTROL_API_KEY)" http://127.0.0.1:8085/config | jq .
```
