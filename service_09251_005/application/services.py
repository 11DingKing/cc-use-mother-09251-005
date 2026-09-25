"""应用服务：受理、派单、抢单、转派、到场、补能、关闭与超时升级。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from service_09251_005.application.errors import (
    CapacityError,
    ClaimConflictError,
    CompensationFailedError,
    ConflictError,
    NotFoundError,
    TransferFailedError,
    ValidationError,
)
from service_09251_005.application.ports import Clock, IdGenerator, Notifier
from service_09251_005.domain.models import (
    ADVICE_GUIDE_PREFIX,
    ADVICE_MOBILE_CHARGE,
    ADVICE_NO_SITE,
    DIRECTION_BOTH,
    REQUEST_CLOSED,
    REQUEST_OPEN,
    Capability,
    DispatchOrder,
    DutyWindow,
    HandoffRecord,
    IncidentType,
    Location,
    OrderStatus,
    RescueRequest,
    RescueTeam,
    RiskLevel,
    RoadClosure,
    ChargingSite,
    UNCOMMITTED_STATUSES,
    compute_priority,
    iso,
    sla_for_priority,
    team_capable,
)


@dataclass(frozen=True)
class DispatchConfig:
    merge_window: timedelta = timedelta(minutes=30)
    merge_radius_km: float = 1.0
    km_per_battery_pct: float = 4.0
    escalation_backoff: timedelta = timedelta(minutes=5)
    max_candidates: int = 5


class CapacityGateway:
    """运力占用端口：默认落在仓储的原子更新上，测试可注入失败。"""

    def __init__(self, repo) -> None:
        self._repo = repo

    def reserve(self, team_id: str) -> bool:
        return self._repo.try_reserve_team(team_id)

    def release(self, team_id: str) -> None:
        self._repo.release_team(team_id)


def _validate_report(
    reporter_name: str,
    reporter_phone: str,
    location: Location,
    battery_pct: float,
    occupants: int,
) -> None:
    if not reporter_name.strip():
        raise ValidationError("reporter_name required")
    if not reporter_phone.strip():
        raise ValidationError("reporter_phone required")
    if not location.road_id.strip() or not location.direction.strip():
        raise ValidationError("location road_id/direction required")
    if location.mile_marker_km < 0:
        raise ValidationError("mile_marker_km must be >= 0")
    if not 0.0 <= battery_pct <= 100.0:
        raise ValidationError("battery_pct must be within 0..100")
    if occupants < 1:
        raise ValidationError("occupants must be >= 1")


class IntakeService:
    """受理求援：重复呼叫合并，但绝不吞掉更高风险信息。"""

    def __init__(self, repo, clock: Clock, idgen: IdGenerator, dispatch: "DispatchService",
                 notifier: Notifier, cfg: DispatchConfig) -> None:
        self._repo = repo
        self._clock = clock
        self._idgen = idgen
        self._dispatch = dispatch
        self._notifier = notifier
        self._cfg = cfg

    def register_report(
        self,
        *,
        reporter_name: str,
        reporter_phone: str,
        location: Location,
        incident_type: IncidentType,
        battery_pct: float,
        risk: RiskLevel,
        occupants: int = 1,
    ) -> tuple[RescueRequest, DispatchOrder | None, bool]:
        _validate_report(reporter_name, reporter_phone, location, battery_pct, occupants)
        now = self._clock.now()
        duplicate = self._find_duplicate(reporter_phone, location, incident_type, now)
        if duplicate is not None:
            order = self._merge_into(duplicate, now=now, reporter_phone=reporter_phone,
                                     battery_pct=battery_pct, risk=risk, occupants=occupants,
                                     location=location, incident_type=incident_type)
            return duplicate, order, True

        request = RescueRequest(
            request_id=self._idgen.new_id("REQ"),
            reporter_name=reporter_name.strip(),
            reporter_phone=reporter_phone.strip(),
            location=location,
            incident_type=incident_type,
            battery_pct=battery_pct,
            risk=risk,
            occupants=occupants,
            created_at=now,
            priority=compute_priority(risk, battery_pct, incident_type, occupants),
        )
        self._repo.save_request(request)
        order = self._dispatch.dispatch_new(request)
        self._repo.record_event("REQUEST_ACCEPTED", {
            "request_id": request.request_id, "priority": request.priority, "at": iso(now),
        })
        return request, order, False

    def _find_duplicate(
        self, phone: str, location: Location, incident_type: IncidentType, now: datetime
    ) -> RescueRequest | None:
        phone_key = "".join(ch for ch in phone if ch.isdigit())
        candidates = self._repo.find_merge_candidates(phone_key, location, self._cfg.merge_radius_km)
        pool = []
        for c in candidates:
            if now - c.created_at > self._cfg.merge_window:
                continue
            known_phones = {"".join(ch for ch in p if ch.isdigit())
                            for p in [c.reporter_phone, *c.extra_phones]}
            same_phone = phone_key in known_phones
            same_place = (
                c.location.road_id == location.road_id
                and c.location.direction == location.direction
                and abs(c.location.mile_marker_km - location.mile_marker_km)
                <= self._cfg.merge_radius_km
                and c.incident_type is incident_type
            )
            if same_phone or same_place:
                pool.append(c)
        if not pool:
            return None
        # 并入风险最高、其次最早创建的主单，避免高风险信息被低风险单吞掉
        return max(pool, key=lambda c: (int(c.risk), -c.created_at.timestamp()))

    def _merge_into(self, target: RescueRequest, *, now: datetime, reporter_phone: str,
                    battery_pct: float, risk: RiskLevel, occupants: int,
                    location: Location, incident_type: IncidentType) -> DispatchOrder | None:
        report_id = self._idgen.new_id("RPT")
        old_priority = target.priority
        target.risk = RiskLevel(max(int(target.risk), int(risk)))
        target.battery_pct = min(target.battery_pct, battery_pct)
        target.occupants = max(target.occupants, occupants)
        target.merged_report_ids.append(report_id)
        phone = reporter_phone.strip()
        if phone != target.reporter_phone and phone not in target.extra_phones:
            target.extra_phones.append(phone)
        target.priority = compute_priority(target.risk, target.battery_pct,
                                           target.incident_type, target.occupants)
        self._repo.save_request(target)
        # 完整保留来话内容，高风险信息有据可查
        self._repo.record_event("REQUEST_MERGED", {
            "request_id": target.request_id,
            "merged_report_id": report_id,
            "at": iso(now),
            "incoming": {
                "phone": phone,
                "risk": int(risk),
                "battery_pct": battery_pct,
                "occupants": occupants,
                "incident_type": incident_type.value,
                "location": location.to_dict(),
            },
        })
        order = self._repo.open_order_for_request(target.request_id)
        if target.priority > old_priority and order is not None:
            if order.committed:
                # 已承诺的单不改动派单，但必须提示值班员风险已升级
                self._notifier.notify("RISK_UPGRADE_ON_COMMITTED", {
                    "order_id": order.order_id,
                    "request_id": target.request_id,
                    "priority": target.priority,
                    "at": iso(now),
                })
            else:
                order = self._dispatch.replan_request(target.request_id, reason="RISK_UPGRADE")
        return order


class DispatchService:
    """派单引擎：按道路方向、补能机会、值班窗口与优先级计算候选。"""

    def __init__(self, repo, clock: Clock, idgen: IdGenerator, notifier: Notifier,
                 cfg: DispatchConfig) -> None:
        self._repo = repo
        self._clock = clock
        self._idgen = idgen
        self._notifier = notifier
        self._cfg = cfg

    # ---- 派单与重派 ----

    def dispatch_new(self, request: RescueRequest) -> DispatchOrder:
        now = self._clock.now()
        candidates, advice = self._rank(request)
        sla = sla_for_priority(request.priority)
        order = DispatchOrder(
            order_id=self._idgen.new_id("ORD"),
            request_id=request.request_id,
            status=OrderStatus.OFFERED if candidates else OrderStatus.PENDING,
            priority=request.priority,
            created_at=now,
            claim_deadline=now + sla,
            next_escalation_at=now + sla,
            candidate_team_ids=candidates,
            energy_advice=advice,
        )
        self._repo.save_order(order)
        self._repo.record_event("ORDER_CREATED", {
            "order_id": order.order_id, "request_id": request.request_id,
            "status": order.status.value, "candidates": candidates,
            "energy_advice": advice, "at": iso(now),
        })
        return order

    def replan_request(self, request_id: str, reason: str) -> DispatchOrder | None:
        """只调整尚未承诺的派单；已承诺（已抢单）的派单一律不动。"""
        request = self._repo.get_request(request_id)
        if request is None or request.status != REQUEST_OPEN:
            return None
        order = self._repo.open_order_for_request(request_id)
        if order is None or order.committed or order.status not in UNCOMMITTED_STATUSES:
            return None
        candidates, advice = self._rank(request)
        order.candidate_team_ids = candidates
        order.energy_advice = advice
        order.priority = request.priority
        order.status = OrderStatus.OFFERED if candidates else OrderStatus.PENDING
        order.version += 1
        self._repo.save_order(order)
        self._repo.record_event("ORDER_REPLANNED", {
            "order_id": order.order_id, "reason": reason,
            "candidates": candidates, "energy_advice": advice,
            "at": iso(self._clock.now()),
        })
        return order

    def replan_road(self, road_id: str, reason: str) -> list[str]:
        changed = []
        for order in self._repo.uncommitted_orders_on_road(road_id):
            if self.replan_request(order.request_id, reason=reason) is not None:
                changed.append(order.order_id)
        return changed

    # ---- 超时升级（重启后继续） ----

    def sweep(self) -> int:
        """扫描到点未承诺的派单并升级；返回本次升级的条数。"""
        now = self._clock.now()
        count = 0
        for order in self._repo.due_escalations(iso(now)):
            request = self._repo.get_request(order.request_id)
            if request is None:
                continue
            order.escalation_level += 1
            candidates, advice = self._rank_relaxed(request)
            if candidates:
                order.candidate_team_ids = candidates
                order.status = OrderStatus.OFFERED
            if advice:
                order.energy_advice = advice
            order.next_escalation_at = now + self._cfg.escalation_backoff * order.escalation_level
            order.version += 1
            self._repo.save_order(order)
            payload = {
                "order_id": order.order_id,
                "request_id": order.request_id,
                "escalation_level": order.escalation_level,
                "priority": order.priority,
                "at": iso(now),
            }
            self._notifier.notify("ORDER_ESCALATED", payload)
            self._repo.record_event("ORDER_ESCALATED", payload)
            count += 1
        return count

    def recover(self) -> int:
        """服务重启后调用：升级进度持久化在库中，直接继续扫描升级。"""
        return self.sweep()

    # ---- 候选计算 ----

    def _rank(self, request: RescueRequest) -> tuple[list[str], str | None]:
        now = self._clock.now()
        loc = request.location
        closures = self._repo.active_closures(loc.road_id)
        advice = self._energy_advice(request)
        scored: list[tuple[float, str]] = []
        for team in self._repo.list_teams():
            if not team.on_duty(now) or not team.has_capacity():
                continue
            if not team.covers_location(loc):
                continue
            if not team_capable(team, request.incident_type, request.risk):
                continue
            if not self._reachable(team, loc, closures):
                continue
            score = 100.0 - min(abs(team.staging_km - loc.mile_marker_km), 100.0)
            if advice == ADVICE_MOBILE_CHARGE and Capability.MOBILE_CHARGE in team.capabilities:
                score += 15.0
            if request.risk >= RiskLevel.HIGH and Capability.MEDICAL in team.capabilities:
                score += 10.0
            scored.append((score, team.team_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tid for _, tid in scored[: self._cfg.max_candidates]], advice

    def _rank_relaxed(self, request: RescueRequest) -> tuple[list[str], str | None]:
        """升级时放宽道路归属限制，只保留能力、值班与运力约束。"""
        now = self._clock.now()
        advice = self._energy_advice(request)
        scored: list[tuple[float, str]] = []
        for team in self._repo.list_teams():
            if not team.on_duty(now) or not team.has_capacity():
                continue
            if not team_capable(team, request.incident_type, request.risk):
                continue
            score = 50.0
            if team.covers_location(request.location):
                score += 30.0
            if advice == ADVICE_MOBILE_CHARGE and Capability.MOBILE_CHARGE in team.capabilities:
                score += 15.0
            scored.append((score, team.team_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tid for _, tid in scored[: self._cfg.max_candidates]], advice

    def _energy_advice(self, request: RescueRequest) -> str | None:
        """低电量趴窝时联动服务区充电拥堵：可达站点全拥堵则偏好移动充电。"""
        if request.incident_type is not IncidentType.LOW_BATTERY:
            return None
        loc = request.location
        reach_km = request.battery_pct * self._cfg.km_per_battery_pct
        sites = [
            s for s in self._repo.list_sites(loc.road_id)
            if s.direction == loc.direction
            and abs(s.km - loc.mile_marker_km) <= reach_km
        ]
        if not sites:
            return ADVICE_NO_SITE
        open_sites = [s for s in sites if not s.congested]
        if open_sites:
            nearest = min(open_sites, key=lambda s: abs(s.km - loc.mile_marker_km))
            return f"{ADVICE_GUIDE_PREFIX}{nearest.site_id}"
        return ADVICE_MOBILE_CHARGE

    @staticmethod
    def _reachable(team: RescueTeam, loc: Location, closures: list[RoadClosure]) -> bool:
        """队伍驻点到求援点的路径若穿过封闭区间，则不可达。"""
        lo, hi = sorted((team.staging_km, loc.mile_marker_km))
        for closure in closures:
            if closure.road_id != loc.road_id:
                continue
            if closure.direction not in (DIRECTION_BOTH, loc.direction):
                continue
            if lo <= closure.to_km and hi >= closure.from_km:
                return False
        return True


class OperationService:
    """派单生命周期操作：抢单、转派、到场、补能、关闭。"""

    def __init__(self, repo, clock: Clock, idgen: IdGenerator, notifier: Notifier,
                 capacity: CapacityGateway, dispatch: DispatchService) -> None:
        self._repo = repo
        self._clock = clock
        self._idgen = idgen
        self._notifier = notifier
        self.capacity = capacity
        self._dispatch = dispatch

    # ---- 抢单 ----

    def claim(self, order_id: str, team_id: str) -> DispatchOrder:
        order = self._require_order(order_id)
        team = self._require_team(team_id)
        request = self._require_request(order.request_id)
        if order.status not in UNCOMMITTED_STATUSES:
            raise ClaimConflictError("order already taken", order_id=order_id)
        if not team_capable(team, request.incident_type, request.risk):
            raise ValidationError("team capability does not cover incident")
        if not team.on_duty(self._clock.now()):
            raise ValidationError("team is off duty")
        if order.escalation_level == 0 and team_id not in order.candidate_team_ids:
            raise ValidationError("team is not a candidate for this order")
        if not self.capacity.reserve(team_id):
            raise CapacityError("team has no free capacity", team_id=team_id)

        order.status = OrderStatus.CLAIMED
        order.assigned_team_id = team_id
        order.committed = True
        order.version += 1
        if not self._repo.try_claim_order(order):
            # 抢单失败必须补偿已占用的运力；补偿失败要落台账
            self._safe_release(team_id, order, context="CLAIM_COMPENSATION")
            raise ClaimConflictError("lost claim race", order_id=order_id)
        self._repo.record_event("ORDER_CLAIMED", {
            "order_id": order_id, "team_id": team_id, "at": iso(self._clock.now()),
        })
        return order

    # ---- 转派（跨机构保留责任交接） ----

    def transfer(self, order_id: str, to_team_id: str, reason: str,
                 operator: str = "system") -> DispatchOrder:
        order = self._require_order(order_id)
        if order.status is not OrderStatus.CLAIMED or order.assigned_team_id is None:
            raise ConflictError("only claimed orders can be transferred")
        to_team = self._require_team(to_team_id)
        request = self._require_request(order.request_id)
        from_team_id = order.assigned_team_id
        from_team = self._require_team(from_team_id)
        if to_team_id == from_team_id:
            raise ValidationError("cannot transfer to the same team")
        if not reason.strip():
            raise ValidationError("transfer reason required")
        if not team_capable(to_team, request.incident_type, request.risk):
            raise ValidationError("target team capability does not cover incident")
        if not to_team.on_duty(self._clock.now()):
            raise ValidationError("target team is off duty")
        if not self.capacity.reserve(to_team_id):
            raise CapacityError("target team has no free capacity", team_id=to_team_id)

        now = self._clock.now()
        order.assigned_team_id = to_team_id
        order.version += 1
        self._repo.save_order(order)
        self._repo.add_handoff(HandoffRecord(
            handoff_id=self._idgen.new_id("HND"),
            order_id=order_id,
            from_agency=from_team.agency_id,
            to_agency=to_team.agency_id,
            from_team=from_team_id,
            to_team=to_team_id,
            reason=reason.strip(),
            at=now,
            note=f"by:{operator}",
        ))
        try:
            self.capacity.release(from_team_id)
        except Exception as exc:  # noqa: BLE001 - 必须兜底并补偿
            self._compensate_transfer(order, from_team, to_team, reason, exc)
            raise TransferFailedError(
                "transfer rolled back after capacity release failure",
                order_id=order_id,
            ) from exc
        self._repo.record_event("ORDER_TRANSFERRED", {
            "order_id": order_id, "from_team": from_team_id, "to_team": to_team_id,
            "from_agency": from_team.agency_id, "to_agency": to_team.agency_id,
            "reason": reason.strip(), "at": iso(now),
        })
        return order

    def _compensate_transfer(self, order: DispatchOrder, from_team: RescueTeam,
                             to_team: RescueTeam, reason: str, cause: Exception) -> None:
        """回滚指派并释放新队运力；补偿失败则记录悬挂状态供人工处理。"""
        try:
            self.capacity.release(to_team.team_id)
            order.assigned_team_id = from_team.team_id
            order.version += 1
            self._repo.save_order(order)
            self._repo.add_handoff(HandoffRecord(
                handoff_id=self._idgen.new_id("HND"),
                order_id=order.order_id,
                from_agency=to_team.agency_id,
                to_agency=from_team.agency_id,
                from_team=to_team.team_id,
                to_team=from_team.team_id,
                reason=f"ROLLBACK:{reason.strip()}",
                at=self._clock.now(),
                note="compensation",
            ))
        except Exception as comp_exc:  # noqa: BLE001 - 补偿失败必须落台账
            self._repo.record_compensation_failure(order.order_id, {
                "stage": "TRANSFER_COMPENSATION",
                "assigned_team_id": order.assigned_team_id,
                "from_team": from_team.team_id,
                "to_team": to_team.team_id,
                "cause": repr(cause),
                "compensation_cause": repr(comp_exc),
                "at": iso(self._clock.now()),
            })
            raise CompensationFailedError(
                "transfer compensation failed; manual reconciliation required",
                order_id=order.order_id,
            ) from comp_exc

    # ---- 到场 / 补能 / 关闭 ----

    def arrive(self, order_id: str) -> DispatchOrder:
        order = self._require_order(order_id)
        order.status = OrderStatus.ARRIVED
        order.arrived_at = self._clock.now()
        order.version += 1
        if not self._repo.transition_order(order, (OrderStatus.CLAIMED,)):
            raise ConflictError("order is not in a claimable-to-arrive state")
        self._repo.record_event("ORDER_ARRIVED", {
            "order_id": order_id, "at": iso(order.arrived_at),
        })
        return order

    def recharge(self, order_id: str, site_id: str | None = None,
                 energy_kwh: float | None = None) -> DispatchOrder:
        order = self._require_order(order_id)
        if site_id is not None and self._repo.get_site(site_id) is None:
            raise NotFoundError("charging site not found", site_id=site_id)
        if energy_kwh is not None and energy_kwh <= 0:
            raise ValidationError("energy_kwh must be positive")
        order.status = OrderStatus.RECHARGING
        order.recharge = {
            "site_id": site_id,
            "energy_kwh": energy_kwh,
            "started_at": iso(self._clock.now()),
        }
        order.version += 1
        if not self._repo.transition_order(order, (OrderStatus.ARRIVED,)):
            raise ConflictError("order must be ARRIVED before recharging")
        self._repo.record_event("ORDER_RECHARGING", {
            "order_id": order_id, "site_id": site_id, "at": iso(self._clock.now()),
        })
        return order

    def close(self, order_id: str, resolution: str = "RESOLVED") -> DispatchOrder:
        order = self._require_order(order_id)
        now = self._clock.now()
        order.status = OrderStatus.CLOSED
        order.closed_at = now
        order.close_resolution = resolution
        order.version += 1
        if not self._repo.transition_order(
            order, (OrderStatus.CLAIMED, OrderStatus.ARRIVED, OrderStatus.RECHARGING)
        ):
            raise ConflictError("order cannot be closed from its current state")
        request = self._repo.get_request(order.request_id)
        if request is not None:
            request.status = REQUEST_CLOSED
            self._repo.save_request(request)
        if order.assigned_team_id is not None:
            try:
                self.capacity.release(order.assigned_team_id)
            except Exception as exc:  # noqa: BLE001
                self._repo.record_compensation_failure(order_id, {
                    "stage": "CLOSE_CAPACITY_RELEASE",
                    "assigned_team_id": order.assigned_team_id,
                    "cause": repr(exc),
                    "at": iso(now),
                })
                raise CompensationFailedError(
                    "order closed but capacity release failed; manual reconciliation required",
                    order_id=order_id,
                ) from exc
        self._repo.record_event("ORDER_CLOSED", {
            "order_id": order_id, "resolution": resolution, "at": iso(now),
        })
        return order

    # ---- 定位更新与道路封闭 ----

    def update_location(self, request_id: str, location: Location) -> tuple[RescueRequest, DispatchOrder | None]:
        request = self._repo.get_request(request_id)
        if request is None:
            raise NotFoundError("request not found", request_id=request_id)
        if request.status != REQUEST_OPEN:
            raise ConflictError("request is closed")
        request.location = location
        self._repo.save_request(request)
        order = self._dispatch.replan_request(request_id, reason="LOCATION_UPDATE")
        self._repo.record_event("LOCATION_UPDATED", {
            "request_id": request_id,
            "committed": bool(order is None),
            "at": iso(self._clock.now()),
        })
        return request, order

    def register_closure(self, road_id: str, direction: str,
                         from_km: float, to_km: float) -> tuple[RoadClosure, list[str]]:
        if from_km > to_km:
            raise ValidationError("from_km must be <= to_km")
        closure = RoadClosure(
            closure_id=self._idgen.new_id("CLS"),
            road_id=road_id,
            direction=direction,
            from_km=from_km,
            to_km=to_km,
            created_at=self._clock.now(),
        )
        self._repo.save_closure(closure)
        changed = self._dispatch.replan_road(road_id, reason="ROAD_CLOSURE")
        self._repo.record_event("ROAD_CLOSED", {
            "closure_id": closure.closure_id, "road_id": road_id,
            "replanned_orders": changed, "at": iso(self._clock.now()),
        })
        return closure, changed

    # ---- 内部 ----

    def _safe_release(self, team_id: str, order: DispatchOrder, context: str) -> None:
        try:
            self.capacity.release(team_id)
        except Exception as exc:  # noqa: BLE001
            self._repo.record_compensation_failure(order.order_id, {
                "stage": context,
                "team_id": team_id,
                "cause": repr(exc),
                "at": iso(self._clock.now()),
            })
            raise CompensationFailedError(
                "compensation release failed; manual reconciliation required",
                order_id=order.order_id,
            ) from exc

    def _require_order(self, order_id: str) -> DispatchOrder:
        order = self._repo.get_order(order_id)
        if order is None:
            raise NotFoundError("order not found", order_id=order_id)
        return order

    def _require_team(self, team_id: str) -> RescueTeam:
        team = self._repo.get_team(team_id)
        if team is None:
            raise NotFoundError("team not found", team_id=team_id)
        return team

    def _require_request(self, request_id: str) -> RescueRequest:
        request = self._repo.get_request(request_id)
        if request is None:
            raise NotFoundError("request not found", request_id=request_id)
        return request


class AdminService:
    """登记救援队与补能设施（接口边界的写入口）。"""

    def __init__(self, repo, clock: Clock, idgen: IdGenerator) -> None:
        self._repo = repo
        self._clock = clock
        self._idgen = idgen

    def register_team(self, *, agency_id: str, name: str, capabilities: list[str],
                      road_id: str, direction: str, coverage_from_km: float,
                      coverage_to_km: float, staging_km: float,
                      duty_windows: list[dict] | None = None,
                      max_concurrent: int = 2, team_id: str | None = None) -> RescueTeam:
        if coverage_from_km > coverage_to_km:
            raise ValidationError("coverage_from_km must be <= coverage_to_km")
        if max_concurrent < 1:
            raise ValidationError("max_concurrent must be >= 1")
        try:
            caps = frozenset(Capability(c) for c in capabilities)
        except ValueError as exc:
            raise ValidationError(f"unknown capability: {exc}") from exc
        windows = tuple(DutyWindow.from_dict(w) for w in (duty_windows or []))
        if not windows:
            raise ValidationError("at least one duty window required")
        team = RescueTeam(
            team_id=team_id or self._idgen.new_id("TEM"),
            agency_id=agency_id,
            name=name,
            capabilities=caps,
            road_id=road_id,
            direction=direction,
            coverage_from_km=float(coverage_from_km),
            coverage_to_km=float(coverage_to_km),
            staging_km=float(staging_km),
            duty_windows=windows,
            max_concurrent=max_concurrent,
        )
        self._repo.save_team(team)
        return team

    def register_site(self, *, road_id: str, direction: str, km: float,
                      queue: int = 0, congested_threshold: int = 3,
                      site_id: str | None = None) -> ChargingSite:
        if queue < 0 or congested_threshold < 1:
            raise ValidationError("invalid site queue/threshold")
        site = ChargingSite(
            site_id=site_id or self._idgen.new_id("SITE"),
            road_id=road_id,
            direction=direction,
            km=float(km),
            queue=queue,
            congested_threshold=congested_threshold,
        )
        self._repo.save_site(site)
        return site
