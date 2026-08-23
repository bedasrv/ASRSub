#!/usr/bin/env python3
"""ASRSub dashboard v2 (FastAPI + static HTML).

All reads and actions are served by control_api_v2.ControlAPIv2 (in-process
proxy); the CONTROL_API_KEY token never reaches the browser.
"""

import os

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader

import control_api_v2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html")

api2 = control_api_v2.ControlAPIv2()
app = FastAPI(
    title="ASRSub API",
    description=(
        "ASRSub subtitle-pipeline control API. Read endpoints are open "
        "(telemetry; the dashboard browser never holds a token). Write "
        "endpoints require the header `X-API-Key: <CONTROL_API_KEY>`.\n\n"
        "Inbound webhooks (on the orchestrator port, NOT here):\n"
        "- `POST /tdarr-webhook` (Tdarr plugin signal; carries `X-Control-Key`, "
        "body = tdarr file payload) -> extracts embedded jpn/eng subs.\n"
        "- `POST /sonarr-webhook` (Sonarr events; `X-Control-Key`) -> "
        "episode-upgrade/info trigger.\n\n"
        "Outbound webhooks: see `POST /api2/webhook/test`; payloads are "
        "HMAC-SHA256 signed as `X-Webhook-Signature-V2` over "
        "`<X-Webhook-Timestamp>.<body>` with the configured secret."
    ),
    version="2.0.0",
    openapi_tags=[
        {"name": "telemetry", "description": "Read-only telemetry (open)."},
        {"name": "control", "description": "Write/control endpoints (require X-API-Key)."},
        {"name": "webhooks", "description": "Outbound webhook helpers (require X-API-Key)."},
    ],
)

# Documentation-only auth scheme: the OpenAPI marks write routes as secured
# so Swagger shows the Authorize button, but the dependency never blocks —
# the actual token check happens in the in-process proxy (api2.handle).
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
require_key = Depends(api_key_header)


_original_openapi = app.openapi


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = _original_openapi()
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "ApiKeyAuth"
    ] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    # Write routes are secured; read/telemetry routes stay open.
    for path_item in schema.get("paths", {}).values():
        for method, op in path_item.items():
            if method.lower() == "post":
                op["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


def _resp(code, obj):
    return JSONResponse(obj, status_code=code)


def _token():
    return api2.control_token()


def _status_example():
    return {
        "paused": False,
        "queue": {"wanted": 10, "movies": 12},
        "state_counts": {"done": 50, "error": 0, "pending": 0, "total": 50},
        "series": {"total": 60, "done": 50, "remaining": 10},
        "models": {"asr_backend": "whisper", "asr_model": "large-v3-turbo", "vad_model": "silero"},
        "current": None,
    }


def _library_example():
    return {
        "items": [
            {
                "item_key": "e:123",
                "kind": "series",
                "sonarr_episode_id": 123,
                "series": "Example Series",
                "episode": "S01E01",
                "title": "First Episode",
                "season": 1,
                "episode_number": 1,
                "wanted": True,
                "excluded": False,
                "languages": [{"language": "id", "status": "wanted"}],
                "pending_action": None,
            }
        ],
        "total": 1,
        "movies": 0,
    }


def _wanted_example():
    return {
        "total": 10,
        "items": [
            {
                "sonarrEpisodeId": 123,
                "series": "Example Series",
                "episode": "S01E01",
                "title": "First Episode",
                "missing": ["id"],
                "state": {"status": "new", "elapsed_s": None, "ts": None},
            }
        ],
        "exclusions": {},
    }


def _webhook_test_example():
    return {
        "ok": True,
        "configured": True,
        "results": [
            {"url": "http://127.0.0.1:9999/hook", "status": 200, "latency_ms": 12, "error": None}
        ],
    }


@app.get("/", summary="Dashboard page", include_in_schema=False)
def index():
    return FileResponse(HTML_FILE)


@app.get(
    "/api2/status",
    tags=["telemetry"],
    summary="Pipeline status: pause state, current item, state counts, models",
    responses={200: {"description": "OK", "content": {"application/json": {"example": _status_example()}}}},
)
def api2_status():
    return _resp(*api2.handle("GET", "/api2/status"))


@app.get(
    "/api2/activity",
    tags=["telemetry"],
    summary="Recent activity feed (state tail + Bazarr history merged)",
)
def api2_activity():
    return _resp(*api2.handle("GET", "/api2/activity"))


@app.get(
    "/api2/wanted",
    tags=["telemetry"],
    summary="Bazarr wanted list (series episodes missing target subs)",
    responses={200: {"description": "OK", "content": {"application/json": {"example": _wanted_example()}}}},
)
def api2_wanted():
    return _resp(*api2.handle("GET", "/api2/wanted"))


@app.get(
    "/api2/library",
    tags=["telemetry"],
    summary="Library view; ?scope=active|all|inactive",
    responses={200: {"description": "OK", "content": {"application/json": {"example": _library_example()}}}},
)
def api2_library(scope: str = "active"):
    body = {"scope": scope} if scope and scope != "active" else None
    return _resp(*api2.handle("GET", "/api2/library", body))


@app.get(
    "/api2/config",
    tags=["telemetry"],
    summary="Effective config (secrets masked)",
)
def api2_config():
    return _resp(*api2.handle("GET", "/api2/config"))


@app.post(
    "/api2/config",
    tags=["control"],
    summary="Set config overrides (values persisted to config.overrides.json; null deletes)",
    dependencies=[require_key],
)
def api2_config_set(body: dict = None):
    return _resp(*api2.handle("POST", "/api2/config", body or {}, token=_token()))


@app.get(
    "/api2/health",
    tags=["telemetry"],
    summary="Liveness probe",
)
def api2_health():
    return _resp(*api2.handle("GET", "/api2/health"))


@app.get(
    "/api2/provenance",
    tags=["telemetry"],
    summary="Subtitle registry / provenance rows",
)
def api2_provenance():
    return _resp(*api2.handle("GET", "/api2/provenance"))


@app.post(
    "/api2/episode/{ep_id}/retry",
    tags=["control"],
    summary="Re-process item (series id or m:<radarrId>); clears state + registry + files",
    dependencies=[require_key],
)
def api2_episode_retry(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/retry", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{ep_id}/skip",
    tags=["control"],
    summary="Skip this pass (movies: 409)",
    dependencies=[require_key],
)
def api2_episode_skip(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/skip", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{ep_id}/delete",
    tags=["control"],
    summary="Delete generated subtitles for item",
    dependencies=[require_key],
)
def api2_episode_delete(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/delete", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{ep_id}/exclude",
    tags=["control"],
    summary="Exclude from pipeline",
    dependencies=[require_key],
)
def api2_episode_exclude(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/exclude", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{ep_id}/unexclude",
    tags=["control"],
    summary="Remove exclusion",
    dependencies=[require_key],
)
def api2_episode_unexclude(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/unexclude", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{id}/monitor",
    tags=["control"],
    summary="Set Sonarr episode monitored flag",
    dependencies=[require_key],
)
def api2_monitor(id: int, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{id}/monitor", body or {}, token=_token())
    )


@app.post(
    "/api2/episode/{id}/search",
    tags=["control"],
    summary="Trigger Sonarr EpisodeSearch command",
    dependencies=[require_key],
)
def api2_search(id: int, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{id}/search", body or {}, token=_token())
    )


@app.get(
    "/api2/exclusions",
    tags=["telemetry"],
    summary="Current exclusions",
)
def api2_exclusions():
    return _resp(*api2.handle("GET", "/api2/exclusions"))


@app.post(
    "/api2/pause",
    tags=["control"],
    summary="Pause the daemon (in-memory; a restart resumes it)",
    dependencies=[require_key],
)
def api2_pause():
    return _resp(*api2.handle("POST", "/api2/pause", {}, token=_token()))


@app.post(
    "/api2/resume",
    tags=["control"],
    summary="Resume the daemon",
    dependencies=[require_key],
)
def api2_resume():
    return _resp(*api2.handle("POST", "/api2/resume", {}, token=_token()))


@app.post(
    "/api2/run-once",
    tags=["control"],
    summary="Run one pass even while paused",
    dependencies=[require_key],
)
def api2_run_once():
    return _resp(*api2.handle("POST", "/api2/run-once", {}, token=_token()))


@app.post(
    "/api2/wake",
    tags=["control"],
    summary="Wake the daemon loop early",
    dependencies=[require_key],
)
def api2_wake():
    return _resp(*api2.handle("POST", "/api2/wake", {}, token=_token()))


@app.post(
    "/api2/webhook/test",
    tags=["webhooks"],
    summary="Send a test event to configured webhook URLs; returns per-URL delivery result",
    description=(
        "Optional body {\"url\": \"http://...\"} tests a single URL instead of the "
        "configured WEBHOOK_URLS/HERMES_WEBHOOK_URL list. Payload is HMAC-SHA256 "
        "signed with the configured secret (same scheme as outbound events)."
    ),
    dependencies=[require_key],
    responses={200: {"description": "OK", "content": {"application/json": {"example": _webhook_test_example()}}}},
)
def api2_webhook_test(body: dict = None):
    return _resp(
        *api2.handle("POST", "/api2/webhook/test", body or {}, token=_token())
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
