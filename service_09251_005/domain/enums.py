"""领域枚举。"""
from __future__ import annotations

import enum


class EmergencyType(str, enum.Enum):
    LOW_BATTERY = "low_battery"        # 低电量趴窝
    CHARGER_FAULT = "charger_fault"    # 充电设备故障（在桩旁充不上）
    ACCIDENT = "accident"              # 普通事故


class CaseStatus(str, enum.Enum):
    OPEN = "open"                  # 已受理，存在待承诺的派单计划
    PARTIALLY_COMMITTED = "partially_committed"  # 部分资源已承诺，仍有待承诺项
    IN_PROGRESS = "in_progress"    # 全部必需要素已承诺，救援队处置中
    RESOLVED = "resolved"          # 已补能/排障完成，等待关单
    CLOSED = "closed"
    CANCELLED = "cancelled"


class AssignmentStatus(str, enum.Enum):
    PROPOSED = "proposed"          # 规划产物，尚未承诺（可随定位/封路重规划）
    OFFERED = "offered"            # 已向救援队发单，等待抢单
    CLAIMED = "claimed"            # 已承诺（抢单成功）
    ARRIVED = "arrived"
    ENERGIZED = "energized"        # 补能/排障动作已完成
    COMPLETED = "completed"
    REVOKED = "revoked"            # 承诺前撤回（改派未承诺部分）
    TRANSFERRED = "transferred"    # 已通过交接转派给另一机构
    EXPIRED = "expired"


class Capability(str, enum.Enum):
    MOBILE_CHARGING = "mobile_charging"  # 移动补能车
    TOWING = "towing"                    # 拖车
    ACCIDENT_RESCUE = "accident_rescue"  # 事故救援
    CHARGER_REPAIR = "charger_repair"    # 充电设备维修
    FAST_INSPECT = "fast_inspect"        # 快速到场勘查


class ServiceType(str, enum.Enum):
    MOBILE_POWER = "mobile_power"  # 移动补能
    TOW_TO_STATION = "tow_to_station"  # 拖至服务区补能
    ONSITE_REPAIR = "onsite_repair"    # 现场排障（充电设备故障）
    ACCIDENT_HANDLE = "accident_handle"


class Priority(str, enum.Enum):
    ROUTINE = "routine"
    URGENT = "urgent"
    CRITICAL = "critical"

    @property
    def level(self) -> int:
        return {"routine": 0, "urgent": 1, "critical": 2}[self.value]


class EnergyAction(str, enum.Enum):
    MOBILE_RECHARGE = "mobile_recharge"
    STATION_RECHARGE = "station_recharge"
    FAULT_CLEARED = "fault_cleared"


class HandoverState(str, enum.Enum):
    PROPOSED = "proposed"        # 发起方提出转派
    ACKNOWLEDGED = "acknowledged"  # 接收方确认承接，责任已交接
    REJECTED = "rejected"
    CANCELLED = "cancelled"
