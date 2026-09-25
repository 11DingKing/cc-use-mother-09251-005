"""JSON API 边界：纯函数路由 + http.server 适配。

路由层不持有业务逻辑，只做字段校验、错误码映射与隐私 scope 注入。
scope 由调用方凭据决定，这里用 X-Privacy-Scopes 头模拟授权后的声明。
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..application.service import RescueService
from ..domain.cases import PrivacyField
from ..domain.enums import Capability
from ..domain.station import ChargingStation
from ..domain.team import DutyWindow
from ..errors import DispatchError

Handler = Callable[[dict, dict], Any]


def _iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class JsonApi:
    def __init__(self, service: RescueService) -> None:
        self.svc = service
        self.routes: list[tuple[str, re.Pattern, Handler]] = []
        self._register()

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        rx = re.compile("^" + re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn: Handler) -> Handler:
            self.routes.append((method, rx, fn))
            return fn
        return deco

    def _register(self) -> None:
        s = self.svc

        @self.route("POST", "/admin/nodes")
        def _(b, _h):
            s.add_node(b["node_id"], b.get("name", ""))
            return {"ok": True}

        @self.route("POST", "/admin/segments")
        def _(b, _h):
            s.add_segment(
                b["segment_id"], b["a"], b["b"], float(b["distance_km"]),
                direction=b.get("direction", "bidirectional"),
                speed_kmh=float(b.get("speed_kmh", 80.0)),
            )
            return {"ok": True}

        @self.route("POST", "/admin/roads/close")
        def _(b, _h):
            affected = s.close_road(b["segment_id"], b.get("reason", ""))
            return {"affected_cases": affected}

        @self.route("POST", "/admin/teams")
        def _(b, _h):
            wins = [
                DutyWindow(
                    start_local=w["start"], end_local=w["end"],
                    weekdays=(
                        DutyWindow.weekdays_from_names(w["weekdays"])
                        if w.get("weekdays") else frozenset(range(7))
                    ),
                    tz_name=w.get("tz", "Asia/Shanghai"),
                )
                for w in b.get("duty_windows", [])
            ]
            team = s.register_team(
                b["team_id"], b["name"], b["org_id"], b["base_node"],
                {Capability(c) for c in b["capabilities"]},
                duty_windows=wins,
                mobile_power_kwh=float(b.get("mobile_power_kwh", 0.0)),
            )
            return {"team_id": team.team_id}

        @self.route("POST", "/admin/stations")
        def _(b, _h):
            st = ChargingStation(
                station_id=b["station_id"], name=b["name"],
                node_id=b["node_id"], total_ports=int(b["total_ports"]),
                occupied=int(b.get("occupied", 0)),
                reserved=int(b.get("reserved", 0)),
                queue_length=int(b.get("queue_length", 0)),
                avg_charge_minutes=float(b.get("avg_charge_minutes", 40.0)),
                offline=bool(b.get("offline", False)),
            )
            s.register_station(st)
            return {"station_id": st.station_id}

        @self.route("POST", "/admin/congestion")
        def _(b, _h):
            s.set_station_congestion(
                b["station_id"],
                occupied=b.get("occupied"),
                queue_length=b.get("queue_length"),
                offline=b.get("offline"),
            )
            return {"ok": True}

        @self.route("POST", "/intake")
        def _(b, _h):
            scopes = {PrivacyField(x) for x in b.get("consent_scopes", [])}
            case, merged = s.intake(
                b["emergency_type"], b["location_node"], b["phone"],
                battery_pct=b.get("battery_pct"),
                plate=b.get("plate"),
                occupants=int(b.get("occupants", 1)),
                injured=bool(b.get("injured", False)),
                vulnerable=bool(b.get("vulnerable", False)),
                in_driving_lane=bool(b.get("in_driving_lane", False)),
                fire_or_smoke=bool(b.get("fire_or_smoke", False)),
                severe_weather_exposure=bool(
                    b.get("severe_weather_exposure", False)
                ),
                reported_level=b.get("reported_level", "low"),
                consent_scopes=scopes,
                source=b.get("source", "hotline"),
                note=b.get("note", ""),
            )
            return {"case_id": case.case_id, "merged": merged,
                    "status": case.status, "priority": case.priority}

        @self.route("GET", "/cases/{case_id}")
        def _(_b, h):
            cid = h["_params"]["case_id"]
            qs = h["_query"]
            if "scope" in qs:
                scopes = {PrivacyField(x) for x in qs.get("scope", [])}
            else:
                # 未显式声明时，接线员标准视图沿用呼叫人已授权的范围
                scopes = set(s.get_case(cid).consent_scopes)
            return s.case_view(cid, scopes)

        @self.route("POST", "/cases/{case_id}/location")
        def _(b, h):
            plan = s.update_location(
                h["_params"]["case_id"], b["to_node"],
                b.get("reason", "location_update")
            )
            return {"feasible": plan.feasible,
                    "slots": len(plan.slots)}

        @self.route("GET", "/cases/{case_id}/assignments")
        def _(_b, h):
            cid = h["_params"]["case_id"]
            s.get_case(cid)
            with s.repo.lock:
                items = [
                    {
                        "assignment_id": a.assignment_id,
                        "team_id": a.responsible_team_id,
                        "org_id": a.responsible_org_id,
                        "service_type": a.service_type,
                        "need_key": a.need_key,
                        "status": a.status,
                        "eta_minutes": round(a.eta_minutes, 1),
                        "station_id": a.station_id,
                        "station_reserved": a.station_reserved,
                        "wait_minutes": round(a.wait_minutes, 1),
                        "parent_assignment_id": a.parent_assignment_id,
                        "chain_index": a.chain_index,
                    }
                    for a in s.repo.assignments.values()
                    if a.case_id == cid
                ]
            return {"assignments": items}

        @self.route("GET", "/cases/{case_id}/handovers")
        def _(_b, h):
            chain = s.handover_chain(h["_params"]["case_id"])
            return {
                "handovers": [
                    {
                        "handover_id": x.handover_id,
                        "state": x.state,
                        "from_org_id": x.from_org_id,
                        "to_org_id": x.to_org_id,
                        "from_team_id": x.from_team_id,
                        "to_team_id": x.to_team_id,
                        "reason": x.reason,
                        "chain_index": x.chain_index,
                        "acknowledged_at": (
                            x.acknowledged_at.isoformat()
                            if x.acknowledged_at else None
                        ),
                    }
                    for x in chain
                ]
            }

        @self.route("POST", "/assignments/{assignment_id}/claim")
        def _(b, h):
            asg = s.claim(
                h["_params"]["assignment_id"], b["team_id"],
                at=_iso(b.get("at")),
            )
            return {"assignment_id": asg.assignment_id, "status": asg.status,
                    "team_id": asg.responsible_team_id}

        @self.route("POST", "/assignments/{assignment_id}/arrive")
        def _(b, h):
            asg = s.arrive(
                h["_params"]["assignment_id"], at=_iso(b.get("at")),
                note=b.get("note", ""),
            )
            return {"assignment_id": asg.assignment_id, "status": asg.status}

        @self.route("POST", "/assignments/{assignment_id}/energize")
        def _(b, h):
            asg = s.energize(
                h["_params"]["assignment_id"],
                action=b.get("action", "mobile_recharge"),
                kwh_delivered=float(b.get("kwh_delivered", 0.0)),
                note=b.get("note", ""), at=_iso(b.get("at")),
            )
            return {"assignment_id": asg.assignment_id, "status": asg.status}

        @self.route("POST", "/assignments/{assignment_id}/transfers")
        def _(b, h):
            ho = s.propose_transfer(
                h["_params"]["assignment_id"], b["to_team_id"],
                b.get("reason", ""), b.get("situational_summary", ""),
                at=_iso(b.get("at")),
            )
            return {"handover_id": ho.handover_id, "state": ho.state}

        @self.route("POST", "/transfers/{handover_id}/ack")
        def _(b, h):
            asg = s.acknowledge_transfer(
                h["_params"]["handover_id"], b["team_id"],
                at=_iso(b.get("at")),
            )
            return {"assignment_id": asg.assignment_id, "status": asg.status,
                    "team_id": asg.responsible_team_id,
                    "org_id": asg.responsible_org_id}

        @self.route("POST", "/transfers/{handover_id}/reject")
        def _(b, h):
            ho = s.reject_transfer(
                h["_params"]["handover_id"], b.get("reason", ""),
                b["team_id"], at=_iso(b.get("at")),
            )
            return {"handover_id": ho.handover_id, "state": ho.state}

        @self.route("POST", "/cases/{case_id}/close")
        def _(b, h):
            case = s.close_case(
                h["_params"]["case_id"],
                resolution_note=b.get("note", ""), at=_iso(b.get("at")),
            )
            return {"case_id": case.case_id, "status": case.status}

        @self.route("POST", "/tick")
        def _(b, _h):
            return s.run_due(now=_iso(b.get("at")))

    # ---- 分派 ----
    def handle(
        self, method: str, target: str, body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        parts = urlsplit(target)
        query = parse_qs(parts.query)
        h: dict[str, Any] = dict(headers or {})
        h["_query"] = query
        body = body or {}
        for m, rx, fn in self.routes:
            if m != method:
                continue
            match = rx.match(parts.path)
            if not match:
                continue
            h["_params"] = match.groupdict()
            try:
                result = fn(body, h)
                return 200, {"ok": True, "data": result}
            except DispatchError as exc:
                return 409, {"ok": False, "error": exc.code,
                             "message": str(exc)}
            except KeyError as exc:
                return 400, {"ok": False, "error": "missing_field",
                             "message": f"缺少字段 {exc}"}
            except (ValueError, TypeError) as exc:
                return 400, {"ok": False, "error": "bad_request",
                             "message": str(exc)}
            except Exception:  # 兜底，防止单请求异常炸掉服务
                return 500, {"ok": False, "error": "internal",
                             "message": traceback.format_exc(limit=2)}
        return 404, {"ok": False, "error": "not_found",
                     "message": f"{method} {parts.path}"}


def build_handler(api: JsonApi) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默
            pass

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._do("GET")

        def do_POST(self) -> None:
            self._do("POST")

        def _do(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._reply(400, {"ok": False, "error": "bad_json"})
                return
            status, payload = api.handle(method, self.path, body, dict(self.headers))
            self._reply(status, payload)

    return _Handler


def serve(api: JsonApi, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), build_handler(api))
    httpd.serve_forever()
