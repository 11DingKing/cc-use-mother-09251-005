"""派单规划：道路可达性 × 补能机会 × 充电拥堵 × 值班窗口 × 优先级。

规划产物是“尚未承诺”的派单建议（PROPOSED）。承诺前的派单可因
定位更新或道路封闭被整体重算；已承诺（抢单后）的部分冻结不动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..domain.cases import Case
from ..domain.enums import Capability, ServiceType
from ..domain.network import RoadNetwork, Route
from ..domain.station import ChargingStation
from ..domain.team import Team

# 各类服务预计占用救援队的时长（分钟）
SERVICE_DURATION_MINUTES = {
    ServiceType.MOBILE_POWER.value: 30,
    ServiceType.TOW_TO_STATION.value: 50,
    ServiceType.ONSITE_REPAIR.value: 60,
    ServiceType.ACCIDENT_HANDLE.value: 45,
}
LOW_BATTERY_IMMOBILE_PCT = 8.0   # 低于该值判定趴窝
TOW_SPEED_KMH = 60.0
RESPONSE_SPEED_KMH = 70.0


@dataclass
class SlotRequirement:
    service_type: str
    required_caps: set[Capability]
    duration_minutes: float
    need_key: str  # energy / repair / accident，用于识别已被承诺覆盖的需求


@dataclass
class Candidate:
    team: Team
    route: Route
    eta_minutes: float
    score: float
    reason: str
    service_type: str = ServiceType.MOBILE_POWER.value
    # 拖车方案专属
    station: Optional[ChargingStation] = None
    station_route: Optional[Route] = None
    wait_minutes: float = 0.0


@dataclass
class PlanSlot:
    requirement: SlotRequirement
    chosen: Optional[Candidate] = None
    alternatives: list[Candidate] = field(default_factory=list)
    infeasible_reasons: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return self.chosen is not None


@dataclass
class DispatchPlan:
    case_id: str
    planned_at: datetime
    slots: list[PlanSlot]

    @property
    def feasible(self) -> bool:
        return all(s.feasible for s in self.slots)

    @property
    def required_team_ids(self) -> set[str]:
        return {s.chosen.team.team_id for s in self.slots if s.chosen}

    def infeasible_report(self) -> list[str]:
        report: list[str] = []
        for slot in self.slots:
            if not slot.feasible:
                report.append(
                    f"{slot.requirement.service_type}: "
                    + ("; ".join(slot.infeasible_reasons) or "无候选救援队")
                )
        return report


class Dispatcher:
    def __init__(
        self,
        network: RoadNetwork,
        stations: dict[str, ChargingStation],
        teams: dict[str, Team],
    ) -> None:
        self.network = network
        self.stations = stations
        self.teams = teams

    # ---- 需求拆解 ----
    def requirements_for(self, case: Case) -> list[SlotRequirement]:
        reqs: list[SlotRequirement] = []
        et = case.emergency_type
        if et == "accident":
            reqs.append(
                SlotRequirement(
                    ServiceType.ACCIDENT_HANDLE.value,
                    {Capability.ACCIDENT_RESCUE},
                    SERVICE_DURATION_MINUTES[ServiceType.ACCIDENT_HANDLE.value],
                    need_key="accident",
                )
            )
        if et == "charger_fault":
            reqs.append(
                SlotRequirement(
                    ServiceType.ONSITE_REPAIR.value,
                    {Capability.CHARGER_REPAIR},
                    SERVICE_DURATION_MINUTES[ServiceType.ONSITE_REPAIR.value],
                    need_key="repair",
                )
            )
        # 低电量，或充不上电且电池也见底 → 需要补能
        need_energy = et == "low_battery" or (
            et == "charger_fault"
            and case.battery_pct is not None
            and case.battery_pct <= 20.0
        )
        if need_energy:
            reqs.append(
                SlotRequirement(
                    # 移动补能与拖至服务区互为备选，由候选评分裁决
                    ServiceType.MOBILE_POWER.value,
                    {Capability.MOBILE_CHARGING},
                    SERVICE_DURATION_MINUTES[ServiceType.MOBILE_POWER.value],
                    need_key="energy",
                )
            )
        return reqs

    # ---- 规划 ----
    def plan(
        self,
        case: Case,
        now: datetime,
        exclude_needs: Optional[set[str]] = None,
        exclude_teams: Optional[set[str]] = None,
    ) -> DispatchPlan:
        """exclude_needs: 已被已承诺派单覆盖的需求，不再规划。
        exclude_teams: 已被占用（本单已承诺或他单执行中）的队伍。"""
        reqs = self.requirements_for(case)
        if exclude_needs:
            reqs = [r for r in reqs if r.need_key not in exclude_needs]
        slots: list[PlanSlot] = []
        used_teams: set[str] = set(exclude_teams or ())
        for req in reqs:
            slot = self._plan_slot(case, req, now, used_teams)
            slots.append(slot)
            if slot.chosen:
                used_teams.add(slot.chosen.team.team_id)
        return DispatchPlan(case.case_id, now, slots)

    def _plan_slot(
        self,
        case: Case,
        req: SlotRequirement,
        now: datetime,
        used_teams: set[str],
    ) -> PlanSlot:
        slot = PlanSlot(requirement=req)
        candidates: list[Candidate] = []

        if req.service_type == ServiceType.MOBILE_POWER.value:
            for team in self.teams.values():
                cand, reason = self._mobile_candidate(case, team, now, used_teams)
                if cand is not None:
                    candidates.append(cand)
                elif reason:
                    slot.infeasible_reasons.append(reason)
            for cand, reason in self._tow_candidates(case, now, used_teams):
                if cand is not None:
                    candidates.append(cand)
                elif reason:
                    slot.infeasible_reasons.append(reason)
        else:
            for team in self.teams.values():
                cand, reason = self._direct_candidate(
                    case, req, team, now, used_teams
                )
                if cand is not None:
                    candidates.append(cand)
                elif reason:
                    slot.infeasible_reasons.append(reason)

        candidates.sort(key=lambda c: c.score)
        slot.alternatives = candidates
        slot.chosen = candidates[0] if candidates else None
        slot.infeasible_reasons = list(dict.fromkeys(slot.infeasible_reasons))[:6]
        return slot

    def _eligibility(
        self, team: Team, required: set[Capability], used_teams: set[str]
    ) -> tuple[bool, Optional[str]]:
        """返回 (应考虑, 拒绝原因)。静默跳过的队伍两者都为空语义。"""
        if team.team_id in used_teams:
            return False, None
        if not team.has_capability(required):
            return False, None
        if team.is_busy:
            return False, f"{team.name} 正在执行其他任务"
        return True, None

    def _duty_check(
        self, team: Team, arrive: datetime, duration_minutes: float
    ) -> Optional[str]:
        if not team.on_duty(arrive):
            return f"{team.name} 预计到场时不在值班窗口"
        end = arrive + timedelta(minutes=duration_minutes)
        if team.duty_window_covering(arrive, end) is None:
            return f"{team.name} 值班窗口无法覆盖整个处置作业"
        return None

    def _mobile_candidate(
        self,
        case: Case,
        team: Team,
        now: datetime,
        used_teams: set[str],
    ) -> tuple[Optional[Candidate], Optional[str]]:
        eligible, reason = self._eligibility(
            team, {Capability.MOBILE_CHARGING}, used_teams
        )
        if not eligible:
            return None, reason
        route = self.network.shortest_route(team.base_node, case.location_node)
        if not route.reachable:
            return None, f"{team.name} 沿当前道路无法到达求援位置"
        eta = route.distance_km / RESPONSE_SPEED_KMH * 60
        arrive = now + timedelta(minutes=eta)
        duty_reason = self._duty_check(
            team, arrive,
            SERVICE_DURATION_MINUTES[ServiceType.MOBILE_POWER.value],
        )
        if duty_reason:
            return None, duty_reason
        score = self._score(eta, 0.0, case)
        if (
            case.battery_pct is not None
            and case.battery_pct <= LOW_BATTERY_IMMOBILE_PCT
        ):
            score -= 5  # 趴窝场景移动补能天然适配
        return Candidate(
            team=team, route=route, eta_minutes=eta, score=score,
            reason="移动补能车可达且值班覆盖",
            service_type=ServiceType.MOBILE_POWER.value,
        ), None

    def _direct_candidate(
        self,
        case: Case,
        req: SlotRequirement,
        team: Team,
        now: datetime,
        used_teams: set[str],
    ) -> tuple[Optional[Candidate], Optional[str]]:
        eligible, reason = self._eligibility(team, req.required_caps, used_teams)
        if not eligible:
            return None, reason
        route = self.network.shortest_route(team.base_node, case.location_node)
        if not route.reachable:
            return None, f"{team.name} 沿当前道路无法到达求援位置"
        eta = route.distance_km / RESPONSE_SPEED_KMH * 60
        arrive = now + timedelta(minutes=eta)
        duty_reason = self._duty_check(team, arrive, req.duration_minutes)
        if duty_reason:
            return None, duty_reason
        return Candidate(
            team=team, route=route, eta_minutes=eta,
            score=self._score(eta, 0.0, case),
            reason=f"沿当前道路 {route.distance_km:.0f}km 可达，值班覆盖",
            service_type=req.service_type,
        ), None

    def _tow_candidates(
        self, case: Case, now: datetime, used_teams: set[str]
    ) -> list[tuple[Optional[Candidate], Optional[str]]]:
        out: list[tuple[Optional[Candidate], Optional[str]]] = []
        tow_teams = [
            t for t in self.teams.values()
            if t.has_capability({Capability.TOWING})
            and t.team_id not in used_teams
        ]
        if not tow_teams:
            return out
        # 可达的服务区，按“拖车里程 + 拥堵等待折算里程”排序
        station_options: list[tuple[ChargingStation, Route, float]] = []
        for st in self.stations.values():
            if st.offline:
                continue
            sr = self.network.shortest_route(case.location_node, st.node_id)
            if sr.reachable:
                station_options.append(
                    (st, sr, st.estimated_wait_minutes())
                )
        station_options.sort(
            key=lambda x: x[1].distance_km + x[2] * TOW_SPEED_KMH / 60
        )
        if not station_options:
            return [(None, "没有沿当前道路可达的可用服务区充电站")]

        for team in tow_teams:
            if team.is_busy:
                out.append((None, f"{team.name} 正在执行其他任务"))
                continue
            best: Optional[Candidate] = None
            last_reason = ""
            for st, sr, wait in station_options:
                reach = self.network.shortest_route(
                    team.base_node, case.location_node
                )
                if not reach.reachable:
                    last_reason = f"{team.name} 无法到达趴窝点"
                    continue
                eta = reach.distance_km / TOW_SPEED_KMH * 60
                arrive = now + timedelta(minutes=eta)
                duty_reason = self._duty_check(
                    team, arrive,
                    SERVICE_DURATION_MINUTES[ServiceType.TOW_TO_STATION.value],
                )
                if duty_reason:
                    last_reason = duty_reason
                    continue
                score = self._score(eta, wait, case)
                score += (reach.distance_km + sr.distance_km) * 0.05
                cand = Candidate(
                    team=team, route=reach, eta_minutes=eta, score=score,
                    reason=f"拖至 {st.name}，服务区等待约 {wait:.0f} 分钟",
                    service_type=ServiceType.TOW_TO_STATION.value,
                    station=st, station_route=sr, wait_minutes=wait,
                )
                if best is None or cand.score < best.score:
                    best = cand
            if best is not None:
                out.append((best, None))
            elif last_reason:
                out.append((None, last_reason))
        return out

    def _score(self, eta: float, wait: float, case: Case) -> float:
        # 风险越高，时间成本权重越大
        weight = 1.0 + case.risk.effective_level.score * 0.5
        return eta * weight + wait * 1.5
