"""测试夹具：构建小型高速路网、服务区与救援队。"""
from __future__ import annotations

from datetime import datetime, timezone

from service_09251_005.application import Repository, RescueService
from service_09251_005.clock import FixedClock
from service_09251_005.domain.enums import Capability
from service_09251_005.domain.station import ChargingStation
from service_09251_005.domain.team import DutyWindow

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)  # 周五 20:00 北京
ALLDAY_SH = DutyWindow("00:00", "23:59", frozenset(range(7)), "Asia/Shanghai")


def build_world(snapshot_path: str | None = None, start=T0):
    """道路布局：

        baseMobile --10km-- j1 --20km-- svc(服务区 st1) --15km-- stranded
        baseTow ----40km---- j1
        baseRepair(在 svc 旁 = svc 节点)
        baseAccident --100km-- stranded
        detour: stranded --5-- detour --500-- j1   （s3 封闭后的远绕行）
        oneway: j1 -> eastOnly 单向 30km
    """
    repo = Repository(snapshot_path)
    clk = FixedClock(start)
    svc = RescueService(repo, clk)
    for n in ["baseMobile", "j1", "svc", "stranded", "baseTow",
              "baseAccident", "detour", "eastOnly"]:
        svc.add_node(n, n)
    svc.add_segment("s1", "baseMobile", "j1", 10)
    svc.add_segment("s2", "j1", "svc", 20)
    svc.add_segment("s3", "svc", "stranded", 15)
    svc.add_segment("s4", "baseTow", "j1", 40)
    svc.add_segment("s5", "baseAccident", "stranded", 100)
    svc.add_segment("s6", "stranded", "detour", 5)
    svc.add_segment("s7", "detour", "j1", 500)
    svc.add_segment("s8", "j1", "eastOnly", 30, direction="atob")
    return repo, clk, svc


def register_standard_teams(svc, windows=None):
    w = windows or ALLDAY_SH
    svc.register_team(
        "tMobile", "移动补能一队", "ORG_A", "baseMobile",
        {Capability.MOBILE_CHARGING}, duty_windows=[w],
        mobile_power_kwh=20.0,
    )
    svc.register_team(
        "tTow", "拖车一队", "ORG_A", "baseTow",
        {Capability.TOWING}, duty_windows=[w],
    )
    svc.register_team(
        "tRepair", "充电维修一组", "ORG_R", "svc",
        {Capability.CHARGER_REPAIR}, duty_windows=[ALLDAY_SH],
    )
    svc.register_team(
        "tAcc", "事故救援一队", "ORG_B", "baseAccident",
        {Capability.ACCIDENT_RESCUE}, duty_windows=[ALLDAY_SH],
    )


def station_free():
    return ChargingStation("st1", "阳澄湖服务区", "svc", total_ports=4)


def station_full():
    return ChargingStation(
        "st1", "阳澄湖服务区", "svc", total_ports=2,
        occupied=2, queue_length=2,
    )


def assignments_for(repo, case_id, status=None):
    return [
        a for a in repo.assignments.values()
        if a.case_id == case_id and (status is None or a.status == status)
    ]
