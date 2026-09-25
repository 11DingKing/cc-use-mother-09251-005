"""补偿失败：充电位释放失败进台账，退避重试，重启后继续，恢复后成功。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from service_09251_005.domain.station import ChargingStation

from helpers import (
    T0,
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
)


class _FlakyGateway:
    """前 fail_for 次释放失败；之后恢复。"""

    def __init__(self, fail_for: int = 999) -> None:
        self.fail_for = fail_for
        self.attempts = 0

    def reserve(self, s) -> None:
        s.reserve_port()

    def confirm_occupied(self, s) -> None:
        s.confirm_occupied()

    def release_reservation(self, s) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_for:
            raise RuntimeError("station backend 500")
        s.release_reservation()

    def release_occupied(self, s) -> None:
        s.release_occupied()


class CompensationTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.repo, self.clk, self.svc = build_world(self.path)
        register_standard_teams(self.svc)
        # 移除移动补能能力，迫使选择“拖至服务区”方案以产生充电位预约
        self.repo.teams.pop("tMobile")
        self.svc.register_station(station_free())  # st1 @ svc，有空位

    def test_failed_release_goes_to_ledger_and_retries(self) -> None:
        gw = _FlakyGateway(fail_for=2)
        self.svc.gateway = gw
        # 趴窝在 svc 旁，拖车规划必选可达的 st1 并成功预约
        case, _ = self.svc.intake(
            "low_battery", "svc", "13800000000", battery_pct=4.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertTrue(asg.station_reserved)
        self.assertEqual(self.repo.stations["st1"].reserved, 1)

        # 定位变化导致未承诺派单撤销：第 1 次释放失败 -> 台账登记，资源不丢账
        self.svc.update_location(case.case_id, "eastOnly", "滑入单向支路")
        self.assertEqual(len(self.repo.compensations), 1)
        entry = self.repo.compensations[0]
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["payload"]["station_id"], "st1")
        # 越过首次退避（2 秒）后重试：第 2 次释放仍失败，计数增加、保持 pending
        self.clk.advance(10)
        self.svc.run_due()
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["state"], "pending")

        # 再次到期后网关恢复（fail_for=2：前两次释放失败），重试成功
        self.clk.advance_minutes(10)
        self.svc.run_due()
        self.assertEqual(entry["state"], "done")
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(self.repo.stations["st1"].reserved, 0)

    def test_persistent_failure_backs_off_then_marks_manual(self) -> None:
        gw = _FlakyGateway(fail_for=999)
        self.svc.gateway = gw
        case, _ = self.svc.intake(
            "low_battery", "svc", "13811111111", battery_pct=3.0
        )
        self.svc.update_location(case.case_id, "eastOnly", "滑入单向支路")
        entry = self.repo.compensations[0]
        # 连续驱动直到超过最大尝试次数
        for _ in range(12):
            self.clk.advance_minutes(100000)
            self.svc.run_due()
        self.assertEqual(entry["state"], "failed_awaiting_manual")
        self.assertGreaterEqual(entry["attempts"], 10)

    def test_compensation_survives_restart(self) -> None:
        gw = _FlakyGateway(fail_for=999)
        self.svc.gateway = gw
        case, _ = self.svc.intake(
            "low_battery", "svc", "13822222222", battery_pct=2.0
        )
        self.svc.update_location(case.case_id, "eastOnly", "滑入单向支路")
        self.assertEqual(self.repo.compensations[0]["state"], "pending")

        # 重启：新仓储从快照恢复，替换健康网关后到期补偿被执行
        from service_09251_005.application import (
            LocalStationGateway,
            Repository,
            RescueService,
        )
        from service_09251_005.clock import FixedClock

        repo2 = Repository(self.path)
        self.assertTrue(repo2.load())
        self.assertEqual(len(repo2.compensations), 1)
        self.assertEqual(repo2.stations["st1"].reserved, 1)
        clk2 = FixedClock(self.clk.now() + timedelta(seconds=1))
        svc2 = RescueService(repo2, clk2, LocalStationGateway())
        svc2.run_due()
        self.assertEqual(repo2.compensations[0]["state"], "done")
        self.assertEqual(repo2.stations["st1"].reserved, 0)

    def test_reserved_port_released_on_normal_replan_path(self) -> None:
        # 健康网关：车辆移到单向末端 eastOnly——拖车可达现场，但 eastOnly
        # 无法回 j1（s8 单向），故无可达服务区、无新预约；旧预约必须立即归还
        case, _ = self.svc.intake(
            "low_battery", "svc", "13833333333", battery_pct=5.0
        )
        self.assertEqual(self.repo.stations["st1"].reserved, 1)
        self.svc.update_location(case.case_id, "eastOnly", "滑入单向支路")
        self.assertEqual(self.repo.compensations, [])
        self.assertEqual(self.repo.stations["st1"].reserved, 0)

    def test_offer_expiry_releases_reservation_then_reoffers(self) -> None:
        # 拖车方案持有 st1 预约；报价 5 分钟无人抢单：预约必须归还，
        # 之后重新派单时再次预约（不留悬挂占用，也不产生补偿台账）
        case, _ = self.svc.intake(
            "low_battery", "stranded", "13844444444", battery_pct=3.0
        )
        asg = assignments_for(self.repo, case.case_id, "offered")[0]
        self.assertTrue(asg.station_reserved)
        self.assertEqual(self.repo.stations["st1"].reserved, 1)
        self.clk.advance_minutes(6)
        self.svc.run_due()
        self.assertEqual(self.repo.compensations, [])
        # 旧单过期释放，新单重新预约 -> 占用恒为 1，无泄漏
        self.assertEqual(asg.status, "expired")
        fresh = assignments_for(self.repo, case.case_id, "offered")
        self.assertEqual(len(fresh), 1)
        self.assertTrue(fresh[0].station_reserved)
        self.assertEqual(self.repo.stations["st1"].reserved, 1)


if __name__ == "__main__":
    unittest.main()
