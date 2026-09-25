"""全生命周期：受理→派单→抢单→到场→补能→关单，及关单前置校验。"""
from __future__ import annotations

import unittest

from service_09251_005.errors import ConflictError

from helpers import (
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)
        self.svc.register_station(station_free())

    def _open_low_battery(self, phone="138", **kw):
        defaults = dict(battery_pct=4.0)
        defaults.update(kw)
        return self.svc.intake("low_battery", "stranded", phone, **defaults)

    def test_happy_path_mobile_recharge(self) -> None:
        case, _ = self._open_low_battery()
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.service_type, "mobile_power")
        self.svc.claim(asg.assignment_id, "tMobile")
        self.assertEqual(case.status, "in_progress")
        self.svc.arrive(asg.assignment_id, note="已放置三角警示牌")
        self.assertEqual(asg.status, "arrived")
        self.svc.energize(asg.assignment_id, kwh_delivered=15.0)
        self.assertEqual(asg.status, "energized")
        self.assertEqual(case.status, "resolved")
        closed = self.svc.close_case(case.case_id, "电量恢复，驶离")
        self.assertEqual(closed.status, "closed")
        self.assertIsNotNone(closed.closed_at)
        self.assertFalse(self.repo.teams["tMobile"].is_busy)

    def test_tow_to_station_flow_releases_port(self) -> None:
        self.repo.teams.pop("tMobile")
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=2.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.service_type, "tow_to_station")
        self.assertEqual(self.repo.stations["st1"].reserved, 1)
        self.svc.claim(asg.assignment_id, "tTow")
        self.svc.arrive(asg.assignment_id)
        # 送达服务区并完成充电：预约转占用再释放
        self.svc.energize(asg.assignment_id, action="station_recharge")
        self.assertEqual(self.repo.stations["st1"].reserved, 0)
        self.assertEqual(self.repo.stations["st1"].occupied, 0)
        self.svc.close_case(case.case_id)
        self.assertEqual(case.status, "closed")

    def test_cannot_close_before_service_done(self) -> None:
        case, _ = self._open_low_battery()
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.svc.claim(asg.assignment_id, "tMobile")
        with self.assertRaises(ConflictError):
            self.svc.close_case(case.case_id)  # 未到场/补能
        self.assertEqual(case.status, "in_progress")

    def test_arrive_requires_claim(self) -> None:
        case, _ = self._open_low_battery()
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        with self.assertRaises(ConflictError):
            self.svc.arrive(asg.assignment_id)

    def test_location_update_replans_uncommitted_only(self) -> None:
        case, _ = self._open_low_battery()
        offered = assignments_for(self.repo, case.case_id, "offered")
        self.assertEqual(len(offered), 1)
        # 车辆从 stranded 滑到 svc：未承诺，派单重算（距离变短）
        self.svc.update_location(case.case_id, "svc", "缓行至服务区入口")
        new = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertLess(new.eta_minutes, offered[0].eta_minutes)
        self.assertEqual(case.location_node, "svc")
        self.assertEqual(len(case.location_history), 1)

    def test_accident_flow(self) -> None:
        case, _ = self.svc.intake(
            "accident", "stranded", "139",
            in_driving_lane=True, reported_level="high",
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertEqual(asg.service_type, "accident_handle")
        self.svc.claim(asg.assignment_id, "tAcc")
        self.svc.arrive(asg.assignment_id)
        self.svc.energize(asg.assignment_id, action="fault_cleared")
        self.assertEqual(case.status, "resolved")
        self.svc.close_case(case.case_id)
        self.assertEqual(case.status, "closed")


if __name__ == "__main__":
    unittest.main()
