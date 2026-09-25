"""跨机构转派：责任交接链保留；补偿失败落台账。"""
import unittest

from support import add_team, make_app, report
from service_09251_005.application.errors import (
    CompensationFailedError,
    TransferFailedError,
)
from service_09251_005.application.services import CapacityGateway


class FlakyCapacity(CapacityGateway):
    """对指定队伍的 release 注入故障，模拟外部系统掉线。"""

    def __init__(self, inner: CapacityGateway, fail_releases_for: set[str]):
        self._inner = inner
        self._fail = set(fail_releases_for)

    def reserve(self, team_id: str) -> bool:
        return self._inner.reserve(team_id)

    def release(self, team_id: str) -> None:
        if team_id in self._fail:
            raise RuntimeError(f"capacity store unreachable for {team_id}")
        return self._inner.release(team_id)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.app, self.clock, self.notifier, _ = make_app()
        add_team(self.app, "T-A", agency="AGENCY-ALPHA",
                 capabilities=("MOBILE_CHARGE", "TOW"), staging=110.0)
        add_team(self.app, "T-B", agency="AGENCY-BETA",
                 capabilities=("MOBILE_CHARGE", "TOW"), staging=118.0)
        _, self.order, _ = report(self.app)
        self.app.ops.claim(self.order.order_id, "T-A")

    def tearDown(self):
        self.app.close()

    def test_cross_agency_handoff_chain_preserved(self):
        order = self.app.ops.transfer(self.order.order_id, "T-B",
                                      reason="跨机构支援：Beta 移动充电车更近",
                                      operator="dispatcher-7")
        self.assertEqual(order.assigned_team_id, "T-B")
        handoffs = self.app.repo.list_handoffs(self.order.order_id)
        self.assertEqual(len(handoffs), 1)
        h = handoffs[0]
        self.assertEqual(h.from_agency, "AGENCY-ALPHA")
        self.assertEqual(h.to_agency, "AGENCY-BETA")
        self.assertEqual(h.from_team, "T-A")
        self.assertEqual(h.to_team, "T-B")
        self.assertIn("跨机构支援", h.reason)
        # 责任随交接转移：当前责任方 = 接手队伍
        self.assertEqual(order.assigned_team_id, h.to_team)
        # 运力台账一致
        self.assertEqual(self.app.repo.get_team("T-A").load, 0)
        self.assertEqual(self.app.repo.get_team("T-B").load, 1)

    def test_compensation_rolls_back_when_release_fails(self):
        self.app.ops.capacity = FlakyCapacity(self.app.capacity, {"T-A"})
        with self.assertRaises(TransferFailedError):
            self.app.ops.transfer(self.order.order_id, "T-B", reason="链路抖动演练")
        order = self.app.repo.get_order(self.order.order_id)
        # 补偿成功：责任回滚给原队伍，交接链保留正反两条记录
        self.assertEqual(order.assigned_team_id, "T-A")
        handoffs = self.app.repo.list_handoffs(self.order.order_id)
        self.assertEqual(len(handoffs), 2)
        self.assertTrue(handoffs[1].reason.startswith("ROLLBACK:"))
        self.assertEqual(handoffs[1].to_agency, "AGENCY-ALPHA")
        self.assertEqual(self.app.repo.get_team("T-B").load, 0)
        self.assertEqual(self.app.repo.list_compensation_failures(), [])

    def test_compensation_failure_is_recorded(self):
        self.app.ops.capacity = FlakyCapacity(self.app.capacity, {"T-A", "T-B"})
        with self.assertRaises(CompensationFailedError):
            self.app.ops.transfer(self.order.order_id, "T-B", reason="双故障演练")
        failures = self.app.repo.list_compensation_failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["order_id"], self.order.order_id)
        self.assertEqual(failures[0]["stage"], "TRANSFER_COMPENSATION")
        # 悬挂状态如实保留：派单指向新队伍，等待人工对账
        order = self.app.repo.get_order(self.order.order_id)
        self.assertEqual(order.assigned_team_id, "T-B")
        handoffs = self.app.repo.list_handoffs(self.order.order_id)
        self.assertEqual(len(handoffs), 1)  # 正向交接已留痕


if __name__ == "__main__":
    unittest.main()
