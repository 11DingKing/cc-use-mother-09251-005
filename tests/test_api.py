"""HTTP 接口冒烟：受理→抢单→到场→补能→关闭全生命周期。"""
import http.client
import json
import threading
import unittest

from support import ALWAYS_ON, make_app
from service_09251_005.interfaces.http_api import make_server


class ApiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, _, _, _ = make_app()
        cls.server = make_server(cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()

    def _call(self, method, path, body=None, token="dispatcher-token"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Operator-Token"] = token
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_full_lifecycle(self):
        # 登记救援队与充电站
        status, body = self._call("POST", "/api/teams", {
            "team_id": "T-API", "agency_id": "AGENCY-API", "name": "接口队",
            "capabilities": ["MOBILE_CHARGE", "TOW"],
            "road_id": "G15", "direction": "NORTHBOUND",
            "coverage_from_km": 100, "coverage_to_km": 150, "staging_km": 112,
            "duty_windows": [ALWAYS_ON], "max_concurrent": 2,
        })
        self.assertEqual(status, 201, body)
        status, body = self._call("POST", "/api/charging-sites", {
            "site_id": "S-API", "road_id": "G15", "direction": "NORTHBOUND",
            "km": 122, "queue": 0,
        })
        self.assertEqual(status, 201, body)

        # 受理
        status, body = self._call("POST", "/api/requests", {
            "reporter_name": "李四", "reporter_phone": "13811112222",
            "location": {"road_id": "G15", "direction": "NORTHBOUND",
                         "mile_marker_km": 120},
            "incident_type": "LOW_BATTERY", "battery_pct": 9,
            "risk": "HIGH", "occupants": 3,
        })
        self.assertEqual(status, 201, body)
        self.assertFalse(body["merged"])
        order_id = body["order"]["order_id"]
        self.assertEqual(body["order"]["status"], "OFFERED")
        self.assertEqual(body["order"]["energy_advice"], "GUIDE_TO_SITE:S-API")

        # 抢单
        status, body = self._call("POST", f"/api/orders/{order_id}/claim",
                                  {"team_id": "T-API"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["order"]["status"], "CLAIMED")
        self.assertTrue(body["order"]["committed"])

        # 重复抢单 → 409
        status, body = self._call("POST", f"/api/orders/{order_id}/claim",
                                  {"team_id": "T-API"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "CLAIM_CONFLICT")

        # 到场 → 补能 → 关闭
        status, body = self._call("POST", f"/api/orders/{order_id}/arrive")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["order"]["status"], "ARRIVED")
        status, body = self._call("POST", f"/api/orders/{order_id}/recharge",
                                  {"site_id": "S-API", "energy_kwh": 22.5})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["order"]["status"], "RECHARGING")
        status, body = self._call("POST", f"/api/orders/{order_id}/close",
                                  {"resolution": "补能完成，车辆自行驶离"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["order"]["status"], "CLOSED")

        # 关闭后队伍运力释放
        team = self.app.repo.get_team("T-API")
        self.assertEqual(team.load, 0)

    def test_scope_enforcement(self):
        # team-token 持有 dispatch:write，可登记；伪造令牌一律 401
        status, body = self._call("POST", "/api/teams", {
            "agency_id": "X", "name": "x", "capabilities": ["TOW"],
            "road_id": "G15", "direction": "NORTHBOUND",
            "coverage_from_km": 0, "coverage_to_km": 1, "staging_km": 0,
            "duty_windows": [ALWAYS_ON],
        }, token="team-token")
        self.assertEqual(status, 201, body)
        status, body = self._call("POST", "/api/teams", {}, token="forged")
        self.assertEqual(status, 401)

    def test_unknown_order_404(self):
        status, body = self._call("GET", "/api/orders/ORD-999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
