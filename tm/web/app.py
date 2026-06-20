"""Starlette app factory for the local tm web UI."""

from __future__ import annotations

import secrets
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from tm._paths import default_db_path, default_socket_path
from tm.daemon import DaemonClient
from tm.local_agents import probe_agents
from tm.web.services import (
    agent_selection,
    build_capabilities,
    build_dashboard,
    build_now,
    build_status,
    capture_calendar_text,
    capture_telegram_json,
    capture_voice_text,
    ensure_web_db,
    export_payload,
    privacy_forget,
    privacy_redact,
    reextract_case,
    run_backup,
    selected_agent_params,
)

__all__ = ["create_app"]

_WEB_TOKEN_HEADER = "x-tm-web-token"


def create_app(
    *,
    db_path: Path | None = None,
    socket_path: Path | None = None,
    static_dir: Path | None = None,
) -> Any:
    """Create the local web API and static frontend app.

    Starlette is imported lazily so the default CLI remains usable without the
    optional ``web`` dependency set installed.
    """

    try:
        from starlette.applications import Starlette
        from starlette.responses import HTMLResponse, JSONResponse
        from starlette.routing import Mount, Route
        from starlette.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - exercised by CLI envs
        raise RuntimeError("Install tm with the 'web' extra to run tm web") from exc

    resolved_db = ensure_web_db(db_path or default_db_path())
    resolved_socket = socket_path or default_socket_path()
    resolved_static = static_dir or _default_static_dir()
    web_token = secrets.token_urlsafe(32)

    def daemon_json(method: str, params: dict[str, Any]) -> JSONResponse:
        result = _daemon_call(resolved_socket, method, params)
        status_code = 200 if result.get("ok") else 503
        return JSONResponse(result, status_code=status_code)

    async def status(_request: Any) -> JSONResponse:
        payload = build_status(db_path=resolved_db, socket_path=resolved_socket)
        payload["api_token"] = web_token
        return JSONResponse(payload)

    async def capabilities(_request: Any) -> JSONResponse:
        return JSONResponse(build_capabilities())

    async def agents(_request: Any) -> JSONResponse:
        return JSONResponse({"agents": probe_agents()})

    async def select_agent(request: Any) -> JSONResponse:
        payload = await request.json()
        agent_id = str(payload.get("agent_id") or "")
        model = payload.get("model")
        model_str = model if isinstance(model, str) and model else None
        return JSONResponse(agent_selection(agent_id, model=model_str))

    async def now(_request: Any) -> JSONResponse:
        return JSONResponse(build_now(db_path=resolved_db))

    async def dashboard(request: Any) -> JSONResponse:
        since = request.query_params.get("since")
        until = request.query_params.get("until")
        return JSONResponse(
            build_dashboard(db_path=resolved_db, since=since, until=until)
        )

    async def run_debrief(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            transcript = _required_str(payload, "transcript", nonblank=True)
            case_date = _required_str(payload, "case_date")
        except ValueError as exc:
            return _bad_request(str(exc))
        params = selected_agent_params()
        return daemon_json(
            "run_debrief",
            {
                "transcript": transcript,
                "case_date": case_date,
                "backend": params["backend"],
                "model": params["model"],
            },
        )

    async def suggest(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            case_date = _required_str(payload, "case_date")
        except ValueError as exc:
            return _bad_request(str(exc))
        params = selected_agent_params()
        return daemon_json(
            "propose_suggestion",
            {
                "case_date": case_date,
                "case_goal_id": payload.get("case_goal_id"),
                "backend": params["backend"],
                "model": params["model"],
            },
        )

    async def capture_telegram_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            content = _required_str(payload, "content", nonblank=True)
        except ValueError as exc:
            return _bad_request(str(exc))
        return _json_result(capture_telegram_json, db_path=resolved_db, content=content)

    async def capture_calendar_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            content = _required_str(payload, "content", nonblank=True)
        except ValueError as exc:
            return _bad_request(str(exc))
        return _json_result(capture_calendar_text, db_path=resolved_db, content=content)

    async def capture_voice_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            transcript = _required_str(payload, "transcript", nonblank=True)
            case_date = _required_str(payload, "case_date")
        except ValueError as exc:
            return _bad_request(str(exc))
        return _json_result(
            capture_voice_text,
            db_path=resolved_db,
            transcript=transcript,
            case_date=case_date,
        )

    async def export_api(_request: Any) -> JSONResponse:
        return _json_result(export_payload, db_path=resolved_db)

    async def backup_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            output_path = _required_str(payload, "output_path")
        except ValueError as exc:
            return _bad_request(str(exc))
        overwrite = bool(payload.get("overwrite"))
        return _json_result(
            run_backup,
            db_path=resolved_db,
            output_path=Path(output_path),
            overwrite=overwrite,
        )

    async def privacy_redact_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            case_date, event_id = _selector_payload(payload)
        except ValueError as exc:
            return _bad_request(str(exc))
        replacement = payload.get("replacement")
        replacement_str = replacement if isinstance(replacement, str) else "[redacted]"
        return _json_result(
            privacy_redact,
            db_path=resolved_db,
            case_date=case_date,
            event_id=event_id,
            replacement=replacement_str,
        )

    async def privacy_forget_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            case_date, event_id = _selector_payload(payload)
        except ValueError as exc:
            return _bad_request(str(exc))
        return _json_result(
            privacy_forget,
            db_path=resolved_db,
            case_date=case_date,
            event_id=event_id,
        )

    async def reextract_api(request: Any) -> JSONResponse:
        payload = await request.json()
        try:
            case_date = _required_str(payload, "case_date")
        except ValueError as exc:
            return _bad_request(str(exc))
        transcript = payload.get("transcript")
        transcript_str = transcript if isinstance(transcript, str) else None
        return _json_result(
            reextract_case,
            db_path=resolved_db,
            case_date=case_date,
            transcript=transcript_str,
        )

    def guarded(endpoint: Any, *, require_token: bool = True) -> Any:
        async def guarded_endpoint(request: Any) -> Any:
            blocked = _web_request_guard(
                request,
                web_token,
                require_token=require_token,
            )
            if blocked is not None:
                return blocked
            return await endpoint(request)

        return guarded_endpoint

    async def missing_frontend(_request: Any) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><title>tm web</title>"
            "<main style='font-family: system-ui; padding: 2rem'>"
            "<h1>tm web API is running</h1>"
            "<p>Build or run the Vite frontend from <code>frontend/</code>.</p>"
            "</main>",
            status_code=200,
        )

    routes: list[Any] = [
        Route("/api/status", guarded(status, require_token=False), methods=["GET"]),
        Route("/api/capabilities", guarded(capabilities), methods=["GET"]),
        Route("/api/agents", guarded(agents), methods=["GET"]),
        Route("/api/agents/select", guarded(select_agent), methods=["POST"]),
        Route("/api/now", guarded(now), methods=["GET"]),
        Route("/api/dashboard", guarded(dashboard), methods=["GET"]),
        Route("/api/debrief", guarded(run_debrief), methods=["POST"]),
        Route("/api/suggest", guarded(suggest), methods=["POST"]),
        Route("/api/capture/telegram", guarded(capture_telegram_api), methods=["POST"]),
        Route("/api/capture/calendar", guarded(capture_calendar_api), methods=["POST"]),
        Route("/api/capture/voice", guarded(capture_voice_api), methods=["POST"]),
        Route("/api/export", guarded(export_api), methods=["GET"]),
        Route("/api/backup", guarded(backup_api), methods=["POST"]),
        Route("/api/privacy/redact", guarded(privacy_redact_api), methods=["POST"]),
        Route("/api/privacy/forget", guarded(privacy_forget_api), methods=["POST"]),
        Route("/api/reextract", guarded(reextract_api), methods=["POST"]),
    ]
    if resolved_static.exists():
        routes.append(Mount("/", StaticFiles(directory=resolved_static, html=True)))
    else:
        routes.append(Route("/", missing_frontend, methods=["GET"]))

    return Starlette(debug=False, routes=routes)


def _mutation_guard(request: Any, web_token: str) -> Any | None:
    return _web_request_guard(request, web_token, require_token=True)


def _web_request_guard(
    request: Any,
    web_token: str,
    *,
    require_token: bool,
) -> Any | None:
    from starlette.responses import JSONResponse

    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host and not _is_loopback_host(str(host)):
        return JSONResponse(
            {"ok": False, "error": "remote web access blocked"},
            status_code=403,
        )

    sec_fetch_site = str(request.headers.get("sec-fetch-site", "")).lower()
    if sec_fetch_site == "cross-site":
        return JSONResponse(
            {"ok": False, "error": "cross-site request blocked"},
            status_code=403,
        )

    if not require_token:
        return None

    supplied_token = request.headers.get(_WEB_TOKEN_HEADER)
    if not isinstance(supplied_token, str) or not secrets.compare_digest(
        supplied_token, web_token
    ):
        return JSONResponse(
            {"ok": False, "error": "invalid web token"},
            status_code=403,
        )
    return None


def _is_loopback_host(host: str) -> bool:
    if host == "testclient":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _daemon_call(
    socket_path: Path, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        result = DaemonClient(socket_path, timeout=2.0).call(method, **params)
    except Exception as exc:  # noqa: BLE001 - JSON API boundary
        return {
            "ok": False,
            "error": "daemon unavailable",
            "detail": str(exc),
        }
    if isinstance(result, dict):
        return result
    return {"ok": True, "result": result}


def _default_static_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _bad_request(message: str) -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": False, "error": message}, status_code=400)


def _required_str(
    payload: dict[str, Any],
    field: str,
    *,
    nonblank: bool = False,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or (nonblank and not value.strip()):
        raise ValueError(f"{field} required")
    return value


def _json_result(fn: Any, **kwargs: Any) -> Any:
    from starlette.responses import JSONResponse

    try:
        payload = fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - JSON boundary
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
            status_code=500,
        )
    return JSONResponse(payload)


def _selector_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    case_date = payload.get("case_date")
    event_id = payload.get("event_id")
    case_date_str = case_date if isinstance(case_date, str) and case_date else None
    event_id_str = event_id if isinstance(event_id, str) and event_id else None
    if bool(case_date_str) == bool(event_id_str):
        raise ValueError("provide exactly one of case_date or event_id")
    return case_date_str, event_id_str
