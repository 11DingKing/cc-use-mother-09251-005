"""领域模型：求援登记、派单、救援队、道路封闭与补能设施。

所有时间统一以 UTC 感知 datetime 表示；值班窗口等本地时间概念
通过 zoneinfo 时区换算，避免把时区假设写进业务规则。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def iso(dt: datetime) -> str:
    """统一转换为 UTC 文本，保证字典序与时间序一致，便于范围查询。"""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _hhmm(text: str) -> time:
    hh, mm = str(text).split(":")
    return time(int(hh), int(mm))


class IncidentType(str, enum.Enum):
    LOW_BATTERY = "LOW_BATTERY"      # 低电量趴窝
    CHARGER_FAULT = "CHARGER_FAULT"  # 充电设备故障
    ACCIDENT = "ACCIDENT"            # 普通事故


class RiskLevel(enum.IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Capability(str, enum.Enum):
    TOW = "TOW"                      # 拖车
    MOBILE_CHARGE = "MOBILE_CHARGE"  # 移动充电
    CHARGER_REPAIR = "CHARGER_REPAIR"  # 充电设备抢修
    MEDICAL = "MEDICAL"              # 医疗协助


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"          # 暂无候选，等待升级扩大范围
    OFFERED = "OFFERED"          # 已挂牌，等待抢单
    CLAIMED = "CLAIMED"          # 已抢单，救援队已承诺（不可再自动改派）
    ARRIVED = "ARRIVED"          # 已到场
    RECHARGING = "RECHARGING"    # 补能中
    CLOSED = "CLOSED"


# 未承诺 = 仍可被重派/调整的状态
UNCOMMITTED_STATUSES = (OrderStatus.PENDING, OrderStatus.OFFERED)

REQUEST_OPEN = "OPEN"
REQUEST_CLOSED = "CLOSED"

# 补能建议取值
ADVICE_MOBILE_CHARGE = "MOBILE_CHARGE_PREFERRED"
ADVICE_NO_SITE = "NO_SITE_IN_RANGE"
ADVICE_GUIDE_PREFIX = "GUIDE_TO_SITE:"

DIRECTION_BOTH = "BOTH"


@dataclass(frozen=True)
class Location:
    road_id: str
    direction: str
    mile_marker_km: float
    lat: float | None = None
    lon: float | None = None

    def to_dict(self) -> dict:
        return {
            "road_id": self.road_id,
            "direction": self.direction,
            "mile_marker_km": self.mile_marker_km,
            "lat": self.lat,
            "lon": self.lon,
        }

    @staticmethod
    def from_dict(data: dict) -> "Location":
        return Location(
            road_id=str(data["road_id"]),
            direction=str(data["direction"]),
            mile_marker_km=float(data["mile_marker_km"]),
            lat=data.get("lat"),
            lon=data.get("lon"),
        )


@dataclass(frozen=True)
class DutyWindow:
    """按救援队本地时区描述的值班窗口。

    start == end 表示全天值守；start > end 表示跨午夜窗口（如 22:00-06:00）。
    """

    weekdays: frozenset[int]
    start_local: time
    end_local: time
    tz: str

    def covers(self, instant: datetime) -> bool:
        local = instant.astimezone(ZoneInfo(self.tz))
        wd = local.weekday()
        t = local.time().replace(tzinfo=None)
        if self.start_local == self.end_local:
            return wd in self.weekdays
        if self.start_local < self.end_local:
            return wd in self.weekdays and self.start_local <= t < self.end_local
        # 跨午夜：晚间段按当天星期，凌晨段按前一天星期
        if t >= self.start_local:
            return wd in self.weekdays
        if t < self.end_local:
            return (wd - 1) % 7 in self.weekdays
        return False

    def to_dict(self) -> dict:
        return {
            "weekdays": sorted(self.weekdays),
            "start": self.start_local.strftime("%H:%M"),
            "end": self.end_local.strftime("%H:%M"),
            "tz": self.tz,
        }

    @staticmethod
    def from_dict(data: dict) -> "DutyWindow":
        return DutyWindow(
            weekdays=frozenset(int(w) for w in data["weekdays"]),
            start_local=_hhmm(data["start"]),
            end_local=_hhmm(data["end"]),
            tz=str(data["tz"]),
        )


@dataclass
class RescueTeam:
    team_id: str
    agency_id: str
    name: str
    capabilities: frozenset[Capability]
    road_id: str
    direction: str
    coverage_from_km: float
    coverage_to_km: float
    staging_km: float
    duty_windows: tuple[DutyWindow, ...] = ()
    max_concurrent: int = 2
    load: int = 0

    def on_duty(self, instant: datetime) -> bool:
        return any(w.covers(instant) for w in self.duty_windows)

    def covers_location(self, loc: Location) -> bool:
        return (
            self.road_id == loc.road_id
            and self.direction == loc.direction
            and self.coverage_from_km <= loc.mile_marker_km <= self.coverage_to_km
        )

    def has_capacity(self) -> bool:
        return self.load < self.max_concurrent

    def to_dict(self) -> dict:
        return {
            "team_id": self.team_id,
            "agency_id": self.agency_id,
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "road_id": self.road_id,
            "direction": self.direction,
            "coverage_from_km": self.coverage_from_km,
            "coverage_to_km": self.coverage_to_km,
            "staging_km": self.staging_km,
            "duty_windows": [w.to_dict() for w in self.duty_windows],
            "max_concurrent": self.max_concurrent,
            "load": self.load,
        }

    @staticmethod
    def from_dict(data: dict) -> "RescueTeam":
        return RescueTeam(
            team_id=str(data["team_id"]),
            agency_id=str(data["agency_id"]),
            name=str(data["name"]),
            capabilities=frozenset(Capability(c) for c in data["capabilities"]),
            road_id=str(data["road_id"]),
            direction=str(data["direction"]),
            coverage_from_km=float(data["coverage_from_km"]),
            coverage_to_km=float(data["coverage_to_km"]),
            staging_km=float(data["staging_km"]),
            duty_windows=tuple(DutyWindow.from_dict(w) for w in data.get("duty_windows", [])),
            max_concurrent=int(data.get("max_concurrent", 2)),
            load=int(data.get("load", 0)),
        )


@dataclass
class ChargingSite:
    site_id: str
    road_id: str
    direction: str
    km: float
    queue: int = 0
    congested_threshold: int = 3

    @property
    def congested(self) -> bool:
        return self.queue >= self.congested_threshold

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "road_id": self.road_id,
            "direction": self.direction,
            "km": self.km,
            "queue": self.queue,
            "congested_threshold": self.congested_threshold,
        }

    @staticmethod
    def from_dict(data: dict) -> "ChargingSite":
        return ChargingSite(
            site_id=str(data["site_id"]),
            road_id=str(data["road_id"]),
            direction=str(data["direction"]),
            km=float(data["km"]),
            queue=int(data.get("queue", 0)),
            congested_threshold=int(data.get("congested_threshold", 3)),
        )


@dataclass
class RoadClosure:
    closure_id: str
    road_id: str
    direction: str  # 具体方向或 BOTH
    from_km: float
    to_km: float
    created_at: datetime
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "closure_id": self.closure_id,
            "road_id": self.road_id,
            "direction": self.direction,
            "from_km": self.from_km,
            "to_km": self.to_km,
            "created_at": iso(self.created_at),
            "active": self.active,
        }

    @staticmethod
    def from_dict(data: dict) -> "RoadClosure":
        return RoadClosure(
            closure_id=str(data["closure_id"]),
            road_id=str(data["road_id"]),
            direction=str(data["direction"]),
            from_km=float(data["from_km"]),
            to_km=float(data["to_km"]),
            created_at=parse_iso(data["created_at"]),
            active=bool(data.get("active", True)),
        )


@dataclass
class RescueRequest:
    request_id: str
    reporter_name: str
    reporter_phone: str
    location: Location
    incident_type: IncidentType
    battery_pct: float
    risk: RiskLevel
    occupants: int
    created_at: datetime
    priority: int
    status: str = REQUEST_OPEN
    merged_report_ids: list[str] = field(default_factory=list)
    extra_phones: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "reporter_name": self.reporter_name,
            "reporter_phone": self.reporter_phone,
            "location": self.location.to_dict(),
            "incident_type": self.incident_type.value,
            "battery_pct": self.battery_pct,
            "risk": int(self.risk),
            "occupants": self.occupants,
            "created_at": iso(self.created_at),
            "priority": self.priority,
            "status": self.status,
            "merged_report_ids": list(self.merged_report_ids),
            "extra_phones": list(self.extra_phones),
        }

    @staticmethod
    def from_dict(data: dict) -> "RescueRequest":
        return RescueRequest(
            request_id=str(data["request_id"]),
            reporter_name=str(data["reporter_name"]),
            reporter_phone=str(data["reporter_phone"]),
            location=Location.from_dict(data["location"]),
            incident_type=IncidentType(data["incident_type"]),
            battery_pct=float(data["battery_pct"]),
            risk=RiskLevel(int(data["risk"])),
            occupants=int(data["occupants"]),
            created_at=parse_iso(data["created_at"]),
            priority=int(data["priority"]),
            status=str(data.get("status", REQUEST_OPEN)),
            merged_report_ids=[str(x) for x in data.get("merged_report_ids", [])],
            extra_phones=[str(x) for x in data.get("extra_phones", [])],
        )


@dataclass
class HandoffRecord:
    """跨机构转派的责任交接记录，追加式保存，永不改写。"""

    handoff_id: str
    order_id: str
    from_agency: str
    to_agency: str
    from_team: str
    to_team: str
    reason: str
    at: datetime
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "handoff_id": self.handoff_id,
            "order_id": self.order_id,
            "from_agency": self.from_agency,
            "to_agency": self.to_agency,
            "from_team": self.from_team,
            "to_team": self.to_team,
            "reason": self.reason,
            "at": iso(self.at),
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict) -> "HandoffRecord":
        return HandoffRecord(
            handoff_id=str(data["handoff_id"]),
            order_id=str(data["order_id"]),
            from_agency=str(data["from_agency"]),
            to_agency=str(data["to_agency"]),
            from_team=str(data["from_team"]),
            to_team=str(data["to_team"]),
            reason=str(data["reason"]),
            at=parse_iso(data["at"]),
            note=str(data.get("note", "")),
        )


@dataclass
class DispatchOrder:
    order_id: str
    request_id: str
    status: OrderStatus
    priority: int
    created_at: datetime
    claim_deadline: datetime
    next_escalation_at: datetime
    candidate_team_ids: list[str] = field(default_factory=list)
    assigned_team_id: str | None = None
    committed: bool = False
    escalation_level: int = 0
    energy_advice: str | None = None
    version: int = 0
    arrived_at: datetime | None = None
    closed_at: datetime | None = None
    recharge: dict | None = None
    close_resolution: str | None = None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": iso(self.created_at),
            "claim_deadline": iso(self.claim_deadline),
            "next_escalation_at": iso(self.next_escalation_at),
            "candidate_team_ids": list(self.candidate_team_ids),
            "assigned_team_id": self.assigned_team_id,
            "committed": self.committed,
            "escalation_level": self.escalation_level,
            "energy_advice": self.energy_advice,
            "version": self.version,
            "arrived_at": iso(self.arrived_at) if self.arrived_at else None,
            "closed_at": iso(self.closed_at) if self.closed_at else None,
            "recharge": self.recharge,
            "close_resolution": self.close_resolution,
        }

    @staticmethod
    def from_dict(data: dict) -> "DispatchOrder":
        return DispatchOrder(
            order_id=str(data["order_id"]),
            request_id=str(data["request_id"]),
            status=OrderStatus(data["status"]),
            priority=int(data["priority"]),
            created_at=parse_iso(data["created_at"]),
            claim_deadline=parse_iso(data["claim_deadline"]),
            next_escalation_at=parse_iso(data["next_escalation_at"]),
            candidate_team_ids=[str(x) for x in data.get("candidate_team_ids", [])],
            assigned_team_id=data.get("assigned_team_id"),
            committed=bool(data.get("committed", False)),
            escalation_level=int(data.get("escalation_level", 0)),
            energy_advice=data.get("energy_advice"),
            version=int(data.get("version", 0)),
            arrived_at=parse_iso(data["arrived_at"]) if data.get("arrived_at") else None,
            closed_at=parse_iso(data["closed_at"]) if data.get("closed_at") else None,
            recharge=data.get("recharge"),
            close_resolution=data.get("close_resolution"),
        )


def compute_priority(risk: RiskLevel, battery_pct: float, incident_type: IncidentType, occupants: int) -> int:
    """优先级 = 乘员风险为主，叠加电量、事故类型与乘员数。"""
    score = {
        RiskLevel.LOW: 10,
        RiskLevel.MEDIUM: 20,
        RiskLevel.HIGH: 40,
        RiskLevel.CRITICAL: 80,
    }[risk]
    if battery_pct <= 5:
        score += 15
    elif battery_pct <= 15:
        score += 8
    if incident_type is IncidentType.ACCIDENT:
        score += 10
    if occupants >= 4:
        score += 5
    return score


def sla_for_priority(priority: int) -> timedelta:
    """抢单承诺时限：优先级越高，留给救援队的响应窗口越短。"""
    if priority >= 80:
        return timedelta(minutes=5)
    if priority >= 40:
        return timedelta(minutes=15)
    return timedelta(minutes=30)


def capability_options(incident_type: IncidentType, risk: RiskLevel) -> list[frozenset[Capability]]:
    """每种警情可接受的能力组合（任一组合满足即可）。"""
    if incident_type is IncidentType.LOW_BATTERY:
        return [frozenset({Capability.MOBILE_CHARGE}), frozenset({Capability.TOW})]
    if incident_type is IncidentType.CHARGER_FAULT:
        return [frozenset({Capability.CHARGER_REPAIR}), frozenset({Capability.TOW})]
    if risk >= RiskLevel.HIGH:
        return [frozenset({Capability.TOW, Capability.MEDICAL})]
    return [frozenset({Capability.TOW})]


def team_capable(team: RescueTeam, incident_type: IncidentType, risk: RiskLevel) -> bool:
    return any(option <= team.capabilities for option in capability_options(incident_type, risk))
