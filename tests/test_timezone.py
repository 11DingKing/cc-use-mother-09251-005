"""时区边界：值班窗口的跨午夜、工作日边界、不同时区与 DST。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from service_09251_005.domain.team import DutyWindow

SH = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class DutyWindowTests(unittest.TestCase):
    def test_overnight_window_shanghai(self) -> None:
        # 每日 20:00-次日 04:00（北京时间）
        w = DutyWindow("20:00", "04:00", frozenset(range(7)), "Asia/Shanghai")
        # 周五 12:00 UTC = 周五 20:00 北京，窗口起点（含）
        self.assertTrue(w.contains(utc(2026, 9, 25, 12, 0)))
        # 周五 19:00 UTC = 周六 03:00 北京，属于周五夜跨午夜窗口
        self.assertTrue(w.contains(utc(2026, 9, 25, 19, 0)))
        # 周五 11:00 UTC = 周五 19:00 北京，尚未开窗
        self.assertFalse(w.contains(utc(2026, 9, 25, 11, 0)))
        # 周五 20:00 UTC = 周六 04:00 北京，结束点（不含）
        self.assertFalse(w.contains(utc(2026, 9, 25, 20, 0)))
        # 周六 12:00 UTC = 周六 20:00 北京，周六夜窗口开始
        self.assertTrue(w.contains(utc(2026, 9, 26, 12, 0)))

    def test_weekday_boundary_monday_to_friday(self) -> None:
        # 周一至周五 22:00-02:00 北京时间
        w = DutyWindow(
            "22:00", "02:00", frozenset({0, 1, 2, 3, 4}), "Asia/Shanghai"
        )
        # 周六 01:00 北京 = 周五 17:00 UTC：归属周五夜 → 覆盖
        self.assertTrue(w.contains(datetime(2026, 9, 25, 17, 0, tzinfo=UTC)))
        # 周日 01:00 北京 = 周六 17:00 UTC：归属周六（非值班日）→ 不覆盖
        self.assertFalse(w.contains(datetime(2026, 9, 26, 17, 0, tzinfo=UTC)))
        # 周一 01:00 北京 = 周日 17:00 UTC：归属周日（非值班日）→ 不覆盖
        self.assertFalse(w.contains(datetime(2026, 9, 27, 17, 0, tzinfo=UTC)))
        # 周一 22:30 北京 = 周一 14:30 UTC → 覆盖
        self.assertTrue(w.contains(datetime(2026, 9, 28, 14, 30, tzinfo=UTC)))

    def test_different_timezone_urumqi(self) -> None:
        # 乌鲁木齐（UTC+6）队伍 20:00-23:59 值班
        w = DutyWindow("20:00", "23:59", frozenset(range(7)), "Asia/Urumqi")
        # UTC 12:00 = 当地 18:00，未上岗
        self.assertFalse(w.contains(utc(2026, 9, 25, 12, 0)))
        # UTC 14:00 = 当地 20:00，上岗
        self.assertTrue(w.contains(utc(2026, 9, 25, 14, 0)))
        # UTC 17:58 = 当地 23:58，仍在岗
        self.assertTrue(w.contains(utc(2026, 9, 25, 17, 58)))

    def test_dst_berlin_autumn_transition(self) -> None:
        # 柏林 09:00-17:00；2026-10-25 为夏令时结束日（UTC+2 -> UTC+1）
        w = DutyWindow("09:00", "17:00", frozenset(range(7)), "Europe/Berlin")
        # 夏令时结束当天 UTC 08:00 = 柏林 10:00（已切 UTC+1）
        self.assertTrue(w.contains(datetime(2026, 10, 25, 8, 0, tzinfo=UTC)))
        # 当天 UTC 06:30 = 柏林 08:30（冬令时），未开窗
        self.assertFalse(w.contains(datetime(2026, 10, 25, 6, 30, tzinfo=UTC)))
        # 夏末（UTC+2）UTC 06:59 = 柏林 08:59，未开窗；07:00 = 09:00 开窗
        self.assertFalse(w.contains(datetime(2026, 9, 25, 6, 59, tzinfo=UTC)))
        self.assertTrue(w.contains(datetime(2026, 9, 25, 7, 0, tzinfo=UTC)))

    def test_whole_interval_must_be_covered(self) -> None:
        w = DutyWindow("20:00", "23:00", frozenset(range(7)), "Asia/Shanghai")
        # 作业从 22:00 到 23:30 北京：跨过下班点，不被覆盖
        start = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)  # 22:00 北京
        end = start + timedelta(minutes=90)               # 23:30 北京
        self.assertIsNone(w.covers_interval(start, end))
        # 22:00-22:40 北京：完全在窗口内
        self.assertIsNotNone(
            w.covers_interval(start, start + timedelta(minutes=40))
        )

    def test_next_opening_overnight(self) -> None:
        w = DutyWindow(
            "22:00", "02:00", frozenset({0, 1, 2, 3, 4}), "Asia/Shanghai"
        )
        # 周六 12:00 UTC（周六 20:00 北京）询问：下一开窗为周一 22:00 北京
        nxt = w.next_opening(utc(2026, 9, 26, 12, 0)).astimezone(SH)
        self.assertEqual(nxt.weekday(), 0)  # 周一
        self.assertEqual((nxt.hour, nxt.minute), (22, 0))


if __name__ == "__main__":
    unittest.main()
