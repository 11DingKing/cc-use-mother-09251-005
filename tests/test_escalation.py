"""超时升级：墙钟驱动连续升级，重启后补齐欠档，承诺后停止。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from service_09251_005.application import Repository, RescueService
from service_09251_005.clock import FixedClock
from service_09251_005.domain.enums import Capability
from service_09251_005.domain.team import DutyWindow

from helpers import T0, assignments_for, build_world


class EscalationTests(unittest.TestCase):
    def test_escalates_after_sla_and_caps_at_critical(self) -> None:
        repo, clk, svc = build_world()
        # 不登记任何队伍 → 永远无法响应，只能升级
        case, _ = svc.intake(
            "low_battery", "stranded", "138", battery_pct=5.0
        )
        # low 风险 SLA 30 分钟，到期首升
        clk.advance_minutes(30)
        out = svc.run_due()
        self.assertEqual(len(out["escalated"]), 1)
        self.assertEqual(case.priority, "urgent")
        self.assertEqual(case.escalation_level, 1)
        # 每 10 分钟再升一档：t=40 第二档
        clk.advance_minutes(10)
        svc.run_due()
        self.assertEqual(case.priority, "critical")
        self.assertEqual(case.escalation_level, 2)
        # 优先级在 critical 封顶，但升级档位继续累加（持续通知更高值班层级）
        # t=40 档2；推进到 t=70 补齐 @50/@60/@70，共 5 档
        clk.advance_minutes(30)
        svc.run_due()
        self.assertEqual(case.priority, "critical")
        self.assertEqual(case.escalation_level, 5)
        self.assertEqual(len(case.escalations), 5)

    def test_high_risk_has_tighter_sla(self) -> None:
        repo, clk, svc = build_world()
        case, _ = svc.intake(
            "accident", "stranded", "138",
            in_driving_lane=True, reported_level="high",
        )
        # high 风险 SLA 12 分钟
        clk.advance_minutes(11)
        self.assertEqual(svc.run_due()["escalated"], [])
        clk.advance_minutes(1)
        self.assertEqual(len(svc.run_due()["escalated"]), 1)

    def test_commit_stops_escalation(self) -> None:
        repo, clk, svc = build_world()
        from helpers import register_standard_teams
        register_standard_teams(svc)
        case, _ = svc.intake(
            "low_battery", "stranded", "138", battery_pct=5.0
        )
        asg = assignments_for(repo, case.case_id, "offered")[0]
        # 报价 TTL 为 5 分钟：在 TTL 内驱动一次，确认 SLA 未到（low=30）
        clk.advance_minutes(4)
        self.assertEqual(svc.run_due()["escalated"], [])
        svc.claim(asg.assignment_id, "tMobile")
        # 承诺后即使越过 SLA 多个周期也不再升级
        clk.advance_minutes(120)
        out = svc.run_due()
        self.assertEqual(out["escalated"], [])
        self.assertEqual(case.escalation_level, 0)
        self.assertIsNone(case.next_escalation_at)

    def test_escalation_resumes_after_restart(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            w = DutyWindow("00:00", "23:59", frozenset(range(7)),
                           "Asia/Shanghai")

            def build(snapshot):
                repo = Repository(snapshot)
                clk = FixedClock(T0)
                svc = RescueService(repo, clk)
                for n in ["baseA", "sx"]:
                    svc.add_node(n)
                svc.add_segment("g1", "baseA", "sx", 5)
                svc.register_team(
                    "tA", "补能", "O", "baseA",
                    {Capability.MOBILE_CHARGING}, duty_windows=[w],
                )
                return repo, clk, svc

            repo1, clk1, svc1 = build(path)
            case, _ = svc1.intake(
                "low_battery", "sx", "138", battery_pct=2.0
            )
            cid = case.case_id
            # 进程崩溃，未响应
            del svc1, repo1

            # 40 分钟后重启：应一次性补齐所欠升级档位
            repo2 = Repository(path)
            self.assertTrue(repo2.load())
            clk2 = FixedClock(T0 + timedelta(minutes=40))
            svc2 = RescueService(repo2, clk2)
            out = svc2.run_due()
            case2 = repo2.cases[cid]
            self.assertGreaterEqual(len(out["escalated"]), 1)
            self.assertEqual(case2.priority, "critical")
            self.assertGreaterEqual(case2.escalation_level, 2)
            # 升级记录完整保留
            self.assertEqual(len(case2.escalations), case2.escalation_level)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
