"""持久化：线程安全的内存仓储 + JSON 快照（原子替换落盘）。

运行数据不写入源码目录：快照路径由调用方显式给出（如 /tmp 或数据卷）。
序列化覆盖道路、服务区、队伍、工单、派单、交接凭据与补偿台账，
因此重启后超时升级、待重试补偿、待抢单派单都能继续。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from typing import Optional

from ..domain.assignment import Assignment, EnergyFulfillment, Handover
from ..domain.cases import (
    CallRecord,
    Case,
    Escalation,
    LocationUpdate,
    PrivacyField,
    RiskLevel,
    RiskProfile,
)
from ..domain.network import ClosureEvent, RoadNetwork, RoadNode, RoadSegment
from ..domain.station import ChargingStation
from ..domain.team import DutyWindow, Team
from ..domain.enums import Capability


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


class Repository:
    def __init__(self, snapshot_path: Optional[str] = None) -> None:
        self.snapshot_path = snapshot_path
        self.lock = threading.RLock()
        self.network = RoadNetwork()
        self.stations: dict[str, ChargingStation] = {}
        self.teams: dict[str, Team] = {}
        self.cases: dict[str, Case] = {}
        self.assignments: dict[str, Assignment] = {}
        self.handovers: dict[str, Handover] = {}
        self.compensations: list[dict] = []
        self.counters: dict[str, int] = {}

    # ---- ID ----
    def next_id(self, prefix: str) -> str:
        with self.lock:
            n = self.counters.get(prefix, 0) + 1
            self.counters[prefix] = n
            return f"{prefix}-{n:06d}"

    def nodes_seeded(self) -> bool:
        with self.lock:
            return bool(self.network.nodes)

    # ---- 快照 ----
    def save(self) -> None:
        if not self.snapshot_path:
            return
        with self.lock:
            data = self._to_dict()
        d = os.path.dirname(os.path.abspath(self.snapshot_path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", prefix=".snap-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.snapshot_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def load(self) -> bool:
        if not self.snapshot_path or not os.path.exists(self.snapshot_path):
            return False
        with open(self.snapshot_path, encoding="utf-8") as f:
            data = json.load(f)
        with self.lock:
            self._from_dict(data)
        return True

    # ---- 序列化 ----
    def _to_dict(self) -> dict:
        return {
            "counters": self.counters,
            "nodes": [vars(n) for n in self.network.nodes.values()],
            "segments": [vars(s) for s in self.network.segments.values()],
            "closures": [vars(c) for c in self.network.closures],
            "stations": [vars(s) for s in self.stations.values()],
            "teams": [self._team_dict(t) for t in self.teams.values()],
            "cases": [self._case_dict(c) for c in self.cases.values()],
            "assignments": [
                self._assignment_dict(a) for a in self.assignments.values()
            ],
            "handovers": [
                {
                    "handover_id": h.handover_id,
                    "assignment_id": h.assignment_id,
                    "case_id": h.case_id,
                    "from_team_id": h.from_team_id,
                    "from_org_id": h.from_org_id,
                    "to_team_id": h.to_team_id,
                    "to_org_id": h.to_org_id,
                    "proposed_at": _iso(h.proposed_at),
                    "reason": h.reason,
                    "state": h.state,
                    "acknowledged_at": _iso(h.acknowledged_at),
                    "acknowledged_by": h.acknowledged_by,
                    "rejected_at": _iso(h.rejected_at),
                    "reject_reason": h.reject_reason,
                    "situational_summary": h.situational_summary,
                    "chain_index": h.chain_index,
                }
                for h in self.handovers.values()
            ],
            "compensations": self.compensations,
        }

    def _team_dict(self, t: Team) -> dict:
        return {
            "team_id": t.team_id,
            "name": t.name,
            "org_id": t.org_id,
            "base_node": t.base_node,
            "capabilities": sorted(c.value for c in t.capabilities),
            "duty_windows": [
                {
                    "start_local": w.start_local,
                    "end_local": w.end_local,
                    "weekdays": sorted(w.weekdays),
                    "tz_name": w.tz_name,
                }
                for w in t.duty_windows
            ],
            "mobile_power_kwh": t.mobile_power_kwh,
            "unavailable_until": _iso(t.unavailable_until),
            "current_case_id": t.current_case_id,
        }

    def _risk_dict(self, r: RiskProfile) -> dict:
        d = vars(r).copy()
        d["reported_level"] = r.reported_level.value
        return d

    def _risk_from(self, d: dict) -> RiskProfile:
        return RiskProfile(
            occupants=d["occupants"],
            injured=d["injured"],
            vulnerable=d["vulnerable"],
            in_driving_lane=d["in_driving_lane"],
            fire_or_smoke=d["fire_or_smoke"],
            severe_weather_exposure=d["severe_weather_exposure"],
            reported_level=RiskLevel(d["reported_level"]),
        )

    def _case_dict(self, c: Case) -> dict:
        return {
            "case_id": c.case_id,
            "emergency_type": c.emergency_type,
            "created_at": _iso(c.created_at),
            "location_node": c.location_node,
            "battery_pct": c.battery_pct,
            "risk": self._risk_dict(c.risk),
            "plate": c.plate,
            "phone": c.phone,
            "consent_scopes": sorted(s.value for s in c.consent_scopes),
            "calls": [
                {
                    "call_id": k.call_id,
                    "received_at": _iso(k.received_at),
                    "source": k.source,
                    "phone": k.phone,
                    "location_node": k.location_node,
                    "battery_pct": k.battery_pct,
                    "risk": self._risk_dict(k.risk),
                    "note": k.note,
                    "merged_into": k.merged_into,
                }
                for k in c.calls
            ],
            "location_history": [vars(u) | {"at": _iso(u.at)} for u in c.location_history],
            "status": c.status,
            "priority": c.priority,
            "escalations": [vars(e) | {"at": _iso(e.at)} for e in c.escalations],
            "escalation_level": c.escalation_level,
            "response_due_at": _iso(c.response_due_at),
            "next_escalation_at": _iso(c.next_escalation_at),
            "committed_at": _iso(c.committed_at),
            "closed_at": _iso(c.closed_at),
            "merged_case_ids": c.merged_case_ids,
            "highest_risk_call_id": c.highest_risk_call_id,
            "version": c.version,
        }

    def _assignment_dict(self, a: Assignment) -> dict:
        d = {
            k: v for k, v in vars(a).items()
            if k != "fulfillment"
        }
        for k in ("created_at", "offered_at", "offer_expires_at", "claimed_at",
                  "arrived_at", "energized_at", "completed_at", "revoked_at",
                  "required_from", "required_to"):
            d[k] = _iso(getattr(a, k))
        d["fulfillment"] = (
            None if a.fulfillment is None
            else vars(a.fulfillment) | {"at": _iso(a.fulfillment.at)}
        )
        return d

    def _from_dict(self, data: dict) -> None:
        self.counters = data.get("counters", {})
        self.network = RoadNetwork()
        for n in data.get("nodes", []):
            self.network.nodes[n["node_id"]] = RoadNode(**n)
        for s in data.get("segments", []):
            seg = RoadSegment(
                segment_id=s["segment_id"], a=s["a"], b=s["b"],
                distance_km=s["distance_km"], direction=s["direction"],
                speed_kmh=s["speed_kmh"], closed=s["closed"],
                closed_since=s.get("closed_since"),
                closed_reason=s.get("closed_reason", ""),
            )
            self.network.segments[seg.segment_id] = seg
        self.network.closures = [ClosureEvent(**c) for c in data.get("closures", [])]
        self.stations = {}
        for s in data.get("stations", []):
            self.stations[s["station_id"]] = ChargingStation(**s)
        self.teams = {}
        for t in data.get("teams", []):
            wins = [DutyWindow(**w) for w in t["duty_windows"]]
            team = Team(
                team_id=t["team_id"], name=t["name"], org_id=t["org_id"],
                base_node=t["base_node"],
                capabilities=frozenset(Capability(c) for c in t["capabilities"]),
                duty_windows=wins,
                mobile_power_kwh=t.get("mobile_power_kwh", 0.0),
                unavailable_until=_dt(t.get("unavailable_until")),
                current_case_id=t.get("current_case_id"),
            )
            self.teams[team.team_id] = team
        self.cases = {}
        for c in data.get("cases", []):
            calls = [
                CallRecord(
                    call_id=k["call_id"], received_at=_dt(k["received_at"]),
                    source=k["source"], phone=k["phone"],
                    location_node=k["location_node"],
                    battery_pct=k["battery_pct"],
                    risk=self._risk_from(k["risk"]), note=k.get("note", ""),
                    merged_into=k.get("merged_into"),
                )
                for k in c["calls"]
            ]
            case = Case(
                case_id=c["case_id"], emergency_type=c["emergency_type"],
                created_at=_dt(c["created_at"]),
                location_node=c["location_node"],
                battery_pct=c.get("battery_pct"),
                risk=self._risk_from(c["risk"]),
                plate=c.get("plate"), phone=c.get("phone"),
                consent_scopes=frozenset(
                    PrivacyField(s) for s in c.get("consent_scopes", [])
                ),
                calls=calls,
                location_history=[
                    LocationUpdate(
                        at=_dt(u["at"]), from_node=u["from_node"],
                        to_node=u["to_node"], reason=u["reason"],
                    )
                    for u in c.get("location_history", [])
                ],
                status=c["status"], priority=c["priority"],
                escalations=[
                    Escalation(
                        level=e["level"], at=_dt(e["at"]), reason=e["reason"],
                        priority_after=e["priority_after"],
                    )
                    for e in c.get("escalations", [])
                ],
                escalation_level=c.get("escalation_level", 0),
                response_due_at=_dt(c.get("response_due_at")),
                next_escalation_at=_dt(c.get("next_escalation_at")),
                committed_at=_dt(c.get("committed_at")),
                closed_at=_dt(c.get("closed_at")),
                merged_case_ids=c.get("merged_case_ids", []),
                highest_risk_call_id=c.get("highest_risk_call_id"),
                version=c.get("version", 0),
            )
            self.cases[case.case_id] = case
        self.handovers = {}
        for h in data.get("handovers", []):
            ho = Handover(
                handover_id=h["handover_id"], assignment_id=h["assignment_id"],
                case_id=h["case_id"], from_team_id=h["from_team_id"],
                from_org_id=h["from_org_id"], to_team_id=h["to_team_id"],
                to_org_id=h["to_org_id"], proposed_at=_dt(h["proposed_at"]),
                reason=h["reason"], state=h["state"],
                acknowledged_at=_dt(h.get("acknowledged_at")),
                acknowledged_by=h.get("acknowledged_by"),
                rejected_at=_dt(h.get("rejected_at")),
                reject_reason=h.get("reject_reason"),
                situational_summary=h.get("situational_summary", ""),
                chain_index=h.get("chain_index", 0),
            )
            self.handovers[ho.handover_id] = ho
        self.assignments = {}
        for a in data.get("assignments", []):
            ful = a.get("fulfillment")
            fulfillment = (
                EnergyFulfillment(
                    action=ful["action"], at=_dt(ful["at"]),
                    kwh_delivered=ful.get("kwh_delivered", 0.0),
                    station_id=ful.get("station_id"),
                    by_team_id=ful.get("by_team_id", ""),
                    note=ful.get("note", ""),
                )
                if ful else None
            )
            asg = Assignment(
                assignment_id=a["assignment_id"], case_id=a["case_id"],
                team_id=a["team_id"], org_id=a["org_id"],
                service_type=a["service_type"],
                need_key=a.get("need_key", "energy"),
                status=a["status"],
                created_at=_dt(a.get("created_at")),
                offered_at=_dt(a.get("offered_at")),
                offer_expires_at=_dt(a.get("offer_expires_at")),
                claimed_at=_dt(a.get("claimed_at")),
                arrived_at=_dt(a.get("arrived_at")),
                energized_at=_dt(a.get("energized_at")),
                completed_at=_dt(a.get("completed_at")),
                revoked_at=_dt(a.get("revoked_at")),
                plan_reason=a.get("plan_reason", ""),
                route_nodes=a.get("route_nodes", []),
                route_distance_km=a.get("route_distance_km", 0.0),
                eta_minutes=a.get("eta_minutes", 0.0),
                required_from=_dt(a.get("required_from")),
                required_to=_dt(a.get("required_to")),
                station_id=a.get("station_id"),
                station_reserved=a.get("station_reserved", False),
                wait_minutes=a.get("wait_minutes", 0.0),
                energy_kwh=a.get("energy_kwh", 0.0),
                fulfillment=fulfillment,
                handover_ids=a.get("handover_ids", []),
                parent_assignment_id=a.get("parent_assignment_id"),
                chain_index=a.get("chain_index", 0),
                responsible_team_id=a.get("responsible_team_id"),
                responsible_org_id=a.get("responsible_org_id"),
                version=a.get("version", 0),
            )
            self.assignments[asg.assignment_id] = asg
        self.compensations = data.get("compensations", [])
