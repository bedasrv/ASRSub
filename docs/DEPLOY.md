# ASRSub simple immutable Compose deployment

This is the operator runbook for routine ASRSub deployment. It is self-contained:
all routine status, preflight, apply, verification, and rollback actions use
`tools/asrsub_deploy.py`. This checkout does not claim that a production
deployment, rollback, secret activation, or hardened-residue transition has
run.

## 1. Authoritative deployment model

The routine model is one Docker Compose service and one immutable image:

| Setting | Default | Contract |
| --- | --- | --- |
| SSH target | supplied by `--target` | no implicit host is used |
| Expected hostname | supplied by `--expected-hostname` | checked on the target before any write |
| Project directory | `/opt/mediastack/asrsub` | override with `--project-directory` |
| Compose file | `compose.yaml` in the project | override with `--compose-file` |
| Non-secret Compose env file | `.env` in the project | override with `--env-file` |
| Service | `orchestrator` | override with `--service` |
| Compose project name | `asrsub` | override with `--project-name` |
| Readiness port | `8085` via `WEBHOOK_PORT` | override with `--webhook-port` |
| Container media prefix | `/mnt/nas/share/media` via `NAS_MEDIA_PREFIX` | override with `--nas-media-prefix` |
| Image identity | `ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>` | exact digest only |

The tracked candidate is `deploy/compose.simple.yaml`. It has one
`orchestrator`, host networking, `restart: unless-stopped`, and no build
directive. All ASRSub-owned host data is under the project directory:

| Project-relative source | Container target | Mode | Purpose |
| --- | --- | --- | --- |
| `./config` | `/home/user/.config/asr-pipeline` | read-write | application config and ledgers |
| `./cache` | `/home/user/.cache/asr-pipeline` | read-write | cache |
| `./state` | `/var/lib/asrsub/state` | read-write | current image's required notification StateFs |
| `./secrets` | `/run/secrets` | read-only | optional runtime secrets |

The separate StateFs mount is required by the selected Rust image's fixed
`/var/lib/asrsub/state` production backend. Its host source is still the
project-relative `./state`; no `/var/lib/asrsub` host data directory is used by
the simple Compose candidate. The config mount owns the ordinary application
ledgers (`state.jsonl`, `refine_state.jsonl`, `actions.jsonl`,
`subtitle_registry.jsonl`, and related files). The parent secrets bind exposes
the optional webhook at `/run/secrets/discord_webhook`. The provider key file is
the optional project-relative Compose `env_file` `./secrets/provider_keys.env`.
The template contains no secret values and no top-level Compose `secrets:` file.
User access control is external via Pomerium/Pocket ID over HTTPS; the daemon
does not implement user authentication.

The preflight resolves every project-relative source lexically beneath the
project directory, rejects traversal and symlinked parents/targets, and checks
the required data directories before any candidate file is written. It accepts
the candidate mount set or the known hardened legacy mount set for a staged
transition, but never writes or migrates legacy data. NAS media mounts remain
the existing `/mnt/nas/share/media` source at both `/mnt/nas/share/media` and
`/media` container targets.

`ASRSUB_IMAGE` must be the exact lowercase digest reference. A tag, including a
full-SHA lookup tag, is rejected by the tool. The tool never resolves a tag and
never runs a registry lookup to turn a tag into an identity.

## 2. Prerequisites and read-only checks

The operator machine needs Python 3 and an SSH client. The target needs:

- the expected hostname and non-interactive SSH access;
- Docker Engine, the Docker Compose plugin, `findmnt`, and `ss`;
- the active project, Compose file, non-secret `.env`, and `orchestrator` service;
- project-relative `config`, `cache`, `state`, and `secrets` directories as
  real, non-symlinked directories beneath `/opt/mediastack/asrsub`;
- the mounted `/mnt/nas/share/media` directory;
- the optional `secrets/provider_keys.env` file; if present, it is checked only
  by metadata and loaded by Compose;
- an existing healthy service for a deploy backup. A deployment without
  resolvable provider keys will not be ready.

Run these commands from the repository checkout. They are read-only. `status`
reads the current service's legacy-compatible safety metadata. `preflight`
performs the full **pre-apply safety checks**: ownership, paths, real media
mount, secret metadata, intended port ownership, health, and a recoverable
immutable previous repo digest. It accepts only the known legacy data layout or
an already-applied candidate layout; it never guesses an unknown source.

```bash
./tools/asrsub_deploy.py status \
  --target <target-host> \
  --expected-hostname <expected-hostname>

./tools/asrsub_deploy.py preflight \
  --target <target-host> \
  --expected-hostname <expected-hostname>
```

The exact preflight operation does not stage files, create backups, pull an
image, recreate a container, stop a service, restart Docker/systemd, or delete
anything. It uses read-only Docker/Compose queries, `findmnt` for media, file
metadata checks, and port ownership checks. It never prints resolved Compose
configuration, container environment values, or secret contents.

A successful preflight is a structured result with `"ok":true`, an immutable
`previous_immutable_repo_digest`, `current_mount_contract` set to either
`simple` or `legacy-compatible`, `candidate_mount_verification` set to
`not_checked_pre_apply`, `port_owned:true`, and healthy status. The
`candidate_mounts_verified` field is reserved for the post-apply result. A
failed preflight exits non-zero and reports a bounded failure summary. The
summary is intentionally not a copy of Docker stderr.

## 3. CI release descriptor and immutable image

`.github/workflows/release.yml` is the image publishing path. The
`docker/build-push-action` step is named `build`; its `digest` output is the
registry digest emitted for the pushed image. The workflow writes a non-secret
artifact named `release.json` containing:

```json
{
  "asrsub_image": "ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>",
  "image_digest": "sha256:<64-lowercase-hex>",
  "lookup_tag": "ghcr.io/bedasrv/asrsub:<full-git-sha>",
  "git_sha": "<full-git-sha>",
  "build_time": "<UTC timestamp>"
}
```

`lookup_tag` is retained only as a CI/release lookup label. The deploy tool
consumes `asrsub_image` only after its exact digest validation; it does not
consume `lookup_tag`, `git_sha` as an image, or any mutable tag.

After downloading the CI artifact, inspect only the non-secret descriptor and
set the image variable without changing it:

```bash
IMAGE="$(python3 -c 'import json; print(json.load(open("release.json", encoding="utf-8"))["asrsub_image"])')"
printf '%s\n' "$IMAGE"
```

The printed value must match
`ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>`. Do not substitute the
`lookup_tag` value. The repository's `build.sh` remains a local build helper;
it does not produce the CI digest and is not the routine deployment path.

## 4. Exact operator commands

The commands below use placeholders, not credentials. Set `IMAGE` from the
CI `release.json` as shown above. If a site keeps additional non-secret
settings, pass a non-secret source file with `--env-source`; the tool rejects
secret-bearing env keys and never reads the provider key file.

### Status (read-only)

```bash
./tools/asrsub_deploy.py status \
  --target <target-host> \
  --expected-hostname <expected-hostname>
```

This changes nothing. It reads Docker/Compose status, container labels and
safe identity fields, required-path metadata, the real media mount, bounded
port ownership, and current service health. Its result labels a legacy Compose
shape as `current_mount_contract: legacy-compatible`; the
`post-apply candidate mount verification` field `candidate_mounts_verified`
remains `null` before apply. It does not pull, apply, restart, recreate, or
remove a container.

### Preflight (read-only gate)

```bash
./tools/asrsub_deploy.py preflight \
  --target <target-host> \
  --expected-hostname <expected-hostname>
```

This changes nothing. It is the required gate immediately before `deploy`.
It verifies Docker/Compose, active `asrsub`/`orchestrator` ownership, required
paths, `findmnt -T /mnt/nas/share/media`, secret-file metadata, bounded
ownership by the intended `asrsub` listener on `WEBHOOK_PORT`, healthy current
service status, active non-secret env validation, and an immutable previous repo
digest. It also rejects any source layout outside the exact legacy or candidate
contract before deploy can write the project.

### Deploy/apply (the only routine mutating command)

```bash
IMAGE="$(python3 -c 'import json; print(json.load(open("release.json", encoding="utf-8"))["asrsub_image"])')"
./tools/asrsub_deploy.py deploy \
  --target <target-host> \
  --expected-hostname <expected-hostname> \
  --image "$IMAGE"
```

Before any target write, the streamed remote script verifies the target
hostname and runs the pre-apply safety checks. The first simple deployment may
start from the target's **known hardened legacy Compose shape**, as long as the
current service is owned, healthy, safely provisioned, its legacy metadata is
safe, and it has a recoverable previous repo digest. The project-relative
candidate directories must still exist before the candidate can be applied. The preflight
rejects an unsafe or unknown legacy layout before this point. It then:

1. creates a timestamped `.asrsub-rollback/<timestamp>/` backup containing the
   active `compose.yaml`, active non-secret `.env`, the previous immutable repo
   digest, a `backup_kind`, and a **saved previous mount contract** containing
   only paths, destinations, and RW flags;
2. atomically stages the candidate Compose and non-secret env files;
3. runs the quiet Compose validation action (`docker compose config -q`);
4. pulls only the exact digest represented by `ASRSUB_IMAGE` (the remote
   action is the equivalent of `docker compose pull orchestrator` with the
   candidate digest, never a tag);
5. applies only `docker compose ... up -d --no-build --pull=never orchestrator`;
6. waits a bounded 60 seconds for both `/health` and `/ready` to return HTTP 200
   within the one total readiness budget;
7. after both HTTP probes pass, polls Docker health in that same deadline until
   the container reports `healthy`;
8. performs the **post-apply candidate mount verification** and verifies
   `candidate_mounts_verified:true`, the running container's exact image
   reference and repo digest, image ID, mounts, and Docker health status.

It never runs Compose `down`, a Docker daemon/systemd restart, an image prune,
volume prune, broad deletion, or a build. Old images are retained.

### Verify (read-only)

Run the tool status check and the two bounded local HTTP probes from the target
(or from a host that can reach the target's host-network port):

```bash
./tools/asrsub_deploy.py status \
  --target <target-host> \
  --expected-hostname <expected-hostname>

curl --fail --silent --show-error http://127.0.0.1:8085/health
curl --fail --silent --show-error http://127.0.0.1:8085/ready
```

Successful probes return HTTP 200 and JSON/response bodies from the service;
the deployment tool itself does not copy those bodies into its output. A
non-zero `curl` result or HTTP 503 means the deployment is not ready. Verify
also confirms that the active image is the requested `@sha256:` digest, not a
tag.

### Rollback (mutating, but pull-free)

```bash
./tools/asrsub_deploy.py rollback \
  --target <target-host> \
  --expected-hostname <expected-hostname>
```

Rollback checks the hostname before writes and selects the most recent verified
backup whose metadata includes a `backup_kind`, previous immutable repo digest,
and saved previous mount contract. It atomically restores that Compose/env pair.
For a `simple` backup, the saved `.env` and metadata restore the previous
`ASRSUB_IMAGE` digest; a `legacy` backup may retain a tag only when its local
`RepoDigests` match the recorded digest. It then runs
`docker compose ... up -d --no-build --pull=never orchestrator`. It does not
pull, retag, prune, stop first, or use a mutable tag. The rollback path uses the
same one-budget HTTP-then-Docker-health helper as automatic recovery: `/health`
and `/ready` must pass, then Docker must report `healthy` before mount and digest
verification. A `simple` backup must
restore the exact digest in `Config.Image`, and its repo digest must also match.
Its verification reports `config_reference: legacy/tag` and
`repo_digest_matched:true` only for the legacy/tag case. Both kinds must match
the saved previous mount contract and pass health/readiness. If no verified
immutable backup contract exists, it exits non-zero without changing the active
files.

## 5. Health gates and automatic recovery

The deployment gate is bounded. Both endpoints must return 200 within the same
60-second readiness budget after apply, and Docker health must then reach
`healthy` before the deployment is successful:

- `/health` proves the daemon is live;
- `/ready` proves the configured readiness checks, including media/state and
  provider readiness, pass;
- Docker health proves the container healthcheck has converged after the HTTP
  probes, closing the post-apply health race.

Expected success output is a short structured result similar to this; image
IDs and paths are non-secret metadata:

```json
{"backup":"20260918T120000Z","image":"ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>","ok":true,"operation":"deploy","verification":{"candidate_mounts_verified":true,"health":"healthy","mounts_verified":true}}
```

A config, pull, apply, bounded readiness, or post-apply identity failure
restores the timestamped backup and runs the old Compose files with
`--pull=never`. The command returns non-zero even when automatic rollback
succeeds. Expected failure output has `"ok":false` and an explicit rollback
status, for example the following explicit `rollback status` field:

```json
{"backup":"20260918T120000Z","ok":false,"operation":"deploy","rollback":{"ok":true}}
```

If automatic rollback itself fails, do not retry destructive commands. Run the
manual rollback command after fixing only the reported prerequisite, then
repeat the read-only verify checks. A manual rollback never pulls an image.

## 6. Secret handling

Secret values are external deployment inputs. The simple template contains no
secret values and the deploy tool accepts no secret argument.

| Input | Host location | Required metadata |
| --- | --- | --- |
| Optional Discord webhook | `./secrets/discord_webhook` under the project | if present, file `0600`; absence disables it |
| Provider key env file | `./secrets/provider_keys.env` under the project | optional, file `0600`; loaded as optional `env_file` |
| Container projection | `/run/secrets` | read-only parent bind keeps the optional webhook path |

The operator must enforce `chmod 600` on each present secret file and `chmod 700`
on the secret directory without displaying its contents.

### Host-local webhook smoke test

The repository includes a separate, explicit smoke test for one disposable
webhook. This test does not start ASRSub, run a pipeline pass, or verify a
Compose secret mount. It sends one direct HTTP POST to the webhook endpoint.

The default smoke-test input is separate from the deployment input:

- Host-local smoke test: `~/.config/asr-pipeline/secrets/discord_webhook`
- Compose deployment: `./secrets/discord_webhook` under the project, mounted in
  the container as `/run/secrets/discord_webhook`

For the host-local test, store the rotated webhook URL in the default file.
Use a protected directory with mode `0700` and a file with mode `0600`. The file
must contain one exact URL line without leading or trailing whitespace. Do not
place the URL in a command argument, shell history, source file, or test output.

Run the helper only when a real delivery is intended:

```bash
python3 scripts/test_discord_webhook.py --live
```

Without `--live`, the helper sends nothing. The helper rejects symlinks, unsafe
permissions, unsupported URL forms, redirects, and proxies. It prints only a
success status or a generic failure category. A 2xx response proves that the
webhook endpoint accepted the test request; it does not prove a daemon
deployment or a production cutover. Rotate or delete the disposable webhook
after the test according to the secret-management policy.

The preflight reads secret-file metadata with `lstat`; it does not open or read
secret values. The remote script never includes secret file contents, resolved
Compose config, raw Docker output, or raw SSH stderr in its result. Operators
and agents must **never print or read secret values in logs**, shell history,
CI output, or troubleshooting transcripts. Do not run an unfiltered
`docker compose config`, `docker inspect` that includes `.Config.Env`, or a
plain grep that prints env-file values. If a separate reviewed diagnostic must
list provider variable names, use only the names-only form
`grep -oE '^ *[A-Z_]+_API_KEY:'`; do not omit the `-o` names-only option,
because a plain match prints live values. If a secret value appears in output,
stop, rotate it out-of-band, and treat the log as compromised.

Non-secret values belong in the target `.env` and are represented by
`deploy/asrsub.env.example`. The tool may stage `ASRSUB_IMAGE`, `WEBHOOK_PORT`,
`NAS_MEDIA_PREFIX`, and other explicitly non-secret settings. The provider path
is fixed by the tracked Compose template at `./secrets/provider_keys.env`; it is
not an `.env` override and the tool never opens it. Strict preflight validates
the active `.env` before success and rejects secret-bearing keys, URL userinfo,
inline bearer/basic credentials, private-key material, and secret query
parameters. Ordinary non-secret URLs remain valid.

`--compose-source` is not an extension point for this routine deployment. The
local CLI resolves it and accepts only the repository's tracked
`DEFAULT_TEMPLATE` (`deploy/compose.simple.yaml`); an alternate privileged or
extra-service template is rejected before any target request. The textual
contract check remains defense in depth.

`NAS_MEDIA_PREFIX` must be an absolute, normalized, non-root container path. It
must not overlap `/media`, `/run`, `/run/secrets`, the config/cache targets, or
another managed destination. The source/export contract is unchanged: the
preflight still requires `/mnt/nas/share/media` to be on the approved NFS mount.

## 7. Forbidden routine commands

Do not use any of these commands for routine deployment or recovery:

```text
docker compose down
docker compose rm -f
docker system prune -af
docker image prune -af
docker volume prune -af
systemctl restart docker
systemctl restart asrsub-runtime.service
docker tag ...:latest ...
docker compose up -d --build
rm -rf /opt/mediastack/asrsub
```

This includes equivalent aliases, broad deletion of the project or data mounts,
mutable `latest`/SHA-tag deployment, Docker daemon restart, and daemon/systemd
restart. The routine tool deliberately has no `down`, `prune`, `rm`,
`systemctl`, image-prune, or broad-delete command path.

## 8. One-time transition: hardened residue

The repository still contains the previous hardened systemd/drop-in/runtime
implementation for audit and transition purposes. Removing it is **not** part
of routine Compose deployment. It is a separate, explicitly authorized,
one-time change window with a named operator, backup, and read-only inventory.
No such transition has run from this checkout.

The transition inventory must identify, before removal, only the installed
residue that is actually present:

- `/etc/systemd/system/asrsub-recovery.service`;
- `/etc/systemd/system/asrsub-runtime.service`;
- the Docker drop-in
  `/etc/systemd/system/docker.service.d/asrsub-recovery.conf`;
- `/usr/local/libexec/asrsub` and its installed runtime helpers.

The old fixed entrypoints (`asrsub-recover --preflight` and
`asrsub-runtime --reconcile`) are legacy transition evidence, not the routine
path. An authorized transition may disable those units, remove only the
reviewed per-file residue, run the required systemd manager reload, and verify
that the simple Compose service remains the sole owner. It must not use
`rm -rf`, broad deletion, or a daemon/systemd restart as a substitute for an
inventory. Keep the old files until the transition owner confirms the new path;
this document makes no claim that the transition or production deployment was
executed.

## 9. Troubleshooting

- **Hostname mismatch:** stop. Check the target's reported hostname and pass
  the exact value to `--expected-hostname`. No files were written before this
  check.
- **Missing Docker/Compose:** install or repair the target prerequisites. Do
  not bypass the tool with a manual Compose command.
- **Project/service ownership failure:** inspect only safe `docker compose ps`
  metadata and labels. Confirm project `asrsub` and service `orchestrator`; do
  not take over an unrelated project.
- **Missing path or media mount:** create/fix the project-relative `config`,
  `cache`, `state`, and `secrets` directories under the project, or fix the NFS
  mount under the target change process. `findmnt -T /mnt/nas/share/media` must
  identify a real mount, not merely a directory. The preflight rejects missing,
  symlinked, traversing, or project-external data paths.
- **Unsafe project directory:** stop. The project must be a current-owner,
  non-group-writable `0700` directory before lock, pending, or rollback paths
  are opened; symlinks and wrong types are rejected.
- **Port ownership failure:** confirm `WEBHOOK_PORT` and that the intended
  service owns the listening port. Do not stop an unrelated listener.
- **Compose config failure:** inspect the non-secret `.env` names and the
  Compose plugin version. Do not print the rendered config or provider env
  values. Strict preflight rejects URL credentials and obvious inline secret
  values. The optional provider file requires Compose v2.24+ for
  `required: false`. Do not pass an alternate `--compose-source`.
- **Invalid media prefix:** use a normalized absolute non-root destination
  outside `/media`, `/run`, `/run/secrets`, config, cache, and other managed
  paths. Do not change the NFS source/export contract.
- **Pull failure:** confirm that CI published the exact digest in `release.json`
  and that target access is authorized. Never replace it with a tag.
- **Readiness 503, Docker `starting`, or timeout:** read the safe
  `/health`/`/ready` status and application diagnostics. Check the media mount,
  the config mount that owns the current pipeline ledgers, provider-key
  presence without opening key files, and Docker health convergence. The same
  bounded health helper is used during automatic and manual rollback; automatic
  rollback reports its `rollback.ok` status.
- **Digest or mount verification failure:** do not retry with `--pull=always`,
  `latest`, or a manual container replacement. Use the manual rollback command
  and preserve the backup for investigation.
- **No rollback backup:** the tool refuses to guess. Restore a verified backup
  through the approved change process; never reconstruct one from a tag.

## 10. Agent runbook

1. Confirm this checkout and the target are the intended environments; do not
   access production while testing this tool.
2. Obtain CI `release.json`; use only its `asrsub_image` `@sha256:` value.
3. Run `status`, then the exact read-only `preflight`; stop on any failure.
4. Confirm secret metadata only (`0700` directory, `0600` files); never print or
   read secret values.
5. Run `deploy --image "$IMAGE"`; capture only the structured result.
6. Require `ok:true`, immutable image verification, healthy status, and
   `candidate_mounts_verified:true` after apply, plus successful `/health` and
   `/ready` probes.
7. On a failed deploy, require non-zero exit and inspect only `rollback.ok`.
   If false, use `rollback` after fixing the bounded prerequisite.
8. Record the commit, digest, backup timestamp, and structured result without
   recording secrets.
9. Do not remove hardened systemd/drop-in/runtime residue unless a separate
   transition is explicitly authorized and reviewed.

## Reverse proxy and firewall

The host-network service listens on `WEBHOOK_PORT` (default 8085). A reverse
proxy must reach `http://<asrsub-host>:8085`; allow proxy-host to target-host
TCP 8085. Use `/health` for liveness and `/ready` for readiness. These probes
are unauthenticated and must not require SSO. A public 302 only proves the
proxy's login path; verify the upstream path separately. The reverse proxy
supplies external access control for operator routes. Do not expose provider
values while testing the route.

## Existing application notes

Non-control settings remain in `pipeline.env` and the operator config mounts.
An empty environment variable pins nothing and a file value survives it; it
also does not clear a value already in `pipeline.env` or
`config.overrides.json`. The dashboard and API continue to use the single
`orchestrator` service. Provider key rotation takes effect when the container
is recreated by the next immutable apply, not through a live secret reload.
