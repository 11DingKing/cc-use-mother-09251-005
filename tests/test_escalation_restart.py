"""超时升级：重启后升级进度不丢失，继续升级。"""
import tempfile
import unittest
from datetime import timedelta

from support import make_app, report
from service_09251_005.app import build_app
from service_09251_005.application.ports import ListNotifier, ManualClock, SequentialIds


class EscalationRestartTests(unittest.TestCase):
    def test_escalation_continues_after_restart(self):
        data_dir = tempfile.mkdtemp(prefix="rescue-restart-")
        # 第一次运行：无可用队伍，派单进入 PENDING，SLA 30 分钟
        app1, clock1, notifier1, _ = make_app(data_dir=data_dir)
        _, order, _ = report(app1, risk="LOW", battery=60.0)
        self.assertEqual(order.status.value, "PENDING")
        self.assertEqual(order.escalation_level, 0)
        clock1.advance(timedelta(minutes=31))
        self.assertEqual(app1.dispatch.sweep(), 1)
        level_after_first = app1.repo.get_order(order.order_id).escalation_level
        self.assertEqual(level_after_first, 1)
        self.assertEqual(notifier1.kinds().count("ORDER_ESCALATED"), 1)
        app1.close()

        # 模拟重启：同一数据目录、新的时钟与通知器
        clock2 = ManualClock(clock1.now() + timedelta(minutes=9))  # T0+40min
        notifier2 = ListNotifier()
        app2 = build_app(data_dir, clock=clock2, idgen=SequentialIds(),
                         notifier=notifier2)
        try:
            # build_app 启动即恢复扫描：T0+36 到点的第二次升级应已触发
            restored = app2.repo.get_order(order.order_id)
            self.assertEqual(restored.escalation_level, 2)
            self.assertEqual(notifier2.kinds().count("ORDER_ESCALATED"), 1)
            # 下一次升级时间已按退避推进到未来
            self.assertGreater(restored.next_escalation_at, clock2.now())
            # 再次推进时间，升级继续而不是重新开始
            clock2.advance(timedelta(minutes=11))  # T0+51 > T0+40+10
            self.assertEqual(app2.dispatch.sweep(), 1)
            self.assertEqual(app2.repo.get_order(order.order_id).escalation_level, 3)
        finally:
            app2.close()

    def test_escalation_widens_candidates(self):
        app, clock, notifier, _ = make_app()
        try:
            # 队伍不在该路段值守范围，但具备能力：升级后应被纳入候选
            from support import add_team
            add_team(app, "T-ELSEWHERE", capabilities=("MOBILE_CHARGE",),
                     road="G60", cover=(0.0, 300.0))
            _, order, _ = report(app, risk="CRITICAL", battery=2.0)  # SLA 5 分钟
            self.assertEqual(order.status.value, "PENDING")
            clock.advance(timedelta(minutes=6))
            self.assertEqual(app.dispatch.sweep(), 1)
            updated = app.repo.get_order(order.order_id)
            self.assertEqual(updated.escalation_level, 1)
            self.assertIn("T-ELSEWHERE", updated.candidate_team_ids)
            self.assertEqual(updated.status.value, "OFFERED")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
