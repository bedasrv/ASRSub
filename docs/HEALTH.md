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
- `GET /config` — Merged config with secrets masked (by key name, and any `user:pass@` userinfo inside a value, including a scheme-less `user:pass@host`, and in whatever spelling the separating colon arrives: `:`, `%3A` up to eight escape layers deep, `&#58;`/`&#x3a;`/`&colon;` with or without the trailing semicolon (including the legacy `&amp` spelling one layer down), or the full-width colon — and a numeric entity counts whatever follows it, so the credential-free `&#580` is masked rather than risk publishing `svc&#x3aabc@inner`). Masking fails closed: a credential is masked whatever the host looks like — a trailing space, a non-ASCII or percent-encoded host, an alphabetic port, a password that starts with digits and contains a `/`, a user name that itself contains an `@`, a name that contains an `@` *and* a colon written as an escape (`a@b&#58Zk1P@host.lan`, `a@b%3AZk3P/ss@host.lan`, `a@b&colonZk3P/ss@host.lan` — a `#` inside an entity is an entity introducer, not a fragment boundary, and a candidate "host" that carries a colon in any other spelling is not read as one), a value with no scheme whose own text embeds a URL (`nominal@host/redir?url=http://svc:pw@inner`) — and a password containing `/`, `?` or `#` cannot hide behind a path separator; the slot right after the `@` is checked as well — including when that `@` sits past a path separator or after a non-hierarchical scheme, which used to return early and publish the slot (`http://host.lan/a@[root:s3cr3t]`, `mailto:x@b&#58Zk1P`) — so a bracket slot that does not parse as an IPv6 literal with an optional port (`[root:s3cr3t]`, `[::1]x`) and a colon written as an escape in that slot (`b&#58Zk1P`) are masked with the name, while a malformed *port* keeps its colon where a colon belongs (`sonarr.lan:http` stays readable); the legacy semicolon-less `&amp` counts as one escape layer (`&amp#58` is a colon): a colon-free name does not end the check, because the text after the `@` is scanned for its own credential. An encoded colon may *justify* a mask but may not *position* one to the right of a run carrying a literal colon, because the run it would start left that password in the output (`https://user@host.lan/x/s3cr3t://&amp#58@y` published `s3cr3t` until the boundary fell back). Every credential-shaped run in a value is masked, not only the authority's: a value embedding a second credentialed URL in its own path or query (`https://user:pw@gw.lan/redirect?url=http://a:b@c`) becomes `https://***:***@gw.lan/redirect?url=http://***:***@c`, so the URL stays legible instead of the inner pair staying visible. Deliberate trade-offs, all documented at the function and pinned by tests: a `@` that sits past the authority when neither the text in front of the separator nor the text in front of the `@` carries a colon (a base URL like `https://bazarr.lan/api?x=a@b`, or `file:///path@x`) is left alone — the price is that `https://host:6767/path@x` is masked though it holds no credential, and that a scheme-less tail credential is masked at its innermost run, so a query key and its readable prefix are swallowed with it (`https://gw.lan/r?to=svc:pw@inner` becomes `https://***:***@inner`, not `...?***:***@inner`); the port colon of a `host:port` following an already-masked user name is not treated as credential evidence, so `https://user@host:6767/x@y` keeps its readable host and port (`https://***@host:6767/x@y`) while a digit-only password sitting in that host slot is published with it (`https://u@svc:1234/x@y`) — the price of a readable host; a bracketed IPv6 literal counts as a host (`user@[::1]:8080/x@y` stays intact) but an empty host or port falls back to masking; and a semicolon-less named entity is read as a colon wherever it appears, so a credential-free `&colony` is masked too; a `#` straight after an `&` counts as an entity introducer, so a genuine fragment boundary there costs the host (`https://user@host&#frag@x` becomes `https://***@x`); a *colon-free* authority-less user name is published (`https://///pw@host`, `https:///path@x`); a `user:pass` pair with no `@` anywhere after it is indistinguishable from `host:port` and is left as it stands (`https://gw.lan/r?url=https://u:p`); a bracketed host slot counts as a host only when it parses as an IPv6 literal, so `user@[::1]:8080/x@y` stays readable while `user@[root:s3cr3t]/x@y` is masked (a bracket is not a licence to hide a password, though the slot is re-scanned only when it is the innermost colon-bearing candidate, so a bracketed credential whose value also carries its own colon-bearing tail — `user@[svc:pw]:80/a:b@y` — still publishes `svc:pw`: a known residual, not a licence); a scheme-qualified bare user name is masked to `***` though it holds no password (`ssh://git@github.com/owner/repo`), while the scheme-less `user@host` spelling is untouched; known residuals, named rather than hidden, all pre-dating this work and all contrived except the last: a credential colon in the host slot spelled with a *literal* second colon or followed by non-digits is published with the slot (`a@b:1:Zk1P`, `a%3AZk1P@root:s3cr3t]`), a credential carried in a query parameter is invisible to the masker because it has neither a colon nor an `@` (`https://sonarr.lan:8989/api?apikey=…&x=1`), and the alphanumeric guard reads `&amps…` as prose so a legacy ampersand immediately followed by a letter is not decoded (`a&ampsZk1P@host:6767/x@y`); two prices the encoded-colon fix widened, both measured — an encoded colon sitting to the right of a malformed port or of an inner `://` anchors the mask fallback on that colon and swallows the readable host (`nominal@sonarr.lan:http/x=&colon;pw@inner` becomes `nominal@***:***@inner`, and an 8 KB host disappears the same way), and an encoded colon in the host slot now masks credential-free OAuth-style and non-hierarchical values (`https://login.lan/auth?redirect=/a@b%3Ac` becomes `https://***@***:***`, `mailto:a@b%3Ac` becomes `***:***@***:***`). Both are the accepted fail-closed direction rather than a leak: differential execution over 2.05M structured and 40.2M exhaustive values found no value the change publishes that its parent masked, and the fallback is not narrowed for legibility because the same shape with the credential in the path is the leak it closes. and a scheme-less value whose first word is a non-hierarchical scheme with no port after the host (`mailto:admin@example.com`, `urn:isbn:…@x`) is read as that URI — which would also publish a credential whose user name is literally `mailto`/`data`/`tel`/…, the price of not masking every email address. `/config` is not the only sink for these values, and every sink goes through the same masker (`mask_for_log`): a log line that echoes a configured value, the settings form and the overview stat (both served without authentication, so a form field shows its masked value and posts that masked value back — an untouched field is skipped, never written), the `media_root.path` and `state_dir.path` fields of `/ready` and `/api2/ready`, the providers loader's "tried …" and read/parse messages, the whisper endpoint and transport error text, the Jimaku error chains, and the derived single-instance lock filename — a `.daemon.lock` name published a credential on disk, so the lock now sits **beside** the state file under its masked name plus a short digest of the real path, and that masked path is what the `another asrsub daemon holds …` error prints (masked again there, so a credential-shaped *directory* in the path does not ride along) (a credential-free path keeps its exact name, so two state files whose names differ only in their extension still share one lock — the digest is appended only when the mask changed the name — while two paths differing inside the credential no longer collide). The CLI prints one masked cause chain instead of each site printing its own; the price is that a scheme-less message holding an `@` reads as a single userinfo, so the mask replaces the message's own prose as well (`Error: ***:***@host/providers.json"` is one real report — keeping a colon-free prefix was tried and reverted, because it published fragments of the credential spelling, `a@b&#***:***@host.lan`), and `RUST_BACKTRACE` is no longer appended: `reqwest` puts the request URL into its error text, so masking the rendered chain closes the class rather than chasing the sites.
- `POST /pause /resume /run-once /wake /webhook` — Control routes served without
  daemon-side user authentication. Access control is external via
  Pomerium/Pocket ID over HTTPS.
- `/api2/*` — Telemetry + episode actions; mutation routes follow the same
  unauthenticated daemon contract.

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

### Notification boundary (daemon-only outbound)

- **Optional daemon feature:** the long-running daemon reads one protected,
  optional container-side `/run/secrets/discord_webhook` file. For a host-local
  one-shot smoke test, run
  `python3 scripts/test_discord_webhook.py --live`; it reads the protected
  `~/.config/asr-pipeline/secrets/discord_webhook` file without starting the
  daemon or a pipeline pass. The existing strict webhook grammar validates the
  URL; a missing or malformed secret disables outbound notification without
  failing the pipeline. The value is not accepted from config/API/dashboard
  writes or ordinary process configuration.
- **Best effort:** a meaningful pass is admitted with one bounded,
  nonblocking `try_send` into a small in-memory Tokio queue. The notifier
  renders the bounded reports and performs exactly one webhook POST, then
  discards the work. Idle passes produce no notification. There is no durable
  notification state, outbox, replay, reservation, acknowledgement, retry, or
  scheduled tick in this daemon path.
- **Boundaries:** `run-once` does not read the webhook secret or send a
  request. The inbound `POST /webhook` remains the independent Tdarr
  wake/extraction route; this feature adds no bot, gateway,
  listener, command, interaction, or inbound Discord control plane.
- **Failure handling:** render and transport failures only emit a generic
  local classification. URL values, payload bodies, response bodies, paths,
  and credentials are not included in those warnings; notification failures
  do not change pipeline counters, `LastPass`, readiness, API responses, or
  dashboard behavior.
- **Local evidence:** deterministic notifier tests use an in-process fake
  transport and local mock HTTP. The live smoke helper is explicitly opt-in;
  production deployment validation is recorded separately in `docs/DEPLOY.md`.

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

In the current simple Compose production layout, this is a container-internal
path backed by the host source `/opt/mediastack/asrsub/config`. The separate
container-internal `/var/lib/asrsub/state` path is backed by
`/opt/mediastack/asrsub/state`. Do not recreate the retired host-side
`/home/user/.config/asr-pipeline` or `/home/user/.cache/asr-pipeline` mounts.

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

> LANGUAGE fails closed the same way. The audio track is chosen by its real
> language tag (`fre` → `fr`, `pt-BR` → `pt`) and that code is what Whisper is
> sent — never a fabricated one. An **untagged** track is transcribed unforced
> once and the `verbose_json` `language` pins the remaining chunks when the
> endpoint is measured to accept that code; a detected code it rejects leaves
> the later chunks unforced and the detected code is the source. If no code can
> be established, or a request that **carried a pin** — the whole file, or one
> chunk of it — contradicts the code we pinned, the language errors
> like a missing file (`failed`, state row `error`): nothing is installed,
> uploaded, or registered `done`, and the next pass or a `retry` action redoes
> it. The foreign-script (SDH placeholder) guard runs for Japanese sources
> only — a latin source such as French would otherwise be rewritten to
> placeholders. See `README.md` "Pipeline notes" for the pick order.

### 3. Webhooks (no inbox ledger)

The retired Python daemon used a SQLite webhook inbox
(`webhook_inbox.db` + integrity checks). The Rust daemon has **no inbox
DB**: `POST /webhook` (access control is external via Pomerium/Pocket ID and
HTTPS) wakes the pass loop and extracts embedded subtitles on a spawned task, with
concurrent duplicates for the same file collapsed to one extraction.
A POST without a `file`/`filePath`/`path` field is a no-op.
There is nothing to `PRAGMA
integrity_check` — if you migrated from the Python deployment, the stale
`.db` files under the state dir are inert and can be archived away.

> OPS: the current Rust daemon does not require a daemon API-key header for
> `/webhook`; access control is external via Pomerium/Pocket ID over HTTPS.
> Restrict ingress to the approved proxy path and keep the route boundary
> separate from the optional outbound Discord webhook.

### 4. Runtime Pause (no paused boot)

The daemon always starts **unpaused** (no `paused` file, no `PAUSED=1` —
those belonged to the retired Python daemon). Hold/resume at runtime:

```bash
curl -X POST http://127.0.0.1:8085/pause
curl http://127.0.0.1:8085/status | jq .paused
# To resume: POST /resume (or /api2/resume)
```

## Operational Notes

- **No secret values** appear in health/readiness responses, logs, or dashboards. Provider keys remain in the optional provider env file and the Discord webhook remains an optional runtime secret; `pipeline.env` at `/home/user/.config/asr-pipeline/pipeline.env` holds non-secret settings and is mounted as a volume, not injected as environment variables.
- **One daemon serves the dashboard**: exactly one `orchestrator` service runs the daemon, which serves the complete HTML UI at `/` and `/ui/*` and the API at `/api2/*` on `WEBHOOK_PORT` (default 8085). There is no separate dashboard replica: running a second full daemon on a read-only state mount caused an `EROFS` restart loop and risked competing state writers. Dashboard mutations (`POST /ui/…`) use ordinary POST/redirect/GET: a successful write returns `303 See Other` with a `Location` for the resulting complete page, while validation, unknown-action, and write failures return complete HTML with their `400`, `404`, or `500` status. The embedded browser asset follows redirects and replaces the document; it does not maintain client-side page state or poll partial responses. A no-op settings submission remains a `200` complete page. The same validation rule guards both write paths, so `POST /api2/config` refuses an out-of-range value too — a value the loader would only warn about and discard is never persisted.
- **Empty means unset, in every layer**: a variable that is set but empty (or whitespace-only) pins nothing, does not shadow a file value during the merge, is not validated, and is treated as unset by the consumers that read the environment directly — `PROVIDERS_FILE`, `ASRSUB_CONFIG_DIR`, `HOME`, `JIMAKU_BASE_URL`, `JIMAKU_CALL_SLEEP_MS`, `JIMAKU_TIMEOUT`, `ANILIST_BASE_URL`, `ANILIST_CACHE`, `ANILIST_TIMEOUT`, `LLM_TIMEOUT_S`, `LLM_PER_ENDPOINT_CONCURRENCY`, `WHISPER_TIMEOUT_S`, `WHISPER_CONCURRENCY` and every provider entry's `key_env`. Because of that, **blanking a variable no longer clears a value that lives in `pipeline.env` or `config.overrides.json`** — the file value survives. To clear such a value, edit `pipeline.env`, `POST /api2/config {"KEY":""}`, or delete the key from `config.overrides.json`; note the settings form never submits an empty `Secret`, so a password is cleared through one of those, not through the UI.
- **Build vs deploy**: images are built by CI and published under an immutable
  `ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>` identity. The SHA tag is
  only a lookup label. The active routine production path is
  `tools/asrsub_deploy.py`: it snapshots and backs up before mutation, requires
  an idle daemon, applies the exact digest with `--pull=never`, and verifies
  image identity, mounts, health, readiness, and Docker health. The earlier
  hardened systemd/runtime path is not the active production deployment path;
  direct ad-hoc `docker compose up` is not the operator rollout path.
- **Probes** (Compose ships this healthcheck; `/health` for liveness, `/ready` for readiness). The tracked template follows `WEBHOOK_PORT`; the validated rendered projection fixes the value before its Compose hash is recorded:
  ```yaml
  healthcheck:
    test: ["CMD", "/usr/bin/curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:${WEBHOOK_PORT:-8085}/ready"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 10s
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
# Config is served without daemon-side user authentication; Pomerium/Pocket ID protects it upstream.
curl http://127.0.0.1:8085/config | jq .
```
