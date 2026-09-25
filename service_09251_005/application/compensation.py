"""补偿台账与外部资源端口。

预约充电位等外部资源在派单撤销/转派时必须释放；若释放动作本身失败，
不得静默丢弃：登记进补偿台账，重启后继续重试，直至成功。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from ..domain.station import ChargingStation
from ..errors import CompensationError
from .repository import Repository


class StationGateway(Protocol):
    """充电资源端口。生产实现直连领域对象；测试可注入故障版本。"""

    def reserve(self, station: ChargingStation) -> None: ...
    def confirm_occupied(self, station: ChargingStation) -> None: ...
    def release_reservation(self, station: ChargingStation) -> None: ...
    def release_occupied(self, station: ChargingStation) -> None: ...


class LocalStationGateway:
    """默认实现：直接操作内存中的服务区聚合。"""

    def reserve(self, station: ChargingStation) -> None:
        station.reserve_port()

    def confirm_occupied(self, station: ChargingStation) -> None:
        station.confirm_occupied()

    def release_reservation(self, station: ChargingStation) -> None:
        station.release_reservation()

    def release_occupied(self, station: ChargingStation) -> None:
        station.release_occupied()


class CompensationLedger:
    """待补偿动作台账。entries 持久化在 Repository 中。"""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def register(
        self,
        kind: str,
        ref_id: str,
        payload: dict,
        now: datetime,
        reason: str = "",
    ) -> str:
        with self.repo.lock:
            cid = self.repo.next_id("comp")
            self.repo.compensations.append(
                {
                    "comp_id": cid,
                    "kind": kind,               # release_station_reservation ...
                    "ref_id": ref_id,           # assignment_id
                    "payload": payload,         # {"station_id": ...}
                    "state": "pending",
                    "attempts": 0,
                    "last_error": "",
                    "reason": reason,
                    "created_at": now.isoformat(),
                    "next_retry_at": now.isoformat(),
                }
            )
            return cid

    def pending(self, now: datetime) -> list[dict]:
        with self.repo.lock:
            return [
                c
                for c in self.repo.compensations
                if c["state"] == "pending"
                and datetime.fromisoformat(c["next_retry_at"]) <= now
            ]

    def retry_all(
        self,
        gateway: StationGateway,
        now: datetime,
        stations: dict[str, ChargingStation],
        max_attempts: int = 10,
    ) -> list[dict]:
        """尝试执行所有到期补偿。仍失败的保留 pending 并退避；超限标记
        failed_awaiting_manual（不删除，保留责任线索）。返回本次成功项。"""
        succeeded: list[dict] = []
        for entry in self.pending(now):
            kind = entry["kind"]
            station = stations.get(entry["payload"].get("station_id", ""))
            try:
                if station is None:
                    raise CompensationError(
                        f"station {entry['payload'].get('station_id')} 不存在"
                    )
                if kind == "release_station_reservation":
                    gateway.release_reservation(station)
                elif kind == "release_station_occupied":
                    gateway.release_occupied(station)
                else:
                    raise CompensationError(f"未知补偿类型 {kind}")
                entry["state"] = "done"
                entry["attempts"] += 1
                entry["last_error"] = ""
                succeeded.append(entry)
            except Exception as exc:  # 端口失败：保留待重试
                entry["attempts"] += 1
                entry["last_error"] = str(exc)
                if entry["attempts"] >= max_attempts:
                    entry["state"] = "failed_awaiting_manual"
                else:
                    backoff = timedelta(seconds=min(300, 2 ** entry["attempts"]))
                    entry["next_retry_at"] = (now + backoff).isoformat()
        return succeeded
