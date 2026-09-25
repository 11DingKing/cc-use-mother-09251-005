"""可替换端口：时间、标识与外部通知，便于稳定复现状态变化。"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """测试用时钟：线程安全，可定点、可推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._t = start.astimezone(UTC)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._t

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        with self._lock:
            self._t = moment.astimezone(UTC)

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._t += delta
            return self._t


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...


class UuidIds:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"


class SequentialIds:
    """测试用确定性标识。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}-{self._counters[prefix]:06d}"


class Notifier(Protocol):
    def notify(self, kind: str, payload: dict) -> None: ...


class ListNotifier:
    """默认通知端口：收集事件，测试可断言，生产可替换为短信/广播。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        with self._lock:
            self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        with self._lock:
            return [k for k, _ in self.events]
