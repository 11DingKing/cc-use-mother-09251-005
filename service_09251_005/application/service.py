"""救援派单应用服务：用例编排与事务边界。

所有状态变更都在 Repository 的同一把锁内完成并落盘快照，
因此并发抢单只有一个赢家，进程重启后状态可完整恢复。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..clock import Clock
from ..domain.assignment import Assignment, EnergyFulfillment, Handover
from ..domain.cases import (
    CallRecord,
    Case,
    PrivacyField,
    RiskLevel,
    RiskProfile,
)
from ..domain.enums import (
    AssignmentStatus,
    Capability,
    HandoverState,
    ServiceType,
)
from ..dispatch.planner import (
    Candidate,
    Dispatcher,
    DispatchPlan,
    SERVICE_DURATION_MINUTES,
)
from ..errors import (
    AlreadyClaimedError,
    ConflictError,
    NotFoundError,
    PrivacyDeniedError,
    TransferRejectedError,
)
from .compensation import (
    CompensationLedger,
    LocalStationGateway,
    StationGateway,
)
from .repository import Repository

OFFER_TTL_MINUTES = 5
DUP_WINDOW_MINUTES = 60

SERVICE_CAPS = {
    ServiceType.MOBILE_POWER.value: {Capability.MOBILE_CHARGING},
    ServiceType.TOW_TO_STATION.value: {Capability.TOWING},
    ServiceType.ONSITE_REPAIR.value: {Capability.CHARGER_REPAIR},
    ServiceType.ACCIDENT_HANDLE.value: {Capability.ACCIDENT_RESCUE},
}


class RescueService:
    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        gateway: Optional[StationGateway] = None,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.dispatcher = Dispatcher(repo.network, repo.stations, repo.teams)
        self.gateway = gateway or LocalStationGateway()
        self.ledger = CompensationLedger(repo)

    def _commit(self) -> None:
        self.repo.save()

    # ============================================================
    # 资源登记
    # ============================================================
    def register_team(
        self,
        team_id: str,
        name: str,
        org_id: str,
        base_node: str,
        capabilities: set[Capability],
        duty_windows: list | None = None,
        mobile_power_kwh: float = 0.0,
    ):
        from ..domain.team import Team
        with self.repo.lock:
            team = Team(
                team_id=team_id, name=name, org_id=org_id, base_node=base_node,
                capabilities=frozenset(capabilities),
                duty_windows=list(duty_windows or []),
                mobile_power_kwh=mobile_power_kwh,
            )
            self.repo.teams[team_id] = team
            self._commit()
            return team

    def register_station(self, station) -> object:
        with self.repo.lock:
            self.repo.stations[station.station_id] = station
            self._commit()
            return station

    def add_node(self, node_id: str, name: str = "") -> None:
        with self.repo.lock:
            self.repo.network.add_node(node_id, name)
            self._commit()

    def add_segment(self, *args, **kwargs) -> None:
        with self.repo.lock:
            self.repo.network.add_segment(*args, **kwargs)
            self._commit()

    def set_station_congestion(
        self,
        station_id: str,
        *,
        occupied: Optional[int] = None,
        queue_length: Optional[int] = None,
        offline: Optional[bool] = None,
    ) -> None:
        """运营侧更新服务区拥堵/离线状态。"""
        with self.repo.lock:
            st = self.repo.stations[station_id]
            if occupied is not None:
                st.occupied = occupied
            if queue_length is not None:
                st.queue_length = queue_length
            if offline is not None:
                st.offline = offline
            self._commit()

    # ============================================================
    # 受理与重复呼叫合并
    # ============================================================
    def intake(
        self,
        emergency_type: str,
        location_node: str,
        phone: str,
        *,
        battery_pct: Optional[float] = None,
        plate: Optional[str] = None,
        occupants: int = 1,
        injured: bool = False,
        vulnerable: bool = False,
        in_driving_lane: bool = False,
        fire_or_smoke: bool = False,
        severe_weather_exposure: bool = False,
        reported_level: str = "low",
        consent_scopes: Optional[set[PrivacyField]] = None,
        source: str = "hotline",
        note: str = "",
        call_id: Optional[str] = None,
        received_at: Optional[datetime] = None,
    ) -> tuple[Case, bool]:
        """受理求援。返回 (工单, 是否并入了已有工单)。"""
        now = received_at or self.clock.now()
        with self.repo.lock:
            risk = RiskProfile(
                occupants=occupants, injured=injured, vulnerable=vulnerable,
                in_driving_lane=in_driving_lane, fire_or_smoke=fire_or_smoke,
                severe_weather_exposure=severe_weather_exposure,
                reported_level=RiskLevel(reported_level),
            )
            call = CallRecord(
                call_id=call_id or self.repo.next_id("call"),
                received_at=now, source=source, phone=phone,
                location_node=location_node, battery_pct=battery_pct,
                risk=risk, note=note,
            )
            existing = self._find_duplicate(
                phone, plate, emergency_type, location_node, now
            )
            if existing is not None:
                risk_raised = existing.merge_call(call)
                # 更高风险信息可能改变可行性：只重算未承诺部分
                self._replan_uncommitted(existing, now,
                                         reason="duplicate_call_risk_update")
                self._commit()
                return existing, True

            case = Case.open_from_call(call, emergency_type)
            case.case_id = self.repo.next_id("case")
            call.merged_into = None
            case.plate = plate
            case.consent_scopes = frozenset(consent_scopes or set())
            self.repo.cases[case.case_id] = case
            self._replan_uncommitted(case, now, reason="initial_intake")
            self._commit()
            return case, False

    def _find_duplicate(
        self,
        phone: str,
        plate: Optional[str],
        emergency_type: str,
        location_node: str,
        now: datetime,
    ) -> Optional[Case]:
        cutoff = now - timedelta(minutes=DUP_WINDOW_MINUTES)
        for case in self.repo.cases.values():
            if case.is_terminal():
                continue
            if case.emergency_type != emergency_type:
                continue
            if case.created_at < cutoff:
                continue
            same_contact = bool(phone and case.phone == phone) or (
                bool(plate) and bool(case.plate) and case.plate == plate
            )
            if not same_contact:
                continue
            # 位置已漂移到邻近节点仍视为同一事件
            if case.location_node != location_node:
                continue
            return case
        return None

    # ============================================================
    # 规划与重规划（只动未承诺部分）
    # ============================================================
    def _committed_needs(self, case: Case) -> tuple[set[str], set[str]]:
        needs: set[str] = set()
        teams: set[str] = set()
        for a in self.repo.assignments.values():
            if a.case_id != case.case_id:
                continue
            if a.is_committed:
                needs.add(a.need_key)
                teams.add(a.responsible_team_id or a.team_id)
        return needs, teams

    def _assignment_from_candidate(
        self, case: Case, cand: Candidate, now: datetime, need_key: str
    ) -> Assignment:
        arrive = now + timedelta(minutes=cand.eta_minutes)
        end = arrive + timedelta(
            minutes=SERVICE_DURATION_MINUTES.get(cand.service_type, 45)
        )
        asg = Assignment(
            assignment_id=self.repo.next_id("asg"),
            case_id=case.case_id,
            team_id=cand.team.team_id,
            org_id=cand.team.org_id,
            service_type=cand.service_type,
            need_key=need_key,
            status=AssignmentStatus.OFFERED.value,
            created_at=now,
            offered_at=now,
            offer_expires_at=now + timedelta(minutes=OFFER_TTL_MINUTES),
            plan_reason=cand.reason,
            route_nodes=list(cand.route.nodes),
            route_distance_km=cand.route.distance_km,
            eta_minutes=cand.eta_minutes,
            required_from=arrive,
            required_to=end,
            station_id=cand.station.station_id if cand.station else None,
            wait_minutes=cand.wait_minutes,
        )
        # 拖车方案：有空位时立即预约快充位；已满则到场排队（等待已计入评分）
        if cand.station is not None:
            if cand.station.free_ports > 0:
                try:
                    self.gateway.reserve(cand.station)
                    asg.station_reserved = True
                except Exception:
                    asg.station_reserved = False
            else:
                asg.station_reserved = False
        return asg

    def _revoke_uncommitted(
        self, case: Case, now: datetime, reason: str
    ) -> list[Assignment]:
        """撤销所有未终结且未承诺的派单，并释放其占有的外部资源。"""
        revoked: list[Assignment] = []
        for a in list(self.repo.assignments.values()):
            if a.case_id != case.case_id or a.is_terminal or a.is_committed:
                continue
            a.status = AssignmentStatus.REVOKED.value
            a.revoked_at = now
            a.plan_reason = f"{a.plan_reason} | 撤销原因: {reason}"
            self._release_assignment_resources(a, now, reason=reason)
            revoked.append(a)
        return revoked

    def _release_assignment_resources(
        self, assignment: Assignment, now: datetime, reason: str
    ) -> None:
        if assignment.station_id and assignment.station_reserved:
            station = self.repo.stations.get(assignment.station_id)
            if station is not None:
                try:
                    self.gateway.release_reservation(station)
                except Exception as exc:  # 释放失败：进补偿台账，重启后继续
                    self.ledger.register(
                        "release_station_reservation",
                        assignment.assignment_id,
                        {"station_id": assignment.station_id},
                        now,
                        reason=f"{reason}: {exc}",
                    )
            assignment.station_reserved = False

    def _replan_uncommitted(
        self, case: Case, now: datetime, reason: str
    ) -> DispatchPlan:
        # 先撤销未承诺派单（释放旧的充电位预约等）
        self._revoke_uncommitted(case, now, reason)
        covered_needs, busy_teams = self._committed_needs(case)
        # 其他工单正在占用的队伍也不可再派
        for t in self.repo.teams.values():
            if t.is_busy and t.current_case_id != case.case_id:
                busy_teams.add(t.team_id)
        plan = self.dispatcher.plan(
            case, now, exclude_needs=covered_needs, exclude_teams=busy_teams
        )
        for slot in plan.slots:
            if slot.chosen is None:
                continue
            asg = self._assignment_from_candidate(
                case, slot.chosen, now, slot.requirement.need_key
            )
            self.repo.assignments[asg.assignment_id] = asg
        self._derive_case_status(case)
        return plan

    def _derive_case_status(self, case: Case) -> None:
        if case.is_terminal():
            return
        active = [
            a for a in self.repo.assignments.values()
            if a.case_id == case.case_id and not a.is_terminal
        ]
        committed = [a for a in active if a.is_committed]
        offered = [
            a for a in active if a.status == AssignmentStatus.OFFERED.value
        ]
        if case.status == "resolved":
            return
        if committed and not offered:
            case.status = "in_progress"
        elif committed and offered:
            case.status = "partially_committed"
        else:
            case.status = "open"

    # ============================================================
    # 定位更新 / 道路封闭：只调整尚未承诺的部分
    # ============================================================
    def update_location(
        self, case_id: str, to_node: str, reason: str,
        at: Optional[datetime] = None,
    ) -> DispatchPlan:
        now = at or self.clock.now()
        with self.repo.lock:
            case = self._get_case(case_id)
            moved = case.update_location(now, to_node, reason)
            if moved:
                plan = self._replan_uncommitted(case, now, reason="location_update")
                self._commit()
                return plan
            self._commit()
            return self.dispatcher.plan(case, now)

    def close_road(
        self, segment_id: str, reason: str, at: Optional[datetime] = None
    ) -> list[str]:
        """封闭道路并重规划所有受影响但尚未承诺的工单。返回重规划工单 id。"""
        now = at or self.clock.now()
        with self.repo.lock:
            if segment_id not in self.repo.network.segments:
                raise NotFoundError(f"segment {segment_id} 不存在")
            self.repo.network.close_segment(segment_id, now.isoformat(), reason)
            affected: list[str] = []
            for case in list(self.repo.cases.values()):
                if case.is_terminal():
                    continue
                active = [
                    a for a in self.repo.assignments.values()
                    if a.case_id == case.case_id and not a.is_terminal
                ]
                # 承诺过的派单即使路径受影响也不撤销（队伍已在途），
                # 仅当仍存在未承诺部分时才重规划
                if any(not a.is_committed for a in active):
                    self._replan_uncommitted(case, now, reason=f"road_closed:{reason}")
                    affected.append(case.case_id)
            self._commit()
            return affected

    # ============================================================
    # 抢单
    # ============================================================
    def claim(
        self,
        assignment_id: str,
        actor_team_id: str,
        at: Optional[datetime] = None,
    ) -> Assignment:
        now = at or self.clock.now()
        with self.repo.lock:
            asg = self._get_assignment(assignment_id)
            case = self._get_case(asg.case_id)
            if asg.status == AssignmentStatus.CLAIMED.value:
                raise AlreadyClaimedError(
                    f"{assignment_id} 已被 {asg.responsible_team_id} 抢单"
                )
            if asg.status != AssignmentStatus.OFFERED.value:
                raise ConflictError(
                    f"派单状态 {asg.status} 不可抢单"
                )
            if asg.offer_expires_at and now > asg.offer_expires_at:
                raise ConflictError("派单报价已过期，请等待重新派单")
            actor = self.repo.teams.get(actor_team_id)
            if actor is None:
                raise NotFoundError(f"救援队 {actor_team_id} 未登记")
            # 非定向抢单：其他具备能力且值班可达的队伍也可以抢先承诺
            if actor_team_id != asg.team_id:
                self._assert_can_serve(actor, asg, case, now)
                asg.team_id = actor.team_id
                asg.org_id = actor.org_id
                asg.responsible_team_id = actor.team_id
                asg.responsible_org_id = actor.org_id
            asg.status = AssignmentStatus.CLAIMED.value
            asg.claimed_at = now
            actor.current_case_id = case.case_id
            case.mark_committed(now)
            self._derive_case_status(case)
            self._commit()
            return asg

    def _assert_can_serve(self, team, asg: Assignment, case: Case, now: datetime):
        required = SERVICE_CAPS.get(asg.service_type, set())
        if not team.has_capability(required):
            raise ConflictError(f"{team.name} 缺少 {required} 能力")
        if team.is_busy:
            raise ConflictError(f"{team.name} 正在执行其他任务")
        route = self.repo.network.shortest_route(team.base_node, case.location_node)
        if not route.reachable:
            raise ConflictError(f"{team.name} 当前无法到达求援位置")
        arrive = now + timedelta(
            minutes=route.distance_km / 70.0 * 60
        )
        end = arrive + timedelta(
            minutes=SERVICE_DURATION_MINUTES.get(asg.service_type, 45)
        )
        if team.duty_window_covering(arrive, end) is None:
            raise ConflictError(f"{team.name} 值班窗口无法覆盖处置区间")

    # ============================================================
    # 跨机构转派：责任交接不得悬空
    # ============================================================
    def propose_transfer(
        self,
        assignment_id: str,
        to_team_id: str,
        reason: str,
        situational_summary: str = "",
        at: Optional[datetime] = None,
    ) -> Handover:
        now = at or self.clock.now()
        with self.repo.lock:
            asg = self._get_assignment(assignment_id)
            case = self._get_case(asg.case_id)
            if asg.status not in (
                AssignmentStatus.CLAIMED.value,
                AssignmentStatus.ARRIVED.value,
            ):
                raise TransferRejectedError(
                    "只有已承诺（抢单/到场）的派单可以跨机构转派"
                )
            # 已存在未完结交接时不得再次发起（责任不得悬空）
            for hid in asg.handover_ids:
                if self.repo.handovers[hid].state == HandoverState.PROPOSED.value:
                    raise TransferRejectedError("该派单已有进行中的交接")
            target = self.repo.teams.get(to_team_id)
            if target is None:
                raise NotFoundError(f"救援队 {to_team_id} 未登记")
            if target.org_id == asg.responsible_org_id:
                raise TransferRejectedError("接收方与当前责任方属同一机构")
            self._assert_can_serve(target, asg, case, now)
            handover = Handover(
                handover_id=self.repo.next_id("ho"),
                assignment_id=asg.assignment_id,
                case_id=case.case_id,
                from_team_id=asg.responsible_team_id,
                from_org_id=asg.responsible_org_id,
                to_team_id=target.team_id,
                to_org_id=target.org_id,
                proposed_at=now,
                reason=reason,
                situational_summary=situational_summary,
                # 交接序号按工单全局连续，便于完整回溯
                chain_index=sum(
                    1 for h in self.repo.handovers.values()
                    if h.case_id == case.case_id
                ),
            )
            self.repo.handovers[handover.handover_id] = handover
            asg.handover_ids.append(handover.handover_id)
            self._commit()
            return handover

    def acknowledge_transfer(
        self,
        handover_id: str,
        actor_team_id: str,
        at: Optional[datetime] = None,
    ) -> Assignment:
        now = at or self.clock.now()
        with self.repo.lock:
            ho = self.repo.handovers.get(handover_id)
            if ho is None:
                raise NotFoundError(f"交接 {handover_id} 不存在")
            if actor_team_id != ho.to_team_id:
                raise TransferRejectedError("只有接收方可以确认交接")
            if ho.state != HandoverState.PROPOSED.value:
                raise TransferRejectedError(f"交接状态 {ho.state} 不可确认")
            asg = self._get_assignment(ho.assignment_id)
            old_team = self.repo.teams.get(ho.from_team_id)
            new_team = self.repo.teams[ho.to_team_id]
            # 新责任方先落位，再解除旧责任方：责任不出现空窗
            new_team.current_case_id = ho.case_id
            ho.acknowledge(now, actor_team_id)
            asg.responsible_team_id = ho.to_team_id
            asg.responsible_org_id = ho.to_org_id
            if old_team is not None and old_team.current_case_id == ho.case_id:
                old_team.current_case_id = None
            # 若转派时旧派单尚未到场，旧派单标记 TRANSFERRED 并产生一张
            # 新的、接收方名下的派单，保持链路完整
            if asg.status == AssignmentStatus.CLAIMED.value:
                asg.status = AssignmentStatus.TRANSFERRED.value
                new_asg = Assignment(
                    assignment_id=self.repo.next_id("asg"),
                    case_id=asg.case_id,
                    team_id=ho.to_team_id,
                    org_id=ho.to_org_id,
                    service_type=asg.service_type,
                    need_key=asg.need_key,
                    status=AssignmentStatus.CLAIMED.value,
                    created_at=now,
                    claimed_at=now,
                    plan_reason=f"承接转派自 {ho.from_team_id}: {ho.reason}",
                    route_nodes=list(asg.route_nodes),
                    route_distance_km=asg.route_distance_km,
                    eta_minutes=asg.eta_minutes,
                    station_id=asg.station_id,
                    station_reserved=asg.station_reserved,
                    wait_minutes=asg.wait_minutes,
                    parent_assignment_id=asg.assignment_id,
                    chain_index=ho.chain_index,
                )
                # 充电位预约随责任一并移交，不释放不重复占位
                asg.station_reserved = False
                self.repo.assignments[new_asg.assignment_id] = new_asg
                self._commit()
                return new_asg
            self._commit()
            return asg

    def reject_transfer(
        self, handover_id: str, reason: str, actor_team_id: str,
        at: Optional[datetime] = None,
    ) -> Handover:
        now = at or self.clock.now()
        with self.repo.lock:
            ho = self.repo.handovers.get(handover_id)
            if ho is None:
                raise NotFoundError(f"交接 {handover_id} 不存在")
            if actor_team_id != ho.to_team_id:
                raise TransferRejectedError("只有接收方可以拒绝交接")
            if ho.state != HandoverState.PROPOSED.value:
                raise TransferRejectedError(f"交接状态 {ho.state} 不可拒绝")
            ho.reject(now, reason)
            self._commit()
            return ho

    def handover_chain(self, case_id: str) -> list[Handover]:
        with self.repo.lock:
            self._get_case(case_id)
            return [
                h for h in self.repo.handovers.values()
                if h.case_id == case_id
            ]

    # ============================================================
    # 到场 / 补能 / 关闭
    # ============================================================
    def arrive(
        self, assignment_id: str, at: Optional[datetime] = None,
        note: str = "",
    ) -> Assignment:
        now = at or self.clock.now()
        with self.repo.lock:
            asg = self._get_assignment(assignment_id)
            if asg.status != AssignmentStatus.CLAIMED.value:
                raise ConflictError(f"派单状态 {asg.status} 不可到场")
            asg.status = AssignmentStatus.ARRIVED.value
            asg.arrived_at = now
            if note:
                asg.plan_reason = f"{asg.plan_reason} | 到场备注: {note}"
            self._commit()
            return asg

    def energize(
        self,
        assignment_id: str,
        action: str = "mobile_recharge",
        kwh_delivered: float = 0.0,
        note: str = "",
        at: Optional[datetime] = None,
    ) -> Assignment:
        now = at or self.clock.now()
        with self.repo.lock:
            asg = self._get_assignment(assignment_id)
            case = self._get_case(asg.case_id)
            if asg.status not in (
                AssignmentStatus.ARRIVED.value,
                AssignmentStatus.CLAIMED.value,
            ):
                raise ConflictError(f"派单状态 {asg.status} 不可补能/排障")
            if action == "station_recharge":
                if not asg.station_id:
                    raise ConflictError("该派单没有关联的服务区充电站")
                station = self.repo.stations[asg.station_id]
                if asg.station_reserved:
                    # 预约车辆：预约转占用，充电完成后释放
                    self.gateway.confirm_occupied(station)
                    asg.station_reserved = False
                    self.gateway.release_occupied(station)
                # 已满场排队的车辆在社会车位流转中完成充电，不改动社会占用计数
            asg.fulfillment = EnergyFulfillment(
                action=action, at=now, kwh_delivered=kwh_delivered,
                station_id=asg.station_id,
                by_team_id=asg.responsible_team_id, note=note,
            )
            asg.status = AssignmentStatus.ENERGIZED.value
            asg.energized_at = now
            self._maybe_resolve(case)
            self._commit()
            return asg

    def _maybe_resolve(self, case: Case) -> None:
        """全部必需需求都已补能/排障完成 → resolved。"""
        required_needs = {
            r.need_key for r in self.dispatcher.requirements_for(case)
        }
        done_needs = {
            a.need_key for a in self.repo.assignments.values()
            if a.case_id == case.case_id
            and a.status in (
                AssignmentStatus.ENERGIZED.value,
                AssignmentStatus.COMPLETED.value,
            )
        }
        if required_needs <= done_needs:
            case.status = "resolved"

    def close_case(
        self,
        case_id: str,
        resolution_note: str = "",
        at: Optional[datetime] = None,
    ) -> Case:
        now = at or self.clock.now()
        with self.repo.lock:
            case = self._get_case(case_id)
            if case.is_terminal():
                raise ConflictError(f"工单已 {case.status}")
            active = [
                a for a in self.repo.assignments.values()
                if a.case_id == case_id and not a.is_terminal
            ]
            required_needs = {
                r.need_key for r in self.dispatcher.requirements_for(case)
            }
            served_needs = {
                a.need_key for a in active
                if a.status in (
                    AssignmentStatus.ARRIVED.value,
                    AssignmentStatus.ENERGIZED.value,
                    AssignmentStatus.COMPLETED.value,
                )
            }
            if not required_needs <= served_needs:
                missing = required_needs - served_needs
                raise ConflictError(
                    f"尚有需求 {sorted(missing)} 未处置完成，不能关单"
                )
            for a in active:
                if a.status != AssignmentStatus.COMPLETED.value:
                    a.status = AssignmentStatus.COMPLETED.value
                    a.completed_at = now
                team = self.repo.teams.get(a.responsible_team_id or "")
                if team is not None and team.current_case_id == case_id:
                    team.current_case_id = None
            case.status = "closed"
            case.closed_at = now
            if resolution_note:
                case.calls[-1].note = (
                    f"{case.calls[-1].note} | 关单: {resolution_note}"
                ).strip(" |")
            self._commit()
            return case

    # ============================================================
    # 超时与后台驱动（重启后继续升级）
    # ============================================================
    def run_due(self, now: Optional[datetime] = None) -> dict:
        now = now or self.clock.now()
        with self.repo.lock:
            # 1) 补偿重试
            compensated = self.ledger.retry_all(
                self.gateway, now, self.repo.stations
            )
            # 2) 过期报价作废并重派（含部分承诺工单里尚未承诺的剩余需求；
            #    已承诺派单由 exclude_needs 保留，不受影响）
            replanned: list[str] = []
            for case in list(self.repo.cases.values()):
                if case.is_terminal():
                    continue
                expired = [
                    a for a in self.repo.assignments.values()
                    if a.case_id == case.case_id
                    and a.status == AssignmentStatus.OFFERED.value
                    and a.offer_expires_at and now > a.offer_expires_at
                ]
                if expired:
                    for a in expired:
                        # 过期未抢单：释放其预约的外部资源（失败进补偿台账）
                        self._release_assignment_resources(
                            a, now, reason="offer_expired"
                        )
                        a.status = AssignmentStatus.EXPIRED.value
                    self._replan_uncommitted(
                        case, now, reason="offer_expired"
                    )
                    replanned.append(case.case_id)
            # 3) 超时升级
            escalated: list[dict] = []
            for case in list(self.repo.cases.values()):
                if case.is_terminal():
                    continue
                es = case.escalate_due(now)
                for e in es:
                    escalated.append(
                        {"case_id": case.case_id, "level": e.level,
                         "priority_after": e.priority_after}
                    )
            self._commit()
            return {
                "compensated": [c["comp_id"] for c in compensated],
                "replanned": replanned,
                "escalated": escalated,
            }

    def plan_case(self, case_id: str, at: Optional[datetime] = None) -> DispatchPlan:
        """只读：给出当前未承诺需求的规划（不动既有派单）。"""
        now = at or self.clock.now()
        with self.repo.lock:
            case = self._get_case(case_id)
            covered_needs, busy_teams = self._committed_needs(case)
            for t in self.repo.teams.values():
                if t.is_busy and t.current_case_id != case.case_id:
                    busy_teams.add(t.team_id)
            return self.dispatcher.plan(
                case, now, exclude_needs=covered_needs,
                exclude_teams=busy_teams,
            )

    # ============================================================
    # 查询与隐私视图
    # ============================================================
    def get_case(self, case_id: str) -> Case:
        with self.repo.lock:
            return self._get_case(case_id)

    def _get_case(self, case_id: str) -> Case:
        case = self.repo.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"工单 {case_id} 不存在")
        return case

    def _get_assignment(self, assignment_id: str) -> Assignment:
        asg = self.repo.assignments.get(assignment_id)
        if asg is None:
            raise NotFoundError(f"派单 {assignment_id} 不存在")
        return asg

    def case_view(
        self, case_id: str, scopes: set[PrivacyField] | frozenset[str]
    ) -> dict:
        """按授权 scope 输出视图；未授权的隐私字段一律脱敏。"""
        with self.repo.lock:
            case = self._get_case(case_id)
            scope_set = {
                s if isinstance(s, PrivacyField) else PrivacyField(s)
                for s in scopes
            }
            return self._case_dict_safe(case, scope_set)

    def _case_dict_safe(self, case: Case, scopes: set[PrivacyField]) -> dict:
        can_phone = PrivacyField.PHONE in scopes
        can_plate = PrivacyField.PLATE in scopes
        can_loc = PrivacyField.EXACT_LOCATION in scopes
        can_occ = PrivacyField.OCCUPANT_DETAIL in scopes
        return {
            "case_id": case.case_id,
            "emergency_type": case.emergency_type,
            "status": case.status,
            "priority": case.priority,
            "location_node": case.location_node if can_loc else _mask(case.location_node),
            "battery_pct": case.battery_pct,
            "plate": case.plate if (can_plate and case.plate) else _mask(case.plate),
            "phone": case.phone if (can_phone and case.phone) else _mask(case.phone),
            "risk_level": case.risk.effective_level.value,
            "occupants": case.risk.occupants if can_occ else None,
            "risk_flags": (
                {
                    "injured": case.risk.injured,
                    "vulnerable": case.risk.vulnerable,
                    "in_driving_lane": case.risk.in_driving_lane,
                    "fire_or_smoke": case.risk.fire_or_smoke,
                    "severe_weather_exposure": case.risk.severe_weather_exposure,
                }
                if can_occ else None
            ),
            "consent_scopes": sorted(s.value for s in case.consent_scopes),
            "call_count": len(case.calls),
            "highest_risk_call_id": case.highest_risk_call_id,
            "escalation_level": case.escalation_level,
        }

    def assert_scope(self, case: Case, field: PrivacyField) -> None:
        """供写操作/外呼使用：未授权即拒绝，而不是静默返回。"""
        if field not in case.consent_scopes:
            raise PrivacyDeniedError(
                f"未获得 {field.value} 授权，禁止访问该隐私字段"
            )


def _mask(value) -> str:
    if value is None:
        return "***"
    s = str(value)
    if len(s) <= 2:
        return "***"
    # 不保留首尾字符，避免车牌省份、号码尾号等信息泄露
    return "*" * min(len(s), 8)
