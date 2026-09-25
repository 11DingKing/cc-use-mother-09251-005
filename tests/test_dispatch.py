"""派单引擎：道路方向、补能机会、值班窗口与道路封闭的联动。"""
import unittest

from support import add_team, make_app, report
from service_09251_005.domain.models import (
    ADVICE_MOBILE_CHARGE,
    OrderStatus,
)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.app, self.clock, self.notifier, _ = make_app()

    def tearDown(self):
        self.app.close()

    def test_direction_and_capability_filter(self):
        add_team(self.app, "T-NORTH", capabilities=("MOBILE_CHARGE", "TOW"))
        add_team(self.app, "T-SOUTH", capabilities=("MOBILE_CHARGE", "TOW"),
                 direction="SOUTHBOUND")
        add_team(self.app, "T-MEDIC", capabilities=("MEDICAL",), staging=115.0)
        _, order, _ = report(self.app)
        self.assertEqual(order.status, OrderStatus.OFFERED)
        self.assertEqual(order.candidate_team_ids, ["T-NORTH"])

    def test_congested_site_prefers_mobile_charge(self):
        add_team(self.app, "T-MOBILE", capabilities=("MOBILE_CHARGE", "TOW"), staging=110.0)
        add_team(self.app, "T-TOW", capabilities=("TOW",), staging=119.0)
        # 服务区排队 5 >= 阈值 3 → 拥堵，移动充电队优先（哪怕距离更远）
        self.app.admin.register_site(road_id="G15", direction="NORTHBOUND",
                                     km=122.0, queue=5, congested_threshold=3,
                                     site_id="S1")
        _, order, _ = report(self.app, battery=10.0)  # 可达 40km，覆盖 S1
        self.assertEqual(order.energy_advice, ADVICE_MOBILE_CHARGE)
        self.assertEqual(order.candidate_team_ids[0], "T-MOBILE")

    def test_open_site_guides_to_site(self):
        add_team(self.app, "T-TOW", capabilities=("TOW",))
        self.app.admin.register_site(road_id="G15", direction="NORTHBOUND",
                                     km=123.0, queue=0, site_id="S2")
        _, order, _ = report(self.app, battery=10.0)
        self.assertEqual(order.energy_advice, "GUIDE_TO_SITE:S2")

    def test_closure_excludes_unreachable_teams(self):
        add_team(self.app, "T-NEAR", capabilities=("TOW",), staging=110.0)
        add_team(self.app, "T-FAR", capabilities=("TOW",), staging=140.0)
        _, order, _ = report(self.app, km=120.0)
        self.assertEqual(order.candidate_team_ids, ["T-NEAR", "T-FAR"])
        # 封闭 112~150：T-NEAR 路径 110→120 穿越封闭段，T-FAR 在封闭段内，均不可达
        _, changed = self.app.ops.register_closure("G15", "NORTHBOUND", 112.0, 150.0)
        self.assertIn(order.order_id, changed)
        updated = self.app.repo.get_order(order.order_id)
        self.assertEqual(updated.status, OrderStatus.PENDING)
        self.assertEqual(updated.candidate_team_ids, [])

    def test_committed_order_not_replanned_on_closure(self):
        add_team(self.app, "T-NEAR", capabilities=("MOBILE_CHARGE",), staging=110.0)
        _, order, _ = report(self.app, km=120.0)
        self.app.ops.claim(order.order_id, "T-NEAR")
        _, changed = self.app.ops.register_closure("G15", "NORTHBOUND", 112.0, 150.0)
        self.assertEqual(changed, [])  # 已承诺的单不在调整范围
        kept = self.app.repo.get_order(order.order_id)
        self.assertEqual(kept.status, OrderStatus.CLAIMED)
        self.assertEqual(kept.assigned_team_id, "T-NEAR")

    def test_location_update_only_moves_uncommitted(self):
        add_team(self.app, "T-A", capabilities=("TOW",), staging=110.0)
        add_team(self.app, "T-B", capabilities=("TOW",), cover=(150.0, 200.0), staging=190.0)
        req, order, _ = report(self.app, km=120.0)
        self.assertEqual(order.candidate_team_ids, ["T-A"])
        # 未承诺：定位更新后重算候选
        from service_09251_005.domain.models import Location
        _, order2 = self.app.ops.update_location(req.request_id, Location("G15", "NORTHBOUND", 195.0))
        self.assertEqual(order2.candidate_team_ids, ["T-B"])
        # 已承诺：定位更新不再改动派单
        self.app.ops.claim(order2.order_id, "T-B")
        _, order3 = self.app.ops.update_location(req.request_id, Location("G15", "NORTHBOUND", 105.0))
        self.assertIsNone(order3)
        kept = self.app.repo.get_order(order2.order_id)
        self.assertEqual(kept.assigned_team_id, "T-B")
        self.assertEqual(kept.status, OrderStatus.CLAIMED)

    def test_duty_window_gates_dispatch(self):
        from datetime import timedelta

        duty = [{"weekdays": [0, 1, 2, 3, 4, 5, 6],
                 "start": "09:00", "end": "17:00", "tz": "UTC"}]
        add_team(self.app, "T-DAY", capabilities=("TOW",), duty_windows=duty)
        # 时钟在 12:00 UTC：在班，可派
        _, order, _ = report(self.app)
        self.assertEqual(order.candidate_team_ids, ["T-DAY"])
        # 推进到 20:00 UTC：下值，新求援无人可派
        self.clock.advance(timedelta(hours=8))
        _, order2, _ = report(self.app, phone="13855556666", km=130.0)
        self.assertEqual(order2.status, OrderStatus.PENDING)
        self.assertEqual(order2.candidate_team_ids, [])


if __name__ == "__main__":
    unittest.main()
