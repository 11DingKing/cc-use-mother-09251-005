"""服务区与充电拥堵模型。

拥堵用占用率与排队等待时间表达；补能规划可以“预约”一个快充位，
预约成功即占位（reserved），到场补能后转为占用（occupied），
补能结束或派单撤销时必须释放（失败进入补偿台账）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import StationFullError


@dataclass
class ChargingStation:
    station_id: str
    name: str
    node_id: str
    total_ports: int
    occupied: int = 0                 # 社会车辆正在使用
    reserved: int = 0                 # 本系统为救援任务预约
    queue_length: int = 0             # 在场排队车辆数
    avg_charge_minutes: float = 40.0
    offline: bool = False             # 服务区停电/设备群故障

    @property
    def free_ports(self) -> int:
        return max(0, self.total_ports - self.occupied - self.reserved)

    @property
    def occupancy_ratio(self) -> float:
        if self.total_ports == 0:
            return 1.0
        return min(1.0, (self.occupied + self.reserved) / self.total_ports)

    def estimated_wait_minutes(self) -> float:
        """充电拥堵联动：无空位时按队列与平均时长估算等待。"""
        if self.offline:
            return float("inf")
        if self.free_ports > 0:
            # 仍有空位但高占用时给一个轻微缓冲
            return 0.0 if self.occupancy_ratio < 0.8 else 5.0
        ahead = self.queue_length + self.reserved
        return (ahead + 1) * self.avg_charge_minutes / max(1, self.total_ports)

    # ---- 占位/释放 ----
    def reserve_port(self) -> None:
        if self.offline or self.free_ports <= 0:
            raise StationFullError(f"station {self.station_id} 无可用快充位")
        self.reserved += 1

    def confirm_occupied(self) -> None:
        """车辆到场接入：预约转占用。"""
        if self.reserved > 0:
            self.reserved -= 1
        self.occupied += 1

    def release_reservation(self) -> None:
        if self.reserved > 0:
            self.reserved -= 1

    def release_occupied(self) -> None:
        if self.occupied > 0:
            self.occupied -= 1
