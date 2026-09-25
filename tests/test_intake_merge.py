"""重复呼叫合并：合并但不能吞掉更高风险信息。"""
import unittest
from datetime import timedelta

from support import make_app, report


class IntakeMergeTests(unittest.TestCase):
    def setUp(self):
        self.app, self.clock, self.notifier, _ = make_app()

    def tearDown(self):
        self.app.close()

    def test_same_phone_merges_and_keeps_higher_risk(self):
        req1, order1, merged1 = report(self.app, risk="HIGH", battery=8.0)
        self.assertFalse(merged1)
        # 同一手机号再次来电，风险更低：合并且高风险不被吞掉
        req2, order2, merged2 = report(self.app, risk="LOW", battery=40.0)
        self.assertTrue(merged2)
        self.assertEqual(req2.request_id, req1.request_id)
        self.assertEqual(int(req2.risk), 3)  # HIGH 保留
        self.assertEqual(req2.battery_pct, 8.0)  # 取更低电量
        self.assertEqual(len(req2.merged_report_ids), 1)
        self.assertEqual(order2.order_id, order1.order_id)

    def test_later_higher_risk_upgrades_priority(self):
        req1, order1, _ = report(self.app, risk="LOW", battery=60.0)
        old_priority = req1.priority
        req2, order2, merged = report(self.app, phone="13900002222",
                                      risk="CRITICAL", battery=3.0, km=120.4)
        self.assertTrue(merged)
        self.assertEqual(req2.request_id, req1.request_id)
        self.assertEqual(int(req2.risk), 4)  # CRITICAL 覆盖
        self.assertGreater(req2.priority, old_priority)
        # 主叫电话被保留在 extra_phones，不丢失
        self.assertIn("13900002222", req2.extra_phones)
        # 合并事件完整记录来话内容，便于审计
        events = self.app.repo.list_events("REQUEST_MERGED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["incoming"]["risk"], 4)

    def test_distinct_location_creates_new_request(self):
        req1, _, _ = report(self.app, km=120.0)
        req2, _, merged = report(self.app, phone="13700003333", km=200.0)
        self.assertFalse(merged)
        self.assertNotEqual(req2.request_id, req1.request_id)

    def test_merge_window_expires(self):
        req1, _, _ = report(self.app)
        self.clock.advance(timedelta(minutes=31))
        req2, _, merged = report(self.app, phone="13700004444", km=120.2)
        self.assertFalse(merged)
        self.assertNotEqual(req2.request_id, req1.request_id)


if __name__ == "__main__":
    unittest.main()
