"""求援工单：受理信息、乘员风险、重复呼叫合并、优先级与超时升级、隐私授权。

合并原则（重复呼叫不能吞掉更高风险信息）：
- 风险等级与各结构化风险标志只升不降（取所有呼叫的最严值）；
- 电池余量取所有呼叫报告的最小值；
- 每次呼叫都留痕，最高风险来源被显式记录。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


class PrivacyField(str, enum.Enum):
    PHONE = "phone"                  # 来电号码
    PLATE = "plate"                  # 车牌号
    EXACT_LOCATION = "exact_location"  # 精确坐标
    OCCUPANT_DETAIL = "occupant_detail"  # 乘员明细（伤情/老幼孕）


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def score(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]


@dataclass
class RiskProfile:
    """乘员与现场风险。所有字段为保守聚合值（跨呼叫只升不降）。"""
    occupants: int = 1
    injured: bool = False
    vulnerable: bool = False          # 老人/婴幼儿/孕妇
    in_driving_lane: bool = False    # 车辆停在行车道
    fire_or_smoke: bool = False
    severe_weather_exposure: bool = False  # 高温/严寒暴露
    reported_level: RiskLevel = RiskLevel.LOW  # 呼叫方自述等级

    @property
    def derived_level(self) -> RiskLevel:
        if self.fire_or_smoke or self.injured:
            return RiskLevel.CRITICAL
        if self.in_driving_lane or self.vulnerable:
            return RiskLevel.HIGH
        if self.severe_weather_exposure:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @property
    def effective_level(self) -> RiskLevel:
        return max(
            self.reported_level, self.derived_level, key=lambda r: r.score
        )

    def merge(self, other: "RiskProfile") -> "RiskProfile":
        return RiskProfile(
            occupants=max(self.occupants, other.occupants),
            injured=self.injured or other.injured,
            vulnerable=self.vulnerable or other.vulnerable,
            in_driving_lane=self.in_driving_lane or other.in_driving_lane,
            fire_or_smoke=self.fire_or_smoke or other.fire_or_smoke,
            severe_weather_exposure=(
                self.severe_weather_exposure or other.severe_weather_exposure
            ),
            reported_level=max(
                self.reported_level, other.reported_level, key=lambda r: r.score
            ),
        )


@dataclass
class CallRecord:
    call_id: str
    received_at: datetime
    source: str                       # hotline / app / patrol
    phone: str
    location_node: str
    battery_pct: Optional[float]
    risk: RiskProfile
    note: str = ""
    merged_into: Optional[str] = None  # 若被判为重复呼叫，记录主工单


@dataclass
class LocationUpdate:
    at: datetime
    from_node: str
    to_node: str
    reason: str


@dataclass
class Escalation:
    level: int
    at: datetime
    reason: str
    priority_after: str


# 距各风险等级的首次响应时限（分钟）
SLA_FIRST_RESPONSE_MINUTES = {
    RiskLevel.LOW: 30,
    RiskLevel.MEDIUM: 20,
    RiskLevel.HIGH: 12,
    RiskLevel.CRITICAL: 6,
}
ESCALATION_STEP_MINUTES = 10  # 每超时一档的升级间隔


@dataclass
class Case:
    case_id: str
    emergency_type: str
    created_at: datetime
    location_node: str
    battery_pct: Optional[float]
    risk: RiskProfile
    plate: Optional[str] = None
    phone: Optional[str] = None
    # 呼叫人授权本系统使用的隐私字段范围
    consent_scopes: frozenset[PrivacyField] = frozenset()
    calls: list[CallRecord] = field(default_factory=list)
    location_history: list[LocationUpdate] = field(default_factory=list)
    status: str = "open"
    priority: str = "routine"
    escalations: list[Escalation] = field(default_factory=list)
    escalation_level: int = 0
    response_due_at: Optional[datetime] = None
    next_escalation_at: Optional[datetime] = None
    committed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    merged_case_ids: list[str] = field(default_factory=list)
    highest_risk_call_id: Optional[str] = None
    version: int = 0  # 乐观锁

    # ---- 工厂 ----
    @classmethod
    def open_from_call(cls, call: CallRecord, emergency_type: str) -> "Case":
        prio = cls._priority_for(call.risk.effective_level, 0)
        due = call.received_at + timedelta(
            minutes=SLA_FIRST_RESPONSE_MINUTES[call.risk.effective_level]
        )
        case = cls(
            case_id="",
            emergency_type=emergency_type,
            created_at=call.received_at,
            location_node=call.location_node,
            battery_pct=call.battery_pct,
            risk=call.risk,
            plate=None,
            phone=call.phone,
            consent_scopes=frozenset(),
            calls=[call],
            priority=prio,
            response_due_at=due,
            next_escalation_at=due,
            highest_risk_call_id=call.call_id,
        )
        return case

    @staticmethod
    def _priority_for(level: RiskLevel, escalation: int) -> str:
        effective_score = min(3, level.score + escalation)
        mapping = {
            0: "routine",
            1: "urgent",
            2: "critical",
            3: "critical",
        }
        return mapping[effective_score]

    # ---- 重复呼叫合并 ----
    def merge_call(self, call: CallRecord) -> bool:
        """并入一通重复呼叫。返回风险是否因此升高。"""
        before = self.risk.effective_level
        new_risk = self.risk.merge(call.risk)
        self.risk = new_risk
        if call.battery_pct is not None:
            self.battery_pct = (
                call.battery_pct
                if self.battery_pct is None
                else min(self.battery_pct, call.battery_pct)
            )
        if new_risk.effective_level.score > before.score:
            self.highest_risk_call_id = call.call_id
        call.merged_into = self.case_id
        self.calls.append(call)
        # SLA 随更高风险收紧（只提前，不顺延）
        new_due = call.received_at + timedelta(
            minutes=SLA_FIRST_RESPONSE_MINUTES[new_risk.effective_level]
        )
        if self.response_due_at is None or new_due < self.response_due_at:
            self.response_due_at = new_due
            if self.next_escalation_at is None or new_due < self.next_escalation_at:
                self.next_escalation_at = new_due
        self._recompute_priority()
        self.version += 1
        return new_risk.effective_level.score > before.score

    def link_duplicate_case(self, other_case_id: str) -> None:
        if other_case_id not in self.merged_case_ids:
            self.merged_case_ids.append(other_case_id)

    # ---- 定位更新 ----
    def update_location(self, at: datetime, to_node: str, reason: str) -> bool:
        if to_node == self.location_node:
            return False
        self.location_history.append(
            LocationUpdate(at, self.location_node, to_node, reason)
        )
        self.location_node = to_node
        self.version += 1
        return True

    # ---- 升级 ----
    def escalate_due(self, now: datetime) -> list[Escalation]:
        """按墙钟时间连续升级（重启后可直接补齐欠的档位）。返回新增升级记录。"""
        done: list[Escalation] = []
        while (
            self.next_escalation_at is not None
            and now >= self.next_escalation_at
            and self.status not in ("resolved", "closed", "cancelled")
            and not self._is_responded()
        ):
            scheduled_at = self.next_escalation_at
            self.escalation_level += 1
            self.priority = self._priority_for(
                self.risk.effective_level, self.escalation_level
            )
            esc = Escalation(
                level=self.escalation_level,
                # 重启补齐欠档时按计划时刻留痕，而非都记为重启当下
                at=scheduled_at,
                reason="response_timeout",
                priority_after=self.priority,
            )
            self.escalations.append(esc)
            done.append(esc)
            # 按计划期限推进（而非按当前时刻），重启恢复时能一次补齐欠档
            self.next_escalation_at = scheduled_at + timedelta(
                minutes=ESCALATION_STEP_MINUTES
            )
        self.version += 1
        return done

    def _is_responded(self) -> bool:
        """已有救援队承诺即视为响应达成，停止 SLA 升级。"""
        return self.committed_at is not None

    def mark_committed(self, at: datetime) -> None:
        if self.committed_at is None:
            self.committed_at = at
            self.next_escalation_at = None
            self.version += 1

    def _recompute_priority(self) -> None:
        self.priority = self._priority_for(
            self.risk.effective_level, self.escalation_level
        )

    @property
    def risk_level(self) -> RiskLevel:
        return self.risk.effective_level

    def is_terminal(self) -> bool:
        return self.status in ("closed", "cancelled")
