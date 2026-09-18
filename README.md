# ASRSub

Wanted list → remote Whisper ASR → remote LLM translate → Bazarr upload.
Self-looping subtitle pipeline for a Jellyfin/Sonarr/Bazarr library, with a
source ladder (existing ja/en sidecars, Jimaku direct) that skips ASR when
text already exists.

Single Rust binary, **no local models, no GPU** — all inference is remote
via `asrsub_providers.json`. See `docs/` for deploy and health contracts.

## Layout

```
src/            Rust binary (daemon + CLI)
tests/          Rust integration tests (binary boundary) + release-contract
                tests (stdlib unittest, no pytest needed)
assets/         app.js + app.css, embedded in the binary (include_str!);
                the dashboard is server-rendered from src/web/ at /
asrsub_providers.json(.example)
                Remote endpoints for Whisper STT + LLM translation
                (live file untracked — copy the .example and fill keys)
pipeline.env.example
                Non-secret service settings (copy to pipeline.env)
Dockerfile / docker-compose.yml / build.sh / pctl / scripts/
                 (compose, CI gates; release workflow pushes GHCR SHA-tagged
                 images; scripts/deploy_smoke.sh verifies a live deployment)
docs/           DEPLOY.md, HEALTH.md, PLAN.md (historical), PARITY.md
                (port checklist; the retired Python implementation was
                deleted 2026-09-08 and removed from `main` 2026-09-09 —
                rollback is redeploying the previous SHA image per
                docs/DEPLOY.md; reference code survives in git history)
tools/          operator scripts; `probe_wire_langs.py` re-measures the codes
                the configured Whisper endpoint accepts as `language` and
                exits non-zero when they disagree with `src/lang.rs`; it
                refuses to overwrite an existing output CSV unless `--force`
                (re-measuring on the same date needs `--force` or `--out`)
```

## Quickstart

```bash
cargo build                    # debug binary at ./target/debug/asrsub
cargo test                     # 195 unit + 11 integration (offline simulation incl.)
asrsub daemon                  # self-looping daemon (control API on $WEBHOOK_PORT, default 8085)
asrsub run-once                # single pass, print stats JSON, exit
asrsub transcribe -i EP.mkv -o EP.ja.srt
asrsub translate-file -i EP.ja.srt -t id
asrsub refine --ja EP.ja.srt --tr EP.id.srt --write
asrsub health | asrsub config-show
```

## Configuration

Layered, highest wins: process env > `config.overrides.json` >
`pipeline.env` (all under `~/.config/asr-pipeline` or `$ASRSUB_CONFIG_DIR`).
Env is adopted only for pipeline-owned keys (plus keys already in files).

| Key | Meaning |
| --- | ------- |
| `SONARR_URL` / `SONARR_API_KEY` | Episode + series lookup |
| `BAZARR_URL`(+`_2`) / `BAZARR_API_KEY`(+`_2`) | Wanted list, uploads, wanted refill |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` / `JELLYFIN_MEDIA_ROOT` | Library refresh after subtitle events (off without key; **no default URL** — set it explicitly, e.g. `http://jellyfin.lan:8096`) |
| `NAS_MEDIA_PREFIX` | Host/NAS path this daemon reads media at (default `/mnt/nas/share/media`); `/data/…` maps here |
| `JIMAKU_API_KEY` / `JIMAKU_DIRECT_ENABLED` | Direct Jimaku source rung (off without key) |
| `TARGET_LANGS` | e.g. `id,en` (default) |
| `MAX_EPS_PER_RUN` / `EPISODE_CONCURRENCY` | Pass cap / parallel episodes |
| `ASR_CONCURRENCY` / `TRANSLATE_CONCURRENCY` / `UPLOAD_CONCURRENCY` | Stage fan-outs |
| `TRANSLATE_CHUNK` | Lines per LLM request (default 10) |
| `MAX_CUE_MS` | Max cue duration in ms (default 8000) |
| `PROVIDERS_FILE` | Path to `asrsub_providers.json` |
| `CONTROL_API_KEY_FILE` | Control-token secret (`/run/secrets/control_api_key` in compose) |

Remote endpoints, models, and keys live in `asrsub_providers.json`
(LLM list sorted fastest-first with per-endpoint limits + breakers).
That file is UNTRACKED (live keys — purged from git history 2026-09-08):
copy `asrsub_providers.json.example`, fill `api_key` (or leave it empty and
put the `key_env` vars in `~/.config/asr-pipeline/secrets/provider_keys.env`,
which compose loads via `env_file`), `chmod 600`, never commit it. A
non-empty `api_key` wins over its `key_env` fallback, and either way the
value enters the process environment at container start — rotate by editing
the env file and re-running `docker compose up -d`. The keyless template
ships inside the Docker image (see `docs/DEPLOY.md`).

## Optional Discord notifications

The long-running daemon may send one bounded log-style digest through the
optional webhook file at `/run/secrets/discord_webhook`. The URL is validated
at this boundary; missing, empty, malformed, or inaccessible input disables
notifications without stopping subtitle processing. The reserved
`DISCORD_WEBHOOK_URL` key remains rejected from configuration files, process
environment merging, API/dashboard writes, and masked output.

A meaningful pass is admitted with one bounded nonblocking `try_send` into an
in-memory queue. The notifier renders the reports and makes exactly one
best-effort webhook POST, then discards the work. Idle passes send nothing.
There is no durable notification state, outbox, replay, reservation,
acknowledgement, retry, or scheduled tick. `asrsub run-once` does not read the
webhook secret or send a request. Discord is a destination only: this adds no
bot, gateway, listener, command, interaction, or inbound Discord control plane,
and does not change the existing authenticated inbound `/webhook` route.

Render and transport failures emit only a generic local classification; URL,
payload, response-body, path, and credential values are not logged. Local
notifier tests use deterministic in-process fakes for meaningful, idle,
omitted-report, render-failure, and transport-failure cases. No live Discord
request or deployment validation is claimed here.

The repository's `pipeline.env.example` remains non-secret and intentionally
contains no Discord URL, token, enable flag, or outbound webhook setting.

Failover: every LLM chunk races all configured models fastest-first (404
or error → next model, 3 straight failures → 60 s breaker); Whisper tries
`whisper_stt` then `whisper_stt_fallbacks` in order with the same
per-endpoint breakers. (There is no `TRANSLATE_FALLBACK_MODELS` knob —
the whole model list already *is* the fallback list.)

## Control API

GETs are open telemetry; POSTs need `X-API-Key: <control key>`.

- `/` and `/ui/status` server-rendered operator dashboard (embedded; no
  runtime asset directory). `/ui/overview` remains a compatibility alias.
  Read views are open; normal anchors navigate between complete pages.
- `/ui/library` (with `q`, `scope`, `sort`, and `dir` filters),
  `/ui/activity`, `/ui/provenance`, and `/ui/settings`
- `/ui/control/{action}`, `/ui/episode/{id|m:id|e:id}/{action}`, and
  `/ui/config` use authenticated POST/redirect/GET. A successful mutation
  returns `303 See Other`; failures return a complete HTML page with the
  original `401`, `400`, `404`, or `500` status.
  The small embedded browser asset keeps the control key in `sessionStorage`
  and sends it only as `X-API-Key`; it never places the key in a form body,
  URL, cookie, or redirect.
- `/health` liveness · `/ready` readiness (media/providers/state) · `/status` daemon state · `/config` masked config
- `/pause` `/resume` `/run-once` `/wake` control · `/webhook` Tdarr wake + embedded-sub extract
- `/api2/status /health /ready /config /provenance /wanted /library /activity /exclusions`
- `POST /api2/config` writes the settings schema's keys to `config.overrides.json`
  (only editable keys accepted; applied on restart)
- `/api2/episode/{id|m:id|e:id}/retry|skip|delete|exclude|unexclude`
  (`m:` = movie/radarr id; retry/delete accept `{"language","kind"}`)

`pctl` (repo root, stdlib-only) talks to the same routes.

The dashboard's settings form is generated from `config::FIELDS`
(`src/config.rs`), the single source of truth shared with the daemon; a test
guarantees every field is a pipeline-owned key, so the UI cannot drift.

## Pipeline notes

- Provenance is a real first SRT cue (`[AI-generated by ASRSub]`), never a
  header line; uploads are `hi=true` manual uploads of the sidecar written
  just before (that on-disk file is authoritative — staleness is caught at
  the next `discover`, not by post-upload read-back).
- `retry` clears state **and** deletes sidecars, then reprocesses inline in
  the same pass (resolved straight from Sonarr/Radarr — no waiting for
  Bazarr's rescan); `delete` additionally refreshes Jellyfin.
- Only 204/transport-error/429/5xx Bazarr outcomes retry; 400/401/404 fail fast.
- The audio track is chosen by its **real language tag**, once per target
  language: target-language track (no translation) → the media's original
  language (Sonarr/Radarr, best effort) → `ja` → the earlier of the first
  track whose tag names no language (the original in a dual-audio release)
  and the track tagged `en` — so an English dub wins over an untagged
  original only when it comes first physically (`[und(0), eng(1)]` and
  `[eng(0), und(1)]` both take stream 0; the first detects, the second pins
  `en`) → first non-commentary track.
  Commentary/audio-description tracks are skipped when a normal alternative
  exists.
- Whisper is sent that tag's code (container `fre` → `fr`), never a
  fabricated or uncertainty code: a tag that names no language
  (`und`, `unknown`, `""`, `englishus`) is not pinned — it takes the
  detection path. That track is transcribed unforced once and the
  `verbose_json` `language` pins the remaining chunks **only when the
  endpoint is measured to accept that code**; a detected code outside that
  set leaves the later chunks unforced and the source is the detected code.
  If no code can be established, or a request that **carried a pin** (the
  whole file, or one chunk of it) contradicts the code it asserted, the
  language **fails closed** — nothing is installed, uploaded, or registered
  `done`, and a retry can redo it. The accept set is a live measurement of
  the configured endpoint (`tools/probe_wire_langs.py`), not a published
  list: a code the endpoint answers with HTTP 400 must never be sent. The same
  table carries Whisper's own name for each code, so a provider that answers a
  language by name (`tibetan` to a `bo` pin, `haitian creole` to `ht`) is read
  as that language instead of aborting the episode, and every other standard
  spelling of an accepted code is read the same way (`yid` for a `yi` pin,
  `tib`/`bod` for `bo`) — only a report that is genuinely another language
  still fails closed. Ladder rows and
  the translation prompt name the real source (`French`, not `Japanese`),
  and the registry `extra` records `source_lang` on every row plus
  `source_stream` on **ASR rows only** (a ladder row has no chosen audio
  stream).
- The foreign-script (SDH placeholder) guard runs for **Japanese** sources
  only: a latin source such as French would otherwise be rewritten to
  placeholders too.
- `/ready` gates on local prerequisites (media root, providers, writable
  state dir) and reports integrations as diagnostics; `/health` stays cheap.
  No paused-boot yet — see `docs/HEALTH.md`.
- One compose service: the daemon serves the dashboard/API on 8085; deploy
  smoke test in `scripts/deploy_smoke.sh`.
