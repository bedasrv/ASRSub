#!/usr/bin/env python3
"""ASRSub dashboard v2 (FastAPI + static HTML).

All reads and actions are served by control_api_v2.ControlAPIv2 (in-process
proxy); the CONTROL_API_KEY token never reaches the browser.
"""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import control_api_v2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html")

api2 = control_api_v2.ControlAPIv2()
app = FastAPI(title="ASRSub")


def _resp(code, obj):
    return JSONResponse(obj, status_code=code)


def _token():
    return api2.control_token()


@app.get("/")
def index():
    return FileResponse(HTML_FILE)


@app.get("/api2/status")
def api2_status():
    return _resp(*api2.handle("GET", "/api2/status"))


@app.get("/api2/activity")
def api2_activity():
    return _resp(*api2.handle("GET", "/api2/activity"))


@app.get("/api2/wanted")
def api2_wanted():
    return _resp(*api2.handle("GET", "/api2/wanted"))


@app.get("/api2/config")
def api2_config():
    return _resp(*api2.handle("GET", "/api2/config"))


@app.get("/api2/health")
def api2_health():
    return _resp(*api2.handle("GET", "/api2/health"))


@app.post("/api2/episode/{ep_id}/retry")
def api2_episode_retry(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/retry", body or {}, token=_token())
    )


@app.post("/api2/episode/{ep_id}/skip")
def api2_episode_skip(ep_id: str, body: dict = None):
    return _resp(
        *api2.handle("POST", f"/api2/episode/{ep_id}/skip", body or {}, token=_token())
    )


@app.post("/api2/pause")
def api2_pause():
    return _resp(*api2.handle("POST", "/api2/pause", {}, token=_token()))


@app.post("/api2/resume")
def api2_resume():
    return _resp(*api2.handle("POST", "/api2/resume", {}, token=_token()))


@app.post("/api2/run-once")
def api2_run_once():
    return _resp(*api2.handle("POST", "/api2/run-once", {}, token=_token()))


@app.post("/api2/wake")
def api2_wake():
    return _resp(*api2.handle("POST", "/api2/wake", {}, token=_token()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
