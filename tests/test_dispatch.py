"""派单联动：道路方向与封闭、补能方式选择、服务区拥堵、值班窗口、优先级。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from service_09251_005.domain.station import ChargingStation
from service_09251_005.domain.team import DutyWindow

from helpers import (
    T0,
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
    station_full,
)


class DispatchPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)

    def test_low_battery_prefers_mobile_charging_when_closer(self) -> None:
        self.svc.register_station(station_full())
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=4.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        # 移动补能队驻 baseMobile（45km），拖车驻 baseTow（75km）→ 移动补能胜出
        self.assertEqual(asg.service_type, "mobile_power")
        self.assertEqual(asg.team_id, "tMobile")

    def test_tow_when_no_mobile_team(self) -> None:
        # 移除移动补能能力，仅有拖车 + 空闲服务区
        self.repo.teams.pop("tMobile")
        self.svc.register_station(station_free())
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=3.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.service_type, "tow_to_station")
        self.assertEqual(asg.station_id, "st1")
        self.assertTrue(asg.station_reserved)  # 有空位则预约

    def test_congested_station_carries_wait_and_no_reservation(self) -> None:
        self.repo.teams.pop("tMobile")
        self.svc.register_station(station_full())
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=3.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.service_type, "tow_to_station")
        self.assertGreater(asg.wait_minutes, 0)  # 拥堵等待计入方案
        self.assertFalse(asg.station_reserved)   # 满站不预约，到场排队

    def test_offline_station_is_skipped(self) -> None:
        self.repo.teams.pop("tMobile")
        st = station_free()
        st.offline = True
        self.svc.register_station(st)
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=3.0
        )
        plan = self.svc.plan_case(case.case_id)
        self.assertFalse(plan.feasible)
        report = " ".join(plan.infeasible_report())
        self.assertIn("服务区", report)

    def test_road_closure_makes_unreachable_before_commit(self) -> None:
        self.svc.register_station(station_free())
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=4.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.route_nodes[-1], "stranded")
        # 封闭主路 s3：未承诺派单重算，只能走 500km 绕行，ETA 大增
        affected = self.svc.close_road("s3", "事故封路")
        self.assertEqual(affected, [case.case_id])
        new_asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertIn("detour", new_asg.route_nodes)
        self.assertGreater(new_asg.eta_minutes, asg.eta_minutes * 3)
        # 旧派单已撤销
        self.assertEqual(asg.status, "revoked")

    def test_committed_assignment_is_frozen_on_closure(self) -> None:
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=4.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.svc.claim(asg.assignment_id, "tMobile")
        # 封路：已承诺派单不动
        self.svc.close_road("s3", "封路")
        self.assertEqual(asg.status, "claimed")
        self.assertEqual(asg.team_id, "tMobile")
        self.assertNotIn(
            "revoked",
            [a.status for a in assignments_for(self.repo, case.case_id)],
        )

    def test_one_way_segment_direction_enforced(self) -> None:
        from service_09251_005.domain.enums import Capability
        from helpers import ALLDAY_SH
        # s8 仅 j1 -> eastOnly；反方向不可达
        net = self.repo.network
        self.assertTrue(net.is_reachable("j1", "eastOnly"))
        self.assertFalse(net.is_reachable("eastOnly", "j1"))
        # 只保留一支驻在单向末端 eastOnly 的事故队，事故发生在 j1：
        # 它无法逆单向边驶出，故不可达
        for tid in list(self.repo.teams):
            self.repo.teams.pop(tid)
        self.svc.register_team(
            "tIso", "单向末端队", "ORG_I", "eastOnly",
            {Capability.ACCIDENT_RESCUE}, duty_windows=[ALLDAY_SH],
        )
        case, _ = self.svc.intake("accident", "j1", "138")
        plan = self.svc.plan_case(case.case_id)
        self.assertFalse(plan.feasible)
        self.assertIn("无法到达", " ".join(plan.infeasible_report()))

    def test_duty_window_gates_dispatch(self) -> None:
        # 把移动补能队改为仅北京 06:00-08:00 值班（UTC 22:00-00:00）
        early = DutyWindow(
            "06:00", "08:00", frozenset(range(7)), "Asia/Shanghai"
        )
        self.repo.teams["tMobile"].duty_windows = [early]
        self.svc.register_station(station_free())
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=4.0
        )
        offered = assignments_for(self.repo, case.case_id, "offered")
        # 移动队 T0(20:00 北京) 不在岗，只能由拖车承担
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0].service_type, "tow_to_station")
        self.assertEqual(offered[0].team_id, "tTow")

    def test_charger_fault_needs_repair_and_energy_when_low(self) -> None:
        self.svc.register_station(station_free())
        case, _ = self.svc.intake(
            "charger_fault", "svc", "138", battery_pct=10.0
        )
        offered = assignments_for(self.repo, case.case_id, "offered")
        kinds = {a.service_type for a in offered}
        self.assertIn("onsite_repair", kinds)
        # 电量低时同时补能（移动补能队最近）
        self.assertIn("mobile_power", kinds)
        self.assertEqual(case.status, "open")

    def test_partial_commit_status(self) -> None:
        self.svc.register_station(station_free())
        case, _ = self.svc.intake(
            "charger_fault", "svc", "138", battery_pct=10.0
        )
        offered = assignments_for(self.repo, case.case_id, "offered")
        repair = next(a for a in offered if a.service_type == "onsite_repair")
        self.svc.claim(repair.assignment_id, "tRepair")
        self.assertEqual(case.status, "partially_committed")
        energy = next(
            a for a in assignments_for(self.repo, case.case_id, "offered")
            if a.service_type == "mobile_power"
        )
        self.svc.claim(energy.assignment_id, "tMobile")
        self.assertEqual(case.status, "in_progress")

    def test_higher_risk_weights_speed_more(self) -> None:
        # 同一布局下，critical 工单的评分对时间更敏感：
        # 远但在岗的队伍 vs 近队——直接验证评分单调性
        from service_09251_005.domain.cases import RiskProfile, RiskLevel
        from service_09251_005.dispatch.planner import Dispatcher

        low = RiskProfile(reported_level=RiskLevel.LOW)
        crit = RiskProfile(reported_level=RiskLevel.CRITICAL)
        d = Dispatcher(self.repo.network, {}, {})
        s_fast, s_slow = 20.0, 60.0
        gap_low = d._score(s_slow, 0, type("C", (), {"risk": low})()) \
            - d._score(s_fast, 0, type("C", (), {"risk": low})())
        gap_crit = d._score(s_slow, 0, type("C", (), {"risk": crit})()) \
            - d._score(s_fast, 0, type("C", (), {"risk": crit})())
        self.assertGreater(gap_crit, gap_low)


if __name__ == "__main__":
    unittest.main()
