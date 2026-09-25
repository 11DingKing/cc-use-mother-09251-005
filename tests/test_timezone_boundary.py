"""时区边界：跨午夜值班窗口在不同时区下的判定。"""
import unittest
from datetime import datetime, timezone

from support import add_team, make_app, report
from service_09251_005.domain.models import DutyWindow

UTC = timezone.utc

NIGHT_SH = {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00",
            "tz": "Asia/Shanghai"}
NIGHT_LA = {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00",
            "tz": "America/Los_Angeles"}


def at(hour, minute):
    return datetime(2026, 9, 25, hour, minute, tzinfo=UTC)


class DutyWindowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.window = DutyWindow.from_dict(NIGHT_SH)

    def test_overnight_evening_segment(self):
        # 15:30 UTC = 上海 23:30，跨午夜窗口的晚间段
        self.assertTrue(self.window.covers(at(15, 30)))

    def test_overnight_early_morning_segment(self):
        # 21:30 UTC = 上海次日 05:30，跨午夜窗口的凌晨段
        self.assertTrue(self.window.covers(at(21, 30)))

    def test_start_boundary_inclusive(self):
        # 14:00 UTC = 上海 22:00，起点含
        self.assertTrue(self.window.covers(at(14, 0)))

    def test_just_before_start(self):
        # 13:59 UTC = 上海 21:59，尚未到点
        self.assertFalse(self.window.covers(at(13, 59)))

    def test_end_boundary_exclusive(self):
        # 22:00 UTC = 上海次日 06:00，终点不含
        self.assertFalse(self.window.covers(at(22, 0)))

    def test_midday_off_duty(self):
        # 04:00 UTC = 上海 12:00，白天不在班
        self.assertFalse(self.window.covers(at(4, 0)))


class DispatchTimezoneTests(unittest.TestCase):
    def test_same_instant_different_zones(self):
        app, clock, _, _ = make_app()
        try:
            clock.set(at(15, 30))  # 上海 23:30 在班；洛杉矶 08:30 不在班
            add_team(app, "T-SH", capabilities=("MOBILE_CHARGE",), staging=118.0,
                     duty_windows=[NIGHT_SH])
            add_team(app, "T-LA", capabilities=("MOBILE_CHARGE",), staging=110.0,
                     duty_windows=[NIGHT_LA])
            _, order, _ = report(app)
            # 洛杉矶队更近但不在班，只有上海队可派
            self.assertEqual(order.candidate_team_ids, ["T-SH"])
        finally:
            app.close()

    def test_boundary_moment_dispatch(self):
        app, clock, _, _ = make_app()
        try:
            add_team(app, "T-SH", capabilities=("MOBILE_CHARGE",), duty_windows=[NIGHT_SH])
            clock.set(at(13, 59))
            _, order1, _ = report(app, phone="13800000001")
            self.assertEqual(order1.status.value, "PENDING")  # 差一分钟未到班
            clock.set(at(14, 0))
            app.dispatch.replan_request(order1.request_id, reason="SHIFT_CHANGE")
            updated = app.repo.get_order(order1.order_id)
            self.assertEqual(updated.candidate_team_ids, ["T-SH"])
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
