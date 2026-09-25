"""隐私字段授权：受理时授予 scope，读取按 scope 脱敏，未授权写操作被拒。"""
from __future__ import annotations

import unittest

from service_09251_005.domain.cases import PrivacyField
from service_09251_005.errors import PrivacyDeniedError

from helpers import build_world, register_standard_teams


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.clk, self.svc = build_world()
        register_standard_teams(self.svc)
        self.case, _ = self.svc.intake(
            "accident", "stranded", "13812345678",
            plate="沪A12345",
            occupants=3, injured=True, vulnerable=True,
            in_driving_lane=True, reported_level="high",
            consent_scopes={
                PrivacyField.PHONE,
                PrivacyField.OCCUPANT_DETAIL,
            },
        )

    def test_unauthorized_fields_masked(self) -> None:
        view = self.svc.case_view(self.case.case_id, set())
        # 未授权：电话/车牌/精确位置全部脱敏，乘员明细不可见
        self.assertNotIn("13812345678", view["phone"])
        self.assertNotIn("沪", view["plate"])
        self.assertNotEqual(view["location_node"], "stranded")
        self.assertIsNone(view["occupants"])
        self.assertIsNone(view["risk_flags"])

    def test_authorized_fields_visible(self) -> None:
        view = self.svc.case_view(
            self.case.case_id,
            {PrivacyField.PHONE, PrivacyField.PLATE,
             PrivacyField.EXACT_LOCATION, PrivacyField.OCCUPANT_DETAIL},
        )
        self.assertEqual(view["phone"], "13812345678")
        self.assertEqual(view["plate"], "沪A12345")
        self.assertEqual(view["location_node"], "stranded")
        self.assertEqual(view["occupants"], 3)
        self.assertTrue(view["risk_flags"]["injured"])
        self.assertTrue(view["risk_flags"]["vulnerable"])

    def test_partial_scope_only_reveals_that_field(self) -> None:
        view = self.svc.case_view(self.case.case_id, {PrivacyField.PHONE})
        self.assertEqual(view["phone"], "13812345678")
        self.assertNotEqual(view["plate"], "沪A12345")
        self.assertIsNone(view["risk_flags"])

    def test_assert_scope_blocks_unauthorized_use(self) -> None:
        # 模拟外呼/回拨需要电话授权
        self.svc.assert_scope(self.case, PrivacyField.PHONE)  # 已授权
        with self.assertRaises(PrivacyDeniedError):
            self.svc.assert_scope(self.case, PrivacyField.PLATE)
        with self.assertRaises(PrivacyDeniedError):
            self.svc.assert_scope(self.case, PrivacyField.EXACT_LOCATION)

    def test_consent_recorded_and_persisted(self) -> None:
        view = self.svc.case_view(self.case.case_id, set())
        self.assertEqual(
            sorted(view["consent_scopes"]),
            ["occupant_detail", "phone"],
        )

    def test_masking_keeps_short_values_non_revealing(self) -> None:
        view = self.svc.case_view(self.case.case_id, set())
        self.assertIn("***", view["phone"])
        self.assertIn("***", view["location_node"])


if __name__ == "__main__":
    unittest.main()
