"""派单计划、责任交接链与补能记录。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import AssignmentStatus, HandoverState


@dataclass
class Handover:
    """一次跨机构转派的责任交接凭据。"""
    handover_id: str
    assignment_id: str
    case_id: str
    from_team_id: str
    from_org_id: str
    to_team_id: str
    to_org_id: str
    proposed_at: datetime
    reason: str
    state: str = HandoverState.PROPOSED.value
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    rejected_at: Optional[datetime] = None
    reject_reason: Optional[str] = None
    # 交接物：现场状态摘要，接收方据此承接
    situational_summary: str = ""
    chain_index: int = 0

    def acknowledge(self, at: datetime, by: str) -> None:
        self.state = HandoverState.ACKNOWLEDGED.value
        self.acknowledged_at = at
        self.acknowledged_by = by

    def reject(self, at: datetime, reason: str) -> None:
        self.state = HandoverState.REJECTED.value
        self.rejected_at = at
        self.reject_reason = reason


@dataclass
class EnergyFulfillment:
    """补能/排障完成记录。"""
    action: str
    at: datetime
    kwh_delivered: float = 0.0
    station_id: Optional[str] = None
    by_team_id: str = ""
    note: str = ""


@dataclass
class Assignment:
    assignment_id: str
    case_id: str
    team_id: str
    org_id: str
    service_type: str
    need_key: str = "energy"          # energy / repair / accident
    status: str = AssignmentStatus.PROPOSED.value
    created_at: Optional[datetime] = None
    offered_at: Optional[datetime] = None
    offer_expires_at: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    energized_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    # 规划依据（可解释）
    plan_reason: str = ""
    route_nodes: list[str] = field(default_factory=list)
    route_distance_km: float = 0.0
    eta_minutes: float = 0.0
    required_from: Optional[datetime] = None  # 处置需覆盖的值班区间起
    required_to: Optional[datetime] = None
    # 补能联动
    station_id: Optional[str] = None
    station_reserved: bool = False
    wait_minutes: float = 0.0
    energy_kwh: float = 0.0
    fulfillment: Optional[EnergyFulfillment] = None
    # 交接链：本派单关联的交接凭据 id，按时间顺序；实际凭据在仓储中去重保存
    handover_ids: list[str] = field(default_factory=list)
    parent_assignment_id: Optional[str] = None  # 转派来源派单
    chain_index: int = 0
    # 当前责任方（交接确认后更新），冗余落盘便于单表读取
    responsible_team_id: Optional[str] = None
    responsible_org_id: Optional[str] = None
    version: int = 0

    def __post_init__(self) -> None:
        if self.responsible_team_id is None:
            self.responsible_team_id = self.team_id
        if self.responsible_org_id is None:
            self.responsible_org_id = self.org_id

    @property
    def is_committed(self) -> bool:
        """已承诺=抢单之后，不可再被自动重规划撤销。"""
        return self.status in (
            AssignmentStatus.CLAIMED.value,
            AssignmentStatus.ARRIVED.value,
            AssignmentStatus.ENERGIZED.value,
            AssignmentStatus.COMPLETED.value,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            AssignmentStatus.COMPLETED.value,
            AssignmentStatus.REVOKED.value,
            AssignmentStatus.TRANSFERRED.value,
            AssignmentStatus.EXPIRED.value,
        )

    def current_party(self) -> tuple[str, str]:
        """当前责任方（转派确认后切换为接收方）。"""
        return self.responsible_team_id or self.team_id, (
            self.responsible_org_id or self.org_id
        )
