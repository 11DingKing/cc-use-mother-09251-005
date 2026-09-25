"""跨机构转派：责任交接链完整，责任不空窗，拒绝/并发交接受控。"""
from __future__ import annotations

import unittest

from service_09251_005.domain.team import DutyWindow
from service_09251_005.errors import TransferRejectedError

from helpers import (
    ALLDAY_SH,
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
)


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)
        # 两个外部机构的拖车，驻同一可达基地
        from service_09251_005.domain.enums import Capability
        self.svc.register_team(
            "tTowC", "邻市拖车", "ORG_C", "baseTow",
            {Capability.TOWING}, duty_windows=[ALLDAY_SH],
        )
        self.svc.register_team(
            "tTowD", "第三机构拖车", "ORG_D", "baseTow",
            {Capability.TOWING}, duty_windows=[ALLDAY_SH],
        )
        self.svc.register_station(station_free())
        # 移除移动补能，迫使拖车方案，便于观察预约随车交接
        self.repo.teams.pop("tMobile")
        self.case, _ = self.svc.intake(
            "low_battery", "stranded", "138", battery_pct=3.0
        )
        self.asg = assignments_for(self.repo, self.case.case_id, "offered")[0]
        self.svc.claim(self.asg.assignment_id, "tTow")

    def test_handover_moves_responsibility_without_gap(self) -> None:
        ho = self.svc.propose_transfer(
            self.asg.assignment_id, "tTowC", "跨区协作", "电量3%，已在途"
        )
        self.assertEqual(ho.state, "proposed")
        # 提出交接期间，责任仍在发起方（不空窗）
        self.assertEqual(self.asg.responsible_team_id, "tTow")
        self.assertTrue(self.repo.teams["tTow"].is_busy)

        new_asg = self.svc.acknowledge_transfer(ho.handover_id, "tTowC")
        self.assertEqual(ho.state, "acknowledged")
        self.assertEqual(new_asg.team_id, "tTowC")
        self.assertEqual(new_asg.org_id, "ORG_C")
        self.assertEqual(new_asg.parent_assignment_id, self.asg.assignment_id)
        # 旧派单终结、新派单承接；充电预约随责任移交
        self.assertEqual(self.asg.status, "transferred")
        self.assertTrue(new_asg.station_reserved)
        self.assertEqual(self.repo.stations["st1"].reserved, 1)
        # 旧责任方释放，新责任方在岗承接
        self.assertFalse(self.repo.teams["tTow"].is_busy)
        self.assertTrue(self.repo.teams["tTowC"].is_busy)

    def test_handover_chain_is_preserved_across_multiple_transfers(self) -> None:
        ho1 = self.svc.propose_transfer(self.asg.assignment_id, "tTowC", "第一次")
        a2 = self.svc.acknowledge_transfer(ho1.handover_id, "tTowC")
        ho2 = self.svc.propose_transfer(a2.assignment_id, "tTowD", "第二次")
        a3 = self.svc.acknowledge_transfer(ho2.handover_id, "tTowD")
        chain = self.svc.handover_chain(self.case.case_id)
        self.assertEqual([h.chain_index for h in chain], [0, 1])
        self.assertEqual([h.state for h in chain], ["acknowledged", "acknowledged"])
        self.assertEqual(a3.chain_index, 1)
        self.assertEqual(a3.responsible_org_id, "ORG_D")
        self.assertEqual(a3.parent_assignment_id, a2.assignment_id)
        # 链路可回溯到最初责任方
        self.assertEqual(ho1.from_org_id, "ORG_A")
        self.assertEqual(ho2.from_org_id, "ORG_C")

    def test_cannot_start_second_handover_while_one_pending(self) -> None:
        self.svc.propose_transfer(self.asg.assignment_id, "tTowC", "待确认")
        with self.assertRaises(TransferRejectedError):
            self.svc.propose_transfer(self.asg.assignment_id, "tTowD", "抢转")

    def test_reject_keeps_responsibility_with_original(self) -> None:
        ho = self.svc.propose_transfer(self.asg.assignment_id, "tTowC", "请求")
        self.svc.reject_transfer(ho.handover_id, "无空闲", "tTowC")
        self.assertEqual(ho.state, "rejected")
        self.assertEqual(self.asg.status, "claimed")
        self.assertEqual(self.asg.responsible_team_id, "tTow")
        self.assertTrue(self.repo.teams["tTow"].is_busy)
        self.assertFalse(self.repo.teams["tTowC"].is_busy)
        # 拒绝后可以重新发起
        ho2 = self.svc.propose_transfer(self.asg.assignment_id, "tTowD", "再请")
        self.assertEqual(ho2.state, "proposed")

    def test_only_receiver_may_acknowledge(self) -> None:
        ho = self.svc.propose_transfer(self.asg.assignment_id, "tTowC", "x")
        with self.assertRaises(TransferRejectedError):
            self.svc.acknowledge_transfer(ho.handover_id, "tTowD")

    def test_same_org_transfer_rejected(self) -> None:
        # tTow 与 tMobile 同属 ORG_A；给 ORG_A 再补一个拖车驻 baseTow
        from service_09251_005.domain.enums import Capability
        self.svc.register_team(
            "tTowA2", "同机构拖车", "ORG_A", "baseTow",
            {Capability.TOWING}, duty_windows=[ALLDAY_SH],
        )
        with self.assertRaises(TransferRejectedError):
            self.svc.propose_transfer(self.asg.assignment_id, "tTowA2", "内部")

    def test_cannot_transfer_uncommitted_assignment(self) -> None:
        case2, _ = self.svc.intake(
            "low_battery", "svc", "139", battery_pct=2.0
        )
        fresh = assignments_for(self.repo, case2.case_id, "offered")[0]
        with self.assertRaises(TransferRejectedError):
            self.svc.propose_transfer(fresh.assignment_id, "tTowC", "未承诺")


if __name__ == "__main__":
    unittest.main()
