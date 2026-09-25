"""救援队：能力、驻点、值班窗口（本地时区）。

值班窗口用 [start_local, end_local) 配合 IANA 时区表达，
跨午夜窗口用 start >= end 表示（如 20:00-06:00）。
所有对外比较一律转成 UTC。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .enums import Capability

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class DutyWindow:
    start_local: str          # "HH:MM"
    end_local: str            # "HH:MM"，<= start 表示跨午夜
    weekdays: frozenset[int] = frozenset(range(7))  # 0=周一
    tz_name: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        self._tz = ZoneInfo(self.tz_name)
        self._start = time.fromisoformat(self.start_local)
        self._end = time.fromisoformat(self.end_local)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @staticmethod
    def weekdays_from_names(names: list[str]) -> frozenset[int]:
        return frozenset(WEEKDAYS.index(n.lower()) for n in names)

    def contains(self, moment_utc: datetime) -> bool:
        """给定 UTC 时刻是否落在窗口内。

        跨午夜窗口（start>=end）：当日 start 之后的晚段归属“开始日”，
        次日凌晨 end 之前的早段归属“开始日的前一日规则”，即查前一天是否值班。
        """
        local = moment_utc.astimezone(self._tz)
        t = local.time().replace(second=0, microsecond=0)
        d = local.weekday()
        if self._start < self._end:
            return d in self.weekdays and self._start <= t < self._end
        # 跨午夜
        if t >= self._start:
            return d in self.weekdays
        if t < self._end:
            return (d - 1) % 7 in self.weekdays
        return False

    def covers_interval(
        self, start_utc: datetime, end_utc: datetime
    ) -> Optional["DutyWindow"]:
        """整个处置区间都在本窗口内则返回自身，否则 None（分钟粒度采样）。"""
        if end_utc <= start_utc:
            return self if self.contains(start_utc) else None
        cur = start_utc.replace(second=0, microsecond=0)
        step = timedelta(minutes=1)
        end = end_utc
        while cur < end:
            if not self.contains(cur):
                return None
            cur += step
        return self

    def next_opening(self, moment_utc: datetime) -> datetime:
        """从 moment 起（含），下一次窗口开启的 UTC 时刻。"""
        local = moment_utc.astimezone(self._tz)
        for day_offset in range(0, 8):
            day = (local + timedelta(days=day_offset)).date()
            if day.weekday() not in self.weekdays:
                continue
            opening = datetime.combine(day, self._start, self._tz)
            if opening >= local - timedelta(minutes=1):
                return opening.astimezone(ZoneInfo("UTC"))
        # 理论不可达（weekdays 非空时）
        raise RuntimeError("no upcoming duty opening")


@dataclass
class Team:
    team_id: str
    name: str
    org_id: str                          # 所属机构
    base_node: str
    capabilities: frozenset[Capability]
    duty_windows: list[DutyWindow] = field(default_factory=list)
    mobile_power_kwh: float = 0.0       # 单车可输送的应急电量
    # 运行态
    unavailable_until: Optional[datetime] = None  # UTC，执行任务期间
    current_case_id: Optional[str] = None

    def has_capability(self, caps: set[Capability]) -> bool:
        return caps.issubset(self.capabilities)

    def on_duty(self, moment_utc: datetime) -> bool:
        return any(w.contains(moment_utc) for w in self.duty_windows)

    def duty_window_covering(
        self, start_utc: datetime, end_utc: datetime
    ) -> Optional[DutyWindow]:
        for w in self.duty_windows:
            if w.covers_interval(start_utc, end_utc):
                return w
        return None

    @property
    def is_busy(self) -> bool:
        return self.current_case_id is not None
