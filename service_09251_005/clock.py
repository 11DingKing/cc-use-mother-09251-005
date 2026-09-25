"""可替换的时间端口。领域内只认时区感知的 UTC 时刻。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    """生产时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试/回放时钟，可显式推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._t = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> datetime:
        self._t += timedelta(seconds=seconds)
        return self._t

    def advance_minutes(self, minutes: float) -> datetime:
        self._t += timedelta(minutes=minutes)
        return self._t

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._t = moment.astimezone(timezone.utc)
