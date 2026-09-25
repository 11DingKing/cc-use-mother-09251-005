"""隐私字段授权与脱敏。

求援人姓名、电话与精确坐标属于隐私字段，只有持有 privacy:read
权限范围的操作者（如值班调度员）可以读取明文；救援队等角色
只能看到脱敏后的派单所需信息。
"""
from __future__ import annotations

PRIVACY_SCOPE = "privacy:read"
WRITE_SCOPE = "dispatch:write"


class Authorizer:
    """基于令牌的操作者权限范围查询。"""

    def __init__(self, tokens: dict[str, set[str]] | None = None) -> None:
        self._tokens: dict[str, set[str]] = {t: set(s) for t, s in (tokens or {}).items()}

    def scopes_for(self, token: str | None) -> set[str] | None:
        """返回令牌对应的权限范围；未知令牌返回 None。"""
        if token is None:
            return None
        return self._tokens.get(token)


def mask_phone(phone: str) -> str:
    if not phone:
        return phone
    return "*" * max(len(phone) - 2, 0) + phone[-2:]


def mask_name(name: str) -> str:
    if not name:
        return name
    return name[0] + "*" * (len(name) - 1)


def masked_request_payload(data: dict) -> dict:
    """对求援单字典做脱敏副本，不改动原对象。"""
    masked = dict(data)
    masked["reporter_name"] = mask_name(str(data.get("reporter_name", "")))
    masked["reporter_phone"] = mask_phone(str(data.get("reporter_phone", "")))
    masked["extra_phones"] = [mask_phone(str(p)) for p in data.get("extra_phones", [])]
    location = dict(data.get("location") or {})
    location["lat"] = None
    location["lon"] = None
    masked["location"] = location
    return masked


def request_payload_for(data: dict, scopes: set[str]) -> dict:
    """按权限范围返回求援单视图。"""
    if PRIVACY_SCOPE in scopes:
        return dict(data)
    return masked_request_payload(data)
