"""测试共享支撑：确定性时钟、标识与临时数据目录。"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

from service_09251_005.app import build_app
from service_09251_005.application.ports import ListNotifier, ManualClock, SequentialIds

UTC = timezone.utc

# 全天值守窗口（start == end 表示全天）
ALWAYS_ON = {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00", "tz": "Asia/Shanghai"}


def make_clock(hour: int = 12, minute: int = 0, day: int = 25) -> ManualClock:
    return ManualClock(datetime(2026, 9, day, hour, minute, tzinfo=UTC))


def make_app(clock: ManualClock | None = None, data_dir: str | None = None):
    """返回 (app, clock, notifier, data_dir)。"""
    clock = clock or make_clock()
    data_dir = data_dir or tempfile.mkdtemp(prefix="rescue-test-")
    notifier = ListNotifier()
    app = build_app(data_dir, clock=clock, idgen=SequentialIds(), notifier=notifier)
    return app, clock, notifier, data_dir


def add_team(app, team_id: str, *, agency: str = "AGENCY-A",
             capabilities=("TOW",), road: str = "G15", direction: str = "NORTHBOUND",
             cover=(100.0, 150.0), staging: float = 110.0,
             duty_windows=None, max_concurrent: int = 2):
    return app.admin.register_team(
        team_id=team_id,
        agency_id=agency,
        name=f"队伍{team_id}",
        capabilities=list(capabilities),
        road_id=road,
        direction=direction,
        coverage_from_km=cover[0],
        coverage_to_km=cover[1],
        staging_km=staging,
        duty_windows=list(duty_windows) if duty_windows else [dict(ALWAYS_ON)],
        max_concurrent=max_concurrent,
    )


def report(app, *, phone: str = "13800001111", name: str = "张三",
           road: str = "G15", direction: str = "NORTHBOUND", km: float = 120.0,
           incident="LOW_BATTERY", battery: float = 8.0, risk="HIGH", occupants: int = 2):
    from service_09251_005.domain.models import IncidentType, Location, RiskLevel

    return app.intake.register_report(
        reporter_name=name,
        reporter_phone=phone,
        location=Location(road, direction, km),
        incident_type=IncidentType(incident),
        battery_pct=battery,
        risk=RiskLevel[risk] if isinstance(risk, str) else risk,
        occupants=occupants,
    )
