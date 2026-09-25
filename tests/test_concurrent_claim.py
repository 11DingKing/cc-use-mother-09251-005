"""并发抢单：高竞争下只有一个承诺成功，其余得到明确冲突。"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from service_09251_005.domain.enums import Capability
from service_09251_005.domain.team import DutyWindow
from service_09251_005.errors import AlreadyClaimedError

from helpers import build_world, register_standard_teams, assignments_for


class ConcurrentClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)
        # 第二支具备移动补能能力、同样可达的队伍（驻 baseTow）
        self.svc.register_team(
            "tMobile2", "移动补能二队", "ORG_A", "baseTow",
            {Capability.MOBILE_CHARGING},
            duty_windows=[DutyWindow("00:00", "23:59", frozenset(range(7)),
                                     "Asia/Shanghai")],
        )
        self.case, _ = self.svc.intake(
            "low_battery", "stranded", "13800000000", battery_pct=5.0
        )
        self.asg = assignments_for(self.repo, self.case.case_id, "offered")[0]

    def test_only_one_winner_under_contention(self) -> None:
        actors = ["tMobile", "tMobile2"] * 10
        start_gun = threading.Event()
        results: list[tuple[str, object]] = []
        lock = threading.Lock()

        def grab(actor: str) -> None:
            start_gun.wait(timeout=5)
            try:
                self.svc.claim(self.asg.assignment_id, actor)
                with lock:
                    results.append((actor, "won"))
            except AlreadyClaimedError as exc:
                with lock:
                    results.append((actor, exc.code))
            except Exception as exc:  # pragma: no cover
                with lock:
                    results.append((actor, f"unexpected:{exc!r}"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(grab, a) for a in actors]
            start_gun.set()  # 全部任务入队后统一放行
            for f in futures:
                f.result(timeout=10)

        wins = [r for r in results if r[1] == "won"]
        self.assertEqual(len(wins), 1, f"应有且仅有一个赢家: {results}")
        winner = wins[0][0]
        self.assertEqual(self.asg.status, "claimed")
        self.assertEqual(self.asg.responsible_team_id, winner)
        # 输家不得占用救援队
        loser = "tMobile2" if winner == "tMobile" else "tMobile"
        self.assertIsNone(self.repo.teams[loser].current_case_id)
        self.assertEqual(self.repo.teams[winner].current_case_id, self.case.case_id)
        # 工单已获得响应，超时升级停止
        self.assertIsNotNone(self.case.committed_at)
        self.assertIsNone(self.case.next_escalation_at)

    def test_sequence_double_claim_conflicts(self) -> None:
        self.svc.claim(self.asg.assignment_id, "tMobile2")
        with self.assertRaises(AlreadyClaimedError):
            self.svc.claim(self.asg.assignment_id, "tMobile")

    def test_claim_after_expiry_rejected_until_reissue(self) -> None:
        self.clk.advance(301)  # 超过 5 分钟报价 TTL
        with self.assertRaises(Exception):
            self.svc.claim(self.asg.assignment_id, "tMobile")
        # run_due 作废旧报价并重新派单，新单可抢
        self.svc.run_due()
        new = assignments_for(self.repo, self.case.case_id, "offered")
        self.assertEqual(len(new), 1)
        self.svc.claim(new[0].assignment_id, "tMobile")
        self.assertEqual(new[0].status, "claimed")


if __name__ == "__main__":
    unittest.main()
