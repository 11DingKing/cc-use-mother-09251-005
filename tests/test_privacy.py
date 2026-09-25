"""隐私字段授权：姓名、电话与精确坐标按权限范围脱敏。"""
import http.client
import json
import threading
import unittest

from support import make_app, report
from service_09251_005.application.privacy import (
    mask_name,
    mask_phone,
    masked_request_payload,
)
from service_09251_005.interfaces.http_api import make_server


class MaskingUnitTests(unittest.TestCase):
    def test_mask_helpers(self):
        self.assertEqual(mask_phone("13800001111"), "*********11")
        self.assertEqual(mask_name("张三"), "张*")
        self.assertEqual(mask_name("欧阳娜娜"), "欧***")

    def test_payload_masking_drops_precise_coords(self):
        data = {
            "reporter_name": "张三",
            "reporter_phone": "13800001111",
            "extra_phones": ["13900002222"],
            "location": {"road_id": "G15", "direction": "NORTHBOUND",
                         "mile_marker_km": 120.0, "lat": 31.23, "lon": 121.47},
        }
        masked = masked_request_payload(data)
        self.assertNotIn("13800001111", json.dumps(masked, ensure_ascii=False))
        self.assertIsNone(masked["location"]["lat"])
        self.assertIsNone(masked["location"]["lon"])
        # 原对象不被改动
        self.assertEqual(data["location"]["lat"], 31.23)


class PrivacyApiTests(unittest.TestCase):
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

    def setUp(self):
        self.req, _, _ = report(self.app, phone="13800001111", name="张三")

    def _get(self, path, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Operator-Token": token} if token else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def test_dispatcher_scope_sees_plaintext(self):
        status, body = self._get(f"/api/requests/{self.req.request_id}",
                                 token="dispatcher-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["request"]["reporter_phone"], "13800001111")
        self.assertEqual(body["request"]["reporter_name"], "张三")

    def test_team_scope_sees_masked(self):
        status, body = self._get(f"/api/requests/{self.req.request_id}",
                                 token="team-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["request"]["reporter_phone"], "*********11")
        self.assertEqual(body["request"]["reporter_name"], "张*")

    def test_missing_or_unknown_token_rejected(self):
        status, _ = self._get(f"/api/requests/{self.req.request_id}")
        self.assertEqual(status, 401)
        status, _ = self._get(f"/api/requests/{self.req.request_id}", token="forged")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
