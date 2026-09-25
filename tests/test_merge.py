"""重复呼叫合并：合并但不吞掉更高风险信息，SLA 收紧，低风险不回退。"""
from __future__ import annotations

import unittest

from helpers import build_world, register_standard_teams, assignments_for


class DuplicateCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)

    def test_second_call_with_higher_risk_is_preserved(self) -> None:
        case, merged = self.svc.intake(
            "low_battery", "stranded", "13800000000",
            battery_pct=12.0, reported_level="low",
        )
        self.assertFalse(merged)
        first_priority = case.priority
        # 第二通：同位置同电话，报告起火、有伤员
        case2, merged2 = self.svc.intake(
            "low_battery", "stranded", "13800000000",
            battery_pct=11.0, fire_or_smoke=True, injured=True,
            reported_level="critical",
        )
        self.assertTrue(merged2)
        self.assertIs(case2, case)
        self.assertEqual(len(case.calls), 2)
        self.assertEqual(case.risk_level.value, "critical")
        self.assertGreater(
            {"routine": 0, "urgent": 1, "critical": 2}[case.priority],
            {"routine": 0, "urgent": 1, "critical": 2}[first_priority],
        )
        # 最高风险来源被显式标记，没有被“合并”吞掉
        self.assertEqual(case.highest_risk_call_id, case.calls[1].call_id)
        self.assertTrue(case.risk.fire_or_smoke)
        self.assertTrue(case.risk.injured)

    def test_lower_risk_call_does_not_downgrade(self) -> None:
        case, _ = self.svc.intake(
            "accident", "stranded", "138",
            injured=True, reported_level="critical",
        )
        level_before = case.risk_level
        due_before = case.response_due_at
        self.clk.advance(3)
        self.svc.intake(
            "accident", "stranded", "138",
            reported_level="low", note="再问问进度",
        )
        self.assertEqual(case.risk_level, level_before)
        self.assertTrue(case.risk.injured)  # 伤员信息不被低风险呼叫抹掉
        self.assertEqual(case.response_due_at, due_before)

    def test_battery_takes_minimum_across_calls(self) -> None:
        case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=15.0
        )
        self.svc.intake("low_battery", "stranded", "138", battery_pct=6.0)
        self.svc.intake("low_battery", "stranded", "138", battery_pct=9.0)
        self.assertEqual(case.battery_pct, 6.0)

    def test_different_event_not_merged(self) -> None:
        c1, _ = self.svc.intake("accident", "stranded", "138")
        c2, merged = self.svc.intake("low_battery", "j1", "138")
        self.assertFalse(merged)
        self.assertNotEqual(c1.case_id, c2.case_id)

    def test_merge_replans_uncommitted_but_keeps_committed(self) -> None:
        case, _ = self.svc.intake(
            "charger_fault", "svc", "138", battery_pct=40.0
        )
        repair = assignments_for(self.repo, case.case_id, "offered")[0]
        self.svc.claim(repair.assignment_id, "tRepair")  # 维修已承诺
        # 第二通：电量跌到 5%，新增补能需求
        self.svc.intake(
            "charger_fault", "svc", "138", battery_pct=5.0
        )
        active = assignments_for(self.repo, case.case_id)
        # 已承诺的维修仍在，且新增了一张补能派单
        self.assertEqual(repair.status, "claimed")
        self.assertTrue(
            any(a.status == "offered" and a.need_key == "energy" for a in active)
        )
        self.assertEqual(case.status, "partially_committed")


if __name__ == "__main__":
    unittest.main()
