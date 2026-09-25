"""JSON API 边界：受理/抢单/转派/到场/补能/关闭及错误码映射、HTTP 适配。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from service_09251_005.domain.enums import Capability
from service_09251_005.domain.team import DutyWindow
from service_09251_005.interfaces import JsonApi, build_handler

from helpers import (
    ALLDAY_SH,
    assignments_for,
    build_world,
    register_standard_teams,
    station_free,
)


class ApiFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)
        self.svc.register_station(station_free())
        self.api = JsonApi(self.svc)

    def call(self, method, path, body=None):
        return self.api.handle(method, path, body or {})

    def test_full_flow_over_api(self) -> None:
        st, body = self.call("POST", "/intake", {
            "emergency_type": "low_battery",
            "location_node": "stranded",
            "phone": "13800000000",
            "battery_pct": 4.0,
            "injured": True,
            "consent_scopes": ["phone", "plate", "exact_location",
                               "occupant_detail"],
        })
        self.assertEqual(st, 200)
        cid = body["data"]["case_id"]

        st, body = self.call("GET", f"/cases/{cid}", {})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["phone"], "13800000000")

        st, body = self.call("GET", f"/cases/{cid}/assignments", {})
        asg = body["data"]["assignments"][0]
        aid = asg["assignment_id"]

        st, body = self.call("POST", f"/assignments/{aid}/claim",
                             {"team_id": "tMobile"})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "claimed")

        self.assertEqual(self.call("POST", f"/assignments/{aid}/arrive",
                                   {})[0], 200)
        st, body = self.call("POST", f"/assignments/{aid}/energize",
                             {"kwh_delivered": 12.0})
        self.assertEqual(st, 200)
        st, body = self.call("POST", f"/cases/{cid}/close", {"note": "ok"})
        self.assertEqual(body["data"]["status"], "closed")

    def test_concurrent_claim_api_single_winner(self) -> None:
        st, body = self.call("POST", "/intake", {
            "emergency_type": "low_battery", "location_node": "stranded",
            "phone": "138", "battery_pct": 4.0,
        })
        cid = body["data"]["case_id"]
        _, listing = self.call("GET", f"/cases/{cid}/assignments", {})
        aid = listing["data"]["assignments"][0]["assignment_id"]
        results = []
        for actor in ["tMobile", "tTow"]:
            s, b = self.call("POST", f"/assignments/{aid}/claim",
                             {"team_id": actor})
            results.append((actor, s, b["ok"]))
        statuses = sorted(s for _, s, _ in results)
        self.assertEqual(statuses, [200, 409])

    def test_transfer_api_chain(self) -> None:
        self.repo.teams.pop("tMobile")
        st, body = self.call("POST", "/intake", {
            "emergency_type": "low_battery", "location_node": "stranded",
            "phone": "138", "battery_pct": 3.0,
        })
        cid = body["data"]["case_id"]
        _, listing = self.call("GET", f"/cases/{cid}/assignments", {})
        aid = listing["data"]["assignments"][0]["assignment_id"]
        self.svc.register_team(
            "tC", "邻市", "ORG_C", "baseTow",
            {Capability.TOWING}, duty_windows=[ALLDAY_SH],
        )
        self.call("POST", f"/assignments/{aid}/claim", {"team_id": "tTow"})
        st, body = self.call("POST", f"/assignments/{aid}/transfers",
                             {"to_team_id": "tC", "reason": "跨区"})
        hid = body["data"]["handover_id"]
        st, body = self.call("POST", f"/transfers/{hid}/ack",
                             {"team_id": "tC"})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["org_id"], "ORG_C")
        st, body = self.call("GET", f"/cases/{cid}/handovers", {})
        self.assertEqual(body["data"]["handovers"][0]["state"],
                         "acknowledged")

    def test_error_mapping(self) -> None:
        st, body = self.call("GET", "/cases/does-not-exist", {})
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "not_found")
        st, body = self.call("POST", "/intake", {"phone": "138"})
        self.assertEqual(st, 400)
        self.assertEqual(body["error"], "missing_field")
        st, body = self.call("GET", "/nope", {})
        self.assertEqual(st, 404)

    def test_http_server_adapter(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.api))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            payload = json.dumps({
                "emergency_type": "accident", "location_node": "stranded",
                "phone": "138", "in_driving_lane": True,
                "reported_level": "high",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/intake", data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            self.assertTrue(data["ok"])
            self.assertEqual(data["data"]["priority"], "critical")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
