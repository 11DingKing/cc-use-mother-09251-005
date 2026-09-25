"""快照持久化往返：转派链、派单、工单风险与升级在重启后完整恢复。"""
from __future__ import annotations

import os
import tempfile
import unittest

from service_09251_005.application import Repository, RescueService
from service_09251_005.clock import FixedClock
from service_09251_005.domain.cases import PrivacyField
from service_09251_005.domain.team import DutyWindow
from service_09251_005.domain.enums import Capability

from helpers import (
    T0,
    ALLDAY_SH,
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
)


class SnapshotRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_full_state_round_trip_with_handover_chain(self) -> None:
        repo, clk, svc = build_world(self.path)
        register_standard_teams(svc)
        svc.register_station(station_free())
        svc.register_team(
            "tC", "邻市补能", "ORG_C", "baseTow",
            {Capability.MOBILE_CHARGING}, duty_windows=[ALLDAY_SH],
        )
        case, _ = svc.intake(
            "low_battery", "stranded", "138",
            battery_pct=3.0, in_driving_lane=True,
            reported_level="high",
            consent_scopes={PrivacyField.PHONE, PrivacyField.PLATE},
        )
        # 第二通重复呼叫（更高风险），验证 calls 与风险留痕持久化
        svc.intake("low_battery", "stranded", "138",
                   battery_pct=2.0, fire_or_smoke=True,
                   reported_level="critical")
        asg = assignments_for(repo, case.case_id, "offered")[0]
        svc.claim(asg.assignment_id, "tMobile")
        ho = svc.propose_transfer(
            asg.assignment_id, "tC", "跨区", "电量2%，有火情"
        )
        new_asg = svc.acknowledge_transfer(ho.handover_id, "tC")
        svc.arrive(new_asg.assignment_id)
        # 显式落盘（intake/claim 等也已持续落盘）
        repo.save()

        # ---- 重启 ----
        repo2 = Repository(self.path)
        self.assertTrue(repo2.load())
        clk2 = FixedClock(clk.now())
        svc2 = RescueService(repo2, clk2)

        c2 = repo2.cases[case.case_id]
        self.assertEqual(len(c2.calls), 2)
        self.assertEqual(c2.risk_level.value, "critical")
        self.assertTrue(c2.risk.fire_or_smoke)
        self.assertEqual(c2.battery_pct, 2.0)
        self.assertEqual(
            set(s.value for s in c2.consent_scopes), {"phone", "plate"}
        )
        # 道路网络与封闭状态
        self.assertTrue(repo2.network.is_reachable("baseMobile", "stranded"))
        self.assertFalse(repo2.network.is_reachable("eastOnly", "j1"))
        # 派单状态与责任方
        new2 = repo2.assignments[new_asg.assignment_id]
        self.assertEqual(new2.status, "arrived")
        self.assertEqual(new2.responsible_team_id, "tC")
        self.assertEqual(new2.responsible_org_id, "ORG_C")
        self.assertEqual(new2.parent_assignment_id, asg.assignment_id)
        # 交接凭据链完整
        hos = svc2.handover_chain(case.case_id)
        self.assertEqual(len(hos), 1)
        self.assertEqual(hos[0].state, "acknowledged")
        self.assertEqual(hos[0].from_org_id, "ORG_A")
        self.assertEqual(hos[0].to_org_id, "ORG_C")
        self.assertEqual(hos[0].situational_summary, "电量2%，有火情")
        self.assertIsNotNone(hos[0].acknowledged_at)
        # 队伍占用状态恢复
        self.assertTrue(repo2.teams["tC"].is_busy)
        self.assertFalse(repo2.teams["tMobile"].is_busy)
        # 服务区占用计数恢复
        self.assertEqual(repo2.stations["st1"].total_ports, 4)

    def test_no_snapshot_path_means_no_persistence(self) -> None:
        repo, clk, svc = build_world(None)
        register_standard_teams(svc)
        svc.intake("accident", "stranded", "139", reported_level="high")
        repo.save()  # 不应抛错，也不产生文件
        self.assertEqual(repo.load(), False)


if __name__ == "__main__":
    unittest.main()
