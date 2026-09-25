"""HTTP 接口边界：受理、抢单、转派、到场、补能、关闭等 REST 端点。

仅依赖标准库；鉴权通过 X-Operator-Token 头，隐私字段按权限范围脱敏。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from service_09251_005.application.errors import (
    AuthorizationError,
    CapacityError,
    ClaimConflictError,
    CompensationFailedError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from service_09251_005.application.privacy import (
    PRIVACY_SCOPE,
    WRITE_SCOPE,
    request_payload_for,
)
from service_09251_005.domain.models import (
    IncidentType,
    Location,
    RiskLevel,
)


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _location_from(body: dict) -> Location:
    try:
        return Location(
            road_id=str(body["road_id"]),
            direction=str(body["direction"]),
            mile_marker_km=float(body["mile_marker_km"]),
            lat=body.get("lat"),
            lon=body.get("lon"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"invalid location: {exc}") from exc


def _enum(enum_cls, value, field_name: str):
    """同时接受枚举值（"LOW_BATTERY"）与枚举名（"HIGH"）。"""
    try:
        return enum_cls(value)
    except ValueError:
        pass
    try:
        return enum_cls[str(value)]
    except KeyError as exc:
        raise ValidationError(f"invalid {field_name}: {value!r}") from exc


# ---- 端点处理函数：fn(app, match, body, scopes) -> (status, payload) ----

def h_register_team(app, match, body, scopes):
    team = app.admin.register_team(
        agency_id=str(body.get("agency_id", "")),
        name=str(body.get("name", "")),
        capabilities=list(body.get("capabilities", [])),
        road_id=str(body.get("road_id", "")),
        direction=str(body.get("direction", "")),
        coverage_from_km=float(body.get("coverage_from_km", 0)),
        coverage_to_km=float(body.get("coverage_to_km", 0)),
        staging_km=float(body.get("staging_km", 0)),
        duty_windows=list(body.get("duty_windows", [])),
        max_concurrent=int(body.get("max_concurrent", 2)),
        team_id=body.get("team_id"),
    )
    return 201, {"team": team.to_dict()}


def h_register_site(app, match, body, scopes):
    site = app.admin.register_site(
        road_id=str(body.get("road_id", "")),
        direction=str(body.get("direction", "")),
        km=float(body.get("km", 0)),
        queue=int(body.get("queue", 0)),
        congested_threshold=int(body.get("congested_threshold", 3)),
        site_id=body.get("site_id"),
    )
    return 201, {"site": site.to_dict()}


def h_register_request(app, match, body, scopes):
    request, order, merged = app.intake.register_report(
        reporter_name=str(body.get("reporter_name", "")),
        reporter_phone=str(body.get("reporter_phone", "")),
        location=_location_from(body.get("location") or {}),
        incident_type=_enum(IncidentType, body.get("incident_type"), "incident_type"),
        battery_pct=float(body.get("battery_pct", -1)),
        risk=_enum(RiskLevel, body.get("risk"), "risk"),
        occupants=int(body.get("occupants", 1)),
    )
    payload = {
        "request": request_payload_for(request.to_dict(), scopes),
        "merged": merged,
    }
    if order is not None:
        payload["order"] = order.to_dict()
    return 201, payload


def h_get_request(app, match, body, scopes):
    request = app.repo.get_request(match.group(1))
    if request is None:
        raise NotFoundError("request not found")
    return 200, {"request": request_payload_for(request.to_dict(), scopes)}


def h_update_location(app, match, body, scopes):
    request, order = app.ops.update_location(match.group(1), _location_from(body))
    payload = {"request": request_payload_for(request.to_dict(), scopes)}
    payload["order"] = order.to_dict() if order is not None else None
    return 200, payload


def _order_payload(app, order) -> dict:
    return {
        "order": order.to_dict(),
        "handoffs": [h.to_dict() for h in app.repo.list_handoffs(order.order_id)],
    }


def h_get_order(app, match, body, scopes):
    order = app.repo.get_order(match.group(1))
    if order is None:
        raise NotFoundError("order not found")
    return 200, _order_payload(app, order)


def h_claim(app, match, body, scopes):
    order = app.ops.claim(match.group(1), str(body.get("team_id", "")))
    return 200, _order_payload(app, order)


def h_transfer(app, match, body, scopes):
    order = app.ops.transfer(
        match.group(1),
        str(body.get("to_team_id", "")),
        str(body.get("reason", "")),
        operator=str(body.get("operator", "api")),
    )
    return 200, _order_payload(app, order)


def h_arrive(app, match, body, scopes):
    return 200, _order_payload(app, app.ops.arrive(match.group(1)))


def h_recharge(app, match, body, scopes):
    order = app.ops.recharge(
        match.group(1),
        site_id=body.get("site_id"),
        energy_kwh=body.get("energy_kwh"),
    )
    return 200, _order_payload(app, order)


def h_close(app, match, body, scopes):
    order = app.ops.close(match.group(1), resolution=str(body.get("resolution", "RESOLVED")))
    return 200, _order_payload(app, order)


def h_closure(app, match, body, scopes):
    closure, changed = app.ops.register_closure(
        road_id=str(body.get("road_id", "")),
        direction=str(body.get("direction", "")),
        from_km=float(body.get("from_km", 0)),
        to_km=float(body.get("to_km", 0)),
    )
    return 201, {"closure": closure.to_dict(), "replanned_orders": changed}


def h_sweep(app, match, body, scopes):
    return 200, {"escalated": app.dispatch.sweep()}


_ROUTES = [
    ("POST", re.compile(r"^/api/teams$"), WRITE_SCOPE, h_register_team),
    ("POST", re.compile(r"^/api/charging-sites$"), WRITE_SCOPE, h_register_site),
    ("POST", re.compile(r"^/api/requests$"), WRITE_SCOPE, h_register_request),
    ("GET", re.compile(r"^/api/requests/([\w-]+)$"), None, h_get_request),
    ("POST", re.compile(r"^/api/requests/([\w-]+)/location$"), WRITE_SCOPE, h_update_location),
    ("GET", re.compile(r"^/api/orders/([\w-]+)$"), None, h_get_order),
    ("POST", re.compile(r"^/api/orders/([\w-]+)/claim$"), WRITE_SCOPE, h_claim),
    ("POST", re.compile(r"^/api/orders/([\w-]+)/transfer$"), WRITE_SCOPE, h_transfer),
    ("POST", re.compile(r"^/api/orders/([\w-]+)/arrive$"), WRITE_SCOPE, h_arrive),
    ("POST", re.compile(r"^/api/orders/([\w-]+)/recharge$"), WRITE_SCOPE, h_recharge),
    ("POST", re.compile(r"^/api/orders/([\w-]+)/close$"), WRITE_SCOPE, h_close),
    ("POST", re.compile(r"^/api/roads/closures$"), WRITE_SCOPE, h_closure),
    ("POST", re.compile(r"^/api/escalations/sweep$"), WRITE_SCOPE, h_sweep),
]

_ERROR_STATUS = (
    (ValidationError, 400),
    (AuthorizationError, 403),
    (NotFoundError, 404),
    (ClaimConflictError, 409),
    (CapacityError, 409),
    (ConflictError, 409),
    (CompensationFailedError, 500),
    (DomainError, 422),
)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RescueDispatch/1.0"

        def log_message(self, *args):  # 静默访问日志
            pass

        def _send(self, status: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("request body is not valid JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("request body must be a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            try:
                token = self.headers.get("X-Operator-Token")
                scopes = app.authorizer.scopes_for(token)
                if scopes is None:
                    self._send(401, _err("UNAUTHENTICATED", "missing or unknown operator token"))
                    return
                path = self.path.split("?", 1)[0]
                for route_method, pattern, required_scope, fn in _ROUTES:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if not match:
                        continue
                    if required_scope and required_scope not in scopes:
                        self._send(403, _err("FORBIDDEN", f"scope {required_scope} required"))
                        return
                    body = self._read_body() if method == "POST" else {}
                    status, payload = fn(app, match, body, scopes)
                    self._send(status, payload)
                    return
                self._send(404, _err("ROUTE_NOT_FOUND", f"no route for {method} {path}"))
            except Exception as exc:  # noqa: BLE001 - 边界统一映射错误
                for error_cls, status in _ERROR_STATUS:
                    if isinstance(exc, error_cls):
                        self._send(status, _err(exc.code, getattr(exc, "message", str(exc))))
                        return
                self._send(500, _err("INTERNAL", f"unexpected error: {exc!r}"))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def make_server(app, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
