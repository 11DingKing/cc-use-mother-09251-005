"""并发抢单：多线程同时抢同一派单，只允许一个赢家。"""
import threading
import unittest

from support import add_team, make_app, report
from service_09251_005.application.errors import ClaimConflictError
from service_09251_005.domain.models import OrderStatus


class ConcurrentClaimTests(unittest.TestCase):
    def test_only_one_claim_wins(self):
        app, clock, notifier, _ = make_app()
        try:
            # 运力充足，让竞争落在原子抢单更新上
            add_team(app, "T-1", capabilities=("MOBILE_CHARGE", "TOW"), max_concurrent=10)
            _, order, _ = report(app)
            order_id = order.order_id

            results = {"ok": [], "conflict": [], "other": []}
            barrier = threading.Barrier(8)

            def worker():
                try:
                    barrier.wait(timeout=5)
                    app.ops.claim(order_id, "T-1")
                    results["ok"].append(threading.current_thread().name)
                except ClaimConflictError:
                    results["conflict"].append(threading.current_thread().name)
                except Exception as exc:  # noqa: BLE001
                    results["other"].append(repr(exc))

            threads = [threading.Thread(target=worker, name=f"w{i}") for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(results["other"], [], f"unexpected errors: {results['other']}")
            self.assertEqual(len(results["ok"]), 1)
            self.assertEqual(len(results["conflict"]), 7)
            final = app.repo.get_order(order_id)
            self.assertEqual(final.status, OrderStatus.CLAIMED)
            self.assertEqual(final.assigned_team_id, "T-1")
            # 失败者的运力占用全部被补偿释放，最终只占一份
            self.assertEqual(app.repo.get_team("T-1").load, 1)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
